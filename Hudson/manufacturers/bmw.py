"""BMW / MINI manufacturer-specific decoders.

Covers BMW AG (WBA, WBS, WBW, WBY) and MINI (WMW).

Sources:
  - BMW ISTA/ISID workshop diagnostic documentation (community captures)
  - Bimmerpost / E90Post DTC database
  - E-series (E60/E90/E70) and F-series (F30/F10/F25) diagnostic session logs

UDS (ISO 14229 / SID 0x22) is used by all post-2006 BMW and MINI platforms.
Pre-2006 E-series (E46/E39) may negotiate KWP2000 over K-line; those
platforms are rare enough for OBD-II purposes that the UDS path is tried first.
"""

from __future__ import annotations

name = "BMW/MINI"

# Post-2006 BMW and MINI use UDS (ISO 14229, SID 0x22)
DISCOVERY_STRATEGY = "uds"

# Mode 22 ReadDataByIdentifier table.
# Known F/G-series identifiers — decoder implementations pending hardware capture.
UDS_DATA_IDENTIFIERS: dict[int, tuple[str, object]] = {
    # 0xD001: ("Engine speed",               _decode_rpm),      # available on most DME/DDE
    # 0xD003: ("Charge air pressure",        _decode_pressure), # turbocharged petrol/diesel
    # 0xD014: ("Engine coolant temperature", _decode_temp),
    # 0xD036: ("Lambda sensor B1 S1",        _decode_lambda),
}

# Manufacturer-specific DTC descriptions.
# P1xxx = BMW-defined powertrain / drivetrain codes.
# Sources: BMW ISTA DTC database (public workshop documentation extracts).
DTC_DESCRIPTIONS: dict[str, str] = {
    # ── VANOS (variable valve timing) ────────────────────────────────────────────
    "P1519": "VANOS inlet camshaft — solenoid valve open circuit",
    "P1520": "VANOS inlet camshaft — solenoid valve short to ground",
    "P1521": "VANOS inlet camshaft — solenoid valve short to battery positive",
    "P1522": "VANOS exhaust camshaft — solenoid valve open circuit",
    "P1523": "VANOS exhaust camshaft — solenoid valve short to ground",
    "P1524": "VANOS exhaust camshaft — solenoid valve short to battery positive",
    "P1525": "VANOS inlet camshaft — end stop not reached (mechanical fault)",
    "P1526": "VANOS exhaust camshaft — end stop not reached (mechanical fault)",
    # ── Valvetronic / electronic throttle ───────────────────────────────────────────
    "P1545": "Electronic throttle valve — positioning error",
    "P1546": "Electronic throttle valve — control deviation exceeds limit",
    "P1547": "Electronic throttle valve — limp-home position not reached",
    # ── Secondary air injection ────────────────────────────────────────────────────
    "P1420": "Secondary air injection bank 1 — system flow insufficient",
    "P1421": "Secondary air injection bank 1 — check valve malfunction",
    "P1422": "Secondary air injection bank 2 — system flow insufficient",
    "P1423": "Secondary air injection bank 2 — check valve malfunction",
    "P1425": "Secondary air injection — pump relay circuit open",
    "P1426": "Secondary air injection — switchover valve circuit malfunction",
    # ── Fuel high-pressure system ───────────────────────────────────────────────────
    "P1093": "High-pressure fuel system — pressure too low during load enrichment",
    "P1094": "High-pressure fuel system — pressure deviation on deceleration",
    "P1095": "Fuel pressure regulator — activation limit reached",
    # ── Petrol injectors ──────────────────────────────────────────────────────────
    "P1213": "Injector cylinder 1 — short circuit",
    "P1214": "Injector cylinder 2 — short circuit",
    "P1215": "Injector cylinder 3 — short circuit",
    "P1216": "Injector cylinder 4 — short circuit",
    "P1217": "Injector cylinder 5 — short circuit",
    "P1218": "Injector cylinder 6 — short circuit",
    # ── Lambda / fuel trim ──────────────────────────────────────────────────────────────
    "P1127": "Fuel trim adaptation bank 1 — lean limit reached (additive)",
    "P1128": "Fuel trim adaptation bank 1 — rich limit reached (additive)",
    "P1129": "Fuel trim adaptation bank 2 — lean limit reached (additive)",
    "P1130": "Fuel trim adaptation bank 2 — rich limit reached (additive)",
    "P1176": "Post-catalyst O2 correction bank 1 — limit reached",
    "P1177": "Post-catalyst O2 correction bank 2 — limit reached",
    # ── Cooling ──────────────────────────────────────────────────────────────────────────
    "P1291": "Map-controlled thermostat — stuck open (insufficient warm-up)",
    "P1292": "Map-controlled thermostat — stuck closed (overheating risk)",
    "P1293": "Oil condition / level sensor — signal implausible",
    # ── EVAP / tank ventilation ────────────────────────────────────────────────────────
    "P1613": "Evap leak detection pump — circuit malfunction",
    "P1614": "Evap leak detection pump — measurement outside reference limits",
    "P1615": "Tank ventilation valve — open circuit",
    "P1616": "Tank ventilation valve — short to ground",
    "P1617": "Tank ventilation valve — short to battery positive",
    # ── EGR (diesel N47 / N57 / B47 / B57) ───────────────────────────────────────────
    "P1402": "EGR valve — open circuit",
    "P1403": "EGR valve — short to ground",
    "P1404": "EGR valve — short to battery positive",
    "P1406": "EGR position feedback — signal implausible",
    # ── DME / DDE power supply and internal ───────────────────────────────────────────
    "P1600": "DME/DDE — supply voltage low at terminal 30 (KL30)",
    "P1601": "DME/DDE — supply voltage low at terminal 15 (KL15)",
    "P1603": "DME/DDE — internal EEPROM checksum error",
    "P1605": "DME/DDE — knock control circuit malfunction",
    "P1686": "CAN — no communication with transmission control module (EGS)",
    "P1691": "MIL (malfunction indicator lamp) — open circuit",
    "P1693": "MIL (malfunction indicator lamp) — short to battery positive",
}


def lookup_dtc(code: str) -> str | None:
    return DTC_DESCRIPTIONS.get(code)
