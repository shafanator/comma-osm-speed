"""Client for the Comma Connect API.

API reference: the canonical doc is https://api.comma.ai/, and the openapi
spec lives at https://github.com/commaai/comma-api/blob/master/openapi.yaml.

This module mirrors what the connect.comma.ai web UI actually does (verified
via its Chrome devtools traffic).

Endpoints we use
----------------
* `GET /v1/devices/:dongle_id/routes_segments?start={ms}&end={ms}&limit={n}`
    Lists ROUTES (despite the name; each item is a route with a `segment_numbers`
    list inside). Each route carries:
      - `fullname`               route name like `<dongle>|<hex>--<hex>`
      - `url`                    signed blob URL prefix on blob.core.windows.net
      - `segment_numbers`        list of segment indices that exist
      - `segment_start_times`    parallel list of unix-millis per segment
      - start/end timestamps, lat/lng, length, etc.

* `<azureedge-host>/<segment_n>/coords.json`
    Per-segment GPS path on Comma's Azure CDN. Format:
        [{"t": <s-from-segment-start>, "lat": ..., "lng": ..., "speed": <m/s>,
          "dist": <cumulative miles>}, ...]
    Sampled at 1 Hz. Public — no Authorization header needed.

The CDN host (`chffrprivate.azureedge.net`) differs from the blob storage host
(`chffrprivate.blob.core.windows.net`) embedded in the `url` field; we
substitute hosts but keep the rest of the path.

Auth: bearer JWT issued at https://jwt.comma.ai.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse, urlunparse

import requests

from ._retry import retry

log = logging.getLogger(__name__)

# Hosts in the wild for Comma's GPS data.
_BLOB_HOST = "chffrprivate.blob.core.windows.net"
_CDN_HOST = "chffrprivate.azureedge.net"


@dataclass
class RouteSummary:
    """One route as returned by /routes_segments."""

    route_name: str
    start_time_utc_millis: int | None
    end_time_utc_millis: int | None
    length_miles: float
    url_prefix: str  # the route's signed blob URL prefix
    segment_numbers: list[int] = field(default_factory=list)
    segment_start_times_ms: list[int] = field(default_factory=list)


@dataclass
class GpsSample:
    """One GPS fix from the device."""

    t: float  # unix seconds (UTC)
    lat: float
    lon: float
    speed_mps: float
    bearing_deg: float | None = None
    accuracy_m: float | None = None


class CommaClient:
    def __init__(self, jwt: str, dongle_id: str, api_base: str = "https://api.comma.ai"):
        if not jwt:
            raise ValueError("Comma JWT is required (get one at https://jwt.comma.ai)")
        if not dongle_id:
            raise ValueError("Comma dongle_id is required")
        self.jwt = jwt
        self.dongle_id = dongle_id
        self.api_base = api_base.rstrip("/")
        self._session = requests.Session()
        self._session.headers.update(
            {
                "Authorization": f"JWT {jwt}",
                "Accept": "application/json",
                "User-Agent": "comma-osm-speed/0.1",
            }
        )

    @retry(attempts=3, min_delay=1.0, max_delay=10.0)
    def _get_json(self, path: str, **kwargs: Any) -> Any:
        url = f"{self.api_base}{path}"
        log.debug("GET %s", url)
        resp = self._session.get(url, timeout=30, **kwargs)
        resp.raise_for_status()
        return resp.json()

    def list_routes(
        self,
        start_unix_ms: int,
        end_unix_ms: int,
        *,
        limit: int = 1000,
    ) -> list[RouteSummary]:
        """Return RouteSummary objects from the Connect routes_segments endpoint."""
        raw = self._get_json(
            f"/v1/devices/{self.dongle_id}/routes_segments",
            params={"start": start_unix_ms, "end": end_unix_ms, "limit": limit},
        )
        if not isinstance(raw, list):
            log.warning("Unexpected routes_segments payload type: %r", type(raw))
            return []
        out: list[RouteSummary] = []
        for r in raw:
            url = (r.get("url") or "").rstrip("/")
            if not url:
                continue
            route_name = r.get("fullname") or _route_name_from_url(url)
            seg_nums = list(r.get("segment_numbers") or [])
            seg_starts = list(r.get("segment_start_times") or [])
            try:
                seg_nums = [int(n) for n in seg_nums]
                seg_starts = [int(t) for t in seg_starts]
            except (TypeError, ValueError):
                continue
            length_mi = float(r.get("distance", 0.0) or 0.0)
            out.append(
                RouteSummary(
                    route_name=str(route_name),
                    start_time_utc_millis=int(r.get("start_time_utc_millis") or 0) or None,
                    end_time_utc_millis=int(r.get("end_time_utc_millis") or 0) or None,
                    length_miles=length_mi,
                    url_prefix=url,
                    segment_numbers=seg_nums,
                    segment_start_times_ms=seg_starts,
                )
            )
        out.sort(key=lambda r: r.start_time_utc_millis or 0)
        return out

    @retry(attempts=2, min_delay=1.0, max_delay=5.0)
    def _fetch_coords_for_segment(self, segment_url: str) -> list[dict]:
        log.debug("GET %s", segment_url)
        resp = requests.get(
            segment_url,
            timeout=30,
            headers={"User-Agent": "comma-osm-speed/0.1"},
        )
        if resp.status_code == 404:
            return []
        resp.raise_for_status()
        try:
            data = resp.json()
        except ValueError:
            return []
        return data if isinstance(data, list) else []

    def fetch_route_coords(self, route: RouteSummary) -> list[GpsSample]:
        """Walk a route's segments, fetch each `<seg>/coords.json` from the CDN,
        and stitch into a unified GpsSample list with absolute timestamps."""
        if not route.url_prefix or not route.segment_numbers:
            return []

        cdn_prefix = _to_cdn_url(route.url_prefix)
        if not cdn_prefix:
            return []

        samples: list[GpsSample] = []
        # Build per-segment start times. If segment_start_times missing, fall
        # back to the route start + N*60 seconds (segments are 1 minute each).
        seg_count = len(route.segment_numbers)
        if len(route.segment_start_times_ms) >= seg_count:
            starts_ms = route.segment_start_times_ms
        else:
            base = route.start_time_utc_millis or 0
            starts_ms = [base + i * 60_000 for i in range(seg_count)]

        for idx, seg_n in enumerate(route.segment_numbers):
            seg_start_s = starts_ms[idx] / 1000.0 if idx < len(starts_ms) else 0.0
            seg_url = f"{cdn_prefix}/{seg_n}/coords.json"
            try:
                raw = self._fetch_coords_for_segment(seg_url)
            except Exception as exc:  # noqa: BLE001
                log.warning("coords.json fetch failed for %s seg %d: %s", route.route_name, seg_n, exc)
                continue
            for item in raw:
                if not isinstance(item, dict):
                    continue
                try:
                    t_off = float(item.get("t", 0))
                    lat = float(item["lat"])
                    lon = float(item.get("lng") or item.get("lon"))
                    spd = float(item.get("speed", 0.0) or 0.0)
                except (TypeError, ValueError, KeyError):
                    continue
                samples.append(
                    GpsSample(
                        t=seg_start_s + t_off,
                        lat=lat,
                        lon=lon,
                        speed_mps=spd,
                    )
                )
        log.info("Route %s: %d GPS samples across %d segments", route.route_name, len(samples), seg_count)
        return samples


def _to_cdn_url(blob_url: str) -> str:
    """Replace blob.core.windows.net with the CDN host (azureedge.net).

    If the URL is already on the CDN host, returns it unchanged. If it's on a
    completely different host, returns it unchanged too (defensive).
    """
    try:
        parsed = urlparse(blob_url)
    except Exception:
        return ""
    if parsed.netloc == _BLOB_HOST:
        return urlunparse(parsed._replace(netloc=_CDN_HOST))
    return blob_url


def _route_name_from_url(url: str) -> str:
    """Best-effort: derive '<dongle>|<routeid>' from a segment URL."""
    if not url:
        return ""
    parts = url.rstrip("/").split("/")
    if len(parts) < 2:
        return ""
    folder = parts[-1]
    rest = folder.split("_", 1)[1] if "_" in folder else folder
    dongle = ""
    for p in reversed(parts[:-1]):
        if len(p) == 16 and all(c in "0123456789abcdef" for c in p.lower()):
            dongle = p
            break
    return f"{dongle}|{rest}" if dongle else rest


# Kept for backwards compatibility with existing tests.
def _parse_coords_payload(data: Any, route_start_ms: int | None = None) -> list[GpsSample]:
    """Parse a coords.json payload into GpsSamples. Used by tests."""
    if not isinstance(data, list):
        return []
    samples: list[GpsSample] = []
    base_s = (route_start_ms or 0) / 1000.0
    for idx, item in enumerate(data):
        if isinstance(item, dict):
            lon = item.get("lng") or item.get("lon") or item.get("longitude")
            lat = item.get("lat") or item.get("latitude")
            t = item.get("t") or item.get("time") or item.get("timestamp")
            speed = item.get("speed") or item.get("speed_mps") or 0.0
            bearing = item.get("bearing") or item.get("heading")
            acc = item.get("accuracy") or item.get("accuracy_m")
        elif isinstance(item, (list, tuple)) and len(item) >= 3:
            a, b, c = item[0], item[1], item[2]
            extras = item[3:] if len(item) > 3 else ()
            try:
                a_f, b_f, c_f = float(a), float(b), float(c)
            except (TypeError, ValueError):
                continue
            if abs(c_f) > 1e9:
                lon, lat, t = a_f, b_f, c_f
            elif abs(a_f) > 1e9:
                t, lat, lon = a_f, b_f, c_f
            else:
                t = base_s + idx
                if abs(a_f) <= 90 and abs(b_f) <= 180:
                    lat, lon = a_f, b_f
                else:
                    lon, lat = a_f, b_f
            speed = float(extras[0]) if len(extras) > 0 else 0.0
            bearing = float(extras[1]) if len(extras) > 1 else None
            acc = float(extras[2]) if len(extras) > 2 else None
        else:
            continue
        try:
            t_sec = float(t)
            if t_sec > 1e12:
                t_sec /= 1000.0
            samples.append(
                GpsSample(
                    t=t_sec,
                    lat=float(lat),
                    lon=float(lon),
                    speed_mps=float(speed),
                    bearing_deg=float(bearing) if bearing is not None else None,
                    accuracy_m=float(acc) if acc is not None else None,
                )
            )
        except (TypeError, ValueError):
            continue
    return samples
