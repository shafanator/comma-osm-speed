"""End-to-end pipeline: fetch -> match -> analyze -> emit candidates."""
from __future__ import annotations

import csv
import json
import logging
from dataclasses import asdict
from pathlib import Path

from ._redact import redact_route_name
from .analyzer import SpeedCandidate, find_candidates, summarize_way_speeds
from .comma_client import CommaClient
from .config import Config
from .local_source import load_gps_dir
from .map_matcher import MapMatcher, merge_centers_by_way, merge_edges_by_way
from .osm_client import OverpassClient

log = logging.getLogger(__name__)


def run_analysis(
    cfg: Config,
    *,
    start_unix_ms: int | None = None,
    end_unix_ms: int | None = None,
    local_gps_dir: Path | None = None,
    default_units: str = "kmh",
) -> list[SpeedCandidate]:
    """Run the full pipeline and return candidate edits.

    Two data sources: pass `local_gps_dir` to read pre-extracted JSON files
    (no Comma Prime required), or pass `start_unix_ms`/`end_unix_ms` to fetch
    from the Comma Connect cloud (Prime required).
    """
    cfg.output_dir.mkdir(parents=True, exist_ok=True)
    matcher = MapMatcher(cfg.valhalla_url)
    overpass = OverpassClient(cfg.overpass_url)

    all_per_way: dict[int, list[float]] = {}
    all_centers: dict[int, tuple[float, float]] = {}

    def _process_edges(edges):
        per_way = merge_edges_by_way(edges)
        centers = merge_centers_by_way(edges)
        for w, ss in per_way.items():
            all_per_way.setdefault(w, []).extend(ss)
        # Last writer wins for centers (fine — they're representative anyway).
        all_centers.update(centers)

    if local_gps_dir is not None:
        routes = load_gps_dir(local_gps_dir)
        log.info("Loaded %d routes from %s", len(routes), local_gps_dir)
        for r in routes:
            if len(r.samples) < 10:
                log.debug(
                    "Route %s has only %d samples, skipping",
                    redact_route_name(r.route_name),
                    len(r.samples),
                )
                continue
            log.info(
                "Map-matching route %s (%d samples)",
                redact_route_name(r.route_name),
                len(r.samples),
            )
            _process_edges(matcher.match(r.samples))
    else:
        if start_unix_ms is None or end_unix_ms is None:
            raise ValueError("Provide either local_gps_dir or both start/end_unix_ms")
        cfg.require("comma_jwt", "comma_dongle_id")
        comma = CommaClient(cfg.comma_jwt, cfg.comma_dongle_id, cfg.comma_api_base)
        routes = comma.list_routes(start_unix_ms, end_unix_ms)
        log.info("Found %d routes in window", len(routes))
        for r in routes:
            try:
                samples = comma.fetch_route_coords(r)
            except Exception as exc:  # noqa: BLE001
                log.warning("Skipping route %s: %s", redact_route_name(r.route_name), exc)
                continue
            if len(samples) < 10:
                log.debug(
                    "Route %s has only %d samples, skipping",
                    redact_route_name(r.route_name),
                    len(samples),
                )
                continue
            log.info(
                "Map-matching route %s (%d samples)",
                redact_route_name(r.route_name),
                len(samples),
            )
            _process_edges(matcher.match(samples))

    log.info("Aggregated samples across %d unique ways", len(all_per_way))

    stats = summarize_way_speeds(
        all_per_way,
        min_samples=cfg.min_samples_per_way,
        min_speed_mph_for_freeflow=cfg.min_speed_mph_for_freeflow,
    )
    log.info("%d ways have enough free-flow samples to analyze", len(stats))

    way_tags = overpass.get_ways(list(stats.keys()))
    candidates = find_candidates(
        stats,
        way_tags,
        threshold_mph=cfg.threshold_mph,
        default_units=default_units,
        way_centers=all_centers,
    )
    log.info("%d candidate edits above %.1f mph threshold", len(candidates), cfg.threshold_mph)
    return candidates


_CSV_HEADER = [
    "way_id",
    "osm_version",
    "name",
    "highway",
    "current_maxspeed_tag",
    "current_maxspeed_mph",
    "observed_p85_mph",
    "delta_mph",
    "sample_count",
    "proposed_maxspeed_mph",
    "osm_link",
    "mapillary_link",
    "street_view_link",
    "proposal_reason",
]


def _csv_row(c: SpeedCandidate) -> list:
    if c.center_lat is not None and c.center_lon is not None:
        mapillary = (
            f"https://www.mapillary.com/app/?lat={c.center_lat:.6f}"
            f"&lng={c.center_lon:.6f}&z=18"
        )
        google_sv = (
            f"https://www.google.com/maps/@?api=1&map_action=pano"
            f"&viewpoint={c.center_lat:.6f},{c.center_lon:.6f}"
        )
    else:
        mapillary = ""
        google_sv = ""
    return [
        c.way_id,
        c.osm_version,
        c.name_tag or "",
        c.highway_tag or "",
        c.current_maxspeed_tag or "",
        f"{c.current_maxspeed_mph:.1f}" if c.current_maxspeed_mph is not None else "",
        f"{c.observed_p85_mph:.1f}",
        f"{c.delta_mph:+.1f}" if c.delta_mph != float("inf") else "n/a (unset)",
        c.sample_count,
        f"{c.proposed_maxspeed_mph:.1f}",
        f"https://www.openstreetmap.org/way/{c.way_id}",
        mapillary,
        google_sv,
        c.proposal_reason,
    ]


def _write_csv(path: Path, rows: list[SpeedCandidate]) -> None:
    with path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(_CSV_HEADER)
        for c in rows:
            w.writerow(_csv_row(c))


def write_report(candidates: list[SpeedCandidate], out_dir: Path) -> dict[str, Path]:
    """Write candidate reports.

    Splits output into two CSVs because the two cases need different review:
      * `missing_maxspeed.csv` — way has no `maxspeed` tag at all. Adding a
        tag is generally welcome OSM contribution. Verify the posted sign on
        Mapillary first; the `proposed_maxspeed_mph` here is the OBSERVED
        speed, not necessarily what's posted.
      * `mismatched_maxspeed.csv` — way has a `maxspeed` tag but observed
        speed differs by more than the threshold. Higher bar for editing —
        existing tag may be the correct posted limit and you're just driving
        faster than the limit.

    Both CSVs are sorted by `sample_count` descending so the strongest
    evidence appears at the top.
    """
    out_dir.mkdir(parents=True, exist_ok=True)

    missing = [c for c in candidates if c.current_maxspeed_mph is None]
    mismatched = [c for c in candidates if c.current_maxspeed_mph is not None]
    missing.sort(key=lambda c: (-c.sample_count, c.way_id))
    mismatched.sort(key=lambda c: (-c.sample_count, c.way_id))

    paths: dict[str, Path] = {}
    paths["missing"] = out_dir / "missing_maxspeed.csv"
    paths["mismatched"] = out_dir / "mismatched_maxspeed.csv"
    paths["json"] = out_dir / "candidates.json"
    _write_csv(paths["missing"], missing)
    _write_csv(paths["mismatched"], mismatched)

    with paths["json"].open("w") as f:
        json.dump([_serialize(c) for c in candidates], f, indent=2)

    return paths


def _serialize(c: SpeedCandidate) -> dict:
    d = asdict(c)
    # Replace inf -> string for JSON compatibility
    if d["delta_mph"] == float("inf"):
        d["delta_mph"] = "infinity"
    return d
