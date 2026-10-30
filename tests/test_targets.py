import json

import pytest

from comma_osm_speed.coverage import WayCoverage
from comma_osm_speed.targets import AreaWay, find_targets, load_area


def _el(way_id, highway="residential", maxspeed=None, length_deg=0.01):
    tags = {"highway": highway}
    if maxspeed:
        tags["maxspeed"] = maxspeed
    return {
        "type": "way",
        "id": way_id,
        "tags": tags,
        "geometry": [{"lat": 32.8, "lon": -80.0}, {"lat": 32.8 + length_deg, "lon": -80.0}],
    }


class FakeOverpass:
    def __init__(self, elements=None, fail=False):
        self.elements = elements or []
        self.fail = fail
        self.calls = 0

    def query(self, ql):
        self.calls += 1
        if self.fail:
            raise RuntimeError("busy")
        return {"elements": self.elements}


def test_load_area_caches_for_a_week(tmp_path):
    cache = tmp_path / "area_ways.json"
    op = FakeOverpass([_el(1)])

    assert set(load_area(cache, [1], op, now=1000.0)) == {1}
    assert set(load_area(cache, [1], op, now=1000.0 + 6 * 86400)) == {1}
    assert op.calls == 1
    load_area(cache, [1], op, now=1000.0 + 8 * 86400)
    assert op.calls == 2


def test_load_area_falls_back_to_stale_cache(tmp_path):
    cache = tmp_path / "area_ways.json"
    cache.write_text(json.dumps({"fetched_at": 0, "elements": [_el(7)]}))

    assert set(load_area(cache, [7], FakeOverpass(fail=True), now=1e9)) == {7}


def test_load_area_without_cache_raises_when_overpass_fails(tmp_path):
    with pytest.raises(RuntimeError):
        load_area(tmp_path / "area_ways.json", [1], FakeOverpass(fail=True))


def _way(way_id, highway="residential", maxspeed=None, length_deg=0.01):
    return AreaWay(way_id, highway, None, maxspeed, False, [(32.8, -80.0), (32.8 + length_deg, -80.0)])


def test_find_targets_kinds():
    area = {
        1: _way(1),  # untagged, never driven -> target
        2: _way(2, maxspeed="35 mph"),  # tagged, never driven -> not a target
        3: _way(3, maxspeed="35 mph"),  # tagged, under-sampled -> target
        4: _way(4),  # untagged but covered -> already in the review queue
        5: _way(5, highway="primary_link"),  # links aren't targets
    }
    coverage = {3: WayCoverage(3, samples=10, drives=1), 4: WayCoverage(4, samples=40, drives=2)}

    targets = {t.way.way_id: t.kind for t in find_targets(area, coverage, min_samples=30)}

    assert targets == {1: "untagged", 3: "under_sampled"}


def test_find_targets_prefers_ways_one_pass_can_cover():
    # ~1.1 km residential at 25 mph: ~100 samples in one pass. ~110 m: ~10.
    area = {1: _way(1, length_deg=0.001), 2: _way(2, length_deg=0.01)}

    targets = find_targets(area, {}, min_samples=30)

    assert [t.way.way_id for t in targets] == [2, 1]
    assert targets[0].passes_needed == 1
    assert targets[1].passes_needed == 4  # just under 10 per pass
