"""Unit tests for analyzer + osm_client helpers."""
from __future__ import annotations

import math

from comma_osm_speed.analyzer import (
    SpeedCandidate,
    find_candidates,
    infer_proposed_maxspeed,
    summarize_way_speeds,
)
from comma_osm_speed.osm_client import WayTags, parse_maxspeed


def test_parse_maxspeed_bare_number_default_kmh():
    # OSM default: bare number means km/h. 80 km/h ≈ 49.7 mph.
    assert math.isclose(parse_maxspeed("80"), 80 * 0.621371, rel_tol=1e-3)


def test_parse_maxspeed_bare_number_default_mph_country():
    # US country default: bare number means mph.
    assert parse_maxspeed("55", default_units="mph") == 55.0


def test_parse_maxspeed_explicit_mph():
    assert parse_maxspeed("35 mph") == 35.0


def test_parse_maxspeed_explicit_kmh():
    assert math.isclose(parse_maxspeed("100 km/h"), 100 * 0.621371, rel_tol=1e-3)


def test_parse_maxspeed_unparseable_returns_none():
    assert parse_maxspeed("none") is None
    assert parse_maxspeed("signals") is None
    assert parse_maxspeed("RU:urban") is None
    assert parse_maxspeed(None) is None
    assert parse_maxspeed("") is None


def test_summarize_filters_low_sample_ways():
    samples = {
        1: [40.0] * 50,        # enough samples
        2: [40.0] * 5,         # too few samples
        3: [1.0] * 50,         # all below free-flow threshold
    }
    stats = summarize_way_speeds(samples, min_samples=30, min_speed_mph_for_freeflow=5.0)
    assert 1 in stats
    assert 2 not in stats
    assert 3 not in stats


def test_summarize_freeflow_filter_drops_idle_samples():
    # 50 samples of 40 mph + 50 samples of 0 mph (idling). The 85th percentile
    # of the moving subset should be ~40, not pulled down by zeros.
    samples = {1: [40.0] * 50 + [0.0] * 50}
    stats = summarize_way_speeds(samples, min_samples=30, min_speed_mph_for_freeflow=5.0)
    assert math.isclose(stats[1].p85_mph, 40.0, abs_tol=0.5)


def test_summarize_percentiles_are_ordered():
    samples = {1: [20.0, 25.0, 30.0, 35.0, 40.0, 45.0, 50.0, 55.0, 60.0, 65.0] * 5}
    stats = summarize_way_speeds(samples, min_samples=30, min_speed_mph_for_freeflow=5.0)
    s = stats[1]
    assert s.p50_mph <= s.p85_mph <= s.p95_mph <= s.max_mph


def test_find_candidates_flags_large_delta():
    stats = summarize_way_speeds(
        {100: [45.0] * 60},
        min_samples=30,
        min_speed_mph_for_freeflow=5.0,
    )
    way_tags = {
        100: WayTags(
            way_id=100,
            version=3,
            tags={"highway": "residential", "maxspeed": "25 mph", "name": "Test St"},
        )
    }
    cands = find_candidates(stats, way_tags, threshold_mph=10.0, default_units="mph")
    assert len(cands) == 1
    c = cands[0]
    assert c.way_id == 100
    assert c.osm_version == 3
    assert c.current_maxspeed_mph == 25.0
    assert math.isclose(c.observed_p85_mph, 45.0, abs_tol=0.5)
    assert c.delta_mph > 10.0


def test_find_candidates_ignores_small_delta():
    stats = summarize_way_speeds({101: [32.0] * 60}, min_samples=30)
    way_tags = {
        101: WayTags(way_id=101, version=1, tags={"highway": "residential", "maxspeed": "30 mph"})
    }
    cands = find_candidates(stats, way_tags, threshold_mph=10.0, default_units="mph")
    assert cands == []


def test_find_candidates_skips_non_drivable_ways():
    stats = summarize_way_speeds({102: [10.0] * 60}, min_samples=30)
    way_tags = {
        102: WayTags(way_id=102, version=1, tags={"highway": "footway"})
    }
    cands = find_candidates(stats, way_tags, threshold_mph=5.0, default_units="mph")
    assert cands == []


def test_find_candidates_flags_missing_maxspeed():
    stats = summarize_way_speeds({103: [40.0] * 60}, min_samples=30)
    way_tags = {103: WayTags(way_id=103, version=1, tags={"highway": "tertiary"})}
    cands = find_candidates(stats, way_tags, threshold_mph=10.0, default_units="mph")
    assert len(cands) == 1
    assert cands[0].current_maxspeed_mph is None
    assert cands[0].delta_mph == float("inf")


def test_proposed_maxspeed_tag_rounds_to_nearest_5():
    c = SpeedCandidate(
        way_id=1,
        osm_version=1,
        observed_p85_mph=42.3,
        sample_count=100,
        current_maxspeed_tag="30 mph",
        current_maxspeed_mph=30.0,
        proposed_maxspeed_mph=42.3,
        delta_mph=12.3,
        highway_tag="residential",
        name_tag=None,
    )
    assert c.proposed_maxspeed_tag("mph") == "40 mph"
    # Order: mph rounded to 40 first → 40 * 1.609344 = 64.37 kmh → rounds to 65.
    assert c.proposed_maxspeed_tag("kmh") == "65"


def test_infer_proposed_rounds_to_nearest_5():
    # 47 mph observed on a tertiary (typical max 45) — within range, just round.
    p = infer_proposed_maxspeed(47.0, "tertiary")
    assert p.value_mph == 45.0
    assert "rounded to 45 mph" in p.reason
    # No highway-prior flag because 45 <= tertiary's typical 45.
    assert "typical max" not in p.reason


def test_infer_proposed_rounds_up_at_half():
    # 48 mph rounds up to 50.
    assert infer_proposed_maxspeed(48.0, None).value_mph == 50.0


def test_infer_proposed_flags_implausible_highway_type():
    # 47 mph observed on a residential (typical max 30) — round to 45 but flag it.
    p = infer_proposed_maxspeed(47.0, "residential")
    assert p.value_mph == 45.0
    assert "typical max (30 mph)" in p.reason
    assert "residential" in p.reason


def test_infer_proposed_no_flag_when_within_highway_range():
    # 28 mph on a residential rounds to 30, which is plausible — no flag.
    p = infer_proposed_maxspeed(28.0, "residential")
    assert p.value_mph == 30.0
    assert "typical max" not in p.reason


def test_infer_proposed_unknown_highway_never_flags():
    # No prior for an unknown/None highway class, so we never flag.
    p = infer_proposed_maxspeed(70.0, None)
    assert p.value_mph == 70.0
    assert "typical max" not in p.reason


def test_find_candidates_proposed_is_rounded_and_has_reason():
    # Observed 47 mph (not a multiple of 5) should yield a rounded proposal of 45,
    # not the raw 47, and carry a non-empty reason.
    stats = summarize_way_speeds({200: [47.0] * 60}, min_samples=30)
    way_tags = {
        200: WayTags(
            way_id=200,
            version=2,
            tags={"highway": "residential", "maxspeed": "25 mph", "name": "Maple Ave"},
        )
    }
    cands = find_candidates(stats, way_tags, threshold_mph=10.0, default_units="mph")
    assert len(cands) == 1
    c = cands[0]
    assert c.proposed_maxspeed_mph == 45.0
    assert math.isclose(c.observed_p85_mph, 47.0, abs_tol=0.5)
    assert c.proposal_reason  # non-empty
    # residential + 45 mph proposal should be flagged by the highway prior.
    assert "typical max" in c.proposal_reason


def test_find_candidates_missing_maxspeed_has_reason():
    stats = summarize_way_speeds({201: [42.0] * 60}, min_samples=30)
    way_tags = {201: WayTags(way_id=201, version=1, tags={"highway": "tertiary"})}
    cands = find_candidates(stats, way_tags, threshold_mph=10.0, default_units="mph")
    assert len(cands) == 1
    c = cands[0]
    assert c.current_maxspeed_mph is None
    assert c.proposed_maxspeed_mph == 40.0  # 42 -> 40
    assert c.proposal_reason
