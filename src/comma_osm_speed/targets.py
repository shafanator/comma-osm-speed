"""Target finder: roads near your drives that need data.

A target is a drivable way within `radius_m` of a way you've driven that
either has no maxspeed tag (and isn't already covered — covered untagged ways
are in the review queue), or that you've driven but not enough for the
analysis to use (under-sampled).

The roads near your drives come from one Overpass query, cached in
data/area_ways.json and refreshed at most weekly: the public server is often
busy, and road tags change slowly.
"""
from __future__ import annotations

import json
import logging
import math
import time
from dataclasses import dataclass
from pathlib import Path

from .coverage import WayCoverage
from .osm_client import OverpassClient

log = logging.getLogger(__name__)

AREA_MAX_AGE_S = 7 * 24 * 3600
HALF_MILE_M = 805.0

# Roads that get speed limits. Links are fetched for the map but are not
# targets: they're short and rarely carry a maxspeed of their own.
_TARGET_HIGHWAYS = (
    "motorway", "trunk", "primary", "secondary", "tertiary",
    "unclassified", "residential", "living_street",
)
_LINK_HIGHWAYS = ("motorway_link", "trunk_link", "primary_link", "secondary_link", "tertiary_link")

# Rough free-flow speed per road type, used to guess how many samples (1 per
# second) one pass collects. Untagged roads have no limit to go by.
_TYPICAL_MPH = {
    "motorway": 60, "trunk": 50, "primary": 45, "secondary": 40, "tertiary": 35,
    "unclassified": 30, "residential": 25, "living_street": 15,
}


@dataclass
class AreaWay:
    way_id: int
    highway: str
    name: str | None
    maxspeed: str | None
    oneway: bool
    geometry: list[tuple[float, float]]  # (lat, lon) in way order

    @property
    def length_m(self) -> float:
        return sum(haversine_m(a, b) for a, b in zip(self.geometry, self.geometry[1:], strict=False))


@dataclass
class Target:
    way: AreaWay
    kind: str  # "untagged" or "under_sampled"
    samples: int  # free-flow samples already collected
    samples_per_pass: float  # estimated
    passes_needed: int


def haversine_m(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lon1 = map(math.radians, a)
    lat2, lon2 = map(math.radians, b)
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 2 * 6371000.0 * math.asin(math.sqrt(h))


def _area_query(driven_way_ids: list[int], radius_m: float) -> str:
    kinds = "|".join(_TARGET_HIGHWAYS + _LINK_HIGHWAYS)
    ids = ",".join(str(w) for w in sorted(driven_way_ids))
    return f"""[out:json][timeout:180];
way(id:{ids})->.d;
way(around.d:{radius_m:.0f})[highway~"^({kinds})$"][access!~"^(private|no)$"];
out tags geom;"""


def _parse_area(elements: list[dict]) -> dict[int, AreaWay]:
    out: dict[int, AreaWay] = {}
    for el in elements:
        if el.get("type") != "way" or not el.get("geometry"):
            continue
        tags = el.get("tags") or {}
        wid = int(el["id"])
        out[wid] = AreaWay(
            way_id=wid,
            highway=tags.get("highway", ""),
            name=tags.get("name") or tags.get("ref"),
            maxspeed=tags.get("maxspeed"),
            oneway=tags.get("oneway") in ("yes", "1", "true") or tags.get("highway") == "motorway",
            geometry=[(float(p["lat"]), float(p["lon"])) for p in el["geometry"]],
        )
    return out


def load_area(
    cache_path: Path,
    driven_way_ids: list[int],
    overpass: OverpassClient,
    *,
    radius_m: float = HALF_MILE_M,
    max_age_s: float = AREA_MAX_AGE_S,
    now: float | None = None,
) -> dict[int, AreaWay]:
    """Roads within radius_m of the driven ways, from cache when fresh enough.

    Falls back to a stale cache if Overpass is unavailable.
    """
    now = time.time() if now is None else now
    cached = None
    try:
        cached = json.loads(cache_path.read_text())
    except (OSError, ValueError):
        pass
    if cached and now - cached.get("fetched_at", 0) < max_age_s:
        return _parse_area(cached["elements"])
    if not driven_way_ids:
        return {}
    try:
        data = overpass.query(_area_query(driven_way_ids, radius_m))
    except Exception as exc:  # noqa: BLE001
        if cached:
            log.warning("Overpass unavailable (%s); using the cached road list", exc)
            return _parse_area(cached["elements"])
        raise
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = cache_path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"fetched_at": now, "elements": data.get("elements", [])}))
    tmp.replace(cache_path)
    return _parse_area(data.get("elements", []))


def find_targets(
    area: dict[int, AreaWay],
    coverage: dict[int, WayCoverage],
    *,
    min_samples: int = 30,
) -> list[Target]:
    """Targets, most valuable first (fewest passes needed, then longest)."""
    out: list[Target] = []
    for way in area.values():
        if way.highway not in _TARGET_HIGHWAYS:
            continue
        cov = coverage.get(way.way_id)
        samples = cov.samples if cov else 0
        if samples >= min_samples:
            continue  # covered: the analysis already has it
        if way.maxspeed is None:
            kind = "untagged"
        elif cov is not None:
            kind = "under_sampled"
        else:
            continue  # tagged and never driven: nothing to check it against
        mps = _TYPICAL_MPH.get(way.highway, 25) * 0.44704
        per_pass = way.length_m / mps
        needed = math.ceil((min_samples - samples) / per_pass) if per_pass > 0 else 99
        out.append(Target(way, kind, samples, per_pass, needed))
    out.sort(key=lambda t: (t.passes_needed, -t.way.length_m))
    return out
