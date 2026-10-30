"""Detour planner: fit target roads into drives you already make.

1. Regular drives: cluster the start and end points of archived drives into
   places, and count trips between each pair of places.
2. For each regular drive, route it directly with Valhalla, then greedily add
   nearby targets (best first), routing O -> targets -> D each time and keeping
   a target only if the whole detour stays within the time budget.

Each target gets one "through" waypoint at its midpoint, ordered along the
drive. A through point allows no U-turn, so the route drives on past it
rather than touching the road and turning back; the final route is checked to
confirm which targets it really drives. Valhalla calls are paced (1 s apart) and capped per
drive to stay polite to the public server.
"""
from __future__ import annotations

import json
import logging
import math
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlencode

import requests

from ._retry import retry
from .targets import HALF_MILE_M, Target, haversine_m

log = logging.getLogger(__name__)

LatLon = tuple[float, float]

PLACE_RADIUS_M = 400.0
MIN_TRIP_M = 1500.0  # straight-line; shorter "trips" are parking-lot moves
# The public Valhalla server takes at most 10 locations (origin + destination
# + 8), and Google Maps links take up to 9 waypoints (fewer on mobile web).
MAX_TARGETS = 8
MAX_ROUTE_CALLS = 25


@dataclass
class Trip:
    origin: LatLon
    dest: LatLon
    count: int
    route_names: list[str] = field(default_factory=list)


def _route_endpoints(routes_dir: Path) -> list[tuple[str, LatLon, LatLon]]:
    out = []
    for path in sorted(routes_dir.glob("*.json")):
        try:
            samples = json.loads(path.read_text())["samples"]
        except (OSError, ValueError, KeyError) as exc:
            log.warning("Skipping unreadable route %s: %s", path.name, exc)
            continue
        if len(samples) >= 2:
            out.append((path.stem, (samples[0][1], samples[0][2]), (samples[-1][1], samples[-1][2])))
    return out


def find_regular_trips(routes_dir: Path, *, limit: int = 3) -> list[Trip]:
    """Most frequent origin -> destination pairs among archived drives."""
    places: list[list[LatLon]] = []  # each place: the points clustered into it

    def place_of(p: LatLon) -> int:
        for i, pts in enumerate(places):
            if haversine_m(p, pts[0]) <= PLACE_RADIUS_M:
                pts.append(p)
                return i
        places.append([p])
        return len(places) - 1

    pairs: dict[tuple[int, int], list[str]] = {}
    for name, start, end in _route_endpoints(routes_dir):
        a, b = place_of(start), place_of(end)
        if a != b:
            pairs.setdefault((a, b), []).append(name)

    def center(i: int) -> LatLon:
        pts = places[i]
        return (sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts))

    trips = [
        Trip(center(a), center(b), len(names), names)
        for (a, b), names in pairs.items()
        if haversine_m(center(a), center(b)) >= MIN_TRIP_M
    ]
    trips.sort(key=lambda t: -t.count)
    return trips[:limit]


def decode_polyline6(encoded: str) -> list[LatLon]:
    coords, index, lat, lon = [], 0, 0, 0
    while index < len(encoded):
        for which in (0, 1):
            shift = result = 0
            while True:
                b = ord(encoded[index]) - 63
                index += 1
                result |= (b & 0x1F) << shift
                shift += 5
                if b < 0x20:
                    break
            delta = ~(result >> 1) if result & 1 else result >> 1
            if which == 0:
                lat += delta
            else:
                lon += delta
        coords.append((lat / 1e6, lon / 1e6))
    return coords


class RouteNotFound(Exception):
    """Valhalla can't route through these locations (a 400, not worth retrying)."""


class Router:
    """Valhalla /route, paced so a planning run doesn't hammer the server."""

    def __init__(self, valhalla_url: str, *, pause_s: float = 1.0):
        self.url = valhalla_url.rstrip("/")
        self.pause_s = pause_s
        self.calls = 0
        self._last = 0.0
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": "comma-osm-speed/0.1"})

    @retry(attempts=3, min_delay=2.0, max_delay=20.0, no_retry=(RouteNotFound,))
    def _post(self, body: dict) -> dict:
        resp = self._session.post(f"{self.url}/route", json=body, timeout=60)
        if resp.status_code == 400:
            raise RouteNotFound(resp.text[:200])
        resp.raise_for_status()
        return resp.json()

    def route(self, locations: list[dict]) -> tuple[float, list[LatLon]]:
        """(seconds, shape) for driving through `locations` in order."""
        wait = self._last + self.pause_s - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self.calls += 1
        try:
            data = self._post({"locations": locations, "costing": "auto", "directions_type": "none"})
        finally:
            self._last = time.monotonic()
        trip = data["trip"]
        shape: list[LatLon] = []
        for leg in trip["legs"]:
            pts = decode_polyline6(leg["shape"])
            shape.extend(pts[1:] if shape else pts)
        return float(trip["summary"]["time"]), shape


class _Proj:
    """Equirectangular projection to metres around a reference latitude."""

    def __init__(self, lat0: float):
        self.kx = 111320.0 * math.cos(math.radians(lat0))
        self.ky = 110540.0

    def xy(self, p: LatLon) -> tuple[float, float]:
        return (p[1] * self.kx, p[0] * self.ky)


class _Polyline:
    """Distance from points to a polyline, and how far along it they fall."""

    CELL = 200.0

    def __init__(self, shape: list[LatLon]):
        self.proj = _Proj(shape[0][0])
        self.pts = [self.proj.xy(p) for p in shape]
        self.cum = [0.0]
        for a, b in zip(self.pts, self.pts[1:], strict=False):
            self.cum.append(self.cum[-1] + math.dist(a, b))
        self.grid: dict[tuple[int, int], list[int]] = {}
        for i, (a, b) in enumerate(zip(self.pts, self.pts[1:], strict=False)):
            for cx in range(int(min(a[0], b[0]) // self.CELL), int(max(a[0], b[0]) // self.CELL) + 1):
                for cy in range(int(min(a[1], b[1]) // self.CELL), int(max(a[1], b[1]) // self.CELL) + 1):
                    self.grid.setdefault((cx, cy), []).append(i)

    def nearest(self, p: LatLon, max_m: float) -> tuple[float, float]:
        """(distance m, metres along the line); distance is inf beyond max_m."""
        x, y = self.proj.xy(p)
        r = int(max_m // self.CELL) + 1
        cx, cy = int(x // self.CELL), int(y // self.CELL)
        best = (math.inf, 0.0)
        seen: set[int] = set()
        for gx in range(cx - r, cx + r + 1):
            for gy in range(cy - r, cy + r + 1):
                for i in self.grid.get((gx, gy), ()):
                    if i in seen:
                        continue
                    seen.add(i)
                    (ax, ay), (bx, by) = self.pts[i], self.pts[i + 1]
                    dx, dy = bx - ax, by - ay
                    seg2 = dx * dx + dy * dy
                    t = 0.0 if seg2 == 0 else max(0.0, min(1.0, ((x - ax) * dx + (y - ay) * dy) / seg2))
                    d = math.dist((x, y), (ax + t * dx, ay + t * dy))
                    if d < best[0]:
                        best = (d, self.cum[i] + t * math.sqrt(seg2))
        return best if best[0] <= max_m else (math.inf, 0.0)


def _point_along(geom: list[LatLon], frac: float) -> LatLon:
    """The point `frac` of the way along a way's geometry."""
    segs = [haversine_m(a, b) for a, b in zip(geom, geom[1:], strict=False)]
    goal = sum(segs) * frac
    for (a, b), d in zip(zip(geom, geom[1:], strict=False), segs, strict=False):
        if goal <= d and d > 0:
            t = goal / d
            return (a[0] + t * (b[0] - a[0]), a[1] + t * (b[1] - a[1]))
        goal -= d
    return geom[-1]


@dataclass
class _Entry:
    target: Target
    along_m: float
    off_route_m: float
    point: LatLon  # midpoint of the way


def _entry(target: Target, line: _Polyline) -> _Entry | None:
    mid = _point_along(target.way.geometry, 0.5)
    off, along = line.nearest(mid, HALF_MILE_M)
    if math.isinf(off):
        return None
    return _Entry(target, along, off, mid)


def _loc(p: LatLon, kind: str = "through") -> dict:
    return {"lat": round(p[0], 6), "lon": round(p[1], 6), "type": kind}


def _locations(trip: Trip, entries: list[_Entry]) -> list[dict]:
    mids = [_loc(e.point) for e in sorted(entries, key=lambda e: e.along_m)]
    return [_loc(trip.origin, "break"), *mids, _loc(trip.dest, "break")]


def touched_targets(shape: list[LatLon], targets: list[Target], *, within_m: float = 25.0) -> list[Target]:
    """Targets the route drives most (>= 80% of the points) of."""
    line = _Polyline(shape)
    out = []
    for t in targets:
        pts = t.way.geometry
        near = sum(1 for p in pts if not math.isinf(line.nearest(p, within_m)[0]))
        if near >= 0.8 * len(pts):
            out.append(t)
    return out


def google_maps_url(origin: LatLon, dest: LatLon, waypoints: list[LatLon]) -> str:
    params = {
        "api": "1",
        "origin": f"{origin[0]:.6f},{origin[1]:.6f}",
        "destination": f"{dest[0]:.6f},{dest[1]:.6f}",
        "travelmode": "driving",
    }
    if waypoints:
        params["waypoints"] = "|".join(f"{p[0]:.6f},{p[1]:.6f}" for p in waypoints[:9])
    return "https://www.google.com/maps/dir/?" + urlencode(params, safe=",|")


@dataclass
class Detour:
    trip: Trip
    base_s: float
    detour_s: float
    base_shape: list[LatLon]
    shape: list[LatLon]
    chosen: list[Target]  # targets the planner routed through
    touched: list[Target]  # every target the final route drives
    google_maps_url: str


def plan_detour(
    trip: Trip,
    targets: list[Target],
    router: Router,
    *,
    budget_s: float = 20 * 60,
    max_calls: int = MAX_ROUTE_CALLS,
    progress: Callable[[str], None] | None = None,
) -> Detour:
    """Greedy: try the best nearby targets one by one, keep those that fit."""
    base_s, base_shape = router.route([_loc(trip.origin, "break"), _loc(trip.dest, "break")])
    line = _Polyline(base_shape)
    # One-pass targets only: a detour that leaves a road half-sampled doesn't
    # move it into the analysis.
    entries = [e for t in targets if t.passes_needed == 1 and (e := _entry(t, line))]
    # Closest to the drive first, then longest.
    entries.sort(key=lambda e: (round(e.off_route_m / 100), -e.target.way.length_m))

    chosen: list[_Entry] = []
    best_s, best_shape = base_s, base_shape
    calls = 1
    for e in entries:
        if len(chosen) >= MAX_TARGETS or calls >= max_calls:
            break
        if touched_targets(best_shape, [e.target]):
            continue  # the current route already drives it
        calls += 1
        try:
            secs, shape = router.route(_locations(trip, chosen + [e]))
        except Exception as exc:  # noqa: BLE001
            log.warning("Routing through way %d failed: %s", e.target.way.way_id, exc)
            continue
        if secs - base_s <= budget_s:
            chosen.append(e)
            best_s, best_shape = secs, shape
            if progress:
                progress(f"+way {e.target.way.way_id}: {(secs - base_s) / 60:.1f} min extra")

    ordered = sorted(chosen, key=lambda e: e.along_m)
    return Detour(
        trip=trip,
        base_s=base_s,
        detour_s=best_s,
        base_shape=base_shape,
        shape=best_shape,
        chosen=[e.target for e in ordered],
        touched=touched_targets(best_shape, targets),
        google_maps_url=google_maps_url(
            trip.origin, trip.dest, [e.point for e in ordered]
        ),
    )
