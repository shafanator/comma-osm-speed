"""Redaction helpers for values that shouldn't end up in shared logs."""
from __future__ import annotations


def redact_route_name(name: str) -> str:
    """Mask the dongle ID in a ``<dongle>|<routeid>`` route name.

    Route names identify the device they came from, so they should not appear
    verbatim in logs a user might paste into a bug report. The route id half is
    kept intact - it is what makes a log line useful for debugging.
    """
    dongle, sep, rest = name.partition("|")
    return f"***{sep}{rest}" if sep else name
