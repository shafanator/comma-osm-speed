"""Local review web app for stepping through candidate edits.

Run with: `comma-osm-speed review --candidates output/candidates.json`

Endpoints
---------
GET  /                      Single-page review UI.
GET  /api/candidates        JSON list of candidates not on the ignore list.
GET  /api/ignored           JSON list of currently ignored way_ids.
POST /api/ignore            Body: {way_id, comment?}. Appends to ignored_ways.txt.
POST /api/unignore          Body: {way_id}. Removes from ignored_ways.txt.
GET  /api/state             Counts + ignore-file path (header bar).

The backend writes to a single `ignored_ways.txt` so the same list is used by
`analyze`, `trends`, and the scheduled run.
"""
from __future__ import annotations

import json
import logging
import threading
import webbrowser
from pathlib import Path
from typing import Any

from flask import Flask, jsonify, request, send_from_directory

from .osm_client import OverpassClient
from .trends import load_ignore_list

log = logging.getLogger(__name__)

# Where the single-page HTML lives next to this module.
_TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"


def _load_candidates(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return []
    return data if isinstance(data, list) else []


def _append_to_ignore(path: Path, way_id: int, comment: str | None) -> None:
    """Append a way_id to the ignore file (idempotent)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = load_ignore_list(path)
    if way_id in existing:
        return
    line = f"{way_id}"
    if comment:
        # Sanitize newlines and excessive whitespace.
        clean = " ".join(comment.split())[:200]
        line += f"  # {clean}"
    line += "\n"
    with path.open("a") as f:
        f.write(line)


def _remove_from_ignore(path: Path, way_id: int) -> bool:
    """Drop a way_id from the ignore file. Returns True if anything changed."""
    if not path.exists():
        return False
    kept: list[str] = []
    changed = False
    for raw in path.read_text().splitlines(keepends=False):
        stripped = raw.split("#", 1)[0].strip()
        if stripped and stripped.isdigit() and int(stripped) == way_id:
            changed = True
            continue
        kept.append(raw)
    if changed:
        path.write_text("\n".join(kept) + ("\n" if kept else ""))
    return changed


def create_app(
    candidates_path: Path,
    ignore_path: Path,
    *,
    overpass_url: str = "https://overpass-api.de/api/interpreter",
) -> Flask:
    app = Flask(__name__, static_folder=None)
    overpass = OverpassClient(overpass_url)
    # In-memory cache: way_id -> list of {lat, lon}. Stays for the lifetime of
    # the server process so revisiting a candidate doesn't re-hit Overpass.
    geom_cache: dict[int, list[dict[str, float]]] = {}

    @app.get("/")
    def index():
        return send_from_directory(_TEMPLATE_DIR, "review.html")

    @app.get("/api/state")
    def state():
        cands = _load_candidates(candidates_path)
        ignored = load_ignore_list(ignore_path)
        return jsonify(
            candidates_path=str(candidates_path),
            ignore_path=str(ignore_path),
            total=len(cands),
            ignored=len(ignored),
            remaining=sum(1 for c in cands if int(c.get("way_id", -1)) not in ignored),
        )

    @app.get("/api/candidates")
    def candidates():
        cands = _load_candidates(candidates_path)
        ignored = load_ignore_list(ignore_path)
        out = [c for c in cands if int(c.get("way_id", -1)) not in ignored]
        # Sort: highest-evidence first, missing-maxspeed before mismatched.
        out.sort(
            key=lambda c: (
                # Missing (None / "infinity") first, then mismatched.
                0 if c.get("current_maxspeed_mph") is None else 1,
                -int(c.get("sample_count", 0)),
            )
        )
        return jsonify(out)

    @app.get("/api/ignored")
    def ignored_list():
        return jsonify(sorted(load_ignore_list(ignore_path)))

    @app.post("/api/ignore")
    def ignore_one():
        body = request.get_json(force=True, silent=True) or {}
        try:
            wid = int(body["way_id"])
        except (KeyError, TypeError, ValueError):
            return jsonify(error="way_id is required (integer)"), 400
        _append_to_ignore(ignore_path, wid, body.get("comment"))
        return jsonify(ok=True, way_id=wid)

    @app.get("/api/way_geometry/<int:way_id>")
    def way_geometry(way_id: int):
        if way_id in geom_cache:
            return jsonify(way_id=way_id, geometry=geom_cache[way_id], cached=True)
        ql = f"""[out:json][timeout:30];
way({way_id});
out geom;"""
        try:
            data = overpass.query(ql)
        except Exception as exc:  # noqa: BLE001
            log.warning("Overpass failed for way %d: %s", way_id, exc)
            return jsonify(way_id=way_id, geometry=[], error=str(exc)), 502
        coords: list[dict[str, float]] = []
        for el in data.get("elements", []):
            if el.get("type") != "way":
                continue
            for pt in el.get("geometry", []) or []:
                try:
                    coords.append({"lat": float(pt["lat"]), "lon": float(pt["lon"])})
                except (KeyError, TypeError, ValueError):
                    continue
            break
        geom_cache[way_id] = coords
        return jsonify(way_id=way_id, geometry=coords, cached=False)

    @app.post("/api/unignore")
    def unignore_one():
        body = request.get_json(force=True, silent=True) or {}
        try:
            wid = int(body["way_id"])
        except (KeyError, TypeError, ValueError):
            return jsonify(error="way_id is required (integer)"), 400
        changed = _remove_from_ignore(ignore_path, wid)
        return jsonify(ok=True, way_id=wid, changed=changed)

    return app


def serve(
    candidates_path: Path,
    ignore_path: Path,
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    open_browser: bool = True,
    overpass_url: str = "https://overpass-api.de/api/interpreter",
) -> None:
    """Start the review server and (optionally) pop open the browser."""
    app = create_app(candidates_path, ignore_path, overpass_url=overpass_url)

    if open_browser:
        def _open():
            webbrowser.open(f"http://{host}:{port}/")
        threading.Timer(0.5, _open).start()

    log.info("Starting review server at http://%s:%d", host, port)
    # debug=False so we don't get the dev-mode banner / warnings.
    app.run(host=host, port=port, debug=False, use_reloader=False)
