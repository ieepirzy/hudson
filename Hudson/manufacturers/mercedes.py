"""Mercedes-Benz / smart manufacturer-specific decoders.

Covers Mercedes-Benz (WDB, WDC, WDD, WDF, WMX, VSA) and smart (WME).

Sources:
  - Mercedes-Benz XENTRY / DAS diagnostic system documentation
  - Star Diagnosis community database
  - W204/W212/W211 diagnostic captures (M271/M272/OM651/OM642 engines)

UDS (ISO 14229 / SID 0x22) applies to all post-2008 Mercedes platforms
(W204 C-class onwards). Earlier platforms (W211 pre-facelift, W203) use
KWP2000 in mixed mode; the UDS path is always attempted first.
"""

from __future__ import annotations

name = "Mercedes-Benz/smart"

# Post-2008 MB uses UDS (ISO 14229, SID 0x22)
DISCOVERY_STRATEGY = "uds"

# Mode 22 ReadDataByIdentifier table.
# Known ME/CDI identifiers — decoder implementations pending hardware capture.
UDS_DATA_IDENTIFIERS: dict[int, tuple[str, object]] = {
    # 0xF400: ("Engine speed",           _decode_rpm),   # W204/W212 ME/CDI
    # 0xF401: ("Vehicle speed",          _decode_speed),
    # 0xF407: ("Coolant temperature",    _decode_temp),
    # 0xF409: ("Throttle position",      _decode_pct),
    # 0xF40C: ("Calculated engine load", _decode_pct),
}

# Manufacturer-specific DTC descriptions.
# P1xxx = Mercedes-Benz-defined powertrain codes.
# Sources: Mercedes-Benz XENTRY DTC database (public workshop documentation).
DTC_DESCRIPTIONS: dict[str, str] = {
    # ── Electronic throttle / intake ───────────────────────────────────────────────
    "P1105": "MAP sensor — short circuit to battery positive",
    "P1106": "MAP sensor — range/performance error",
    "P1107": "MAP sensor — short circuit to ground",
    "P1120": "Electronic throttle actuator — position sensor 1 circuit malfunction",
    "P1122": "Electronic throttle actuator — position sensor 1 range/performance",
    "P1124": "Electronic throttle actuator — position sensor 2 circuit malfunction",
    "P1126": "Electronic throttle actuator — adaptation not completed",
    # ── Fuel injectors ────────────────────────────────────────────────────────────────
    "P1213": "Fuel injector cylinder 1 — short circuit to ground",
    "P1214": "Fuel injector cylinder 2 — short circuit to ground",
    "P1215": "Fuel injector cylinder 3 — short circuit to ground",
    "P1216": "Fuel injector cylinder 4 — short circuit to ground",
    "P1217": "Fuel injector cylinder 5 — short circuit to ground",
    "P1218": "Fuel injector cylinder 6 — short circuit to ground",
    # ── Lambda / fuel trim ──────────────────────────────────────────────────────────────
    "P1128": "Fuel trim bank 1 — mixture adaptation, lean limit reached",
    "P1129": "Fuel trim bank 1 — mixture adaptation, rich limit reached",
    "P1131": "Fuel trim bank 2 — mixture adaptation, lean limit reached",
    "P1132": "Fuel trim bank 2 — mixture adaptation, rich limit reached",
    "P1167": "Fuel trim — altitude adaptation value out of range",
    # ── Camshaft / ignition ─────────────────────────────────────────────────────────────
    "P1340": "Camshaft position / crankshaft — correlation error bank 1",
    "P1341": "Camshaft position / crankshaft — correlation error bank 2",
    "P1343": "Ignition output stage bank 1 — malfunction",
    "P1344": "Ignition output stage bank 2 — malfunction",
    # ── CAMTRONIC (variable valve lift, M274 / M276 / M278) ───────────────────────
    "P1506": "CAMTRONIC inlet valve lift solenoid — circuit malfunction",
    "P1507": "CAMTRONIC inlet valve lift — control deviation",
    "P1508": "CAMTRONIC exhaust valve lift solenoid — circuit malfunction",
    # ── Secondary air injection ────────────────────────────────────────────────────
    "P1411": "Secondary air injection bank 1 — system malfunction",
    "P1413": "Secondary air injection bank 2 — system malfunction",
    "P1415": "Secondary air injection pump relay — circuit malfunction",
    "P1416": "Secondary air injection solenoid valve — circuit malfunction",
    # ── EVAP ────────────────────────────────────────────────────────────────────────────
    "P1450": "Evap purge valve — circuit malfunction",
    "P1451": "Evap purge valve — stuck open",
    "P1453": "Evap canister vent valve — circuit malfunction",
    # ── EGR (CDI diesel OM651 / OM642) ───────────────────────────────────────────
    "P1402": "EGR valve — solenoid circuit malfunction",
    "P1404": "EGR valve — stuck closed (insufficient flow)",
    "P1405": "EGR cooler bypass valve — circuit malfunction",
    "P1406": "EGR position feedback — plausibility error",
    # ── Boost / charge pressure (CDI / M276 / M278 turbo) ────────────────────────
    "P1555": "Boost pressure control — positive deviation (overboost)",
    "P1556": "Boost pressure control — negative deviation (underboost)",
    "P1558": "Charge pressure control valve — short circuit to ground",
    "P1559": "Charge pressure control valve — short circuit to battery positive",
    # ── Cooling ──────────────────────────────────────────────────────────────────────────
    "P1296": "Coolant temperature — insufficient warm-up within specified time",
    "P1297": "Map-controlled thermostat — performance/stuck open",
    # ── ME / CDI power supply and internal ───────────────────────────────────────────
    "P1600": "ME/CDI — main relay circuit malfunction",
    "P1602": "ME/CDI — supply voltage terminal 30 out of range",
    "P1603": "ME/CDI — internal processor / EEPROM fault",
    "P1609": "Crash shutdown signal — activated (impact sensor triggered)",
    "P1611": "CAN — no communication with EGS (transmission control module)",
    "P1626": "CAN — implausible signal from transmission control module",
    "P1690": "MIL (malfunction indicator lamp) — open circuit",
    "P1693": "MIL (malfunction indicator lamp) — short to battery positive",
}


def lookup_dtc(code: str) -> str | None:
    return DTC_DESCRIPTIONS.get(code)
