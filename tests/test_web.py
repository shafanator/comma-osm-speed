"""Tests for the review web app (without spinning up a real server)."""
from __future__ import annotations

import json
from pathlib import Path


def _make_cands_json(path: Path) -> None:
    path.write_text(json.dumps([
        {
            "way_id": 100,
            "osm_version": 1,
            "observed_p85_mph": 45.0,
            "sample_count": 200,
            "current_maxspeed_tag": None,
            "current_maxspeed_mph": None,
            "proposed_maxspeed_mph": 45.0,
            "delta_mph": "infinity",
            "highway_tag": "secondary",
            "name_tag": "Main St",
            "center_lat": 32.86,
            "center_lon": -80.03,
        },
        {
            "way_id": 200,
            "osm_version": 2,
            "observed_p85_mph": 48.0,
            "sample_count": 80,
            "current_maxspeed_tag": "35 mph",
            "current_maxspeed_mph": 35.0,
            "proposed_maxspeed_mph": 48.0,
            "delta_mph": 13.0,
            "highway_tag": "tertiary",
            "name_tag": "Side St",
            "center_lat": 32.87,
            "center_lon": -80.04,
        },
    ]))


def test_state_and_candidates_filter_ignored(tmp_path: Path):
    from comma_osm_speed.web import create_app

    cands_path = tmp_path / "cands.json"
    ignore_path = tmp_path / "ignore.txt"
    _make_cands_json(cands_path)
    ignore_path.write_text("100\n")

    app = create_app(cands_path, ignore_path)
    client = app.test_client()

    r = client.get("/api/state").get_json()
    assert r["total"] == 2
    assert r["ignored"] == 1
    assert r["remaining"] == 1

    cands = client.get("/api/candidates").get_json()
    way_ids = [c["way_id"] for c in cands]
    assert way_ids == [200]


def test_post_ignore_appends_to_file(tmp_path: Path):
    from comma_osm_speed.trends import load_ignore_list
    from comma_osm_speed.web import create_app

    cands_path = tmp_path / "cands.json"
    ignore_path = tmp_path / "ignore.txt"
    _make_cands_json(cands_path)

    app = create_app(cands_path, ignore_path)
    client = app.test_client()

    r = client.post("/api/ignore", json={"way_id": 100, "comment": "residential default"})
    assert r.status_code == 200
    assert r.get_json() == {"ok": True, "way_id": 100}

    ids = load_ignore_list(ignore_path)
    assert ids == {100}
    text = ignore_path.read_text()
    assert "100" in text
    assert "residential default" in text


def test_post_ignore_is_idempotent(tmp_path: Path):
    from comma_osm_speed.web import create_app

    cands_path = tmp_path / "cands.json"
    ignore_path = tmp_path / "ignore.txt"
    _make_cands_json(cands_path)

    app = create_app(cands_path, ignore_path)
    client = app.test_client()

    client.post("/api/ignore", json={"way_id": 100})
    client.post("/api/ignore", json={"way_id": 100})
    # File contains only one entry for 100.
    content = ignore_path.read_text()
    assert content.count("100") == 1


def test_post_unignore_removes_entry(tmp_path: Path):
    from comma_osm_speed.trends import load_ignore_list
    from comma_osm_speed.web import create_app

    cands_path = tmp_path / "cands.json"
    ignore_path = tmp_path / "ignore.txt"
    _make_cands_json(cands_path)
    ignore_path.write_text("100\n200\n")

    app = create_app(cands_path, ignore_path)
    client = app.test_client()

    r = client.post("/api/unignore", json={"way_id": 100})
    assert r.status_code == 200
    assert r.get_json()["changed"] is True

    assert load_ignore_list(ignore_path) == {200}


def test_post_ignore_rejects_bad_input(tmp_path: Path):
    from comma_osm_speed.web import create_app

    cands_path = tmp_path / "cands.json"
    ignore_path = tmp_path / "ignore.txt"
    _make_cands_json(cands_path)

    app = create_app(cands_path, ignore_path)
    client = app.test_client()

    r = client.post("/api/ignore", json={"comment": "no id"})
    assert r.status_code == 400


def test_index_serves_html(tmp_path: Path):
    from comma_osm_speed.web import create_app

    cands_path = tmp_path / "cands.json"
    ignore_path = tmp_path / "ignore.txt"
    _make_cands_json(cands_path)

    app = create_app(cands_path, ignore_path)
    client = app.test_client()

    r = client.get("/")
    assert r.status_code == 200
    assert b"Speed-limit candidate review" in r.data
