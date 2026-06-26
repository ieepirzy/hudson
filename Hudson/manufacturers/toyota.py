"""Toyota manufacturer-specific decoders.

Sources:
  - Toyota/Lexus TEMS technical service bulletins
  - iATN DTC database (public entries)
  - Techstream enhanced data stream parameter IDs (community-verified subset)

Enhanced PID notes:
  Mode 0x22 identifiers in the 0x01xx range shadow the corresponding mode 01
  PID byte (e.g. 0x0105 = mode 01 PID 0x05 = coolant temp), accessed via UDS
  ReadDataByIdentifier on newer Toyota ECUs (2007+ CAN).

  Mode 0x21 local identifiers (ReadDataByLocalIdentifier) are used on older
  Toyota ECUs. The block layouts here are based on community documentation and
  need field verification against real hardware.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from Hudson.core.connection import ObdConnection

name = "Toyota"
DISCOVERY_STRATEGY = "probe"


# ── Decoder functions (mode 0x22 and 0x21 shared) ────────────────────────────

def _byte_minus40(data: bytes) -> float | None:
    """1-byte value with -40 °C offset (same encoding as mode 01 PID 05/0F)."""
    return float(data[0]) - 40.0 if len(data) >= 1 else None


def _byte_pct(data: bytes) -> float | None:
    """1-byte percentage: raw / 2.55 → % (same as mode 01 load/throttle)."""
    return data[0] / 2.55 if len(data) >= 1 else None


def _rpm_16bit(data: bytes) -> float | None:
    """2-byte RPM: (A*256 + B) / 4 → RPM (same as mode 01 PID 0C)."""
    return ((data[0] << 8 | data[1]) / 4.0) if len(data) >= 2 else None


def _byte_kmh(data: bytes) -> float | None:
    """1-byte vehicle speed in km/h (same as mode 01 PID 0D)."""
    return float(data[0]) if len(data) >= 1 else None


# ── Mode 0x22 enhanced PID definitions ───────────────────────────────────────

@dataclass(frozen=True, slots=True)
class EnhancedPid:
    """A single Toyota UDS (service 0x22) enhanced parameter definition."""

    identifier: int                          # 2-byte UDS data identifier
    name: str
    unit: str
    byte_count: int                          # minimum expected response bytes
    decode: Callable[[bytes], float | None]  # raw data bytes → physical value


# Identifiers are in the 0x01xx range, mirroring standard mode 01 PID numbers.
# Field-verified against 2AZ-FE (2006–2011 Camry/RAV4) community captures.
# Other engine families may differ — treat as a starting point, not ground truth.
ENHANCED_PIDS: dict[str, EnhancedPid] = {
    "COOLANT_TEMP": EnhancedPid(0x0105, "Coolant Temp",      "°C",  1, _byte_minus40),
    "INTAKE_TEMP":  EnhancedPid(0x010F, "Intake Air Temp",   "°C",  1, _byte_minus40),
    "ENGINE_LOAD":  EnhancedPid(0x0110, "Calculated Load",   "%",   1, _byte_pct),
    "RPM":          EnhancedPid(0x010C, "Engine RPM",        "rpm", 2, _rpm_16bit),
    "SPEED":        EnhancedPid(0x010D, "Vehicle Speed",     "km/h",1, _byte_kmh),
    "THROTTLE":     EnhancedPid(0x0111, "Throttle Position", "%",   1, _byte_pct),
}


# ── Mode 0x21 enhanced block definitions ─────────────────────────────────────

@dataclass(frozen=True, slots=True)
class BlockField:
    """One named field within a mode 0x21 data block."""

    name: str
    offset: int                              # byte offset within block data
    count: int                               # number of bytes
    unit: str
    decode: Callable[[bytes], float | None]


@dataclass(frozen=True, slots=True)
class EnhancedBlock:
    """A Toyota mode 0x21 ReadDataByLocalIdentifier block definition."""

    local_id: int          # 1-byte local identifier
    name: str
    fields: tuple[BlockField, ...]


# Block 0x10 is common on 1NZ/2AZ/2ZZ family ECUs. Byte layout needs
# hardware verification — offsets are based on community disassembly notes.
ENHANCED_BLOCKS: dict[str, EnhancedBlock] = {
    "ENGINE_DATA": EnhancedBlock(
        0x10,
        "Engine Data Block",
        (
            BlockField("rpm",         0, 2, "rpm",  _rpm_16bit),
            BlockField("coolant_temp",2, 1, "°C",   _byte_minus40),
            BlockField("intake_temp", 3, 1, "°C",   _byte_minus40),
            BlockField("throttle",    4, 1, "%",    _byte_pct),
            BlockField("engine_load", 5, 1, "%",    _byte_pct),
        ),
    ),
}


# ── Query helpers ─────────────────────────────────────────────────────────────

async def read_enhanced_pid(connection: ObdConnection, pid_key: str) -> float | None:
    """Query a single Toyota enhanced PID via mode 0x22.

    Returns the decoded physical value, or None if the identifier is unknown,
    the ECU didn't respond, or the response was too short.
    """
    pid = ENHANCED_PIDS.get(pid_key)
    if pid is None:
        return None
    response = await connection.query_uds_at_addr(0x7E0, pid.identifier)
    if len(response.data) < pid.byte_count:
        return None
    return pid.decode(response.data)


async def read_enhanced_block(
    connection: ObdConnection,
    block_key: str,
) -> dict[str, float | None] | None:
    """Query a Toyota mode 0x21 data block and parse all defined fields.

    Returns a dict of field_name → physical value, or None if the block key is
    unknown or the ECU didn't respond.  Individual field values are None if
    their byte slice was shorter than expected (truncated response).
    """
    block = ENHANCED_BLOCKS.get(block_key)
    if block is None:
        return None
    data = await connection.query_enhanced_local(block.local_id)
    if data is None:
        return None
    result: dict[str, float | None] = {}
    for field in block.fields:
        chunk = data[field.offset : field.offset + field.count]
        result[field.name] = field.decode(chunk) if len(chunk) == field.count else None
    return result


# ── DTC lookup ────────────────────────────────────────────────────────────────

DTC_DESCRIPTIONS: dict[str, str] = {
    # ── Ignition / coils ──────────────────────────────────────────────────────
    "P1300": "Igniter circuit — No.1",
    "P1305": "Igniter circuit — No.2",
    "P1310": "Igniter circuit — No.3",
    "P1315": "Igniter circuit — No.4",
    "P1320": "Igniter circuit — No.5",
    "P1325": "Igniter circuit — No.6",
    "P1330": "Igniter circuit — No.7",
    "P1335": "Crankshaft position sensor — no signal during cranking",
    "P1340": "Camshaft position sensor — signal mismatch",
    # ── VVT-i ─────────────────────────────────────────────────────────────────
    "P1349": "Variable valve timing — malfunction",
    "P1354": "Variable valve timing — bank 2 malfunction",
    # ── Throttle (ETCS) ───────────────────────────────────────────────────────
    "P1400": "Sub-throttle position sensor — malfunction",
    "P1401": "Sub-throttle position sensor — range/performance",
    "P1633": "ECM malfunction — ETCS",
    # ── Starting / charging ───────────────────────────────────────────────────
    "P1500": "Starter signal circuit — malfunction",
    "P1600": "ECM battery backup — circuit malfunction",
    "P1605": "ECM — rough road data error",
    # ── Cruise / brakes ───────────────────────────────────────────────────────
    "P1520": "Stop lamp switch signal — malfunction",
    "P1565": "Cruise control main switch — malfunction",
    # ── Network / communication ───────────────────────────────────────────────
    "P1645": "Body ECU — malfunction",
}


def lookup_dtc(code: str) -> str | None:
    return DTC_DESCRIPTIONS.get(code)
