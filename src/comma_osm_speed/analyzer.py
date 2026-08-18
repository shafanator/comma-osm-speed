"""Aggregate per-way speed samples and identify candidate OSM edits.

Important note on epistemology
------------------------------
What people drive and what the legal speed limit is are not the same thing.
A way where you observed 85th-percentile speed of 65 mph but OSM says 55 mph
does NOT necessarily mean OSM is wrong — drivers routinely exceed posted
limits. Use this tool to *surface candidates for human verification*, then
check the actual posted signs (Mapillary, Street View, an in-person visit)
before submitting an edit. Auto-editing speed limits in OSM purely from
driving data will get reverted and may flag your account.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

import numpy as np

from .osm_client import WayTags, parse_maxspeed

log = logging.getLogger(__name__)


@dataclass
class WaySpeedStat:
    way_id: int
    sample_count: int
    p50_mph: float
    p85_mph: float
    p95_mph: float
    max_mph: float


@dataclass
class SpeedCandidate:
    """A way where observed speed differs meaningfully from the OSM tag."""

    way_id: int
    osm_version: int
    observed_p85_mph: float
    sample_count: int
    current_maxspeed_tag: str | None
    current_maxspeed_mph: float | None
    proposed_maxspeed_mph: float
    delta_mph: float
    highway_tag: str | None
    name_tag: str | None
    # Representative coordinate on the way (median of observed GPS samples).
    # Used to build deep links into Mapillary/Street View.
    center_lat: float | None = None
    center_lon: float | None = None
    # Short human-readable explanation of how proposed_maxspeed_mph was derived
    # (rounding + highway-type prior). Surfaced in the review UI so the human
    # can sanity-check the proposal. Empty string if not inferred.
    proposal_reason: str = ""

    def proposed_maxspeed_tag(self, units: str = "mph") -> str:
        """Format the proposed maxspeed as an OSM tag value."""
        rounded = _round_to_nearest_5(self.proposed_maxspeed_mph)
        if units == "mph":
            return f"{int(rounded)} mph"
        # kmh
        kmh = rounded * 1.609344
        kmh_rounded = _round_to_nearest_5(kmh)
        return f"{int(kmh_rounded)}"


def _round_to_nearest_5(x: float) -> float:
    return round(x / 5.0) * 5.0


@dataclass
class ProposedSpeed:
    """Result of inferring a likely posted limit from an observed speed.

    `value_mph` is what goes into a candidate's `proposed_maxspeed_mph` — already
    rounded to a realistic posted value. `reason` is a short human-readable
    explanation surfaced in the review UI so the reviewer can sanity-check it.
    """

    value_mph: float
    reason: str


# Typical upper bound on the posted limit for each OSM highway class (mph).
# Used as a *soft* prior: if the rounded observed speed lands above the typical
# ceiling for the class, we flag it in the reasoning rather than silently
# proposing an implausible limit. This is NOT a hard cap — drivers do exceed
# limits and ways are sometimes misclassified, so the human reviewer decides.
_HIGHWAY_TYPICAL_MAX_MPH: dict[str, float] = {
    "living_street": 15.0,
    "service": 25.0,
    "residential": 30.0,
    "unclassified": 45.0,
    "tertiary": 45.0,
    "tertiary_link": 45.0,
    "secondary": 55.0,
    "secondary_link": 55.0,
    "primary": 65.0,
    "primary_link": 65.0,
    "trunk": 70.0,
    "trunk_link": 70.0,
    "motorway": 80.0,
    "motorway_link": 80.0,
}


def infer_proposed_maxspeed(
    observed_p85_mph: float,
    highway_tag: str | None,
) -> ProposedSpeed:
    """Infer a likely posted speed limit from an observed 85th-percentile speed.

    Real posted limits are almost always multiples of 5 mph, so the observed
    free-flow speed (e.g. 47 mph) is rounded to the nearest 5 (45 mph) as a
    baseline. Highway type is then used as a soft prior: if the rounded value
    exceeds what's plausible for the road class (e.g. 45 mph on a
    `residential`), the reason flags it so the reviewer can check whether
    drivers are simply exceeding the limit or the way is misclassified.

    Neighbor-aware refinement (biasing the value toward adjacent ways' maxspeed
    when they agree) is a planned follow-up and intentionally not done here.
    """
    rounded = _round_to_nearest_5(observed_p85_mph)
    reason = (
        f"Observed 85th-percentile {observed_p85_mph:.0f} mph, "
        f"rounded to {int(rounded)} mph (posted limits are multiples of 5)."
    )
    typical_max = _HIGHWAY_TYPICAL_MAX_MPH.get(highway_tag or "")
    if typical_max is not None and rounded > typical_max:
        reason += (
            f" That's above the typical max ({int(typical_max)} mph) for "
            f"highway={highway_tag} — drivers may be exceeding the limit, or the "
            f"way may be misclassified. Verify the posted sign."
        )
    return ProposedSpeed(value_mph=rounded, reason=reason)


def summarize_way_speeds(
    per_way_samples: dict[int, list[float]],
    *,
    min_samples: int = 30,
    min_speed_mph_for_freeflow: float = 5.0,
) -> dict[int, WaySpeedStat]:
    """Compute percentile speed stats per way, after free-flow filtering.

    "Free-flow" filter: drop samples below `min_speed_mph_for_freeflow` mph so
    that idling at lights or sitting in stop-and-go doesn't drag the 85th
    percentile down. (Stopped time is not informative about the speed limit.)
    """
    out: dict[int, WaySpeedStat] = {}
    for way_id, samples in per_way_samples.items():
        moving = [s for s in samples if s >= min_speed_mph_for_freeflow]
        if len(moving) < min_samples:
            continue
        arr = np.asarray(moving, dtype=float)
        out[way_id] = WaySpeedStat(
            way_id=way_id,
            sample_count=int(arr.size),
            p50_mph=float(np.percentile(arr, 50)),
            p85_mph=float(np.percentile(arr, 85)),
            p95_mph=float(np.percentile(arr, 95)),
            max_mph=float(arr.max()),
        )
    return out


def find_candidates(
    stats: dict[int, WaySpeedStat],
    way_tags: dict[int, WayTags],
    *,
    threshold_mph: float = 10.0,
    default_units: str = "kmh",
    way_centers: dict[int, tuple[float, float]] | None = None,
) -> list[SpeedCandidate]:
    """Compare observed 85th-percentile speeds against current OSM maxspeed.

    Emits a candidate per way where |p85 - current_maxspeed| > threshold_mph.
    Ways with no maxspeed tag are emitted as candidates too (proposed = p85).
    """
    way_centers = way_centers or {}
    candidates: list[SpeedCandidate] = []
    for way_id, stat in stats.items():
        tags = way_tags.get(way_id)
        if tags is None:
            log.debug("No OSM tags fetched for way %d — skipping", way_id)
            continue

        # Only consider drivable ways
        hwy = tags.tags.get("highway")
        if hwy in {None, "footway", "path", "cycleway", "pedestrian", "steps", "track"}:
            continue

        ms_tag = tags.tags.get("maxspeed")
        current_mph = parse_maxspeed(ms_tag, default_units=default_units)
        center = way_centers.get(way_id)
        c_lat = center[0] if center else None
        c_lon = center[1] if center else None

        if current_mph is None:
            # Missing or non-numeric maxspeed: emit candidate if we have a
            # reasonable observed speed.
            if stat.p85_mph >= 15.0:
                proposal = infer_proposed_maxspeed(stat.p85_mph, hwy)
                candidates.append(
                    SpeedCandidate(
                        way_id=way_id,
                        osm_version=tags.version,
                        observed_p85_mph=stat.p85_mph,
                        sample_count=stat.sample_count,
                        current_maxspeed_tag=ms_tag,
                        current_maxspeed_mph=None,
                        proposed_maxspeed_mph=proposal.value_mph,
                        delta_mph=float("inf"),
                        highway_tag=hwy,
                        name_tag=tags.tags.get("name"),
                        center_lat=c_lat,
                        center_lon=c_lon,
                        proposal_reason=proposal.reason,
                    )
                )
            continue

        delta = stat.p85_mph - current_mph
        if abs(delta) >= threshold_mph:
            proposal = infer_proposed_maxspeed(stat.p85_mph, hwy)
            candidates.append(
                SpeedCandidate(
                    way_id=way_id,
                    osm_version=tags.version,
                    observed_p85_mph=stat.p85_mph,
                    sample_count=stat.sample_count,
                    current_maxspeed_tag=ms_tag,
                    current_maxspeed_mph=current_mph,
                    proposed_maxspeed_mph=proposal.value_mph,
                    delta_mph=delta,
                    highway_tag=hwy,
                    name_tag=tags.tags.get("name"),
                    center_lat=c_lat,
                    center_lon=c_lon,
                    proposal_reason=proposal.reason,
                )
            )
    return candidates
