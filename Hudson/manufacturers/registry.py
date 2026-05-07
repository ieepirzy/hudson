"""VIN-based manufacturer decoder selection.

The first 3 characters of a VIN are the World Manufacturer Identifier (WMI),
assigned by SAE. We use them to dispatch to the right manufacturer module
for extended PIDs, DTC lookups, and any other vendor-specific decoding.

This is a partial table — we only register WMIs we actively support.
A miss falls back to `generic.py`.

Reference: https://en.wikipedia.org/wiki/World_Manufacturer_Identifier
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


class ManufacturerDecoder(Protocol):
    """Interface every manufacturer module must implement."""

    name: str

    def lookup_dtc(self, code: str) -> str | None:
        """Return human-readable description for a manufacturer-specific DTC, or None."""
        ...


@dataclass(frozen=True, slots=True)
class WmiEntry:
    wmi_prefix: str  # 1-3 chars; longer = more specific match wins
    decoder_module: str  # importable module path


# Ordered most-specific first; first match wins.
_REGISTRY: list[WmiEntry] = [
    # VW T5 family (commercial vans built in Hannover)
    WmiEntry("WV1", "Hudson.manufacturers.vw_audi"),  # VW commercial
    WmiEntry("WV2", "Hudson.manufacturers.vw_audi"),  # VW Bus/T-series
    WmiEntry("WVW", "Hudson.manufacturers.vw_audi"),  # VW passenger
    WmiEntry("WAU", "Hudson.manufacturers.vw_audi"),  # Audi Germany
    # Toyota
    WmiEntry("JT", "Hudson.manufacturers.toyota"),
    WmiEntry("VNK", "Hudson.manufacturers.toyota"),  # Toyota France
    # Volvo (Ford-era 1999–2010)
    WmiEntry("YV1", "Hudson.manufacturers.volvo"),  # Volvo passenger
    WmiEntry("YV2", "Hudson.manufacturers.volvo"),  # Volvo bus/truck
    WmiEntry("YV3", "Hudson.manufacturers.volvo"),  # Volvo truck
]


def select_decoder(vin: str) -> str:
    """Return the importable module path for the decoder matching this VIN.

    Falls back to the generic decoder if no WMI matches.
    """
    if len(vin) < 3:
        return "Hudson.manufacturers.generic"

    vin_upper = vin.upper()
    for entry in _REGISTRY:
        if vin_upper.startswith(entry.wmi_prefix):
            return entry.decoder_module
    return "Hudson.manufacturers.generic"
