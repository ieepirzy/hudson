"""Volvo manufacturer module.

Ford-era Volvos (1999–2010) sit on the KWP2000/UDS boundary.
2006 and earlier are likely KWP2000, 2007+ likely UDS, but runtime
probe is more reliable than year-based guessing.
"""

from __future__ import annotations

name = "Volvo"
DISCOVERY_STRATEGY = "probe"

DTC_DESCRIPTIONS: dict[str, str] = {}


def lookup_dtc(code: str) -> str | None:
    return DTC_DESCRIPTIONS.get(code)
