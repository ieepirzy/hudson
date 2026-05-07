"""Generic fallback decoder when no manufacturer-specific module matches.

Returns None for everything; the caller falls back to SAE J1979 / J2012 lookups.
"""

from __future__ import annotations

name = "Generic"


def lookup_dtc(code: str) -> str | None:
    return None
