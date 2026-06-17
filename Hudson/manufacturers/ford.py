"""Ford manufacturer-specific decoders.

Sources:
  - 95 Mustang shop manual Mode 22 PID table (consistent across Ford/Lincoln PCM gens)
  - FORScan community documentation (Duratorq diesel families)
  - Ford Transit Forum / Ford Automobiles Forum community captures
  - twhitehead/notes-obd2elm327edb (public PID table)

Protocol note:
  Ford uses UDS Mode 22 (ReadDataByIdentifier) for extended PIDs.
  Standard ELM327 header for Ford PCM: ATSH C4 10 F1.
  CAN IDs: 0x7E0 request / 0x7E8 response (standard J2534).
  Request: 22 <ID_HI> <ID_LO> — success response prefix: 62.

Diesel (Duratorq) note:
  DPF/aftertreatment PIDs vary by exact model year and tune.
  PIDs marked "may not respond" are known to be absent on some variants.
  The 2010 MK7 2.2L pre-2012 uses suction-controlled fuel pump —
  pump-learning PIDs differ from post-2012.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

name = "Ford"
DISCOVERY_STRATEGY = "uds"


# ── Decoder functions ─────────────────────────────────────────────────────────

def _byte_minus40(data: bytes) -> float | None:
    return float(data[0]) - 40.0 if len(data) >= 1 else None


def _byte_pct(data: bytes) -> float | None:
    """A × 100 / 255 → %."""
    return data[0] * 100 / 255 if len(data) >= 1 else None


def _rpm_16bit(data: bytes) -> float | None:
    """(A×256 + B) / 4 → RPM."""
    return ((data[0] << 8 | data[1]) / 4.0) if len(data) >= 2 else None


def _byte_mph(data: bytes) -> float | None:
    """1-byte vehicle speed in MPH (Ford VSS encoding)."""
    return float(data[0]) if len(data) >= 1 else None


def _fuel_trim(data: bytes) -> float | None:
    """(A − 128) × 100 / 128 → % (same as J1979 short/long-term trim)."""
    return (data[0] - 128) * 100 / 128 if len(data) >= 1 else None


def _o2_voltage(data: bytes) -> float | None:
    """A × 0.00488 → V (same as J1979 O2 sensor)."""
    return data[0] * 0.00488 if len(data) >= 1 else None


def _spark_advance(data: bytes) -> float | None:
    """(A − 128) / 2 → degrees BTDC."""
    return (data[0] - 128) / 2 if len(data) >= 1 else None


def _byte_direct(data: bytes) -> float | None:
    """Raw byte as-is."""
    return float(data[0]) if len(data) >= 1 else None


def _byte_times5(data: bytes) -> float | None:
    """A × 5 → °C  (EGT sensors on Duratorq)."""
    return float(data[0]) * 5.0 if len(data) >= 1 else None


def _uint16_div10(data: bytes) -> float | None:
    """(A×256 + B) / 10 → km  (DPF regen distance)."""
    return int.from_bytes(data[:2], "big") / 10.0 if len(data) >= 2 else None


def _uint16_quarter_kpa(data: bytes) -> float | None:
    """(A×256 + B) × 0.25 → kPa  (high-res MAP on Duratorq)."""
    return int.from_bytes(data[:2], "big") * 0.25 if len(data) >= 2 else None


def _lambda_16bit(data: bytes) -> float | None:
    """2-byte lambda / equivalence ratio: raw × 3.05 × 10⁻⁵, range 0–2."""
    return int.from_bytes(data[:2], "big") * 3.05e-5 if len(data) >= 2 else None


def _fuel_pump_bit(data: bytes) -> float | None:
    """Bit 0 of first byte → 1.0 (on) or 0.0 (off)."""
    return float(data[0] & 0x01) if len(data) >= 1 else None


def _epc_psi(data: bytes) -> float | None:
    """Electronic Pressure Control: 1 byte × 0.1 → PSI."""
    return data[0] * 0.1 if len(data) >= 1 else None


def _uint16_rpm(data: bytes) -> float | None:
    """2-byte RPM direct (transmission speed sensors)."""
    return float(int.from_bytes(data[:2], "big")) if len(data) >= 2 else None


def _injector_pw(data: bytes) -> float | None:
    """(A×256 + B) / 1000 → ms  (injector pulse width)."""
    return int.from_bytes(data[:2], "big") / 1000.0 if len(data) >= 2 else None


# ── Mode 0x22 enhanced PID definitions ───────────────────────────────────────

@dataclass(frozen=True, slots=True)
class EnhancedPid:
    """A single Ford UDS (service 0x22) enhanced parameter definition."""

    identifier: int
    name: str
    unit: str
    byte_count: int
    decode: Callable[[bytes], float | None]


# ── Generic Ford/Lincoln PCM PIDs (petrol and diesel)  ────────────────────────
# Source: 95 Mustang shop manual; consistent across most Ford/Lincoln PCM gens.
# All identifiers confirmed as Mode 22 (request: 22 <HI> <LO>).
ENHANCED_PIDS: dict[str, EnhancedPid] = {
    "RPM":        EnhancedPid(0x1165, "Engine RPM",               "rpm",  2, _rpm_16bit),
    "VSS":        EnhancedPid(0x11C1, "Vehicle Speed",            "mph",  1, _byte_mph),
    "ECT":        EnhancedPid(0x1139, "Engine Coolant Temp",      "°C",   1, _byte_minus40),
    "IAT":        EnhancedPid(0x1123, "Intake Air Temp",          "°C",   1, _byte_minus40),
    "LOAD":       EnhancedPid(0x115A, "Calculated Engine Load",   "%",    1, _byte_pct),
    "SHRTFT1":    EnhancedPid(0x1158, "Short Term Fuel Trim B1",  "%",    1, _fuel_trim),
    "SHRTFT2":    EnhancedPid(0x1159, "Short Term Fuel Trim B2",  "%",    1, _fuel_trim),
    "LONGFT1":    EnhancedPid(0x1156, "Long Term Fuel Trim B1",   "%",    1, _fuel_trim),
    "LONGFT2":    EnhancedPid(0x1157, "Long Term Fuel Trim B2",   "%",    1, _fuel_trim),
    "O2S11":      EnhancedPid(0x1173, "O2 Sensor B1 S1",         "V",    1, _o2_voltage),
    "O2S12":      EnhancedPid(0x1174, "O2 Sensor B1 S2",         "V",    1, _o2_voltage),
    "O2S21":      EnhancedPid(0x1175, "O2 Sensor B2 S1",         "V",    1, _o2_voltage),
    "O2S22":      EnhancedPid(0x1176, "O2 Sensor B2 S2",         "V",    1, _o2_voltage),
    "EPC":        EnhancedPid(0x11C0, "Electronic Pressure Control", "PSI", 1, _epc_psi),
    "GEAR":       EnhancedPid(0x11B3, "Transmission Gear",        "",     1, _byte_direct),
    "TSS":        EnhancedPid(0x11B4, "Transmission Speed",       "rpm",  2, _uint16_rpm),
    "OSS":        EnhancedPid(0x11B5, "Output Shaft Speed",       "rpm",  2, _uint16_rpm),
    "FUEL_PW1":   EnhancedPid(0x1141, "Injector Pulse Width B1",  "ms",   2, _injector_pw),
    "FUEL_PW2":   EnhancedPid(0x1142, "Injector Pulse Width B2",  "ms",   2, _injector_pw),
    "SPARK_ADV":  EnhancedPid(0x116B, "Desired Spark Timing",     "°",    1, _spark_advance),
    "EVAPCP":     EnhancedPid(0x1166, "Canister Purge Duty Cycle", "%",   1, _byte_pct),
    "FPA":        EnhancedPid(0x110E, "Fuel Pump Control",        "on/off", 1, _fuel_pump_bit),
}

# ── Duratorq diesel PIDs (2.0/2.2 TDCi; Siemens/Continental PCM) ─────────────
# All addresses confirmed via FORScan on Duratorq-family Transit/Mondeo/Galaxy.
# DPF PID availability varies by year — see module docstring.
DURATORQ_PIDS: dict[str, EnhancedPid] = {
    # DPF / aftertreatment
    "DPF_DIFF_PRESS":    EnhancedPid(0x09E2, "DPF Differential Pressure", "kPa",  1, _byte_direct),
    "DPF_LAST_REGEN":    EnhancedPid(0xFD8A, "DPF Last Regen Distance",   "km",   2, _uint16_div10),
    "DPF_AVG_DIST":      EnhancedPid(0xFD89, "DPF Avg Dist Between Regens", "km", 2, _uint16_div10),
    "DPF_FAIL_COUNT":    EnhancedPid(0xFD87, "DPF Failed Regen Count",    "",     1, _byte_direct),
    "DPF_SOOT":          EnhancedPid(0x0579, "DPF Soot Load",             "%",    1, _byte_direct),
    # EGR / air
    "EGR_DC":            EnhancedPid(0x113C, "EGR Valve Duty Cycle",      "%",    1, _byte_pct),
    "MAP_HIRES":         EnhancedPid(0x0370, "Manifold Pressure (hi-res)", "kPa", 2, _uint16_quarter_kpa),
    # Fuel / injection
    "FRT":               EnhancedPid(0x168E, "Fuel Rail Temperature",     "°C",   1, _byte_direct),
    # EGT sensors (DW10C / Kuga TDCi; likely valid on MK7 Transit 2.2)
    "EGT1":              EnhancedPid(0x03F4, "EGT1 (post-turbo pre-cat)", "°C",   1, _byte_times5),
    "EGT2":              EnhancedPid(0x03F5, "EGT2 (pre-DPF)",            "°C",   1, _byte_times5),
    # Lambda / A/C
    "LAMBDA":            EnhancedPid(0xF434, "Lambda / Equivalence Ratio", "",    2, _lambda_16bit),
    "AC_PRESS":          EnhancedPid(0xFD18, "A/C System Pressure",       "kPa",  2, _uint16_quarter_kpa),
}

# Named Duratorq PIDs known to FORScan but without confirmed raw Mode 22 addresses:
# FRP_DSD  (fuel rail pressure desired, kPa)
# FRP.OBDII (fuel rail pressure actual, kPa)
# MAF.OBDII (mass air flow, g/s)
# MAP.OBDII (manifold absolute pressure, kPa)
# MFDES     (mass fuel desired, mg)
# VGTDC     (VGT duty cycle, %)
# EGRV      (EGR valve duty cycle, %)
# CHT       (cylinder head temperature, °C)
# INJ1_DEM–INJ4_DEM (injector fuel demand per cylinder, mg)
# INJ1_MAIN–INJ4_MAIN (injector main pulse width per cylinder, ms)
# PMPLRN_ST (fuel pump learned status — useful for pump health)
# These require hex address verification before implementation.


# ── Bitmask PIDs ─────────────────────────────────────────────────────────────
# PID 0x1101 packs several switch states into one byte.
# Decode each signal by masking the response byte.
BITMASK_PIDS: dict[int, tuple[tuple[int, str, str], ...]] = {
    0x1101: (
        (0x01, "ACCS",  "A/C Cycling Switch"),
        (0x02, "BOO",   "Brake On/Off"),
        (0x04, "4X4L",  "4×4 Low"),
        (0x08, "PNP",   "Park/Neutral"),
        (0x10, "TCS",   "Traction Control"),
    ),
}


# ── DTC lookup ────────────────────────────────────────────────────────────────

DTC_DESCRIPTIONS: dict[str, str] = {
    # ── System readiness ──────────────────────────────────────────────────────
    "P1000": "OBD monitor test cycle not complete",
    # ── Fuel rail pressure ────────────────────────────────────────────────────
    "P1093": "Fuel rail pressure too low during power enrichment",
    "P1094": "Fuel rail pressure sensor — signal high",
    "P1095": "Fuel pressure relief valve — stuck open",
    "P1096": "Fuel pressure switch — stuck closed",
    # ── Glow plugs ────────────────────────────────────────────────────────────
    "P1380": "Glow plug relay — circuit high",
    "P1381": "Glow plug relay — circuit low",
    "P1383": "Glow plug — cylinder 1 circuit open",
    "P1384": "Glow plug — cylinder 2 circuit open",
    "P1385": "Glow plug — cylinder 3 circuit open",
    "P1386": "Glow plug — cylinder 4 circuit open",
    "P1388": "Glow plug — resistance too high",
    "P1389": "Glow plug relay — no voltage supply",
    "P1390": "Glow plug monitoring — circuit fault",
    "P1398": "Glow plug — timing timeout",
    "P1399": "Glow plug system — malfunction",
    # ── EGR ───────────────────────────────────────────────────────────────────
    "P1401": "EGR system — insufficient flow detected",
    "P1409": "EGR valve vacuum — system leak detected",
    # ── EVAP / fuel tank ──────────────────────────────────────────────────────
    "P1450": "EVAP system — unable to bleed fuel tank vacuum",
    "P1451": "EVAP canister vent solenoid — circuit fault",
    # ── Idle air control ──────────────────────────────────────────────────────
    "P1507": "Idle air control — underspeed error (low)",
    "P1508": "Idle air control — underspeed error",
    # ── Throttle / intake ─────────────────────────────────────────────────────
    "P1549": "Intake manifold runner control — solenoid circuit",
    "P1571": "Brake switch — signal missing during cruise",
    "P1579": "Throttle position sensor — voltage above self-test max",
    "P1582": "Idle adaptation — at limit",
    # ── PCM / power supply ────────────────────────────────────────────────────
    "P1633": "KAM (keep-alive memory) supply voltage — below 10 V",
    "P1636": "ICP (injection control pressure) — above expected level",
    "P1637": "Alternator — field control circuit fault",
    "P1639": "Vehicle ID block — corrupted or missing",
    "P1641": "Fuel pump primary circuit — fault",
    # ── Duratorq-specific ─────────────────────────────────────────────────────
    "P1670": "PCM control — external control malfunction (Duratorq)",
    "P1693": "Partner ECM — fault code stored in linked module",
}


def lookup_dtc(code: str) -> str | None:
    from Hudson.core.forscan import lookup as forscan_lookup
    return forscan_lookup(code) or DTC_DESCRIPTIONS.get(code)
