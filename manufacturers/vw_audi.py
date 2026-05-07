"""VW/Audi (VAG) manufacturer-specific decoders.

Sources:
  - Ross-Tech Wiki: https://wiki.ross-tech.com/
  - VAG K+CAN sniffed protocol notes
  - VCDS measuring block IDs

This module is a translation table from Ross-Tech-documented identifiers
to human-readable descriptions, plus UDS mode-22 PID definitions for things
the standard J1979 mode 01 cannot reach (turbo boost, EGT, DPF state, etc).

CURRENT STATUS: skeleton only. Populating this is mostly grunt work against
Ross-Tech docs — best done with the actual T5 in front of us so we can verify
which identifiers the specific engine code (BNZ/AXB/AXC/etc) actually responds
to.
"""

from __future__ import annotations

name = "VW/Audi"


# Mode 22 ReadDataByIdentifier table.
# Format: identifier (2 bytes) -> (description, decoder_function).
# To be populated.
UDS_DATA_IDENTIFIERS: dict[int, tuple[str, object]] = {
    # 0xF40B: ("Boost pressure (actual)", _decode_boost),
    # 0xF40C: ("Boost pressure (specified)", _decode_boost),
    # ...
}


# Manufacturer-specific DTC descriptions (P1xxx and P3xxx primarily).
# Ross-Tech maintains the canonical list.
DTC_DESCRIPTIONS: dict[str, str] = {
    # "P1296": "Cooling system malfunction",
    # "P1402": "EGR system: fault in vacuum supply",
    # ...
}


def lookup_dtc(code: str) -> str | None:
    return DTC_DESCRIPTIONS.get(code)
