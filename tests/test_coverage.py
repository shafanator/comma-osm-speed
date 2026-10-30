import json

from comma_osm_speed.coverage import build_coverage, split_coverage


def _match_file(path, edges):
    path.write_text(json.dumps({"sample_count": 1, "edges": edges}))


def _edge(way_id, speeds):
    return {"way_id": way_id, "speed_samples_mph": speeds}


def test_coverage_counts_free_flow_samples_and_drives(tmp_path):
    _match_file(tmp_path / "a.json", [_edge(1, [2.0, 20.0, 30.0]), _edge(1, [25.0]), _edge(2, [40.0])])
    _match_file(tmp_path / "b.json", [_edge(1, [3.0, 22.0])])

    cov = build_coverage(tmp_path)

    assert cov[1].samples == 4  # the 2.0 and 3.0 mph samples are below free-flow
    assert cov[1].drives == 2  # two edges on one drive count once
    assert cov[1].route_names == ["a", "b"]
    assert cov[2].samples == 1 and cov[2].drives == 1


def test_split_coverage_uses_the_analysis_threshold(tmp_path):
    _match_file(tmp_path / "a.json", [_edge(1, [20.0] * 30), _edge(2, [20.0] * 29)])

    covered, under = split_coverage(build_coverage(tmp_path), min_samples=30)

    assert covered == {1}
    assert under == {2}


def test_unreadable_match_file_is_skipped(tmp_path):
    (tmp_path / "bad.json").write_text("{not json")
    _match_file(tmp_path / "ok.json", [_edge(5, [30.0])])

    assert set(build_coverage(tmp_path)) == {5}
