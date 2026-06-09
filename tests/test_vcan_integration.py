"""Integration tests: SocketCanConnection + fake_ecu.py on vcan0.

Requirements
────────────
- Linux with vcan kernel module loaded
- vcan0 interface up (test fixture attempts setup; skips if unavailable)
- Hudson package installed (pip install -e .)
- python-can and can-isotp installed

Each test starts fake_ecu.py in a subprocess, runs the relevant Hudson code
against vcan0, and asserts the expected outcome.

Run with:
    pytest tests/test_vcan_integration.py -v

For degraded-scenario tests run with:
    pytest tests/test_vcan_integration.py -v -k degraded
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
import time
from pathlib import Path

import pytest

import time

from Hudson.core.socketcan_connection import SocketCanConnection, _to_wire
from Hudson.core.dtc import decode_dtc_list

# ── constants ─────────────────────────────────────────────────────────────────

_VCAN_IFACE   = "vcan0"
_SCENARIO     = Path(__file__).parent.parent / "tools" / "scenarios" / "ford_transit_2010.yaml"
_FAKE_ECU_PY  = Path(__file__).parent.parent / "tools" / "fake_ecu.py"
_ECU_START_S  = 0.6   # seconds to wait after starting fake_ecu.py
_EXPECTED_VIN = "WF0XXXTTFX9D21136"


# ── fixtures ──────────────────────────────────────────────────────────────────

def _vcan_available() -> bool:
    """Return True if vcan0 is up and usable."""
    try:
        import can
        bus = can.Bus(interface="socketcan", channel=_VCAN_IFACE)
        bus.shutdown()
        return True
    except Exception:
        return False


def _try_setup_vcan() -> bool:
    """Attempt to bring vcan0 up.  Requires CAP_NET_ADMIN or passwordless sudo."""
    try:
        subprocess.run(["sudo", "modprobe", "vcan"],        check=True, capture_output=True, timeout=5)
        subprocess.run(["sudo", "ip", "link", "add", _VCAN_IFACE, "type", "vcan"],
                       check=False, capture_output=True, timeout=5)  # ignore if already exists
        subprocess.run(["sudo", "ip", "link", "set", _VCAN_IFACE, "up"],
                       check=True, capture_output=True, timeout=5)
        return True
    except Exception:
        return False


@pytest.fixture(scope="module", autouse=True)
def require_vcan():
    """Module-scoped: ensure vcan0 is up; skip the whole module if not."""
    if not _vcan_available():
        _try_setup_vcan()
    if not _vcan_available():
        pytest.skip(f"{_VCAN_IFACE} not available — run: sudo modprobe vcan && sudo ip link add {_VCAN_IFACE} type vcan && sudo ip link set {_VCAN_IFACE} up")


@pytest.fixture()
def fake_ecu(tmp_path):
    """Start fake_ecu.py (healthy) in a background subprocess."""
    log_path = tmp_path / "fake_ecu.log"
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
def fake_ecu_degraded(tmp_path):
    """Start fake_ecu.py with --degraded in a background subprocess."""
    log_path = tmp_path / "fake_ecu_degraded.log"
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
    """Create (but do not yet connect) a SocketCanConnection."""
    return SocketCanConnection(_VCAN_IFACE)


# ── helpers ───────────────────────────────────────────────────────────────────

async def _open(connection: SocketCanConnection) -> SocketCanConnection:
    await connection.connect()
    return connection


# ── VIN resolution ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_mode09_vin(fake_ecu, conn):
    """Mode 09 PID 02 returns the correct VIN."""
    import obd
    await _open(conn)
    try:
        resp = await conn.query(obd.commands.VIN)
        assert not resp.is_null(), "VIN response was null"
        assert resp.value == _EXPECTED_VIN, f"VIN mismatch: {resp.value!r}"
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_uds_f190_gateway_vin(fake_ecu, conn):
    """VIN chain step 2: UDS 0x22 0xF190 at gateway address 0x7D9."""
    await _open(conn)
    try:
        await conn.send_at("ATD")
        await conn.send_at("ATSH7D9")
        raw = await conn.query_uds(0x22, 0xF190)
        assert raw is not None, "Gateway UDS F190 returned None"
        vin = raw.decode("ascii", errors="replace").strip("\x00").strip()
        assert vin == _EXPECTED_VIN, f"Gateway VIN mismatch: {vin!r}"
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_vin_chain_lands_on_mode09(fake_ecu, conn):
    """Full resolve_vin_chain() succeeds via mode 09 and returns the correct VIN."""
    from Hudson.core.vin import resolve_vin_chain
    await _open(conn)
    try:
        vin = await resolve_vin_chain(conn)  # type: ignore[arg-type]
        assert vin == _EXPECTED_VIN, f"VIN chain returned {vin!r}"
    finally:
        await conn.close()


# ── Mode 01 live data ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_mode01_rpm(fake_ecu, conn):
    """Mode 01 PID 0x0C returns idle RPM ≈ 800."""
    import obd
    await _open(conn)
    try:
        resp = await conn.query(obd.commands.RPM)
        assert not resp.is_null(), "RPM null"
        assert 750 <= resp.value.magnitude <= 850, f"RPM out of range: {resp.value.magnitude}"
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_mode01_coolant_temp(fake_ecu, conn):
    """Coolant temp reads 80 °C (healthy warm idle)."""
    import obd
    await _open(conn)
    try:
        resp = await conn.query(obd.commands.COOLANT_TEMP)
        assert not resp.is_null()
        assert abs(resp.value.magnitude - 80) < 2
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_mode01_supported_commands(fake_ecu, conn):
    """supported_commands() returns a non-empty set including RPM and GET_DTC."""
    import obd
    await _open(conn)
    try:
        cmds = await conn.supported_commands()
        names = {c.name for c in cmds}
        assert obd.commands.RPM in cmds, "RPM not in supported"
        assert "GET_DTC" in names, "GET_DTC not in supported"
    finally:
        await conn.close()


# ── DTC modes 03, 07, 0A ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_mode03_stored_dtcs(fake_ecu, conn):
    """Mode 03 returns P0401 as a stored DTC."""
    import obd
    await _open(conn)
    try:
        resp = await conn.query(obd.commands.GET_DTC)
        assert not resp.is_null(), "Mode 03 null"
        codes = [code for code, _ in resp.value]
        assert "P0401" in codes, f"P0401 not in stored DTCs: {codes}"
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_mode07_pending_dtcs(fake_ecu, conn):
    """Mode 07 returns P0402 as a pending DTC."""
    from Hudson.tui.panes.dtcs import GET_PENDING_DTC
    await _open(conn)
    try:
        resp = await conn.query(GET_PENDING_DTC)
        assert not resp.is_null(), "Mode 07 null"
        dtcs = decode_dtc_list(resp.value)
        codes = [d.code for d in dtcs]
        assert "P0402" in codes, f"P0402 not in pending DTCs: {codes}"
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_mode0a_permanent_dtcs_empty(fake_ecu, conn):
    """Mode 0A returns no permanent DTCs on a healthy vehicle."""
    from Hudson.tui.panes.dtcs import GET_PERMANENT_DTC
    await _open(conn)
    try:
        resp = await conn.query(GET_PERMANENT_DTC)
        # Healthy scenario has no permanent DTCs; either null or empty bytes OK
        if not resp.is_null() and resp.value:
            dtcs = decode_dtc_list(resp.value)
            assert dtcs == [], f"Unexpected permanent DTCs: {dtcs}"
    finally:
        await conn.close()


# ── Mode 22 Ford-specific PIDs ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_mode22_dpf_pressure(fake_ecu, conn):
    """UDS 0x09E2 (DPF differential pressure) is answered by the ECM."""
    await _open(conn)
    try:
        raw = await conn.query_uds(0x22, 0x09E2)
        assert raw is not None, "0x09E2 returned None"
        assert len(raw) >= 2, f"0x09E2 payload too short: {raw!r}"
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_mode22_last_regen(fake_ecu, conn):
    """UDS 0xFD8A (distance since last DPF regen) is answered."""
    await _open(conn)
    try:
        raw = await conn.query_uds(0x22, 0xFD8A)
        assert raw is not None, "0xFD8A returned None"
        assert len(raw) >= 4, f"0xFD8A payload too short: {raw!r}"
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_mode22_fuel_rail_temp(fake_ecu, conn):
    """UDS 0x168E (fuel rail temperature) is answered."""
    await _open(conn)
    try:
        raw = await conn.query_uds(0x22, 0x168E)
        assert raw is not None, "0x168E returned None"
        assert len(raw) >= 1
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_mode22_egr_duty_cycle(fake_ecu, conn):
    """UDS 0x113C (EGR duty cycle) returns ~50% in healthy scenario."""
    await _open(conn)
    try:
        raw = await conn.query_uds(0x22, 0x113C)
        assert raw is not None, "0x113C returned None"
        assert len(raw) >= 1
        # Healthy: raw byte 128 ≈ 50% (128/255*100)
        assert raw[0] > 50, f"EGR duty unexpectedly low in healthy scenario: {raw[0]}"
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_mode22_unknown_pid_returns_none(fake_ecu, conn):
    """Unknown UDS identifier returns None (NRC from ECU)."""
    await _open(conn)
    try:
        raw = await conn.query_uds(0x22, 0xDEAD)
        assert raw is None, f"Expected None for unknown PID, got {raw!r}"
    finally:
        await conn.close()


# ── Degraded scenario ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_degraded_boost_fault_pending(fake_ecu_degraded, conn):
    """Degraded: P0299 (underboost) appears in both stored and pending DTCs."""
    import obd
    from Hudson.tui.panes.dtcs import GET_PENDING_DTC

    await _open(conn)
    try:
        stored_resp = await conn.query(obd.commands.GET_DTC)
        stored_codes = [c for c, _ in stored_resp.value] if not stored_resp.is_null() else []

        pending_resp = await conn.query(GET_PENDING_DTC)
        pending_codes = (
            [d.code for d in decode_dtc_list(pending_resp.value)]
            if not pending_resp.is_null()
            else []
        )

        assert "P0299" in stored_codes, f"P0299 not in stored: {stored_codes}"
        assert "P0299" in pending_codes, f"P0299 not in pending: {pending_codes}"
        assert "P0401" in stored_codes, "P0401 should still be stored in degraded scenario"
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_degraded_egr_duty_stuck_zero(fake_ecu_degraded, conn):
    """Degraded: EGR duty cycle (UDS 0x113C) is 0 (stuck closed)."""
    await _open(conn)
    try:
        raw = await conn.query_uds(0x22, 0x113C)
        assert raw is not None, "0x113C returned None in degraded mode"
        assert raw[0] == 0, f"EGR duty not 0 in degraded scenario: {raw[0]}"
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_degraded_map_underboost(fake_ecu_degraded, conn):
    """Degraded: MAP (0x0B) reads 90 kPa vs 100 kPa in healthy scenario."""
    import obd
    await _open(conn)
    try:
        resp = await conn.query(obd.commands.INTAKE_PRESSURE)
        assert not resp.is_null(), "INTAKE_PRESSURE null in degraded mode"
        assert resp.value.magnitude <= 95, (
            f"MAP should be low in degraded scenario: {resp.value.magnitude} kPa"
        )
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_degraded_fuel_rail_pressure_low(fake_ecu_degraded, conn):
    """Degraded: fuel rail pressure (mode 01 0x23) drops from 350 bar to 180 bar."""
    import obd
    await _open(conn)
    try:
        resp = await conn.query(obd.commands.FUEL_RAIL_PRESSURE_VAC)
        assert not resp.is_null(), "FUEL_RAIL_PRESSURE_VAC null in degraded mode"
        # Healthy = 35000 kPa = 350 bar; degraded = 18000 kPa = 180 bar
        pressure_kpa = resp.value.magnitude
        assert pressure_kpa < 25000, (
            f"Fuel rail pressure not low enough in degraded scenario: {pressure_kpa} kPa"
        )
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_degraded_dpf_pressure_rising(fake_ecu_degraded, conn):
    """Degraded: DPF differential pressure (0x09E2) is higher than healthy."""
    await _open(conn)
    try:
        raw_degraded = await conn.query_uds(0x22, 0x09E2)
        assert raw_degraded is not None
        # Degraded: [0, 70]; healthy: [0, 50] — degraded must be higher
        pressure = int.from_bytes(raw_degraded[:2], "big")
        assert pressure > 60, f"DPF pressure not elevated in degraded scenario: {pressure}"
    finally:
        await conn.close()


# ── Address management ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_send_at_atsh_sets_tx_rx(conn):
    """ATSH7D9 sets tx=0x7D9 and rx=0x7DA (tx+1 for non-ECM addresses)."""
    await _open(conn)
    try:
        await conn.send_at("ATSH7D9")
        assert conn._tx_id == 0x7D9
        assert conn._rx_id == 0x7DA
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_send_at_atd_resets_to_ecm_defaults(conn):
    """ATD after ATSH resets tx/rx back to the ECM default addresses."""
    await _open(conn)
    try:
        await conn.send_at("ATSH7D9")
        assert conn._tx_id == 0x7D9
        await conn.send_at("ATD")
        assert conn._tx_id == SocketCanConnection._DEFAULT_ECM_TX
        assert conn._rx_id == SocketCanConnection._DEFAULT_ECM_RX
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_send_at_atz_resets_to_ecm_defaults(conn):
    """ATZ (full reset) also reverts to ECM default addresses."""
    await _open(conn)
    try:
        await conn.send_at("ATSH7DF")
        await conn.send_at("ATZ")
        assert conn._tx_id == SocketCanConnection._DEFAULT_ECM_TX
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_send_at_ecm_range_uses_plus8(conn):
    """ATSH in the ECM range (0x7E0–0x7E7) uses rx = tx + 8."""
    await _open(conn)
    try:
        await conn.send_at("ATSH7E1")
        assert conn._tx_id == 0x7E1
        assert conn._rx_id == 0x7E1 + 8
    finally:
        await conn.close()


# ── Timeout / no ECU ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_query_uds_timeout_no_listener(conn):
    """query_uds to an address with no listener returns None within the timeout."""
    await _open(conn)
    try:
        # Send to a CAN address nobody is listening on
        await conn.send_at("ATSH6FF")
        t0 = time.monotonic()
        raw = await conn.query_uds(0x22, 0xF190, timeout=0.4)
        elapsed = time.monotonic() - t0
        assert raw is None
        assert elapsed < 1.0, f"Timed out too slowly: {elapsed:.2f}s"
    finally:
        await conn.close()


# ── supported_commands coverage ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_supported_commands_includes_voltage(fake_ecu, conn):
    """CONTROL_MODULE_VOLTAGE (PID 0x42, third bitmask range) appears in supported commands."""
    await _open(conn)
    try:
        cmds = await conn.supported_commands()
        names = {c.name for c in cmds}
        assert "CONTROL_MODULE_VOLTAGE" in names, f"CONTROL_MODULE_VOLTAGE not in {names}"
    finally:
        await conn.close()


# ── _to_wire conversion ───────────────────────────────────────────────────────

def test_to_wire_ascii_hex_command():
    """Standard python-obd ASCII hex commands convert correctly to wire bytes."""
    import obd
    wire = _to_wire(obd.commands.RPM.command)
    assert wire == bytes([0x01, 0x0C])


def test_to_wire_binary_command():
    """Binary byte commands (custom OBDCommands) pass through unchanged."""
    wire = _to_wire(bytes([0x07]))
    assert wire == bytes([0x07])


def test_to_wire_mode09_vin():
    """Mode 09 VIN command (b'0902') converts to [0x09, 0x02]."""
    import obd
    wire = _to_wire(obd.commands.VIN.command)
    assert wire == bytes([0x09, 0x02])
