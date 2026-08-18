"""Tests for the local GPS-JSON loader."""
from __future__ import annotations

import json
from pathlib import Path

from comma_osm_speed.local_source import load_gps_dir, load_route_json


def _write(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload))


def test_load_route_json_list_form(tmp_path: Path):
    p = tmp_path / "r1.json"
    _write(
        p,
        {
            "route_name": "abcd1234abcd5678|2026-05-19--09-30-00",
            "start_time_utc_millis": 1716100000000,
            "samples": [
                [1716100000.0, 37.7, -122.4, 12.0, 90.0],
                [1716100001.0, 37.71, -122.41, 13.5, 91.0],
            ],
        },
    )
    route = load_route_json(p)
    assert route is not None
    assert route.route_name.startswith("abcd1234")
    assert len(route.samples) == 2
    assert route.samples[0].lat == 37.7
    assert route.samples[0].speed_mps == 12.0
    assert route.samples[1].bearing_deg == 91.0


def test_load_route_json_dict_form(tmp_path: Path):
    p = tmp_path / "r2.json"
    _write(
        p,
        {
            "route_name": "rt",
            "samples": [
                {"t": 100.0, "lat": 1.0, "lon": 2.0, "speed_mps": 5.0},
                {"t": 101.0, "lat": 1.1, "lng": 2.1, "speed": 6.0, "bearing": 45},
            ],
        },
    )
    route = load_route_json(p)
    assert route is not None
    assert len(route.samples) == 2
    assert route.samples[1].lon == 2.1
    assert route.samples[1].bearing_deg == 45.0


def test_load_gps_dir_skips_empty_and_unreadable(tmp_path: Path):
    # Valid route
    _write(
        tmp_path / "good.json",
        {
            "route_name": "good",
            "samples": [[1.0, 0.0, 0.0, 0.0]],
        },
    )
    # Empty samples — should be dropped.
    _write(tmp_path / "empty.json", {"route_name": "empty", "samples": []})
    # Malformed JSON — should be skipped, not crash.
    (tmp_path / "broken.json").write_text("{not json")

    routes = load_gps_dir(tmp_path)
    names = {r.route_name for r in routes}
    assert names == {"good"}


def test_load_gps_dir_missing_dir_raises(tmp_path: Path):
    raised = False
    try:
        load_gps_dir(tmp_path / "does-not-exist")
    except FileNotFoundError:
        raised = True
    assert raised, "expected FileNotFoundError"
