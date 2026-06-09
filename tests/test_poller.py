"""Tests for Hudson.core.poller — tiered async PID poller."""

from __future__ import annotations

import asyncio

import obd
import pytest

from Hudson.core.poller import Poller, PollSpec, Reading
from tests.fixtures.fake_connection import FakeConnection


class _FailConnection(FakeConnection):
    """FakeConnection whose query() always raises."""

    async def query(self, cmd, force=False):
        raise RuntimeError("simulated query failure")


@pytest.mark.asyncio
async def test_poller_enqueues_readings() -> None:
    """Poller enqueues at least one Reading per spec within a short window."""
    conn = FakeConnection()
    await conn.connect()
    q: asyncio.Queue[Reading] = asyncio.Queue()
    spec = PollSpec(obd.commands.RPM, period_s=0.05)
    poller = Poller(conn, [spec], q)
    await poller.start()
    await asyncio.sleep(0.3)
    await poller.stop()
    assert not q.empty()
    reading = q.get_nowait()
    assert reading.command is obd.commands.RPM
    assert isinstance(reading.received_at, float)


@pytest.mark.asyncio
async def test_poller_multiple_specs_each_produce_readings() -> None:
    """Each PollSpec independently produces readings."""
    conn = FakeConnection()
    await conn.connect()
    q: asyncio.Queue[Reading] = asyncio.Queue()
    specs = [
        PollSpec(obd.commands.RPM, period_s=0.05),
        PollSpec(obd.commands.COOLANT_TEMP, period_s=0.05),
    ]
    poller = Poller(conn, specs, q)
    await poller.start()
    await asyncio.sleep(0.5)
    await poller.stop()
    commands_seen: set[object] = set()
    while not q.empty():
        commands_seen.add(q.get_nowait().command)
    assert obd.commands.RPM in commands_seen
    assert obd.commands.COOLANT_TEMP in commands_seen


@pytest.mark.asyncio
async def test_poller_on_reading_callback_fires() -> None:
    """on_reading callback is invoked synchronously for each queued reading."""
    conn = FakeConnection()
    await conn.connect()
    q: asyncio.Queue[Reading] = asyncio.Queue()
    received: list[Reading] = []
    spec = PollSpec(obd.commands.RPM, period_s=0.05)
    poller = Poller(conn, [spec], q, on_reading=received.append)
    await poller.start()
    await asyncio.sleep(0.3)
    await poller.stop()
    assert len(received) > 0
    assert all(r.command is obd.commands.RPM for r in received)


@pytest.mark.asyncio
async def test_poller_callback_count_matches_queue_size() -> None:
    """Callback fires exactly once per queued reading."""
    conn = FakeConnection()
    await conn.connect()
    q: asyncio.Queue[Reading] = asyncio.Queue()
    received: list[Reading] = []
    spec = PollSpec(obd.commands.RPM, period_s=0.05)
    poller = Poller(conn, [spec], q, on_reading=received.append)
    await poller.start()
    await asyncio.sleep(0.3)
    await poller.stop()
    assert q.qsize() == len(received)


@pytest.mark.asyncio
async def test_poller_stop_halts_readings() -> None:
    """After stop(), the queue receives no further readings."""
    conn = FakeConnection()
    await conn.connect()
    q: asyncio.Queue[Reading] = asyncio.Queue()
    spec = PollSpec(obd.commands.RPM, period_s=0.05)
    poller = Poller(conn, [spec], q)
    await poller.start()
    await asyncio.sleep(0.2)
    await poller.stop()
    size_at_stop = q.qsize()
    await asyncio.sleep(0.2)
    assert q.qsize() == size_at_stop


@pytest.mark.asyncio
async def test_poller_double_start_raises() -> None:
    """Starting an already-running Poller raises RuntimeError."""
    conn = FakeConnection()
    await conn.connect()
    q: asyncio.Queue[Reading] = asyncio.Queue()
    spec = PollSpec(obd.commands.RPM, period_s=0.1)
    poller = Poller(conn, [spec], q)
    await poller.start()
    try:
        with pytest.raises(RuntimeError, match="already started"):
            await poller.start()
    finally:
        await poller.stop()


@pytest.mark.asyncio
async def test_poller_query_exception_does_not_crash() -> None:
    """A query that always raises does not crash the poller; it just backsoff."""
    conn = _FailConnection()
    await conn.connect()
    q: asyncio.Queue[Reading] = asyncio.Queue()
    spec = PollSpec(obd.commands.RPM, period_s=0.01)
    poller = Poller(conn, [spec], q)
    await poller.start()
    await asyncio.sleep(0.15)
    await poller.stop()
    assert q.empty()


@pytest.mark.asyncio
async def test_poller_on_reading_exception_is_swallowed() -> None:
    """Exception raised inside on_reading callback is caught; poller continues."""
    conn = FakeConnection()
    await conn.connect()
    q: asyncio.Queue[Reading] = asyncio.Queue()
    call_count = 0

    def bad_callback(r: Reading) -> None:
        nonlocal call_count
        call_count += 1
        raise ValueError("deliberate callback failure")

    spec = PollSpec(obd.commands.RPM, period_s=0.05)
    poller = Poller(conn, [spec], q, on_reading=bad_callback)
    await poller.start()
    await asyncio.sleep(0.3)
    await poller.stop()
    assert call_count > 0


@pytest.mark.asyncio
async def test_poller_reading_response_is_not_null() -> None:
    """Readings produced from FakeConnection have non-null responses."""
    conn = FakeConnection()
    await conn.connect()
    q: asyncio.Queue[Reading] = asyncio.Queue()
    spec = PollSpec(obd.commands.RPM, period_s=0.05)
    poller = Poller(conn, [spec], q)
    await poller.start()
    await asyncio.sleep(0.2)
    await poller.stop()
    assert not q.empty()
    assert not q.get_nowait().response.is_null()


@pytest.mark.asyncio
async def test_poller_empty_specs_starts_and_stops() -> None:
    """A Poller with no specs starts and stops cleanly without error."""
    conn = FakeConnection()
    await conn.connect()
    q: asyncio.Queue[Reading] = asyncio.Queue()
    poller = Poller(conn, [], q)
    await poller.start()
    await asyncio.sleep(0.05)
    await poller.stop()
    assert q.empty()


@pytest.mark.asyncio
async def test_poller_timestamps_are_non_decreasing() -> None:
    """Reading.received_at timestamps are monotonically non-decreasing."""
    conn = FakeConnection()
    await conn.connect()
    q: asyncio.Queue[Reading] = asyncio.Queue()
    spec = PollSpec(obd.commands.RPM, period_s=0.05)
    poller = Poller(conn, [spec], q)
    await poller.start()
    await asyncio.sleep(0.5)
    await poller.stop()
    readings: list[Reading] = []
    while not q.empty():
        readings.append(q.get_nowait())
    assert len(readings) >= 2
    for prev, curr in zip(readings, readings[1:]):
        assert curr.received_at >= prev.received_at


@pytest.mark.asyncio
async def test_poller_slow_period_produces_few_readings() -> None:
    """A slow PollSpec (period=0.3 s) produces 1–2 readings over 0.5 s."""
    conn = FakeConnection()
    await conn.connect()
    q: asyncio.Queue[Reading] = asyncio.Queue()
    spec = PollSpec(obd.commands.RPM, period_s=0.3)
    poller = Poller(conn, [spec], q)
    await poller.start()
    await asyncio.sleep(0.5)
    await poller.stop()
    count = q.qsize()
    assert 1 <= count <= 3
