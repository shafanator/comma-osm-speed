"""Read-side OSM client: Overpass for current tags, OSM API for fetching ways.

The submitter (osm_submitter.py) handles write operations separately.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any
from xml.etree import ElementTree as ET

import requests

from ._retry import retry

log = logging.getLogger(__name__)

# OSM maxspeed values can look like: "30", "30 mph", "50 km/h", "RU:urban", "none", "walk"
_NUMERIC_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(mph|km/h|kmh|kph)?\s*$", re.IGNORECASE)


@dataclass
class WayTags:
    way_id: int
    version: int
    tags: dict[str, str]


def parse_maxspeed(value: str | None, default_units: str = "kmh") -> float | None:
    """Parse an OSM maxspeed tag to mph.

    Returns None for things like 'none', 'signals', country defaults, or
    anything we can't interpret as a number. OSM convention: bare number
    means km/h, except in countries where mph is the default.
    """
    if not value:
        return None
    m = _NUMERIC_RE.match(value)
    if not m:
        return None
    n = float(m.group(1))
    unit = (m.group(2) or "").lower()
    if unit == "mph":
        return n
    if unit in ("km/h", "kmh", "kph"):
        return n * 0.621371
    # Bare number — honor country default.
    if default_units == "mph":
        return n
    return n * 0.621371


class OverpassClient:
    def __init__(self, url: str = "https://overpass-api.de/api/interpreter"):
        self.url = url
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": "comma-osm-speed/0.1"})

    @retry(attempts=3, min_delay=2.0, max_delay=30.0)
    def query(self, ql: str) -> dict[str, Any]:
        log.debug("Overpass QL: %s", ql)
        resp = self._session.post(self.url, data={"data": ql}, timeout=120)
        resp.raise_for_status()
        return resp.json()

    def get_ways(self, way_ids: list[int]) -> dict[int, WayTags]:
        """Fetch current tags + version for a batch of way IDs."""
        if not way_ids:
            return {}
        # De-dup and batch in chunks of 200 to keep query size sane.
        out: dict[int, WayTags] = {}
        unique = sorted(set(way_ids))
        for i in range(0, len(unique), 200):
            chunk = unique[i : i + 200]
            ids = ",".join(str(w) for w in chunk)
            ql = f"""[out:json][timeout:60];
way(id:{ids});
out tags meta;"""
            data = self.query(ql)
            for el in data.get("elements", []):
                if el.get("type") != "way":
                    continue
                wid = int(el["id"])
                out[wid] = WayTags(
                    way_id=wid,
                    version=int(el.get("version", 0)),
                    tags={str(k): str(v) for k, v in (el.get("tags") or {}).items()},
                )
        return out


def fetch_way_xml(way_id: int, osm_api_base: str) -> ET.Element:
    """Fetch a way as raw OSM XML (needed for changeset modification).

    Uses the public OSM API (no auth required for reads).
    """
    url = f"{osm_api_base.rstrip('/')}/api/0.6/way/{way_id}"
    resp = requests.get(
        url,
        headers={"User-Agent": "comma-osm-speed/0.1", "Accept": "application/xml"},
        timeout=30,
    )
    resp.raise_for_status()
    root = ET.fromstring(resp.text)
    way = root.find("way")
    if way is None:
        raise RuntimeError(f"OSM API returned no <way> for id {way_id}")
    return way
