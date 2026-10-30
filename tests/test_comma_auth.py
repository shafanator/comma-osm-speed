"""Tests for Comma JWT checks, auth errors, and the coords.json host fallback."""
from __future__ import annotations

import base64
import datetime as dt
import json
import logging

import pytest

from comma_osm_speed import _retry, comma_client
from comma_osm_speed.comma_client import (
    CommaAuthError,
    CommaClient,
    RouteSummary,
    check_jwt_expiry,
    jwt_expiry,
)

NOW = dt.datetime(2026, 10, 26, tzinfo=dt.timezone.utc)
DONGLE = "0123456789abcdef"


def _jwt(claims: dict) -> str:
    def b64(obj: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")

    return f"{b64({'alg': 'HS256'})}.{b64(claims)}.sig"


def _valid_jwt() -> str:
    return _jwt({"exp": (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=60)).timestamp()})


class _Resp:
    def __init__(self, status: int, body=None):
        self.status_code = status
        self._body = body

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def test_jwt_expiry_reads_exp_claim():
    exp = NOW + dt.timedelta(days=30)
    assert jwt_expiry(_jwt({"exp": exp.timestamp()})) == exp


def test_jwt_without_exp_returns_none():
    assert jwt_expiry(_jwt({"identity": "x"})) is None


@pytest.mark.parametrize("token", ["not-a-jwt", "a.!!!.c"])
def test_malformed_jwt_raises(token):
    with pytest.raises(CommaAuthError):
        jwt_expiry(token)


def test_expired_jwt_raises_with_date():
    token = _jwt({"exp": (NOW - dt.timedelta(days=1)).timestamp()})
    with pytest.raises(CommaAuthError, match="expired on 2026-10-25"):
        check_jwt_expiry(token, now=NOW)


def test_jwt_expiring_soon_warns(caplog):
    token = _jwt({"exp": (NOW + dt.timedelta(days=3)).timestamp()})
    with caplog.at_level(logging.WARNING):
        check_jwt_expiry(token, now=NOW)
    assert "expires on 2026-10-29" in caplog.text


def test_jwt_far_from_expiry_is_quiet(caplog):
    token = _jwt({"exp": (NOW + dt.timedelta(days=60)).timestamp()})
    with caplog.at_level(logging.WARNING):
        check_jwt_expiry(token, now=NOW)
    assert caplog.text == ""


def test_client_rejects_expired_jwt():
    token = _jwt({"exp": 1.0})
    with pytest.raises(CommaAuthError):
        CommaClient(token, DONGLE)


@pytest.mark.parametrize("status", [401, 403])
def test_verify_auth_raises_on_rejected_token(monkeypatch, status):
    client = CommaClient(_valid_jwt(), DONGLE)
    calls = []

    def fake_get(url, **kwargs):
        calls.append(url)
        return _Resp(status)

    monkeypatch.setattr(client._session, "get", fake_get)
    with pytest.raises(CommaAuthError, match=f"HTTP {status}"):
        client.verify_auth()
    # Auth failures aren't retried.
    assert calls == ["https://api.comma.ai/v1/me/"]


def test_verify_auth_passes_on_200(monkeypatch):
    client = CommaClient(_valid_jwt(), DONGLE)
    monkeypatch.setattr(client._session, "get", lambda url, **kw: _Resp(200, {"id": "x"}))
    client.verify_auth()


def _route(**overrides) -> RouteSummary:
    fields = dict(
        route_name=f"{DONGLE}|0000010a--a51155e496",
        start_time_utc_millis=1_000_000,
        end_time_utc_millis=None,
        length_miles=1.0,
        url_prefix="https://chffrprivate.blob.core.windows.net/chffrprivate3/v2/x/y_z",
        segment_numbers=[0],
        segment_start_times_ms=[1_000_000],
    )
    fields.update(overrides)
    return RouteSummary(**fields)


def test_coords_fall_back_to_blob_host_when_cdn_fails(monkeypatch):
    client = CommaClient(_valid_jwt(), DONGLE)
    seen = []

    def fake_get(url, **kwargs):
        seen.append(url)
        if "azureedge.net" in url:
            return _Resp(503)
        return _Resp(200, [{"t": 1, "lat": 32.7, "lng": -80.0, "speed": 10.0}])

    monkeypatch.setattr(comma_client.requests, "get", fake_get)
    monkeypatch.setattr(_retry.time, "sleep", lambda s: None)
    samples = client.fetch_route_coords(_route())
    assert len(samples) == 1
    assert "azureedge.net" in seen[0]
    assert "blob.core.windows.net" in seen[-1]


def test_coords_use_segment_number_for_missing_start_times(monkeypatch):
    client = CommaClient(_valid_jwt(), DONGLE)
    monkeypatch.setattr(
        comma_client.requests,
        "get",
        lambda url, **kw: _Resp(200, [{"t": 0, "lat": 32.7, "lng": -80.0, "speed": 10.0}]),
    )
    # Segment 1 was never uploaded, so numbers skip from 0 to 2.
    samples = client.fetch_route_coords(
        _route(segment_numbers=[0, 2], segment_start_times_ms=[])
    )
    assert [s.t for s in samples] == [1000.0, 1120.0]
