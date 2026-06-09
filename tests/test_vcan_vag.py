"""Integration tests: VW Golf Mk6 TDI scenario on vcan0.

Exercises SocketCanConnection against the VW-specific YAML scenario,
then validates VAG manufacturer module lookups (DTC descriptions, VIN WMI).

Run with:
    pytest tests/test_vcan_vag.py -v
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import pytest

from Hudson.core.socketcan_connection import SocketCanConnection
from Hudson.core.dtc import decode_dtc_list
from Hudson.manufacturers import vw_audi
from Hudson.manufacturers.registry import select_decoder

# ── constants ─────────────────────────────────────────────────────────────────

_VCAN_IFACE   = "vcan0"
_SCENARIO     = Path(__file__).parent.parent / "tools" / "scenarios" / "vw_golf_mk6_tdi.yaml"
_FAKE_ECU_PY  = Path(__file__).parent.parent / "tools" / "fake_ecu.py"
_ECU_START_S  = 0.6
_EXPECTED_VIN = "WVWZZZ1KZBM057145"


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
def fake_ecu_vag(tmp_path):
    """Start fake_ecu.py with the VW Golf Mk6 TDI scenario."""
    log_path = tmp_path / "fake_ecu_vag.log"
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
def fake_ecu_vag_degraded(tmp_path):
    """Start fake_ecu.py with the VW Golf degraded profile."""
    log_path = tmp_path / "fake_ecu_vag_degraded.log"
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

def test_vag_vin_wmi_is_vw() -> None:
    """The VIN in the scenario starts with a VW WMI (WVW)."""
    assert _EXPECTED_VIN.startswith("WVW")


def test_vag_select_decoder_returns_vw_audi() -> None:
    """select_decoder routes a WVW VIN to the vw_audi module."""
    module_path = select_decoder(_EXPECTED_VIN)
    assert module_path == "Hudson.manufacturers.vw_audi"


@pytest.mark.asyncio
async def test_vag_mode09_vin(fake_ecu_vag, conn) -> None:
    """Mode 09 PID 02 returns the correct VW VIN."""
    import obd
    await _open(conn)
    try:
        resp = await conn.query(obd.commands.VIN)
        assert not resp.is_null(), "VIN response was null"
        assert resp.value == _EXPECTED_VIN
    finally:
        await conn.close()


# ── Mode 01 live data ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_vag_mode01_rpm(fake_ecu_vag, conn) -> None:
    """Mode 01 RPM returns idle value ≈ 800 rpm."""
    import obd
    await _open(conn)
    try:
        resp = await conn.query(obd.commands.RPM)
        assert not resp.is_null()
        assert 750 <= resp.value.magnitude <= 850
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_vag_mode01_coolant_temp(fake_ecu_vag, conn) -> None:
    """Coolant temperature reads 80 °C (warm idle)."""
    import obd
    await _open(conn)
    try:
        resp = await conn.query(obd.commands.COOLANT_TEMP)
        assert not resp.is_null()
        assert abs(resp.value.magnitude - 80) < 2
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_vag_mode01_map(fake_ecu_vag, conn) -> None:
    """MAP reads 100 kPa (atmospheric, turbo at rest)."""
    import obd
    await _open(conn)
    try:
        resp = await conn.query(obd.commands.INTAKE_PRESSURE)
        assert not resp.is_null()
        assert abs(resp.value.magnitude - 100) < 2
    finally:
        await conn.close()


# ── DTCs ──────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_vag_stored_dtc_p2002(fake_ecu_vag, conn) -> None:
    """P2002 (DPF efficiency below threshold) appears in stored DTCs."""
    import obd
    await _open(conn)
    try:
        resp = await conn.query(obd.commands.GET_DTC)
        assert not resp.is_null()
        codes = [c for c, _ in resp.value]
        assert "P2002" in codes, f"P2002 not in stored: {codes}"
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_vag_pending_dtcs_empty_when_healthy(fake_ecu_vag, conn) -> None:
    """Healthy VW scenario has no pending DTCs."""
    from Hudson.tui.panes.dtcs import GET_PENDING_DTC
    await _open(conn)
    try:
        resp = await conn.query(GET_PENDING_DTC)
        if not resp.is_null() and resp.value:
            dtcs = decode_dtc_list(resp.value)
            assert dtcs == [], f"Unexpected pending DTCs: {dtcs}"
    finally:
        await conn.close()


# ── Mode 22 VAG-specific PIDs ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_vag_uds_ecu_version(fake_ecu_vag, conn) -> None:
    """UDS 0xF189 returns the ECU software version."""
    await _open(conn)
    try:
        raw = await conn.query_uds(0x22, 0xF189)
        assert raw is not None, "0xF189 returned None"
        assert len(raw) >= 4
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_vag_uds_boost_setpoint(fake_ecu_vag, conn) -> None:
    """UDS 0x115E (boost pressure setpoint) returns a 2-byte payload."""
    await _open(conn)
    try:
        raw = await conn.query_uds(0x22, 0x115E)
        assert raw is not None, "0x115E returned None"
        assert len(raw) == 2
        setpoint_mbar = int.from_bytes(raw, "big")
        assert setpoint_mbar == 1600
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_vag_uds_boost_actual(fake_ecu_vag, conn) -> None:
    """UDS 0x115F (boost actual) returns a 2-byte payload ≤ setpoint."""
    await _open(conn)
    try:
        raw = await conn.query_uds(0x22, 0x115F)
        assert raw is not None, "0x115F returned None"
        assert len(raw) == 2
        actual_mbar = int.from_bytes(raw, "big")
        assert 0 < actual_mbar <= 1600
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_vag_uds_dpf_soot_normal(fake_ecu_vag, conn) -> None:
    """UDS 0x1803 (DPF soot) is low in the healthy scenario."""
    await _open(conn)
    try:
        raw = await conn.query_uds(0x22, 0x1803)
        assert raw is not None, "0x1803 returned None"
        assert len(raw) == 2
        soot_raw = int.from_bytes(raw, "big")
        assert soot_raw < 100, f"Soot unexpectedly high in healthy scenario: {soot_raw}"
    finally:
        await conn.close()


# ── VAG DTC description table ─────────────────────────────────────────────────

def test_vag_dtc_lookup_known_code() -> None:
    """A VAG-specific DTC in the table returns a non-empty description."""
    desc = vw_audi.lookup_dtc("P1176")
    assert desc is not None
    assert len(desc) > 5


def test_vag_dtc_lookup_generic_code_returns_none() -> None:
    """A generic SAE code (P2002) not in the VAG table returns None."""
    assert vw_audi.lookup_dtc("P2002") is None


def test_vag_dtc_lookup_unknown_returns_none() -> None:
    """Completely unknown code returns None without raising."""
    assert vw_audi.lookup_dtc("P9999") is None


# ── Degraded scenario ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_vag_degraded_p0299_stored(fake_ecu_vag_degraded, conn) -> None:
    """Degraded: P0299 (underboost) is in both stored and pending DTCs."""
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

        assert "P0299" in stored, f"P0299 not stored: {stored}"
        assert "P0299" in pending, f"P0299 not pending: {pending}"
        assert "P2002" in stored, "P2002 should still be stored in degraded"
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_vag_degraded_dpf_soot_elevated(fake_ecu_vag_degraded, conn) -> None:
    """Degraded: DPF soot (0x1803) is much higher than in the healthy scenario."""
    await _open(conn)
    try:
        raw = await conn.query_uds(0x22, 0x1803)
        assert raw is not None
        soot_raw = int.from_bytes(raw, "big")
        assert soot_raw > 100, f"DPF soot not elevated in degraded scenario: {soot_raw}"
    finally:
        await conn.close()
