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

    # ── VAG group ────────────────────────────────────────────────────────────
    # Volkswagen
    WmiEntry("WV1", "Hudson.manufacturers.vw_audi"),  # VW commercial vehicles
    WmiEntry("WV2", "Hudson.manufacturers.vw_audi"),  # VW Bus / T-series
    WmiEntry("WVW", "Hudson.manufacturers.vw_audi"),  # VW passenger cars (Germany)
    WmiEntry("WVG", "Hudson.manufacturers.vw_audi"),  # VW MPV / SUV (Touareg etc.)
    WmiEntry("VWV", "Hudson.manufacturers.vw_audi"),  # VW Spain
    WmiEntry("3VW", "Hudson.manufacturers.vw_audi"),  # VW Mexico
    WmiEntry("9BW", "Hudson.manufacturers.vw_audi"),  # VW Brazil
    WmiEntry("8AW", "Hudson.manufacturers.vw_audi"),  # VW Argentina
    WmiEntry("2V4", "Hudson.manufacturers.vw_audi"),  # VW Canada
    WmiEntry("2V8", "Hudson.manufacturers.vw_audi"),  # VW Canada
    # Audi
    WmiEntry("WAU", "Hudson.manufacturers.vw_audi"),  # Audi Germany
    WmiEntry("WA1", "Hudson.manufacturers.vw_audi"),  # Audi SUV (Q5/Q7/Q8)
    WmiEntry("WUA", "Hudson.manufacturers.vw_audi"),  # quattro GmbH
    WmiEntry("TRU", "Hudson.manufacturers.vw_audi"),  # Audi Hungary (A3/TT/Q3)
    WmiEntry("93U", "Hudson.manufacturers.vw_audi"),  # Audi Brazil
    WmiEntry("93V", "Hudson.manufacturers.vw_audi"),  # Audi Brazil
    # SEAT
    WmiEntry("VSS", "Hudson.manufacturers.vw_audi"),  # SEAT Spain
    # Škoda
    WmiEntry("TMB", "Hudson.manufacturers.vw_audi"),  # Škoda Czech Republic
    # Porsche
    WmiEntry("WP0", "Hudson.manufacturers.vw_audi"),  # Porsche passenger (911/Cayman/Boxster)
    WmiEntry("WP1", "Hudson.manufacturers.vw_audi"),  # Porsche SUV (Cayenne/Macan)
    # Bentley
    WmiEntry("SCA", "Hudson.manufacturers.vw_audi"),  # Bentley UK

    # ── BMW group ─────────────────────────────────────────────────────────────
    WmiEntry("WBA", "Hudson.manufacturers.bmw"),   # BMW AG passenger cars
    WmiEntry("WBS", "Hudson.manufacturers.bmw"),   # BMW M GmbH
    WmiEntry("WBW", "Hudson.manufacturers.bmw"),   # BMW (alternate)
    WmiEntry("WBY", "Hudson.manufacturers.bmw"),   # BMW i (electric)
    WmiEntry("WMW", "Hudson.manufacturers.bmw"),   # MINI

    # ── Mercedes-Benz / Daimler ───────────────────────────────────────────────
    WmiEntry("WDB", "Hudson.manufacturers.mercedes"),  # Mercedes-Benz passenger (classic)
    WmiEntry("WDC", "Hudson.manufacturers.mercedes"),  # Mercedes-Benz SUV (GLE/GLC/GLS)
    WmiEntry("WDD", "Hudson.manufacturers.mercedes"),  # Mercedes-Benz passenger (modern)
    WmiEntry("WDF", "Hudson.manufacturers.mercedes"),  # Mercedes-Benz commercial vehicles
    WmiEntry("WME", "Hudson.manufacturers.mercedes"),  # smart
    WmiEntry("WMX", "Hudson.manufacturers.mercedes"),  # Mercedes-AMG
    WmiEntry("VSA", "Hudson.manufacturers.mercedes"),  # Mercedes-Benz Spain

    # ── Toyota group ──────────────────────────────────────────────────────────
    # Japan-built
    WmiEntry("JT1", "Hudson.manufacturers.toyota"),  # Toyota passenger car
    WmiEntry("JT2", "Hudson.manufacturers.toyota"),  # Toyota passenger car
    WmiEntry("JT3", "Hudson.manufacturers.toyota"),  # Toyota MPV/SUV
    WmiEntry("JT4", "Hudson.manufacturers.toyota"),  # Toyota truck
    WmiEntry("JT5", "Hudson.manufacturers.toyota"),  # Toyota incomplete vehicle
    WmiEntry("JT6", "Hudson.manufacturers.toyota"),  # Lexus SUV
    WmiEntry("JT8", "Hudson.manufacturers.toyota"),  # Lexus passenger
    WmiEntry("JTH", "Hudson.manufacturers.toyota"),  # Lexus passenger
    WmiEntry("JTJ", "Hudson.manufacturers.toyota"),  # Lexus SUV
    WmiEntry("JTN", "Hudson.manufacturers.toyota"),  # Toyota (alternate Japan)
    WmiEntry("JT",  "Hudson.manufacturers.toyota"),  # Toyota Japan (2-char fallback)
    # North America-built
    WmiEntry("4T1", "Hudson.manufacturers.toyota"),  # Toyota USA passenger
    WmiEntry("4T3", "Hudson.manufacturers.toyota"),  # Toyota USA SUV
    WmiEntry("5TD", "Hudson.manufacturers.toyota"),  # Toyota USA minivan/SUV / Lexus SUV
    WmiEntry("5TF", "Hudson.manufacturers.toyota"),  # Toyota USA truck
    WmiEntry("2T1", "Hudson.manufacturers.toyota"),  # Toyota Canada
    WmiEntry("2T2", "Hudson.manufacturers.toyota"),  # Toyota Canada
    # Europe
    WmiEntry("VNK", "Hudson.manufacturers.toyota"),  # Toyota France
    WmiEntry("SB1", "Hudson.manufacturers.toyota"),  # Toyota UK

    # ── Volvo ─────────────────────────────────────────────────────────────────
    WmiEntry("YV1", "Hudson.manufacturers.volvo"),  # Volvo passenger cars
    WmiEntry("YV2", "Hudson.manufacturers.volvo"),  # Volvo buses / heavy trucks
    WmiEntry("YV3", "Hudson.manufacturers.volvo"),  # Volvo trucks
    WmiEntry("YV4", "Hudson.manufacturers.volvo"),  # Volvo XC / SUV range
    WmiEntry("4V1", "Hudson.manufacturers.volvo"),  # Volvo USA
    WmiEntry("4V2", "Hudson.manufacturers.volvo"),  # Volvo USA
    WmiEntry("4V3", "Hudson.manufacturers.volvo"),  # Volvo USA
    WmiEntry("4V4", "Hudson.manufacturers.volvo"),  # Volvo USA
    WmiEntry("4V5", "Hudson.manufacturers.volvo"),  # Volvo USA
    WmiEntry("4V6", "Hudson.manufacturers.volvo"),  # Volvo USA
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
