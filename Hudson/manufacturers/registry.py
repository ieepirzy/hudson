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
    WmiEntry("WV1", "hudson.manufacturers.vw_audi"),  # VW commercial
    WmiEntry("WV2", "hudson.manufacturers.vw_audi"),  # VW Bus/T-series
    WmiEntry("WVW", "hudson.manufacturers.vw_audi"),  # VW passenger
    WmiEntry("WAU", "hudson.manufacturers.vw_audi"),  # Audi Germany
    # Toyota
    WmiEntry("JT", "hudson.manufacturers.toyota"),
    WmiEntry("VNK", "hudson.manufacturers.toyota"),  # Toyota France
]


def select_decoder(vin: str) -> str:
    """Return the importable module path for the decoder matching this VIN.

    Falls back to the generic decoder if no WMI matches.
    """
    if len(vin) < 3:
        return "hudson.manufacturers.generic"

    vin_upper = vin.upper()
    for entry in _REGISTRY:
        if vin_upper.startswith(entry.wmi_prefix):
            return entry.decoder_module
    return "hudson.manufacturers.generic"
