"""Integration tests: Ford Transit MK7 2.2L Duratorq TDCi scenario on vcan0.

Exercises SocketCanConnection against the Ford Transit YAML scenario,
then validates Ford manufacturer module lookups (DTC descriptions, VIN WMI,
enhanced PID decoders for Duratorq diesel).

Run with:
    pytest tests/test_vcan_ford.py -v
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import pytest

from Hudson.core.socketcan_connection import SocketCanConnection
from Hudson.core.dtc import decode_dtc_list
from Hudson.manufacturers import ford
from Hudson.manufacturers.registry import select_decoder

# ── constants ─────────────────────────────────────────────────────────────────

_VCAN_IFACE   = "vcan0"
_SCENARIO     = Path(__file__).parent.parent / "tools" / "scenarios" / "ford_transit_mk7_22_tdci.yaml"
_FAKE_ECU_PY  = Path(__file__).parent.parent / "tools" / "fake_ecu.py"
_ECU_START_S  = 0.6
_EXPECTED_VIN = "WF0TXXGBWATY12345"


# ── fixtures ──────────────────────────────────────────────────────────────────

def _vcan_available() -> bool:
    try:
        import can
        bus = can.Bus(interface="socketcan", channel=_VCAN_IFACE)
        bus.shutdown()
        return True
    except Exception:
        return False


def _try_setup_vcan() -> bool:
    try:
        subprocess.run(["sudo", "modprobe", "vcan"], check=True, capture_output=True, timeout=5)
        subprocess.run(["sudo", "ip", "link", "add", _VCAN_IFACE, "type", "vcan"],
                       check=False, capture_output=True, timeout=5)
        subprocess.run(["sudo", "ip", "link", "set", _VCAN_IFACE, "up"],
                       check=True, capture_output=True, timeout=5)
        return True
    except Exception:
        return False


@pytest.fixture(scope="module", autouse=True)
def require_vcan():
    if not _vcan_available():
        _try_setup_vcan()
    if not _vcan_available():
        pytest.skip(f"{_VCAN_IFACE} not available")


@pytest.fixture()
def fake_ecu_ford(tmp_path):
    """Start fake_ecu.py with the Ford Transit healthy scenario."""
    log_path = tmp_path / "fake_ecu_ford.log"
    proc = subprocess.Popen(
        [sys.executable, str(_FAKE_ECU_PY),
         "--scenario", str(_SCENARIO),
         "--interface", _VCAN_IFACE],
        stdout=log_path.open("wb"),
        stderr=subprocess.STDOUT,
    )
    time.sleep(_ECU_START_S)
    yield proc
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


@pytest.fixture()
def fake_ecu_ford_degraded(tmp_path):
    """Start fake_ecu.py with the Ford Transit degraded (clogged DPF) profile."""
    log_path = tmp_path / "fake_ecu_ford_degraded.log"
    proc = subprocess.Popen(
        [sys.executable, str(_FAKE_ECU_PY),
         "--scenario", str(_SCENARIO),
         "--interface", _VCAN_IFACE,
         "--degraded"],
        stdout=log_path.open("wb"),
        stderr=subprocess.STDOUT,
    )
    time.sleep(_ECU_START_S)
    yield proc
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


@pytest.fixture()
def conn():
    return SocketCanConnection(_VCAN_IFACE)


async def _open(connection: SocketCanConnection) -> SocketCanConnection:
    await connection.connect()
    return connection


# ── VIN and manufacturer detection ───────────────────────────────────────────

def test_ford_vin_wmi_is_wf0() -> None:
    """The VIN in the scenario starts with Ford Europe WMI (WF0)."""
    assert _EXPECTED_VIN.startswith("WF0")


def test_ford_select_decoder_returns_ford() -> None:
    """select_decoder routes a WF0 VIN to the ford module."""
    assert select_decoder(_EXPECTED_VIN) == "Hudson.manufacturers.ford"


def test_ford_select_decoder_us_vin() -> None:
    """1FT (Ford USA F-series/Transit) also routes to the ford module."""
    assert select_decoder("1FTBF2A64BEA12345") == "Hudson.manufacturers.ford"


# ── Mode 01 live data ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_ford_mode01_rpm(fake_ecu_ford, conn) -> None:
    """Mode 01 RPM returns idle value ≈ 820 rpm."""
    import obd
    await _open(conn)
    try:
        resp = await conn.query(obd.commands.RPM)
        assert not resp.is_null()
        assert 800 <= resp.value.magnitude <= 850
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_ford_mode01_coolant_temp(fake_ecu_ford, conn) -> None:
    """Coolant temperature reads 85 °C (warm idle)."""
    import obd
    await _open(conn)
    try:
        resp = await conn.query(obd.commands.COOLANT_TEMP)
        assert not resp.is_null()
        assert abs(resp.value.magnitude - 85) < 2
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_ford_mode01_map_atmospheric(fake_ecu_ford, conn) -> None:
    """MAP reads ~101 kPa (near atmospheric, turbo at rest at idle)."""
    import obd
    await _open(conn)
    try:
        resp = await conn.query(obd.commands.INTAKE_PRESSURE)
        assert not resp.is_null()
        assert 98 <= resp.value.magnitude <= 104
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_ford_mode09_vin(fake_ecu_ford, conn) -> None:
    """Mode 09 PID 02 returns the correct Ford VIN."""
    import obd
    await _open(conn)
    try:
        resp = await conn.query(obd.commands.VIN)
        assert not resp.is_null(), "VIN response was null"
        assert resp.value == _EXPECTED_VIN
    finally:
        await conn.close()


# ── DTCs ──────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_ford_no_stored_dtcs_when_healthy(fake_ecu_ford, conn) -> None:
    """Healthy Transit scenario has no stored DTCs."""
    import obd
    await _open(conn)
    try:
        resp = await conn.query(obd.commands.GET_DTC)
        if not resp.is_null() and resp.value:
            assert resp.value == [], f"Unexpected stored DTCs: {resp.value}"
    finally:
        await conn.close()


# ── Mode 22 Ford generic PIDs ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_ford_uds_rpm(fake_ecu_ford, conn) -> None:
    """Ford Mode 22 PID 0x1165 (RPM) returns ~820 rpm."""
    await _open(conn)
    try:
        raw = await conn.query_uds(0x22, 0x1165)
        assert raw is not None, "0x1165 returned None"
        assert len(raw) == 2
        rpm = ford.ENHANCED_PIDS["RPM"].decode(raw)
        assert rpm is not None
        assert 800 <= rpm <= 850
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_ford_uds_ect(fake_ecu_ford, conn) -> None:
    """Ford Mode 22 PID 0x1139 (ECT) returns 85 °C."""
    await _open(conn)
    try:
        raw = await conn.query_uds(0x22, 0x1139)
        assert raw is not None, "0x1139 returned None"
        temp = ford.ENHANCED_PIDS["ECT"].decode(raw)
        assert temp is not None
        assert abs(temp - 85) < 2
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_ford_uds_bitmask_pnp(fake_ecu_ford, conn) -> None:
    """PID 0x1101 bitmask has PNP bit set (vehicle in park at idle)."""
    await _open(conn)
    try:
        raw = await conn.query_uds(0x22, 0x1101)
        assert raw is not None, "0x1101 returned None"
        assert len(raw) >= 1
        pnp_mask = 0x08
        assert raw[0] & pnp_mask, f"PNP bit not set in 0x1101: {raw[0]:#04x}"
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_ford_uds_fuel_pump_on(fake_ecu_ford, conn) -> None:
    """PID 0x110E bit 0 indicates fuel pump running."""
    await _open(conn)
    try:
        raw = await conn.query_uds(0x22, 0x110E)
        assert raw is not None, "0x110E returned None"
        pump = ford.ENHANCED_PIDS["FPA"].decode(raw)
        assert pump == 1.0, "Fuel pump should be on at idle"
    finally:
        await conn.close()


# ── Mode 22 Duratorq diesel PIDs ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_ford_uds_dpf_diff_pressure_low(fake_ecu_ford, conn) -> None:
    """DPF differential pressure (0x09E2) is low on a healthy filter at idle."""
    await _open(conn)
    try:
        raw = await conn.query_uds(0x22, 0x09E2)
        assert raw is not None, "0x09E2 returned None"
        pressure = ford.DURATORQ_PIDS["DPF_DIFF_PRESS"].decode(raw)
        assert pressure is not None
        assert pressure < 10, f"DPF pressure unexpectedly high: {pressure} kPa"
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_ford_uds_dpf_soot_normal(fake_ecu_ford, conn) -> None:
    """DPF soot load (0x0579) is below 40 % in the healthy scenario."""
    await _open(conn)
    try:
        raw = await conn.query_uds(0x22, 0x0579)
        assert raw is not None, "0x0579 returned None"
        soot = ford.DURATORQ_PIDS["DPF_SOOT"].decode(raw)
        assert soot is not None
        assert soot < 40, f"Soot load unexpectedly high: {soot} %"
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_ford_uds_dpf_last_regen_distance(fake_ecu_ford, conn) -> None:
    """DPF last regen distance (0xFD8A) decodes to ~35 km."""
    await _open(conn)
    try:
        raw = await conn.query_uds(0x22, 0xFD8A)
        assert raw is not None, "0xFD8A returned None"
        dist = ford.DURATORQ_PIDS["DPF_LAST_REGEN"].decode(raw)
        assert dist is not None
        assert abs(dist - 35.0) < 1
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_ford_uds_egt1_warm_idle(fake_ecu_ford, conn) -> None:
    """EGT1 (0x03F4) reads ~320 °C at warm diesel idle."""
    await _open(conn)
    try:
        raw = await conn.query_uds(0x22, 0x03F4)
        assert raw is not None, "0x03F4 returned None"
        egt = ford.DURATORQ_PIDS["EGT1"].decode(raw)
        assert egt is not None
        assert 280 <= egt <= 380
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_ford_uds_map_hires(fake_ecu_ford, conn) -> None:
    """High-res MAP (0x0370) reads ~102 kPa at idle."""
    await _open(conn)
    try:
        raw = await conn.query_uds(0x22, 0x0370)
        assert raw is not None, "0x0370 returned None"
        map_kpa = ford.DURATORQ_PIDS["MAP_HIRES"].decode(raw)
        assert map_kpa is not None
        assert 98 <= map_kpa <= 106
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_ford_uds_lambda_lean(fake_ecu_ford, conn) -> None:
    """Lambda (0xF434) is lean (> 1.0) — expected for diesel at idle."""
    await _open(conn)
    try:
        raw = await conn.query_uds(0x22, 0xF434)
        assert raw is not None, "0xF434 returned None"
        lam = ford.DURATORQ_PIDS["LAMBDA"].decode(raw)
        assert lam is not None
        assert lam > 1.0, f"Lambda should be lean at diesel idle, got {lam:.3f}"
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_ford_uds_ecu_version(fake_ecu_ford, conn) -> None:
    """UDS 0xF189 returns a 4-byte ECU software version string."""
    await _open(conn)
    try:
        raw = await conn.query_uds(0x22, 0xF189)
        assert raw is not None, "0xF189 returned None"
        assert len(raw) >= 4
    finally:
        await conn.close()


# ── Degraded scenario: clogged DPF ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_ford_degraded_p2002_stored(fake_ecu_ford_degraded, conn) -> None:
    """Degraded: P2002 (DPF efficiency below threshold) is in stored DTCs."""
    import obd
    from Hudson.tui.panes.dtcs import GET_PENDING_DTC
    await _open(conn)
    try:
        stored_resp = await conn.query(obd.commands.GET_DTC)
        stored = [c for c, _ in stored_resp.value] if not stored_resp.is_null() else []

        pending_resp = await conn.query(GET_PENDING_DTC)
        pending = (
            [d.code for d in decode_dtc_list(pending_resp.value)]
            if not pending_resp.is_null()
            else []
        )

        assert "P2002" in stored, f"P2002 not stored: {stored}"
        assert "P2463" in stored, f"P2463 (soot accumulation) not stored: {stored}"
        assert "P2002" in pending, f"P2002 not pending: {pending}"
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_ford_degraded_dpf_soot_elevated(fake_ecu_ford_degraded, conn) -> None:
    """Degraded: DPF soot load (0x0579) exceeds 70 %."""
    await _open(conn)
    try:
        raw = await conn.query_uds(0x22, 0x0579)
        assert raw is not None
        soot = ford.DURATORQ_PIDS["DPF_SOOT"].decode(raw)
        assert soot is not None
        assert soot > 70, f"DPF soot not elevated in degraded scenario: {soot} %"
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_ford_degraded_dpf_pressure_elevated(fake_ecu_ford_degraded, conn) -> None:
    """Degraded: DPF differential pressure (0x09E2) exceeds 10 kPa."""
    await _open(conn)
    try:
        raw = await conn.query_uds(0x22, 0x09E2)
        assert raw is not None
        pressure = ford.DURATORQ_PIDS["DPF_DIFF_PRESS"].decode(raw)
        assert pressure is not None
        assert pressure > 10, f"DPF pressure not elevated in degraded scenario: {pressure} kPa"
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_ford_degraded_egt_elevated(fake_ecu_ford_degraded, conn) -> None:
    """Degraded: EGT1 is elevated relative to healthy idle (failed regens)."""
    await _open(conn)
    try:
        raw = await conn.query_uds(0x22, 0x03F4)
        assert raw is not None
        egt = ford.DURATORQ_PIDS["EGT1"].decode(raw)
        assert egt is not None
        assert egt > 380, f"EGT1 not elevated in degraded scenario: {egt} °C"
    finally:
        await conn.close()


# ── DTC description table ─────────────────────────────────────────────────────

def test_ford_dtc_lookup_glow_plug() -> None:
    """A Ford glow plug DTC returns a non-empty description."""
    desc = ford.lookup_dtc("P1384")
    assert desc is not None
    assert len(desc) > 5


def test_ford_dtc_lookup_duratorq_specific() -> None:
    """P1670 (Duratorq-specific PCM fault) is in the Ford DTC table."""
    desc = ford.lookup_dtc("P1670")
    assert desc is not None
    assert "Duratorq" in desc


def test_ford_dtc_lookup_generic_sae_returns_none() -> None:
    """A generic SAE code (P0300) not in the Ford table returns None."""
    assert ford.lookup_dtc("P0300") is None


def test_ford_dtc_lookup_unknown_returns_none() -> None:
    """Completely unknown code returns None without raising."""
    assert ford.lookup_dtc("P9999") is None


# ── Decoder unit tests ────────────────────────────────────────────────────────

def test_ford_rpm_decoder() -> None:
    """RPM decoder: (12×256+208)/4 = 820 rpm."""
    raw = bytes([12, 208])
    assert ford.ENHANCED_PIDS["RPM"].decode(raw) == 820.0


def test_ford_ect_decoder() -> None:
    """ECT decoder: 125−40 = 85 °C."""
    assert ford.ENHANCED_PIDS["ECT"].decode(bytes([125])) == 85.0


def test_ford_egt1_decoder() -> None:
    """EGT1 decoder: 64×5 = 320 °C."""
    assert ford.DURATORQ_PIDS["EGT1"].decode(bytes([64])) == 320.0


def test_ford_dpf_regen_dist_decoder() -> None:
    """DPF last regen decoder: (1×256+94)/10 = 35.0 km."""
    assert ford.DURATORQ_PIDS["DPF_LAST_REGEN"].decode(bytes([1, 94])) == 35.0


def test_ford_map_hires_decoder() -> None:
    """High-res MAP decoder: (1×256+152)×0.25 = 102.0 kPa."""
    assert ford.DURATORQ_PIDS["MAP_HIRES"].decode(bytes([1, 152])) == 102.0


def test_ford_lambda_decoder() -> None:
    """Lambda decoder: 59016×3.05×10⁻⁵ ≈ 1.80."""
    raw = bytes([230, 136])
    lam = ford.DURATORQ_PIDS["LAMBDA"].decode(raw)
    assert lam is not None
    assert abs(lam - 1.8) < 0.01


def test_ford_fuel_trim_decoder_zero() -> None:
    """Fuel trim decoder: (128−128)×100/128 = 0 %."""
    assert ford.ENHANCED_PIDS["SHRTFT1"].decode(bytes([128])) == 0.0


def test_ford_fuel_pump_bit_on() -> None:
    """FPA decoder: bit0=1 → 1.0."""
    assert ford.ENHANCED_PIDS["FPA"].decode(bytes([0x01])) == 1.0


def test_ford_fuel_pump_bit_off() -> None:
    """FPA decoder: bit0=0 → 0.0."""
    assert ford.ENHANCED_PIDS["FPA"].decode(bytes([0x00])) == 0.0
