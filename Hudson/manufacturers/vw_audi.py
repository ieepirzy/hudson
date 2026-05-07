"""VW/Audi (VAG) manufacturer-specific decoders.

Sources:
  - Ross-Tech Wiki: https://wiki.ross-tech.com/
  - VAG K+CAN sniffed protocol notes
  - VCDS measuring block IDs

This module is a translation table from Ross-Tech-documented identifiers
to human-readable descriptions, plus UDS mode-22 PID definitions for things
the standard J1979 mode 01 cannot reach (turbo boost, EGT, DPF state, etc).
"""

from __future__ import annotations

name = "VW/Audi"

# post-2008 VAG uses UDS (ISO 14229 / AMV standard)
DISCOVERY_STRATEGY = "uds"


# Mode 22 ReadDataByIdentifier table.
# Format: identifier (2 bytes) -> (description, decoder_function).
# Read-only (0x22) only — writes (0x2E), I/O control (0x2F), and
# routine control (0x31) are intentionally not implemented.
UDS_DATA_IDENTIFIERS: dict[int, tuple[str, object]] = {
    # 0xF40B: ("Boost pressure (actual)", _decode_boost),
    # 0xF40C: ("Boost pressure (specified)", _decode_boost),
}


# Manufacturer-specific DTC descriptions.
# P1xxx / P3xxx = VAG-defined powertrain.
# Source: Ross-Tech VCDS DTC database (public wiki entries).
DTC_DESCRIPTIONS: dict[str, str] = {
    # ── Air / MAF ────────────────────────────────────────────────────────────
    "P1100": "MAF sensor — signal outside specified range",
    "P1101": "MAF sensor — short to ground",
    "P1102": "MAF sensor — short to battery positive",
    "P1103": "MAF sensor — implausible signal",
    # ── O2 / Lambda ──────────────────────────────────────────────────────────
    "P1111": "O2 sensor heater B1 S1 — open circuit",
    "P1115": "O2 sensor heater B1 S2 — open circuit",
    "P1127": "Long-term fuel trim additive, bank 1 — limit reached",
    "P1128": "Long-term fuel trim multiplicative, bank 1 — limit reached",
    "P1130": "Long-term fuel trim additive air, bank 1 — limit reached",
    "P1136": "Long-term fuel trim additive air, bank 2 — limit reached",
    "P1176": "O2 correction behind cat, bank 1 — limit reached",
    "P1177": "O2 correction behind cat, bank 2 — limit reached",
    "P1196": "O2 sensor B1 S1 — slow response",
    "P1197": "O2 sensor B2 S1 — slow response",
    # ── Fuel pressure / injection ─────────────────────────────────────────────
    "P1188": "Fuel pressure regulator — activation limit reached",
    "P1189": "Fuel pressure regulation during start — malfunction",
    # ── Knock ─────────────────────────────────────────────────────────────────
    "P1321": "Knock sensor 3 — signal too low (bank 2)",
    "P1325": "Cylinder 1 — knock control limit reached",
    "P1326": "Cylinder 2 — knock control limit reached",
    "P1327": "Cylinder 3 — knock control limit reached",
    "P1328": "Cylinder 4 — knock control limit reached",
    "P1329": "Cylinder 5 — knock control limit reached",
    "P1334": "Cylinder 6 — knock control limit reached",
    "P1386": "Internal control module — knock control circuit error",
    # ── Cam / crank ───────────────────────────────────────────────────────────
    "P1340": "Crankshaft/camshaft position sensor — signals out of sequence",
    "P1390": "Camshaft position sensor G40 — no signal",
    # ── Cooling ───────────────────────────────────────────────────────────────
    "P1296": "Cooling system — malfunction",
    "P3081": "Engine temperature too low, bank 1",
    "P3082": "Engine temperature too low, bank 2",
    # ── Boost / charge pressure ───────────────────────────────────────────────
    "P1297": "Boost pressure control valve N75 — open circuit",
    "P1546": "Boost pressure control valve — open/short to ground",
    "P1548": "Boost pressure control — deviation too high",
    "P1555": "Charge pressure control — positive deviation",
    "P1556": "Charge pressure control — negative deviation",
    "P1557": "Charge pressure control — limit value reached",
    # ── EGR ───────────────────────────────────────────────────────────────────
    "P1402": "EGR vacuum regulator solenoid — open/short to ground",
    "P1403": "EGR solenoid — short to battery positive",
    # ── Secondary air injection ───────────────────────────────────────────────
    "P1421": "Secondary air injection — switchover valve short to ground",
    "P1422": "Secondary air injection — short to battery positive",
    # ── Evap / tank ventilation ───────────────────────────────────────────────
    "P1425": "Tank ventilation valve — short to ground",
    "P1426": "Tank ventilation valve — short to battery positive",
    "P1491": "EVAP purge solenoid — open circuit",
    "P1543": "EVAP emission system — open/short to ground",
    # ── Throttle / drive-by-wire ──────────────────────────────────────────────
    "P1545": "Throttle position control — malfunction",
    "P1551": "Barometric pressure sensor — supply voltage",
    "P1558": "Throttle actuator — electrical malfunction",
    "P1559": "Throttle actuator — power stage not ready",
    "P1580": "Throttle actuator B1 — stiff/tight",
    "P1581": "Throttle actuator B1 — malfunction",
    "P1582": "Idle adaptation at limit",
    "P1584": "ECM drive-by-wire — adaptation malfunction",
    "P1585": "Throttle actuator — internal temperature too high",
    "P1676": "Drive-by-wire MIL circuit — open",
    "P1677": "Drive-by-wire MIL circuit — short to ground",
    "P1686": "Drive-by-wire — no communication between ECMs",
    # ── ECM power / internal ──────────────────────────────────────────────────
    "P1600": "ECM supply voltage B+ terminal 15 — low voltage",
    "P1602": "Power supply B+ terminal 30 — low voltage",
    "P1603": "Internal control module EEPROM error",
    "P1604": "Internal control module — software error",
    "P1605": "Rough road/acceleration sensor — electrical malfunction",
    "P1609": "Crash shut-off activated",
    "P1611": "MIL call-up circuit / transmission control module",
    "P1626": "CAN — incorrect signal from transmission control module",
    "P1640": "Internal control module — EEPROM error",
    "P1690": "MIL — open circuit",
    "P1693": "MIL — short to battery positive",
    "P1780": "Engine intervention readiness signal — not plausible",
}


def lookup_dtc(code: str) -> str | None:
    return DTC_DESCRIPTIONS.get(code)
