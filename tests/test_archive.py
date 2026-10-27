"""Tests for the drive archive, refresh locking, map-match caching, and the refresh endpoints."""
from __future__ import annotations

import datetime as dt
import fcntl
import json
import threading

import pytest

from comma_osm_speed import archive, pipeline
from comma_osm_speed.archive import RefreshBusy, sync_routes
from comma_osm_speed.comma_client import GpsSample, RouteSummary
from comma_osm_speed.config import Config
from comma_osm_speed.local_source import load_gps_dir
from comma_osm_speed.map_matcher import MatchedEdge
from comma_osm_speed.web import create_app

NOW = dt.datetime(2026, 10, 26, tzinfo=dt.timezone.utc)


def _route(route_id: str, segments: list[int]) -> RouteSummary:
    return RouteSummary(
        route_name=f"0123456789abcdef|{route_id}",
        start_time_utc_millis=1_000_000,
        end_time_utc_millis=None,
        length_miles=1.0,
        url_prefix="https://example.invalid/x",
        segment_numbers=segments,
    )


class FakeClient:
    def __init__(self, routes, samples=None):
        self.routes = routes
        self.samples = samples if samples is not None else [GpsSample(1.0, 32.7, -80.0, 10.0)]
        self.fetched: list[str] = []
        self.window = None

    def list_routes(self, start_ms, end_ms):
        self.window = (start_ms, end_ms)
        return self.routes

    def fetch_route_coords(self, route):
        self.fetched.append(route.route_name)
        return self.samples


def test_sync_saves_routes_in_local_source_format(tmp_path):
    client = FakeClient([_route("0000004b1--4e31a1dc72", [0, 1])])
    assert sync_routes(client, tmp_path, now=NOW) == 1

    path = tmp_path / "0000004b1--4e31a1dc72.json"
    assert path.exists()  # no dongle ID in the file name
    routes = load_gps_dir(tmp_path)
    assert routes[0].route_name == "0123456789abcdef|0000004b1--4e31a1dc72"
    assert routes[0].samples[0].speed_mps == 10.0
    # Looks back past Connect's ~1 week so a few missed syncs lose nothing.
    assert client.window[0] == int((NOW - dt.timedelta(days=10)).timestamp() * 1000)


def test_sync_skips_archived_routes(tmp_path):
    client = FakeClient([_route("a", [0, 1])])
    sync_routes(client, tmp_path, now=NOW)
    assert sync_routes(client, tmp_path, now=NOW) == 0
    assert len(client.fetched) == 1


def test_sync_refetches_route_that_gained_segments(tmp_path):
    sync_routes(FakeClient([_route("a", [0])]), tmp_path, now=NOW)
    client = FakeClient([_route("a", [0, 1])])
    assert sync_routes(client, tmp_path, now=NOW) == 1
    assert json.loads((tmp_path / "a.json").read_text())["segment_numbers"] == [0, 1]


def test_sync_skips_route_without_gps_yet(tmp_path):
    assert sync_routes(FakeClient([_route("a", [0])], samples=[]), tmp_path, now=NOW) == 0
    assert not (tmp_path / "a.json").exists()


def test_refresh_refuses_to_run_twice(tmp_path):
    with (tmp_path / ".refresh.lock").open("w") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        with pytest.raises(RefreshBusy):
            archive.refresh(Config(), tmp_path, tmp_path / "out")


class CountingMatcher:
    def __init__(self):
        self.calls = 0

    def match(self, samples):
        self.calls += 1
        return [MatchedEdge(7, 0.1, [30.0], [32.7], [-80.0], 0, 1, None)]


def test_match_cache_reuses_result_for_same_samples(tmp_path):
    sync_routes(FakeClient([_route("a", [0])]), tmp_path / "routes", now=NOW)
    route = load_gps_dir(tmp_path / "routes")[0]
    matcher = CountingMatcher()

    first = pipeline._match_local_route(matcher, route, tmp_path / "matches")
    second = pipeline._match_local_route(matcher, route, tmp_path / "matches")
    assert matcher.calls == 1
    assert second == first

    route.samples.append(GpsSample(2.0, 32.71, -80.0, 11.0))
    pipeline._match_local_route(matcher, route, tmp_path / "matches")
    assert matcher.calls == 2


def _client(tmp_path, refresh_fn):
    app = create_app(tmp_path / "candidates.json", tmp_path / "ignored.txt", refresh_fn=refresh_fn)
    return app.test_client()


def test_refresh_endpoint_runs_in_background_and_reports_result(tmp_path):
    release = threading.Event()

    def fake_refresh():
        release.wait(5)
        return archive.RefreshResult(new_routes=2, archived_routes=9, candidates=4)

    client = _client(tmp_path, fake_refresh)
    assert client.post("/api/refresh").status_code == 202
    assert client.get("/api/refresh").get_json()["running"] is True
    assert client.post("/api/refresh").status_code == 409

    release.set()
    for _ in range(100):
        status = client.get("/api/refresh").get_json()
        if not status["running"]:
            break
        threading.Event().wait(0.02)
    assert status["result"] == {"new_routes": 2, "archived_routes": 9, "candidates": 4}
    assert status["error"] is None


def test_refresh_endpoint_reports_errors(tmp_path):
    def failing():
        raise RefreshBusy("A refresh is already running.")

    client = _client(tmp_path, failing)
    client.post("/api/refresh")
    for _ in range(100):
        status = client.get("/api/refresh").get_json()
        if not status["running"]:
            break
        threading.Event().wait(0.02)
    assert status["error"] == "A refresh is already running."


def test_refresh_endpoint_disabled_without_refresh_fn(tmp_path):
    client = _client(tmp_path, None)
    assert client.get("/api/refresh").get_json()["available"] is False
    assert client.post("/api/refresh").status_code == 404
