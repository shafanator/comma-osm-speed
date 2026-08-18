"""Snap GPS traces to OSM way IDs using Valhalla's trace_attributes endpoint.

Valhalla is a hidden-Markov-style map matcher: it consumes a GPS polyline and
returns the OSM ways it traversed, with per-edge geometry, length, and the
speed limit Valhalla knows about. For each input GPS sample we get back which
edge it belongs to.

We use the public OSM-hosted Valhalla at https://valhalla1.openstreetmap.de by
default. For more than a few routes a day, host your own (it's free, runs on
Docker): https://github.com/valhalla/valhalla
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

import requests

from ._retry import retry
from .comma_client import GpsSample

log = logging.getLogger(__name__)


@dataclass
class MatchedEdge:
    """One OSM way segment traversed by the trace."""

    way_id: int
    length_km: float
    # Speed samples (mph) observed by the device while on this edge.
    speed_samples_mph: list[float]
    # Lat/lon of each sample that hit this edge — used to recover a
    # representative coordinate for the way (for Mapillary/Street View links).
    lat_samples: list[float]
    lon_samples: list[float]
    # Bounding box-ish hint, used for sanity in logs.
    begin_shape_index: int
    end_shape_index: int
    # The maxspeed Valhalla *thinks* this edge has, in mph. Used as a hint, not
    # the ground truth — we re-query Overpass for authoritative current tags.
    valhalla_speed_limit_mph: float | None


class MapMatcher:
    def __init__(self, valhalla_url: str = "https://valhalla1.openstreetmap.de"):
        self.url = valhalla_url.rstrip("/")
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": "comma-osm-speed/0.1"})

    @retry(attempts=3, min_delay=2.0, max_delay=20.0)
    def _post_trace_attributes(self, body: dict) -> dict:
        resp = self._session.post(f"{self.url}/trace_attributes", json=body, timeout=120)
        resp.raise_for_status()
        return resp.json()

    def match(self, samples: list[GpsSample]) -> list[MatchedEdge]:
        """Snap a GPS trace to OSM edges and return per-edge speed samples."""
        if len(samples) < 2:
            return []

        # Valhalla wants WGS84 coords with timestamps in unix seconds.
        # Downsample if the trace is huge (>16k points causes server issues).
        step = max(1, len(samples) // 8000)
        thinned = samples[::step]

        shape = [
            {"lat": s.lat, "lon": s.lon, "time": s.t} for s in thinned
        ]
        body = {
            "shape": shape,
            "costing": "auto",
            "shape_match": "map_snap",
            "filters": {
                "attributes": [
                    "edge.way_id",
                    "edge.length",
                    "edge.speed_limit",
                    "edge.begin_shape_index",
                    "edge.end_shape_index",
                    "matched.edge_index",
                    "matched.point",
                ],
                "action": "include",
            },
        }

        data = self._post_trace_attributes(body)
        edges = data.get("edges", []) or []
        matched_points = data.get("matched_points", []) or []

        # Build per-edge sample lists by walking matched_points.
        per_edge_speeds: dict[int, list[float]] = {}
        per_edge_lats: dict[int, list[float]] = {}
        per_edge_lons: dict[int, list[float]] = {}
        for idx, mp in enumerate(matched_points):
            edge_idx = mp.get("edge_index")
            if edge_idx is None or edge_idx < 0 or edge_idx >= len(edges):
                continue
            if idx >= len(thinned):
                continue
            s = thinned[idx]
            per_edge_speeds.setdefault(edge_idx, []).append(s.speed_mps * 2.236936)
            per_edge_lats.setdefault(edge_idx, []).append(s.lat)
            per_edge_lons.setdefault(edge_idx, []).append(s.lon)

        out: list[MatchedEdge] = []
        for i, e in enumerate(edges):
            way_id = e.get("way_id")
            if not way_id:
                continue
            speed_limit = e.get("speed_limit")  # mph in valhalla
            out.append(
                MatchedEdge(
                    way_id=int(way_id),
                    length_km=float(e.get("length", 0.0)),
                    speed_samples_mph=per_edge_speeds.get(i, []),
                    lat_samples=per_edge_lats.get(i, []),
                    lon_samples=per_edge_lons.get(i, []),
                    begin_shape_index=int(e.get("begin_shape_index", 0)),
                    end_shape_index=int(e.get("end_shape_index", 0)),
                    valhalla_speed_limit_mph=float(speed_limit) if speed_limit else None,
                )
            )
        return out


def merge_edges_by_way(edges: list[MatchedEdge]) -> dict[int, list[float]]:
    """Collapse multiple matched edges into per-way speed sample lists."""
    out: dict[int, list[float]] = {}
    for e in edges:
        out.setdefault(e.way_id, []).extend(e.speed_samples_mph)
    return out


def merge_centers_by_way(edges: list[MatchedEdge]) -> dict[int, tuple[float, float]]:
    """Return one representative (lat, lon) per way: the median of the GPS
    samples that landed on that way during matching.

    Median is a good pick: robust to map-matching jitter at edge endpoints,
    and stays on the road (unlike a bounding-box centroid which can land
    in a parking lot if the way has weird geometry).
    """
    lats: dict[int, list[float]] = {}
    lons: dict[int, list[float]] = {}
    for e in edges:
        lats.setdefault(e.way_id, []).extend(e.lat_samples)
        lons.setdefault(e.way_id, []).extend(e.lon_samples)
    out: dict[int, tuple[float, float]] = {}
    for way_id in lats:
        ll = sorted(lats[way_id])
        oo = sorted(lons[way_id])
        if not ll or not oo:
            continue
        out[way_id] = (ll[len(ll) // 2], oo[len(oo) // 2])
    return out
