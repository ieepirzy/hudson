"""Tests for the KWP2000 transport layer (Hudson/core/kwp2000.py).

This suite covers only the ISO 14230 framing mechanics: session lifecycle,
mock-response injection, block queries, and parse_block field extraction.
It has no dependency on any manufacturer module.
"""

from __future__ import annotations

import pytest

from Hudson.core.kwp2000 import KwpBlock, KwpField, KwpSession
from tests.fixtures.fake_connection import FakeConnection, FakeVolvoConnection

# Generic test payloads — no manufacturer semantics attached.
_PAYLOAD_A = bytes([0x01, 0x02, 0x03, 0x04, 0x05, 0x06])
_PAYLOAD_B = bytes([0xFF, 0xFE])

_TEST_RESPONSES: dict[int, bytes] = {
    0x01: _PAYLOAD_A,
    0x02: _PAYLOAD_B,
}

# Minimal decoders used in parse_block tests — pure math, no manufacturer meaning.
def _identity_byte(data: bytes) -> float | None:
    return float(data[0]) if len(data) >= 1 else None

def _two_byte_div4(data: bytes) -> float | None:
    return ((data[0] << 8 | data[1]) / 4.0) if len(data) >= 2 else None


# ── Session lifecycle ─────────────────────────────────────────────────────────

async def test_mock_session_starts_successfully() -> None:
    session = KwpSession(FakeConnection(), mock_responses=_TEST_RESPONSES)
    assert await session.start_diagnostic_session() is True


async def test_real_session_start_returns_false() -> None:
    session = KwpSession(FakeConnection(), mock_responses=None)
    assert await session.start_diagnostic_session() is False


async def test_is_mock_true_when_responses_injected() -> None:
    session = KwpSession(FakeConnection(), mock_responses=_TEST_RESPONSES)
    assert session.is_mock is True


async def test_is_mock_false_when_no_responses() -> None:
    session = KwpSession(FakeConnection(), mock_responses=None)
    assert session.is_mock is False


async def test_query_before_session_start_raises() -> None:
    session = KwpSession(FakeConnection(), mock_responses=_TEST_RESPONSES)
    with pytest.raises(RuntimeError, match="not started"):
        await session.query_block(0x01)


async def test_close_resets_started_flag() -> None:
    session = KwpSession(FakeConnection(), mock_responses=_TEST_RESPONSES)
    await session.start_diagnostic_session()
    await session.close()
    with pytest.raises(RuntimeError, match="not started"):
        await session.query_block(0x01)


# ── Block queries ─────────────────────────────────────────────────────────────

async def test_mock_query_known_block_returns_injected_bytes() -> None:
    session = KwpSession(FakeConnection(), mock_responses=_TEST_RESPONSES)
    await session.start_diagnostic_session()
    assert await session.query_block(0x01) == _PAYLOAD_A


async def test_mock_query_second_known_block() -> None:
    session = KwpSession(FakeConnection(), mock_responses=_TEST_RESPONSES)
    await session.start_diagnostic_session()
    assert await session.query_block(0x02) == _PAYLOAD_B


async def test_mock_query_unknown_block_returns_none() -> None:
    session = KwpSession(FakeConnection(), mock_responses=_TEST_RESPONSES)
    await session.start_diagnostic_session()
    assert await session.query_block(0xFE) is None


async def test_empty_mock_responses_always_returns_none() -> None:
    session = KwpSession(FakeConnection(), mock_responses={})
    await session.start_diagnostic_session()
    assert await session.query_block(0x01) is None


# ── parse_block ───────────────────────────────────────────────────────────────

def test_parse_single_byte_field() -> None:
    session = KwpSession(FakeConnection(), mock_responses={})
    defn = KwpBlock(0x01, "Test", (KwpField("a", 0, 1, "unit", _identity_byte),))
    assert session.parse_block(defn, bytes([0x2A]))=={"a": pytest.approx(42.0)}


def test_parse_two_byte_field() -> None:
    session = KwpSession(FakeConnection(), mock_responses={})
    defn = KwpBlock(0x01, "Test", (KwpField("v", 0, 2, "unit", _two_byte_div4),))
    # 0x1770 = 6000; / 4 = 1500
    assert session.parse_block(defn, bytes([0x17, 0x70]))=={"v": pytest.approx(1500.0)}


def test_parse_multiple_fields_at_different_offsets() -> None:
    session = KwpSession(FakeConnection(), mock_responses={})
    defn = KwpBlock(
        0x01,
        "Test",
        (
            KwpField("first",  0, 2, "unit", _two_byte_div4),
            KwpField("second", 2, 1, "unit", _identity_byte),
            KwpField("third",  3, 1, "unit", _identity_byte),
        ),
    )
    data = bytes([0x17, 0x70, 0x7D, 0x4B])
    result = session.parse_block(defn, data)
    assert result["first"]  == pytest.approx(1500.0)
    assert result["second"] == pytest.approx(125.0)
    assert result["third"]  == pytest.approx(75.0)


def test_parse_field_beyond_data_returns_none() -> None:
    session = KwpSession(FakeConnection(), mock_responses={})
    defn = KwpBlock(
        0x01,
        "Test",
        (
            KwpField("good",    0, 2, "unit", _two_byte_div4),
            KwpField("phantom", 10, 2, "unit", _two_byte_div4),
        ),
    )
    result = session.parse_block(defn, bytes([0x17, 0x70, 0x00, 0x00]))
    assert result["good"]    == pytest.approx(1500.0)
    assert result["phantom"] is None


def test_parse_empty_fields_tuple() -> None:
    session = KwpSession(FakeConnection(), mock_responses={})
    assert session.parse_block(KwpBlock(0x01, "Empty", ()), bytes([0xFF])) == {}


def test_parse_empty_data_all_fields_none() -> None:
    session = KwpSession(FakeConnection(), mock_responses={})
    defn = KwpBlock(0x01, "Test", (KwpField("x", 0, 1, "unit", _identity_byte),))
    assert session.parse_block(defn, b"") == {"x": None}


# ── Real-hardware path (via FakeConnection) ───────────────────────────────────

async def test_real_path_sends_atsp3() -> None:
    conn = FakeConnection()
    session = KwpSession(conn, mock_responses=None)
    # FakeConnection.query_kwp_service returns None → session fails to start
    await session.start_diagnostic_session()
    assert "ATSP3" in conn._send_at_history


async def test_real_path_restores_atsp0_on_failure() -> None:
    conn = FakeConnection()
    session = KwpSession(conn, mock_responses=None)
    started = await session.start_diagnostic_session()
    assert started is False
    assert conn.protocol_kline is False
    assert "ATSP0" in conn._send_at_history


async def test_real_path_starts_when_kwp_service_responds() -> None:
    conn = FakeVolvoConnection()
    session = KwpSession(conn, mock_responses=None)
    started = await session.start_diagnostic_session()
    assert started is True
    assert session._started is True


async def test_real_path_close_sends_atsp0() -> None:
    conn = FakeVolvoConnection()
    session = KwpSession(conn, mock_responses=None)
    await session.start_diagnostic_session()
    conn._send_at_history.clear()
    await session.close()
    assert "ATSP0" in conn._send_at_history
    assert session._started is False
