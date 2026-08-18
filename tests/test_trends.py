"""Tests for the trends module."""
from __future__ import annotations

import json
from pathlib import Path

from comma_osm_speed.trends import (
    build_histories,
    discover_run_dirs,
    load_ignore_list,
    load_snapshot,
    write_recurring_csv,
    write_summary_csv,
)


def _write_run(root: Path, date: str, cands: list[dict]) -> Path:
    rd = root / date
    rd.mkdir(parents=True, exist_ok=True)
    (rd / "candidates.json").write_text(json.dumps(cands))
    return rd


def test_discover_run_dirs_filters_to_dates(tmp_path: Path):
    _write_run(tmp_path, "2026-05-01", [])
    _write_run(tmp_path, "2026-05-08", [])
    (tmp_path / "logs").mkdir()
    (tmp_path / "random.txt").write_text("nope")
    dirs = discover_run_dirs(tmp_path)
    assert [p.name for p in dirs] == ["2026-05-01", "2026-05-08"]


def _stub_cand(way_id: int, samples: int = 50):
    return {
        "way_id": way_id,
        "osm_version": 1,
        "observed_p85_mph": 45.0,
        "sample_count": samples,
        "current_maxspeed_tag": None,
        "current_maxspeed_mph": None,
        "proposed_maxspeed_mph": 45.0,
        "delta_mph": "infinity",
        "highway_tag": "secondary",
        "name_tag": "Foo",
        "center_lat": 32.86,
        "center_lon": -80.03,
    }


def test_load_snapshot_returns_snapshots(tmp_path: Path):
    rd = _write_run(tmp_path, "2026-05-01", [_stub_cand(1), _stub_cand(2)])
    snaps = load_snapshot(rd)
    assert len(snaps) == 2
    assert snaps[0].run_date == "2026-05-01"


def test_build_histories_groups_by_way(tmp_path: Path):
    _write_run(tmp_path, "2026-05-01", [_stub_cand(1, 50), _stub_cand(2, 30)])
    _write_run(tmp_path, "2026-05-08", [_stub_cand(1, 60), _stub_cand(3, 20)])
    hist = build_histories(tmp_path)
    assert hist[1].weeks_seen == 2
    assert hist[2].weeks_seen == 1
    assert hist[3].weeks_seen == 1
    assert hist[1].latest.sample_count == 60


def test_write_recurring_csv_filters_by_min_weeks_and_ignore(tmp_path: Path):
    _write_run(tmp_path, "2026-05-01", [_stub_cand(1), _stub_cand(2), _stub_cand(3)])
    _write_run(tmp_path, "2026-05-08", [_stub_cand(1), _stub_cand(2)])
    _write_run(tmp_path, "2026-05-15", [_stub_cand(1)])

    hist = build_histories(tmp_path)
    out = tmp_path / "rec.csv"
    n = write_recurring_csv(hist, out, min_weeks=2, ignore_ids={2})
    # way 1: 3 weeks, way 2: 2 weeks (but ignored), way 3: 1 week (filtered)
    assert n == 1
    lines = out.read_text().splitlines()
    # Header + 1 row
    assert len(lines) == 2
    way_ids_in_csv = {line.split(",", 1)[0] for line in lines[1:]}
    assert way_ids_in_csv == {"1"}


def test_write_summary_csv_one_row_per_run(tmp_path: Path):
    _write_run(tmp_path, "2026-05-01", [_stub_cand(1), _stub_cand(2)])
    _write_run(tmp_path, "2026-05-08", [_stub_cand(1)])
    out = tmp_path / "summary.csv"
    n = write_summary_csv(tmp_path, out)
    assert n == 2
    lines = out.read_text().splitlines()
    assert lines[0].startswith("run_date,")
    assert "2026-05-01" in lines[1]
    assert "2026-05-08" in lines[2]


def test_load_ignore_list_parses_and_skips_comments(tmp_path: Path):
    p = tmp_path / "ignore.txt"
    p.write_text(
        """
# header comment
123
456  # trailing comment
not-a-number
789
"""
    )
    ids = load_ignore_list(p)
    assert ids == {123, 456, 789}


def test_load_ignore_list_missing_file_returns_empty():
    assert load_ignore_list(None) == set()
