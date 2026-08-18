"""Tests for the Comma coords payload parser + URL helpers."""
from __future__ import annotations

from comma_osm_speed.comma_client import _parse_coords_payload, _to_cdn_url


def test_to_cdn_swaps_blob_host_to_cdn():
    blob = "https://chffrprivate.blob.core.windows.net/chffrprivate3/v2/abc/def_ghi"
    cdn = _to_cdn_url(blob)
    assert cdn == "https://chffrprivate.azureedge.net/chffrprivate3/v2/abc/def_ghi"


def test_to_cdn_passes_unrecognized_host_through():
    other = "https://example.com/some/path"
    assert _to_cdn_url(other) == other


def test_to_cdn_preserves_path_and_query():
    blob = "https://chffrprivate.blob.core.windows.net/path?sig=foo"
    out = _to_cdn_url(blob)
    assert out == "https://chffrprivate.azureedge.net/path?sig=foo"


def test_parse_coords_dict_form_new_shape():
    # The current Comma coords.json shape.
    raw = [
        {"t": 2, "lat": 33.02, "lng": -80.16, "speed": 0.1, "dist": 0.0},
        {"t": 3, "lat": 33.022, "lng": -80.161, "speed": 0.5, "dist": 6.5e-05},
    ]
    out = _parse_coords_payload(raw)
    assert len(out) == 2
    assert out[0].lon == -80.16
    assert out[1].speed_mps == 0.5


def test_parse_dict_form():
    raw = [
        {"lng": -122.4, "lat": 37.7, "t": 1700000000.0, "speed": 12.5, "bearing": 90, "accuracy": 3.2},
        {"lng": -122.4001, "lat": 37.7001, "t": 1700000001.0, "speed": 13.0},
    ]
    out = _parse_coords_payload(raw)
    assert len(out) == 2
    assert out[0].lon == -122.4
    assert out[0].lat == 37.7
    assert out[0].speed_mps == 12.5
    assert out[0].bearing_deg == 90.0
    assert out[1].bearing_deg is None


def test_parse_list_form_with_speed():
    raw = [
        [-122.4, 37.7, 1700000000.0, 12.5],
        [-122.4001, 37.7001, 1700000001.0, 13.0],
    ]
    out = _parse_coords_payload(raw)
    assert len(out) == 2
    assert out[0].speed_mps == 12.5


def test_parse_list_form_no_speed():
    raw = [[-122.4, 37.7, 1700000000.0]]
    out = _parse_coords_payload(raw)
    assert len(out) == 1
    assert out[0].speed_mps == 0.0


def test_skips_junk_entries():
    raw = [
        [-122.4, 37.7, 1700000000.0],
        "not a coord",
        {"lng": "bogus", "lat": 0, "t": 0},  # bogus lng -> skipped
        {},  # no fields -> skipped
    ]
    out = _parse_coords_payload(raw)
    assert len(out) == 1


def test_non_list_returns_empty():
    assert _parse_coords_payload({"nope": True}) == []
    assert _parse_coords_payload(None) == []
