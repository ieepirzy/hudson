"""Tests for Hudson.core.telemetry — HTTP telemetry client."""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import obd
import pytest

from Hudson.core.init import InitResult
from Hudson.core.poller import Reading
from Hudson.core.telemetry import TelemetryClient
from tests.fixtures.fake_connection import FakeConnection, _make_response, _FakeQuantity


# ── helpers ───────────────────────────────────────────────────────────────────

def _mock_http_client() -> MagicMock:
    """Return a mock httpx.AsyncClient whose .post() records calls."""
    client = MagicMock()
    response = MagicMock()
    response.status_code = 200
    client.post = AsyncMock(return_value=response)
    client.aclose = AsyncMock()
    return client


def _fake_init_result(**kwargs) -> InitResult:
    defaults = dict(vin="WV2ZZZ7HZ8H123456", manufacturer_name="VW/Audi", protocol_name="CAN")
    defaults.update(kwargs)
    return InitResult(**defaults)


def _fake_reading(magnitude: float = 800.0) -> Reading:
    cmd = obd.commands.RPM
    resp = _make_response(cmd, _FakeQuantity(magnitude))
    return Reading(command=cmd, response=resp, received_at=time.monotonic())


def _null_reading() -> Reading:
    cmd = obd.commands.RPM
    resp = _make_response(cmd, None)
    return Reading(command=cmd, response=resp, received_at=time.monotonic())


@pytest.fixture()
def client() -> TelemetryClient:
    c = TelemetryClient("test-token")
    c._http = _mock_http_client()
    return c


# ── record_reading ─────────────────────────────────────────────────────────────

def test_record_reading_enqueues_item(client: TelemetryClient) -> None:
    """A reading with a numeric value is added to the internal queue."""
    client.record_reading(_fake_reading(800.0))
    assert not client._reading_queue.empty()


def test_record_reading_queued_item_has_correct_fields(client: TelemetryClient) -> None:
    """Queued item contains pid, value, and ts keys."""
    client.record_reading(_fake_reading(1234.5))
    item = client._reading_queue.get_nowait()
    assert item["pid"] == "RPM"
    assert abs(item["value"] - 1234.5) < 0.01
    assert isinstance(item["ts"], float)


def test_record_reading_null_response_not_enqueued(client: TelemetryClient) -> None:
    """A reading whose response.value has no .magnitude is silently discarded."""
    client.record_reading(_null_reading())
    assert client._reading_queue.empty()


def test_record_reading_full_queue_drops_silently(client: TelemetryClient) -> None:
    """When the queue is at capacity, further readings are dropped without error."""
    client._reading_queue = asyncio.Queue(maxsize=2)
    client.record_reading(_fake_reading(1.0))
    client.record_reading(_fake_reading(2.0))
    client.record_reading(_fake_reading(3.0))  # should not raise
    assert client._reading_queue.qsize() == 2


# ── _flush ─────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_flush_posts_readings(client: TelemetryClient) -> None:
    """_flush drains the queue and POSTs a 'readings' event."""
    client.record_reading(_fake_reading(800.0))
    client.record_reading(_fake_reading(900.0))
    await client._flush()
    client._http.post.assert_called_once()
    payload = client._http.post.call_args.kwargs["json"]
    assert payload["event"] == "readings"
    assert len(payload["readings"]) == 2
    assert client._reading_queue.empty()


@pytest.mark.asyncio
async def test_flush_empty_queue_does_not_post(client: TelemetryClient) -> None:
    """_flush on an empty queue makes no HTTP request."""
    await client._flush()
    client._http.post.assert_not_called()


@pytest.mark.asyncio
async def test_flush_includes_session_id(client: TelemetryClient) -> None:
    """The flushed payload includes the session_id."""
    client.record_reading(_fake_reading())
    await client._flush()
    payload = client._http.post.call_args.kwargs["json"]
    assert payload["session_id"] == client._session_id


# ── start / stop ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_start_posts_session_start(client: TelemetryClient) -> None:
    """start() POSTs a session_start event with VIN and manufacturer."""
    result = _fake_init_result()
    await client.start(result)
    client._batch_task.cancel()
    try:
        await client._batch_task
    except asyncio.CancelledError:
        pass
    call_payload = client._http.post.call_args_list[0].kwargs["json"]
    assert call_payload["event"] == "session_start"
    assert call_payload["vin"] == result.vin
    assert call_payload["manufacturer"] == result.manufacturer_name
    assert call_payload["protocol"] == result.protocol_name


@pytest.mark.asyncio
async def test_stop_posts_session_end(client: TelemetryClient) -> None:
    """stop() POSTs a session_end event after flushing readings."""
    result = _fake_init_result()
    await client.start(result)
    await client.stop()
    events = [call.kwargs["json"]["event"] for call in client._http.post.call_args_list]
    assert "session_end" in events


@pytest.mark.asyncio
async def test_stop_flushes_buffered_readings(client: TelemetryClient) -> None:
    """stop() flushes any readings accumulated before the batch timer fires."""
    result = _fake_init_result()
    await client.start(result)
    client.record_reading(_fake_reading(750.0))
    await client.stop()
    events = [call.kwargs["json"]["event"] for call in client._http.post.call_args_list]
    assert "readings" in events


# ── record_dtcs ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_record_dtcs_posts_immediately(client: TelemetryClient) -> None:
    """record_dtcs creates a fire-and-forget task that POSTs a dtc_scan event."""
    await client.record_dtcs(["P0401"], ["P0402"], [])
    await asyncio.sleep(0.05)  # let the fire-and-forget task complete
    payload = client._http.post.call_args.kwargs["json"]
    assert payload["event"] == "dtc_scan"
    assert payload["stored"] == ["P0401"]
    assert payload["pending"] == ["P0402"]
    assert payload["permanent"] == []


@pytest.mark.asyncio
async def test_record_dtcs_includes_session_id(client: TelemetryClient) -> None:
    """DTC scan POST includes the session_id for correlation."""
    await client.record_dtcs([], [], [])
    await asyncio.sleep(0.05)
    payload = client._http.post.call_args.kwargs["json"]
    assert payload["session_id"] == client._session_id


# ── network errors ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_network_error_does_not_raise(client: TelemetryClient) -> None:
    """HTTP errors in _post are logged and swallowed, never raised to the caller."""
    client._http.post = AsyncMock(side_effect=ConnectionError("network down"))
    client.record_reading(_fake_reading())
    await client._flush()  # should not raise


@pytest.mark.asyncio
async def test_http_4xx_does_not_raise(client: TelemetryClient) -> None:
    """A 4xx HTTP response is logged but not raised."""
    bad_response = MagicMock()
    bad_response.status_code = 401
    bad_response.text = "Unauthorized"
    client._http.post = AsyncMock(return_value=bad_response)
    client.record_reading(_fake_reading())
    await client._flush()  # should not raise
