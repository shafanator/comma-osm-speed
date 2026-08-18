"""Weekly trend analysis across historical analyze runs.

Each run writes its full candidates.json into ./history/YYYY-MM-DD/. This
module loads all those snapshots and identifies way_ids that recur — those
are the highest-confidence edit targets, since they show up week after week
as you drive the same routes.

Outputs (under the latest run's folder):
    recurring_candidates.csv  - way_ids seen in >= 2 weekly runs
    trend_summary.csv         - aggregate stats: total candidates by week
"""
from __future__ import annotations

import csv
import json
import logging
import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

_DATE_DIR_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


@dataclass
class WaySnapshot:
    """One way's appearance in one historical run."""

    run_date: str
    way_id: int
    sample_count: int
    observed_p85_mph: float
    current_maxspeed_tag: str | None
    current_maxspeed_mph: float | None
    proposed_maxspeed_mph: float
    highway_tag: str | None
    name_tag: str | None
    center_lat: float | None
    center_lon: float | None


@dataclass
class WayHistory:
    """All historical sightings of one way across runs."""

    way_id: int
    snapshots: list[WaySnapshot] = field(default_factory=list)

    @property
    def weeks_seen(self) -> int:
        return len({s.run_date for s in self.snapshots})

    @property
    def latest(self) -> WaySnapshot:
        return max(self.snapshots, key=lambda s: s.run_date)

    @property
    def first_seen(self) -> str:
        return min(s.run_date for s in self.snapshots)

    @property
    def trend_samples(self) -> str:
        """Human-readable per-run sample count, oldest -> newest."""
        ordered = sorted(self.snapshots, key=lambda s: s.run_date)
        return " → ".join(f"{s.run_date}:{s.sample_count}" for s in ordered)


def discover_run_dirs(history_root: Path) -> list[Path]:
    """Return all YYYY-MM-DD subdirs under `history_root`, sorted ascending."""
    if not history_root.is_dir():
        return []
    return sorted(
        [p for p in history_root.iterdir() if p.is_dir() and _DATE_DIR_RE.match(p.name)],
        key=lambda p: p.name,
    )


def load_snapshot(run_dir: Path) -> list[WaySnapshot]:
    """Load one run's candidates.json into WaySnapshot list."""
    cj = run_dir / "candidates.json"
    if not cj.exists():
        log.debug("No candidates.json in %s", run_dir)
        return []
    try:
        data = json.loads(cj.read_text())
    except (OSError, ValueError) as exc:
        log.warning("Couldn't read %s: %s", cj, exc)
        return []
    out: list[WaySnapshot] = []
    for d in data:
        try:
            out.append(
                WaySnapshot(
                    run_date=run_dir.name,
                    way_id=int(d["way_id"]),
                    sample_count=int(d.get("sample_count", 0)),
                    observed_p85_mph=float(d.get("observed_p85_mph", 0.0)),
                    current_maxspeed_tag=d.get("current_maxspeed_tag"),
                    current_maxspeed_mph=(
                        float(d["current_maxspeed_mph"])
                        if d.get("current_maxspeed_mph") is not None
                        else None
                    ),
                    proposed_maxspeed_mph=float(d.get("proposed_maxspeed_mph", 0.0)),
                    highway_tag=d.get("highway_tag"),
                    name_tag=d.get("name_tag"),
                    center_lat=d.get("center_lat"),
                    center_lon=d.get("center_lon"),
                )
            )
        except (KeyError, TypeError, ValueError) as exc:
            log.debug("Skipping bad candidate in %s: %s", cj, exc)
    return out


def build_histories(history_root: Path) -> dict[int, WayHistory]:
    """Walk every run dir and group candidates by way_id."""
    histories: dict[int, WayHistory] = defaultdict(lambda: WayHistory(way_id=0))
    for run_dir in discover_run_dirs(history_root):
        for snap in load_snapshot(run_dir):
            if histories[snap.way_id].way_id == 0:
                histories[snap.way_id] = WayHistory(way_id=snap.way_id, snapshots=[])
            histories[snap.way_id].snapshots.append(snap)
    return histories


def write_recurring_csv(
    histories: dict[int, WayHistory],
    out_path: Path,
    *,
    min_weeks: int = 2,
    ignore_ids: set[int] | None = None,
) -> int:
    """Write a CSV of way_ids seen in >= `min_weeks` runs.

    Returns count of rows written.
    """
    ignore_ids = ignore_ids or set()
    rows: list[WayHistory] = [
        h for h in histories.values() if h.weeks_seen >= min_weeks and h.way_id not in ignore_ids
    ]
    rows.sort(key=lambda h: (-h.weeks_seen, -h.latest.sample_count))

    with out_path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(
            [
                "way_id",
                "weeks_seen",
                "first_seen",
                "latest_run",
                "trend_samples",
                "name",
                "highway",
                "current_maxspeed_tag",
                "latest_observed_p85_mph",
                "latest_proposed_maxspeed_mph",
                "osm_link",
                "mapillary_link",
                "street_view_link",
            ]
        )
        for h in rows:
            latest = h.latest
            if latest.center_lat is not None and latest.center_lon is not None:
                mapillary = (
                    f"https://www.mapillary.com/app/?lat={latest.center_lat:.6f}"
                    f"&lng={latest.center_lon:.6f}&z=18"
                )
                sv = (
                    f"https://www.google.com/maps/@?api=1&map_action=pano"
                    f"&viewpoint={latest.center_lat:.6f},{latest.center_lon:.6f}"
                )
            else:
                mapillary = sv = ""
            w.writerow(
                [
                    h.way_id,
                    h.weeks_seen,
                    h.first_seen,
                    latest.run_date,
                    h.trend_samples,
                    latest.name_tag or "",
                    latest.highway_tag or "",
                    latest.current_maxspeed_tag or "",
                    f"{latest.observed_p85_mph:.1f}",
                    f"{latest.proposed_maxspeed_mph:.1f}",
                    f"https://www.openstreetmap.org/way/{h.way_id}",
                    mapillary,
                    sv,
                ]
            )
    return len(rows)


def write_summary_csv(history_root: Path, out_path: Path) -> int:
    """Write a per-run summary row: date, total candidates, missing vs mismatched."""
    rows = []
    for run_dir in discover_run_dirs(history_root):
        snaps = load_snapshot(run_dir)
        missing = sum(1 for s in snaps if s.current_maxspeed_mph is None)
        mismatched = len(snaps) - missing
        rows.append((run_dir.name, len(snaps), missing, mismatched))

    with out_path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["run_date", "total_candidates", "missing_maxspeed", "mismatched_maxspeed"])
        for r in rows:
            w.writerow(r)
    return len(rows)


def load_ignore_list(path: Path | None) -> set[int]:
    """Read a text file of way_ids to ignore. Format: one ID per line, # comments OK."""
    if path is None or not path.exists():
        return set()
    out: set[int] = set()
    for line in path.read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        try:
            out.add(int(line))
        except ValueError:
            log.warning("Ignoring un-parseable line in ignore file: %r", line)
    return out
