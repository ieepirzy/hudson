"""Toyota manufacturer-specific decoders.

Sources:
  - Toyota/Lexus TEMS technical service bulletins
  - iATN DTC database (public entries)

Note: Toyota's enhanced PIDs are less openly documented than VAG's.
Mode 22 support is pending field verification.
"""

from __future__ import annotations

name = "Toyota"
DISCOVERY_STRATEGY = "mode01_only"


DTC_DESCRIPTIONS: dict[str, str] = {
    # ── Ignition / coils ─────────────────────────────────────────────────────
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
    # ── Fuel system ───────────────────────────────────────────────────────────
    "P1349": "Variable valve timing — malfunction",
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
