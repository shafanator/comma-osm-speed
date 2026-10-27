"""Recommended drives: coverage map + targets + detours, written for the review page."""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path

from .config import Config
from .coverage import build_coverage, split_coverage
from .detours import Detour, Router, find_regular_trips, plan_detour
from .osm_client import OverpassClient
from .targets import AreaWay, Target, find_targets, load_area

log = logging.getLogger(__name__)


def _geom(way: AreaWay) -> list[list[float]]:
    return [[round(lat, 5), round(lon, 5)] for lat, lon in way.geometry]


def _target_json(t: Target) -> dict:
    return {
        "way_id": t.way.way_id,
        "name": t.way.name,
        "highway": t.way.highway,
        "kind": t.kind,
        "length_m": round(t.way.length_m),
        "samples": t.samples,
        "passes_needed": t.passes_needed,
    }


def _detour_json(d: Detour) -> dict:
    return {
        "origin": list(d.trip.origin),
        "dest": list(d.trip.dest),
        "drives": d.trip.count,
        "base_min": round(d.base_s / 60, 1),
        "extra_min": round((d.detour_s - d.base_s) / 60, 1),
        "shape": [[round(a, 5), round(b, 5)] for a, b in d.shape],
        "base_shape": [[round(a, 5), round(b, 5)] for a, b in d.base_shape],
        "waypoints": [_target_json(t) for t in d.chosen],
        "covers": [_target_json(t) for t in d.touched],
        "covers_km": round(sum(t.way.length_m for t in d.touched) / 1000, 1),
        "google_maps_url": d.google_maps_url,
    }


def build_recommendations(
    cfg: Config,
    data_dir: Path,
    output_dir: Path,
    *,
    budget_min: float = 20.0,
    max_trips: int = 3,
) -> Path:
    """Plan detours for the regular drives and write output/recommendations.json."""
    coverage = build_coverage(data_dir / "matches", min_speed_mph=cfg.min_speed_mph_for_freeflow)
    covered, under = split_coverage(coverage, cfg.min_samples_per_way)
    area = load_area(data_dir / "area_ways.json", sorted(coverage), OverpassClient(cfg.overpass_url))
    targets = find_targets(area, coverage, min_samples=cfg.min_samples_per_way)
    log.info(
        "%d driven ways (%d covered), %d nearby roads, %d targets",
        len(coverage), len(covered), len(area), len(targets),
    )

    router = Router(cfg.valhalla_url)
    detours = []
    for trip in find_regular_trips(data_dir / "routes", limit=max_trips):
        try:
            detours.append(plan_detour(trip, targets, router, budget_s=budget_min * 60))
        except Exception as exc:  # noqa: BLE001
            log.warning("Couldn't plan a detour for a drive: %s", exc)
    log.info("Planned %d detours with %d Valhalla calls", len(detours), router.calls)

    result = {
        "generated_at": time.time(),
        "budget_min": budget_min,
        "min_samples": cfg.min_samples_per_way,
        "counts": {
            "driven": len(coverage),
            "covered": len(covered),
            "under_sampled": len(under),
            "targets": len(targets),
            "one_pass_targets": sum(1 for t in targets if t.passes_needed == 1),
        },
        "layers": {
            "covered": [_geom(area[w]) for w in sorted(covered) if w in area],
            "under_sampled": [_geom(area[w]) for w in sorted(under) if w in area],
            # Never-driven targets; driven ones already show as under-sampled.
            "targets": [_geom(t.way) for t in targets if t.way.way_id not in coverage],
        },
        "detours": [_detour_json(d) for d in detours],
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "recommendations.json"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(result))
    tmp.replace(path)
    return path
