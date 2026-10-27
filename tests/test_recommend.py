import json

from comma_osm_speed import recommend
from comma_osm_speed.config import Config


class FakeOverpass:
    def __init__(self, url):
        pass

    def query(self, ql):
        geom = [{"lat": 32.80 + i * 0.001, "lon": -79.995} for i in range(11)]
        return {"elements": [{"type": "way", "id": 9, "tags": {"highway": "residential"}, "geometry": geom}]}


class FakeRouter:
    calls = 0

    def __init__(self, url):
        pass

    def route(self, locations):
        first, last = locations[0], locations[-1]
        n = 50
        shape = [
            (first["lat"] + (last["lat"] - first["lat"]) * i / n, first["lon"] + (last["lon"] - first["lon"]) * i / n)
            for i in range(n + 1)
        ]
        return 600.0 + 300.0 * (len(locations) - 2), shape


def test_build_recommendations_writes_page_data(tmp_path, monkeypatch):
    monkeypatch.setattr(recommend, "OverpassClient", FakeOverpass)
    monkeypatch.setattr(recommend, "Router", FakeRouter)
    data = tmp_path / "data"
    (data / "matches").mkdir(parents=True)
    (data / "routes").mkdir()
    (data / "matches" / "r1.json").write_text(
        json.dumps({"sample_count": 2, "edges": [{"way_id": 1, "speed_samples_mph": [30.0] * 40}]})
    )
    (data / "routes" / "r1.json").write_text(
        json.dumps({"samples": [[0, 32.70, -80.0, 10.0], [1, 32.95, -80.0, 10.0]]})
    )

    path = recommend.build_recommendations(Config(), data, tmp_path / "out")

    out = json.loads(path.read_text())
    assert out["counts"]["covered"] == 1
    assert out["counts"]["targets"] == 1
    assert len(out["layers"]["targets"]) == 1
    [detour] = out["detours"]
    assert detour["extra_min"] == 5.0
    assert [w["way_id"] for w in detour["waypoints"]] == [9]
    assert "google.com/maps/dir" in detour["google_maps_url"]
    assert (data / "area_ways.json").exists()
