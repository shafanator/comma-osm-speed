"""Local archive of drives, so they outlive Comma Connect's ~1-week retention.

Each route is saved as `<routes_dir>/<route_id>.json` in the same format
`local_source` reads, so `analyze --gps-dir` works on the archive directly.
The dongle ID is left out of file names; it stays inside the file only as part
of `route_name`.

`refresh()` is what the daily timer and the review page's "Analyze now" button
both run: sync new drives, then analyze the whole archive.
"""
from __future__ import annotations

import datetime as dt
import fcntl
import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path

from ._redact import redact_route_name
from .comma_client import CommaClient, RouteSummary
from .config import Config
from .local_source import route_file_stem
from .osm_submitter import write_osc
from .pipeline import run_analysis, write_report
from .trends import load_ignore_list

log = logging.getLogger(__name__)

# Connect keeps about a week; look back a little further so a missed day or
# two of syncing still catches everything.
SYNC_LOOKBACK = dt.timedelta(days=10)


class RefreshBusy(RuntimeError):
    """Another refresh is already running."""


@dataclass
class RefreshResult:
    new_routes: int
    archived_routes: int
    candidates: int


def _write_json_atomic(path: Path, data: object) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data))
    os.replace(tmp, path)


def _archived_segments(path: Path) -> list[int] | None:
    try:
        return json.loads(path.read_text()).get("segment_numbers")
    except (OSError, ValueError, AttributeError):
        return None


def sync_routes(
    client: CommaClient,
    routes_dir: Path,
    *,
    now: dt.datetime | None = None,
) -> int:
    """Save every Connect route not already archived. Returns how many were saved.

    A route is re-fetched if Connect now lists more segments than the archived
    copy has — drives that were still uploading at the last sync get completed.
    """
    routes_dir.mkdir(parents=True, exist_ok=True)
    now = now or dt.datetime.now(dt.timezone.utc)
    start_ms = int((now - SYNC_LOOKBACK).timestamp() * 1000)
    end_ms = int(now.timestamp() * 1000)

    saved = 0
    for route in client.list_routes(start_ms, end_ms):
        path = routes_dir / f"{route_file_stem(route.route_name)}.json"
        if path.exists() and _archived_segments(path) == route.segment_numbers:
            continue
        if _save_route(client, route, path):
            saved += 1
    log.info("Synced %d new or updated routes into %s", saved, routes_dir)
    return saved


def _save_route(client: CommaClient, route: RouteSummary, path: Path) -> bool:
    samples = client.fetch_route_coords(route)
    if not samples:
        log.info("Route %s has no GPS yet, will retry next sync", redact_route_name(route.route_name))
        return False
    _write_json_atomic(
        path,
        {
            "route_name": route.route_name,
            "start_time_utc_millis": route.start_time_utc_millis,
            "segment_numbers": route.segment_numbers,
            "samples": [[s.t, s.lat, s.lon, s.speed_mps] for s in samples],
        },
    )
    return True


def refresh(
    cfg: Config,
    data_dir: Path,
    output_dir: Path,
    *,
    ignore_file: Path | None = None,
    default_units: str = "mph",
) -> RefreshResult:
    """Sync new drives from Connect, then analyze the whole archive.

    Holds a lock file in `data_dir` so the timer and the review page can't run
    two refreshes at once; raises RefreshBusy if one is already running.
    """
    data_dir.mkdir(parents=True, exist_ok=True)
    with (data_dir / ".refresh.lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RefreshBusy("A refresh is already running.") from exc

        cfg.require("comma_jwt", "comma_dongle_id")
        client = CommaClient(cfg.comma_jwt, cfg.comma_dongle_id, cfg.comma_api_base)
        client.verify_auth()
        routes_dir = data_dir / "routes"
        new_routes = sync_routes(client, routes_dir)

        cfg.output_dir = output_dir
        candidates = run_analysis(
            cfg,
            local_gps_dir=routes_dir,
            match_cache_dir=data_dir / "matches",
            default_units=default_units,
        )
        ignore_ids = load_ignore_list(ignore_file) if ignore_file else set()
        candidates = [c for c in candidates if c.way_id not in ignore_ids]
        write_report(candidates, output_dir)
        write_osc(candidates, str(output_dir / "candidates.osc"), units=default_units)

        return RefreshResult(
            new_routes=new_routes,
            archived_routes=len(list(routes_dir.glob("*.json"))),
            candidates=len(candidates),
        )
