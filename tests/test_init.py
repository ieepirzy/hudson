"""Tests for Hudson.core.init — init sequence state machine.

Tests use FakeConnection variants so no hardware is required.  Where UDS
discovery would be triggered, the connection returns None for 0xF189 to keep
the test fast (skips the full UDS sweep path).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import patch

import pytest

from Hudson.core.init import InitEvent, InitResult, InitStep, run_init
from tests.fixtures.fake_connection import (
    FakeConnection,
    FakeNoMode09VinConnection,
)


# ── connection helpers ────────────────────────────────────────────────────────

class FakeNoUdsConnection(FakeConnection):
    """FakeConnection that returns None for all UDS queries.

    Forces the init sequence to skip UDS/ECU-version discovery and proceed
    directly to the supported-PIDs probe, keeping tests fast.
    """

    async def query_uds(self, service: int, identifier: int, timeout: float = 0.15) -> bytes | None:
        await asyncio.sleep(0.01)
        return None


class FakeKLineConnection(FakeNoUdsConnection):
    """FakeConnection that reports a K-line protocol name.

    Causes _is_kline=True in init.py, which forces strategy to 'mode01_only'
    and skips all UDS steps (even for manufacturers with DISCOVERY_STRATEGY='uds').
    """

    @property
    def protocol_name(self) -> str:
        return "ISO 14230-4 KWP (slow init)"


class FakeNoVinConnection(FakeNoUdsConnection):
    """FakeConnection where both mode 09 and UDS VIN return nothing."""

    async def query(self, cmd, force: bool = False):
        import obd as _obd
        if cmd is _obd.commands.VIN:
            from obd import OBDResponse
            return OBDResponse(command=cmd, messages=[])
        return await super().query(cmd, force=force)


# ── fixture: redirect EcuCache to tmp_path ────────────────────────────────────

@pytest.fixture(autouse=True)
def _redirect_ecu_cache(tmp_path: Path, monkeypatch):
    """Redirect EcuCache to a temp SQLite file to avoid touching ~/.hudson/."""
    from Hudson.core.ecu_cache import EcuCache

    def _make_cache():
        return EcuCache(tmp_path / "ecu_cache.db")

    monkeypatch.setattr("Hudson.core.init.EcuCache", _make_cache)


# ── helpers ───────────────────────────────────────────────────────────────────

async def _run(conn) -> tuple[InitResult, list[InitEvent]]:
    events: asyncio.Queue[InitEvent] = asyncio.Queue()
    result = await run_init(conn, events)
    collected: list[InitEvent] = []
    while not events.empty():
        collected.append(events.get_nowait())
    return result, collected


def _step_events(events: list[InitEvent], step: InitStep) -> list[InitEvent]:
    return [e for e in events if e.step == step]


# ── happy path ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_happy_path_completes() -> None:
    """Full init with FakeNoUdsConnection reaches READY step."""
    conn = FakeNoUdsConnection(vin="WV2ZZZ7HZ8H123456")
    result, events = await _run(conn)
    ready_events = _step_events(events, InitStep.READY)
    assert len(ready_events) >= 1
    assert ready_events[-1].done


@pytest.mark.asyncio
async def test_happy_path_result_fields() -> None:
    """InitResult is populated with VIN, manufacturer name, and supported commands."""
    conn = FakeNoUdsConnection(vin="WV2ZZZ7HZ8H123456")
    result, _ = await _run(conn)
    assert result.vin == "WV2ZZZ7HZ8H123456"
    assert result.manufacturer_name == "VW/Audi"
    assert len(result.supported_commands) > 0


@pytest.mark.asyncio
async def test_protocol_name_in_result() -> None:
    """protocol_name from the connection appears in InitResult."""
    conn = FakeNoUdsConnection()
    result, _ = await _run(conn)
    assert "CAN" in result.protocol_name or "15765" in result.protocol_name


@pytest.mark.asyncio
async def test_all_init_steps_emit_events() -> None:
    """Every InitStep is represented in the emitted events."""
    conn = FakeNoUdsConnection()
    _, events = await _run(conn)
    steps_seen = {e.step for e in events}
    expected = {
        InitStep.CONNECT, InitStep.PROTOCOL, InitStep.VIN,
        InitStep.MANUFACTURER, InitStep.ECU_VERSION, InitStep.UDS_DISCOVERY,
        InitStep.KWP_SESSION, InitStep.SUPPORTED_PIDS, InitStep.READY,
    }
    assert expected <= steps_seen


# ── manufacturer detection ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_vw_vin_selects_vw_audi_module() -> None:
    """A VIN with WV2 WMI selects the VW/Audi manufacturer module."""
    conn = FakeNoUdsConnection(vin="WV2ZZZ7HZ8H123456")
    result, _ = await _run(conn)
    assert result.manufacturer_name == "VW/Audi"
    assert result.manufacturer_module is not None


@pytest.mark.asyncio
async def test_unknown_wmi_falls_back_to_generic() -> None:
    """A VIN with an unregistered WMI falls back to Generic."""
    conn = FakeNoUdsConnection(vin="XYZ00000000000000")
    result, _ = await _run(conn)
    assert result.manufacturer_name == "Generic"


@pytest.mark.asyncio
async def test_toyota_vin_selects_toyota_module() -> None:
    """A JT-prefix VIN selects the Toyota manufacturer module."""
    conn = FakeNoUdsConnection(vin="JT000000000000001")
    result, _ = await _run(conn)
    assert result.manufacturer_name == "Toyota"


# ── VIN failure handling ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_vin_null_does_not_crash() -> None:
    """When VIN resolution fails, init completes with result.vin=None."""
    conn = FakeNoVinConnection()
    result, events = await _run(conn)
    assert result.vin is None
    vin_events = _step_events(events, InitStep.VIN)
    done = [e for e in vin_events if e.done]
    assert len(done) >= 1
    assert done[-1].error is not None  # error is reported, not raised


@pytest.mark.asyncio
async def test_vin_null_selects_generic_manufacturer() -> None:
    """Without a VIN, manufacturer falls back to Generic."""
    conn = FakeNoVinConnection()
    result, _ = await _run(conn)
    assert result.manufacturer_name == "Generic"


# ── K-line protocol — UDS skipped ────────────────────────────────────────────

@pytest.mark.asyncio
async def test_kline_protocol_skips_uds() -> None:
    """K-line protocol forces strategy=mode01_only; all UDS steps are 'not applicable'."""
    conn = FakeKLineConnection(vin="YV1RS61T242397765")  # Volvo S60
    _, events = await _run(conn)
    uds_events = _step_events(events, InitStep.UDS_DISCOVERY)
    assert any(e.done and "not applicable" in e.detail for e in uds_events)
    ecu_events = _step_events(events, InitStep.ECU_VERSION)
    assert any(e.done and "not applicable" in e.detail for e in ecu_events)


@pytest.mark.asyncio
async def test_kline_protocol_still_reaches_ready() -> None:
    """K-line init still reaches READY despite skipping UDS."""
    conn = FakeKLineConnection()
    result, events = await _run(conn)
    assert any(e.step == InitStep.READY and e.done for e in events)


# ── UDS not supported (0xF189 returns None) ───────────────────────────────────

@pytest.mark.asyncio
async def test_no_uds_response_skips_discovery() -> None:
    """When 0xF189 returns None, UDS discovery is skipped gracefully."""
    conn = FakeNoUdsConnection(vin="WVW000000000000000")
    result, events = await _run(conn)
    uds_events = _step_events(events, InitStep.UDS_DISCOVERY)
    assert any(e.done and "skipped" in e.detail.lower() for e in uds_events)
    assert result.ecu_version is None
    assert result.uds_identifiers == []


@pytest.mark.asyncio
async def test_no_uds_result_has_supported_pids() -> None:
    """supported_commands is still populated via mode 01 bitmask probing."""
    conn = FakeNoUdsConnection()
    result, _ = await _run(conn)
    assert len(result.supported_commands) > 0


# ── event structure ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_connect_events_include_done() -> None:
    """CONNECT step emits a start event and a done=True event."""
    conn = FakeNoUdsConnection()
    _, events = await _run(conn)
    connect_events = _step_events(events, InitStep.CONNECT)
    assert any(not e.done for e in connect_events)   # start
    assert any(e.done for e in connect_events)         # done


@pytest.mark.asyncio
async def test_manufacturer_event_carries_name() -> None:
    """MANUFACTURER done event detail contains the manufacturer name."""
    conn = FakeNoUdsConnection(vin="WV2ZZZ7HZ8H123456")
    _, events = await _run(conn)
    mfr_done = [e for e in events if e.step == InitStep.MANUFACTURER and e.done]
    assert mfr_done
    assert "VW" in mfr_done[-1].detail or "Audi" in mfr_done[-1].detail
