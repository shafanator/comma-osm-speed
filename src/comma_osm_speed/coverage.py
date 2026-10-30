"""Coverage map: which OSM ways the archive has driven, and how well.

Built from the map-match cache (data/matches), so it costs no network calls.
A way counts as "covered" once it has as many free-flow samples as the
analysis needs (min_samples_per_way); below that it is "under-sampled" and
the analysis skips it.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)


@dataclass
class WayCoverage:
    way_id: int
    # Free-flow samples (>= min_speed) across all drives: what the analysis counts.
    samples: int = 0
    # How many archived drives passed over this way.
    drives: int = 0
    route_names: list[str] = field(default_factory=list)


def build_coverage(
    matches_dir: Path, *, min_speed_mph: float = 5.0
) -> dict[int, WayCoverage]:
    """Aggregate the match cache into per-way sample and drive counts."""
    out: dict[int, WayCoverage] = {}
    for path in sorted(matches_dir.glob("*.json")):
        try:
            edges = json.loads(path.read_text())["edges"]
        except (OSError, ValueError, KeyError) as exc:
            log.warning("Skipping unreadable match file %s: %s", path.name, exc)
            continue
        seen_here: set[int] = set()
        for e in edges:
            wid = int(e["way_id"])
            cov = out.setdefault(wid, WayCoverage(way_id=wid))
            cov.samples += sum(1 for s in e.get("speed_samples_mph", []) if s >= min_speed_mph)
            if wid not in seen_here:
                seen_here.add(wid)
                cov.drives += 1
                cov.route_names.append(path.stem)
    return out


def split_coverage(
    coverage: dict[int, WayCoverage], min_samples: int
) -> tuple[set[int], set[int]]:
    """(covered, under_sampled) way IDs."""
    covered = {w for w, c in coverage.items() if c.samples >= min_samples}
    return covered, set(coverage) - covered
