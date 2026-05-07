"""Toyota manufacturer-specific decoders.

CURRENT STATUS: skeleton only. Toyota's enhanced PIDs are less openly
documented than VAG's; we may end up sticking close to standard J1979
plus a thin DTC description table.
"""

from __future__ import annotations

name = "Toyota"

DTC_DESCRIPTIONS: dict[str, str] = {}


def lookup_dtc(code: str) -> str | None:
    return DTC_DESCRIPTIONS.get(code)
