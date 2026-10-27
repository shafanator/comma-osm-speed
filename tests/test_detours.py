import json
from urllib.parse import parse_qs, urlparse

from comma_osm_speed.detours import (
    RouteNotFound,
    Trip,
    decode_polyline6,
    find_regular_trips,
    google_maps_url,
    plan_detour,
    touched_targets,
)
from comma_osm_speed.targets import AreaWay, Target

HOME = (32.860, -80.020)
WORK = (32.780, -79.935)


def _route_file(path, start, end):
    path.write_text(json.dumps({"samples": [[0, *start, 10.0], [1, *end, 10.0]]}))


def test_regular_trips_cluster_nearby_endpoints(tmp_path):
    _route_file(tmp_path / "a.json", HOME, WORK)
    _route_file(tmp_path / "b.json", (HOME[0] + 0.001, HOME[1]), WORK)  # ~110 m away: same place
    _route_file(tmp_path / "c.json", WORK, HOME)
    _route_file(tmp_path / "d.json", HOME, (HOME[0] + 0.005, HOME[1]))  # ~550 m: too short a trip

    trips = find_regular_trips(tmp_path)

    assert [(t.count, sorted(t.route_names)) for t in trips] == [(2, ["a", "b"]), (1, ["c"])]


def _encode_polyline6(points):
    out, prev = [], (0, 0)
    for lat, lon in points:
        cur = (round(lat * 1e6), round(lon * 1e6))
        for delta in (cur[0] - prev[0], cur[1] - prev[1]):
            v = ~(delta << 1) if delta < 0 else delta << 1
            while v >= 0x20:
                out.append(chr((0x20 | (v & 0x1F)) + 63))
                v >>= 5
            out.append(chr(v + 63))
        prev = cur
    return "".join(out)


def test_decode_polyline6_round_trips():
    pts = [(32.86227, -80.026444), (32.862261, -80.026443), (32.78, -79.935)]
    assert decode_polyline6(_encode_polyline6(pts)) == pts


def test_google_maps_url_caps_waypoints_at_nine():
    url = google_maps_url(HOME, WORK, [(32.8, -80.0)] * 12)
    q = parse_qs(urlparse(url).query)
    assert q["origin"] == ["32.860000,-80.020000"]
    assert len(q["waypoints"][0].split("|")) == 9


def _target(way_id, lat, length_deg=0.01):
    # A north-south way starting at `lat`, -80.0.
    way = AreaWay(way_id, "residential", None, None, False, [(lat, -80.0), (lat + length_deg / 2, -80.0), (lat + length_deg, -80.0)])
    return Target(way, "untagged", 0, 100.0, 1)


def _line(a, b, n=50):
    return [(a[0] + (b[0] - a[0]) * i / n, a[1] + (b[1] - a[1]) * i / n) for i in range(n + 1)]


def test_touched_targets_needs_most_of_the_way_driven():
    t = _target(1, 32.80)
    assert touched_targets(_line((32.79, -80.0), (32.82, -80.0)), [t]) == [t]
    assert touched_targets(_line((32.79, -80.0), (32.804, -80.0)), [t]) == []


class FakeRouter:
    """Base route straight north along lon -80.0; each waypoint adds 6 minutes."""

    def __init__(self, fail_way_lat=None):
        self.calls = []
        self.fail_way_lat = fail_way_lat

    def route(self, locations):
        self.calls.append(locations)
        mids = locations[1:-1]
        if self.fail_way_lat is not None and any(abs(m["lat"] - self.fail_way_lat) < 1e-3 for m in mids):
            raise RouteNotFound("no route")
        first, last = locations[0], locations[-1]
        return 600.0 + 360.0 * len(mids), _line((first["lat"], first["lon"]), (last["lat"], last["lon"]))


def test_plan_detour_keeps_targets_within_budget_and_orders_waypoints():
    trip = Trip((32.70, -80.0), (32.95, -80.0), 1)
    # Offset east of the route so the base route doesn't already drive them.
    targets = [_target(i, 32.90 - i * 0.03) for i in range(5)]
    for t in targets:
        t.way.geometry = [(lat, -79.995) for lat, _ in t.way.geometry]

    d = plan_detour(trip, targets, FakeRouter(), budget_s=20 * 60)

    assert d.detour_s - d.base_s <= 20 * 60
    assert len(d.chosen) == 3  # 3 x 6 min fits in 20; a 4th doesn't
    lats = [t.way.geometry[0][0] for t in d.chosen]
    assert lats == sorted(lats)  # waypoints follow the drive's direction
    assert len(parse_qs(urlparse(d.google_maps_url).query)["waypoints"][0].split("|")) == 3


def test_plan_detour_skips_targets_valhalla_cannot_route():
    trip = Trip((32.70, -80.0), (32.95, -80.0), 1)
    targets = [_target(1, 32.80), _target(2, 32.85)]
    for t in targets:
        t.way.geometry = [(lat, -79.995) for lat, _ in t.way.geometry]

    d = plan_detour(trip, targets, FakeRouter(fail_way_lat=32.805))

    assert [t.way.way_id for t in d.chosen] == [2]


def test_plan_detour_ignores_targets_far_from_the_drive():
    trip = Trip((32.70, -80.0), (32.95, -80.0), 1)
    far = _target(1, 32.80)
    far.way.geometry = [(lat, -79.95) for lat, _ in far.way.geometry]  # ~4.7 km east

    router = FakeRouter()
    d = plan_detour(trip, [far], router)

    assert d.chosen == [] and len(router.calls) == 1
