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


def test_event_appends_flat_fields_to_log(tmp_path):
    from comma_osm_speed.web import create_app

    events = tmp_path / "data" / "review_events.jsonl"
    client = create_app(
        tmp_path / "candidates.json", tmp_path / "ignored.txt", events_path=events
    ).test_client()
    assert client.post("/api/event", json={"event": "view", "way_id": 100, "kind": "missing"}).status_code == 200
    client.post("/api/event", json={"event": "open_link", "link": "edit", "nested": {"x": 1}})

    rows = [json.loads(line) for line in events.read_text().splitlines()]
    assert [r["event"] for r in rows] == ["view", "open_link"]
    assert rows[0]["way_id"] == 100 and "ts" in rows[0]
    assert "nested" not in rows[1]  # only flat values are kept


def test_event_requires_event_name(tmp_path):
    from comma_osm_speed.web import create_app

    client = create_app(
        tmp_path / "c.json", tmp_path / "i.txt", events_path=tmp_path / "e.jsonl"
    ).test_client()
    assert client.post("/api/event", json={"way_id": 1}).status_code == 400


def test_event_is_noop_without_log_path(tmp_path):
    from comma_osm_speed.web import create_app

    client = create_app(tmp_path / "c.json", tmp_path / "i.txt").test_client()
    assert client.post("/api/event", json={"event": "view"}).status_code == 204


def _fixed_client(tmp_path):
    from comma_osm_speed.web import create_app

    cands = tmp_path / "candidates.json"
    _make_cands_json(cands)
    app = create_app(cands, tmp_path / "ignored.txt", fixed_path=tmp_path / "data" / "fixed_ways.json")
    return app.test_client(), cands


def test_fixed_way_hidden_until_next_analysis(tmp_path):
    client, _ = _fixed_client(tmp_path)
    assert client.post("/api/fixed", json={"way_id": 200}).status_code == 200
    ids = [c["way_id"] for c in client.get("/api/candidates").get_json()]
    assert ids == [100]
    assert client.get("/api/state").get_json()["remaining"] == 1
    # Not added to the ignore list.
    assert not (tmp_path / "ignored.txt").exists()


def test_fixed_way_comes_back_first_with_fixed_at_after_new_analysis(tmp_path):
    import os
    import time

    client, cands = _fixed_client(tmp_path)
    client.post("/api/fixed", json={"way_id": 200})
    # A newer analysis that still flags way 200.
    later = time.time() + 5
    os.utime(cands, (later, later))

    out = client.get("/api/candidates").get_json()
    assert [c["way_id"] for c in out] == [200, 100]
    assert out[0]["fixed_at"]
    assert "fixed_at" not in out[1]


def test_fixed_requires_way_id(tmp_path):
    client, _ = _fixed_client(tmp_path)
    assert client.post("/api/fixed", json={}).status_code == 400


def test_way_geometry_returns_ordered_points_and_live_tags(tmp_path, monkeypatch):
    from comma_osm_speed import osm_client
    from comma_osm_speed.web import create_app

    payload = {"elements": [
        {"type": "node", "id": 2, "lat": 32.2, "lon": -80.2},
        {"type": "node", "id": 1, "lat": 32.1, "lon": -80.1},
        {"type": "way", "id": 300009020, "version": 6, "nodes": [1, 2],
         "tags": {"highway": "service", "maxspeed": "25 mph"}},
    ]}
    calls = []

    class Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return payload

    def fake_get(url, **kwargs):
        calls.append(url)
        return Resp()

    monkeypatch.setattr(osm_client.requests, "get", fake_get)
    client = create_app(tmp_path / "c.json", tmp_path / "i.txt").test_client()

    body = client.get("/api/way_geometry/300009020").get_json()
    assert body["geometry"] == [{"lat": 32.1, "lon": -80.1}, {"lat": 32.2, "lon": -80.2}]
    assert body["version"] == 6
    assert body["maxspeed"] == "25 mph"
    assert calls == ["https://api.openstreetmap.org/api/0.6/way/300009020/full.json"]
    # Not cached, so an edit made after the first view still shows up.
    client.get("/api/way_geometry/300009020")
    assert len(calls) == 2


def test_recommendations_served_from_next_to_candidates(tmp_path):
    from comma_osm_speed.web import create_app

    client = create_app(tmp_path / "candidates.json", tmp_path / "i.txt").test_client()
    assert client.get("/api/recommendations").status_code == 404
    (tmp_path / "recommendations.json").write_text('{"detours": []}')
    assert client.get("/api/recommendations").get_json() == {"detours": []}
    assert client.get("/drives").status_code == 200
