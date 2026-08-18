"""Load GPS traces from pre-extracted JSON files (offline mode).

The default workflow fetches routes from Comma Connect via JWT, but
`comma-osm-speed analyze --gps-dir ./gps` lets you work entirely offline
against JSON files you've produced some other way (e.g. an on-device
extractor that walks `/data/media/0/realdata` on the C3X and emits one
JSON file per route).

JSON file format (one per route):
    {
      "route_name": "<dongle>|YYYY-MM-DD--HH-MM-SS",
      "start_time_utc_millis": 1716000000000,
      "samples": [
        [t_unix_seconds, lat, lon, speed_mps, bearing_deg],
        ...
      ]
    }
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

from .comma_client import GpsSample

log = logging.getLogger(__name__)


@dataclass
class LocalRoute:
    """One route loaded from a local JSON extract."""

    route_name: str
    start_time_utc_millis: int | None
    samples: list[GpsSample] = field(default_factory=list)


def load_route_json(path: Path) -> LocalRoute | None:
    """Parse one extracted-route JSON file into a LocalRoute."""
    try:
        with path.open() as f:
            data = json.load(f)
    except (OSError, ValueError) as exc:
        log.warning("Could not read %s: %s", path, exc)
        return None

    route_name = data.get("route_name") or path.stem
    start_ms = data.get("start_time_utc_millis")
    raw_samples = data.get("samples") or []

    samples: list[GpsSample] = []
    for s in raw_samples:
        if isinstance(s, dict):
            t = s.get("t")
            lat = s.get("lat")
            lon = s.get("lon") or s.get("lng")
            speed = s.get("speed_mps") or s.get("speed") or 0.0
            bearing = s.get("bearing_deg") or s.get("bearing")
            acc = s.get("accuracy_m") or s.get("accuracy")
        elif isinstance(s, (list, tuple)) and len(s) >= 3:
            t, lat, lon = s[0], s[1], s[2]
            speed = s[3] if len(s) > 3 else 0.0
            bearing = s[4] if len(s) > 4 else None
            acc = s[5] if len(s) > 5 else None
        else:
            continue
        try:
            samples.append(
                GpsSample(
                    t=float(t),
                    lat=float(lat),
                    lon=float(lon),
                    speed_mps=float(speed),
                    bearing_deg=float(bearing) if bearing is not None else None,
                    accuracy_m=float(acc) if acc is not None else None,
                )
            )
        except (TypeError, ValueError):
            log.debug("Skipping unparseable sample in %s: %r", path, s)
            continue

    return LocalRoute(
        route_name=str(route_name),
        start_time_utc_millis=int(start_ms) if start_ms else None,
        samples=samples,
    )


def load_gps_dir(directory: Path) -> list[LocalRoute]:
    """Load every *.json file in `directory` as a route."""
    directory = Path(directory)
    if not directory.is_dir():
        raise FileNotFoundError(f"{directory} is not a directory")

    routes: list[LocalRoute] = []
    for f in sorted(directory.glob("*.json")):
        r = load_route_json(f)
        if r is None:
            continue
        if not r.samples:
            log.info("Route %s has no GPS samples, skipping", r.route_name)
            continue
        routes.append(r)
    log.info("Loaded %d routes from %s", len(routes), directory)
    return routes
