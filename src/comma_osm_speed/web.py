"""Local review web app for stepping through candidate edits.

Run with: `comma-osm-speed review --candidates output/candidates.json`

Endpoints
---------
GET  /                      Single-page review UI.
GET  /api/candidates        JSON list of candidates not on the ignore list.
GET  /api/ignored           JSON list of currently ignored way_ids.
POST /api/ignore            Body: {way_id, comment?}. Appends to ignored_ways.txt.
POST /api/unignore          Body: {way_id}. Removes from ignored_ways.txt.
POST /api/fixed             Body: {way_id}. Marks a way fixed in OSM: hidden until
                            the next analysis; if still flagged then, it returns
                            with its fix date (`fixed_at`).
GET  /api/state             Counts + ignore-file path + last update (header bar).
GET  /api/way_geometry/<id> The way's outline plus its live OSM version and
                            maxspeed, to flag ways changed since the analysis.
POST /api/refresh           Start a sync + analyze in the background.
POST /api/event             Body: {event, way_id?, ...}. Appends a usage event to
                            review_events.jsonl (kept on this machine only).
GET  /api/refresh           Status of the background refresh.

The backend writes to a single `ignored_ways.txt` so the same list is used by
`analyze`, `trends`, and the scheduled run.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import threading
import webbrowser
from collections.abc import Callable
from pathlib import Path
from typing import Any

from flask import Flask, jsonify, request, send_from_directory

from .osm_client import fetch_way_live
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


def _load_fixed(path: Path | None) -> dict[int, str]:
    """way_id -> ISO time it was marked fixed."""
    if path is None or not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return {int(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}


def _visible_candidates(
    candidates_path: Path, ignore_path: Path, fixed_path: Path | None
) -> list[dict[str, Any]]:
    """Candidates to show: not ignored, and not fixed since the last analysis.

    A way marked fixed stays hidden until a newer analysis exists. If that
    analysis still flags it, it comes back with `fixed_at` set so the page can
    say so.
    """
    ignored = load_ignore_list(ignore_path)
    fixed = _load_fixed(fixed_path)
    analyzed_at = _mtime_iso(candidates_path) or ""
    out = []
    for c in _load_candidates(candidates_path):
        wid = int(c.get("way_id", -1))
        if wid in ignored:
            continue
        if wid in fixed:
            if fixed[wid] >= analyzed_at:
                continue
            c = {**c, "fixed_at": fixed[wid]}
        out.append(c)
    return out


def create_app(
    candidates_path: Path,
    ignore_path: Path,
    *,
    osm_api_base: str = "https://api.openstreetmap.org",
    refresh_fn: Callable[[], Any] | None = None,
    events_path: Path | None = None,
    fixed_path: Path | None = None,
) -> Flask:
    app = Flask(__name__, static_folder=None)
    # Background refresh (sync + analyze). Only one runs at a time; the result
    # is the summary `refresh_fn` returns, or the error it raised.
    refresh_state: dict[str, Any] = {
        "running": False, "started": None, "finished": None, "result": None, "error": None,
    }
    refresh_lock = threading.Lock()

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
            remaining=len(_visible_candidates(candidates_path, ignore_path, fixed_path)),
            updated=_mtime_iso(candidates_path),
        )

    @app.get("/api/candidates")
    def candidates():
        out = _visible_candidates(candidates_path, ignore_path, fixed_path)
        # Sort: ways that came back after a fix first, then missing-maxspeed
        # before mismatched, highest-evidence first within each.
        out.sort(
            key=lambda c: (
                0 if c.get("fixed_at") else 1,
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
        # Not cached: the version and maxspeed must be current to show edits
        # made since this candidate was last opened.
        try:
            live = fetch_way_live(way_id, osm_api_base)
        except Exception as exc:  # noqa: BLE001
            log.warning("OSM API failed for way %d: %s", way_id, exc)
            return jsonify(way_id=way_id, geometry=[], error=str(exc)), 502
        return jsonify(way_id=way_id, **live)

    def _run_refresh() -> None:
        try:
            result = refresh_fn()
            refresh_state["result"] = vars(result) if hasattr(result, "__dict__") else result
        except Exception as exc:  # noqa: BLE001
            log.exception("Refresh failed")
            refresh_state["error"] = str(exc)
        finally:
            refresh_state["finished"] = _now_iso()
            refresh_state["running"] = False

    @app.get("/api/refresh")
    def refresh_status():
        return jsonify(available=refresh_fn is not None, **refresh_state)

    @app.post("/api/refresh")
    def refresh_start():
        if refresh_fn is None:
            return jsonify(error="Refresh isn't enabled for this server."), 404
        with refresh_lock:
            if refresh_state["running"]:
                return jsonify(error="A refresh is already running."), 409
            refresh_state.update(
                running=True, started=_now_iso(), finished=None, result=None, error=None
            )
        threading.Thread(target=_run_refresh, daemon=True).start()
        return jsonify(ok=True), 202

    @app.post("/api/event")
    def log_event():
        if events_path is None:
            return "", 204
        body = request.get_json(force=True, silent=True)
        if not isinstance(body, dict) or not isinstance(body.get("event"), str):
            return jsonify(error="event is required (string)"), 400
        # Only flat, short values: this is a usage log, not a data store.
        record = {"ts": _now_iso()}
        for k, v in list(body.items())[:20]:
            if isinstance(v, (str, int, float, bool)) or v is None:
                record[str(k)[:40]] = v[:200] if isinstance(v, str) else v
        events_path.parent.mkdir(parents=True, exist_ok=True)
        with events_path.open("a") as f:
            f.write(json.dumps(record) + "\n")
        return jsonify(ok=True)

    @app.post("/api/fixed")
    def mark_fixed():
        if fixed_path is None:
            return jsonify(error="Fixed tracking isn't enabled for this server."), 404
        body = request.get_json(force=True, silent=True) or {}
        try:
            wid = int(body["way_id"])
        except (KeyError, TypeError, ValueError):
            return jsonify(error="way_id is required (integer)"), 400
        fixed = _load_fixed(fixed_path)
        fixed[wid] = _now_iso()
        fixed_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = fixed_path.with_suffix(".tmp")
        tmp.write_text(json.dumps({str(k): v for k, v in sorted(fixed.items())}, indent=1))
        tmp.replace(fixed_path)
        return jsonify(ok=True, way_id=wid, fixed_at=fixed[wid])

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


def _now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def _mtime_iso(path: Path) -> str | None:
    try:
        ts = path.stat().st_mtime
    except OSError:
        return None
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).isoformat(timespec="seconds")


def serve(
    candidates_path: Path,
    ignore_path: Path,
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    open_browser: bool = True,
    osm_api_base: str = "https://api.openstreetmap.org",
    refresh_fn: Callable[[], Any] | None = None,
    events_path: Path | None = None,
    fixed_path: Path | None = None,
) -> None:
    """Start the review server and (optionally) pop open the browser."""
    app = create_app(
        candidates_path,
        ignore_path,
        osm_api_base=osm_api_base,
        refresh_fn=refresh_fn,
        events_path=events_path,
        fixed_path=fixed_path,
    )

    if open_browser:
        def _open():
            webbrowser.open(f"http://{host}:{port}/")
        threading.Timer(0.5, _open).start()

    log.info("Starting review server at http://%s:%d", host, port)
    # debug=False so we don't get the dev-mode banner / warnings.
    app.run(host=host, port=port, debug=False, use_reloader=False)
