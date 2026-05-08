"""Volvo manufacturer module.

Ford-era Volvos (1999–2010) straddle the KWP2000/UDS boundary.
Runtime probe (0xF189) is more reliable than year-based guessing.

KWP2000 measuring block definitions here target the Volvo P2 platform
(S60/V70/S80/XC90, 1999–2007) with Bosch ME7 / Denso engine management.
Block IDs and byte layouts are based on Vadis/VIDA documentation and
community reverse engineering — verify against real hardware before use.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from Hudson.core.kwp2000 import KwpBlock, KwpField, KwpSession

if TYPE_CHECKING:
    pass

name = "Volvo"
DISCOVERY_STRATEGY = "probe"


# ── Decoder functions (standard J1979 formulas) ───────────────────────────────

def _byte_minus40(data: bytes) -> float | None:
    """1-byte value with -40 °C offset."""
    return float(data[0]) - 40.0 if len(data) >= 1 else None


def _byte_pct(data: bytes) -> float | None:
    """1-byte percentage: raw / 2.55 → %."""
    return data[0] / 2.55 if len(data) >= 1 else None


def _rpm_16bit(data: bytes) -> float | None:
    """2-byte RPM: (A*256 + B) / 4 → RPM."""
    return ((data[0] << 8 | data[1]) / 4.0) if len(data) >= 2 else None


def _byte_kmh(data: bytes) -> float | None:
    """1-byte vehicle speed in km/h."""
    return float(data[0]) if len(data) >= 1 else None


# ── KWP2000 block definitions (Volvo P2 / ME7) ───────────────────────────────

VOLVO_BLOCKS: dict[str, KwpBlock] = {
    # Block 0x01: primary engine data stream.
    # Byte layout (ME7/Denso, needs hardware verification):
    #   0-1  Engine RPM (uint16, /4)
    #   2    Coolant temperature (uint8, -40 °C)
    #   3    Intake air temperature (uint8, -40 °C)
    #   4    Throttle position (uint8, /2.55 %)
    #   5    Calculated engine load (uint8, /2.55 %)
    "ENGINE_DATA": KwpBlock(
        0x01,
        "Engine Data",
        (
            KwpField("rpm",         0, 2, "rpm", _rpm_16bit),
            KwpField("coolant_temp",2, 1, "°C",  _byte_minus40),
            KwpField("intake_temp", 3, 1, "°C",  _byte_minus40),
            KwpField("throttle",    4, 1, "%",   _byte_pct),
            KwpField("engine_load", 5, 1, "%",   _byte_pct),
        ),
    ),
}

# Standard gate attribute: init sequence checks for kwp_blocks to decide
# whether to attempt a KWP2000 K-line session for this manufacturer.
kwp_blocks = VOLVO_BLOCKS

# Mock payloads for VOLVO_BLOCKS — injected into KwpSession when is_mock is True.
# Values match the FakeConnection waveform-generator defaults for consistency:
#   RPM=1500 → 0x1770/4, coolant=85°C → raw 125, intake=35°C → raw 75,
#   throttle≈15% → raw 38, load≈30% → raw 77
MOCK_KWP_RESPONSES: dict[int, bytes] = {
    0x01: bytes([0x17, 0x70, 0x7D, 0x4B, 0x26, 0x4D]),
}

# Legacy alias for backward-compatibility with existing imports.
MOCK_VOLVO_KWP_RESPONSES = MOCK_KWP_RESPONSES


# ── Query helper ──────────────────────────────────────────────────────────────

async def read_kwp_block(
    session: KwpSession,
    block_key: str,
) -> dict[str, float | None] | None:
    """Read and parse a Volvo KWP2000 measuring block by name.

    Returns a dict of field_name → physical value, or None if the block
    key is unknown or the ECU didn't respond.  Individual field values are
    None if their byte slice was shorter than expected.
    """
    defn = VOLVO_BLOCKS.get(block_key)
    if defn is None:
        return None
    data = await session.query_block(defn.block_id)
    if data is None:
        return None
    return session.parse_block(defn, data)


# ── DTC lookup ────────────────────────────────────────────────────────────────

DTC_DESCRIPTIONS: dict[str, str] = {}


def lookup_dtc(code: str) -> str | None:
    return DTC_DESCRIPTIONS.get(code)
