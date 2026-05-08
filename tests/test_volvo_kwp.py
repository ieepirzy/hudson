"""Tests for Volvo KWP2000 measuring block layer (manufacturers/volvo.py).

Exercises VOLVO_BLOCKS definitions, MOCK_VOLVO_KWP_RESPONSES fixtures,
and the read_kwp_block helper — all via KwpSession in mock mode.
"""

from __future__ import annotations

import pytest

from Hudson.core.kwp2000 import KwpSession
from Hudson.manufacturers.volvo import (
    MOCK_KWP_RESPONSES,
    VOLVO_BLOCKS,
    _byte_minus40,
    _byte_pct,
    _byte_kmh,
    _rpm_16bit,
    read_kwp_block,
)
from tests.fixtures.fake_connection import FakeConnection


def _volvo_session() -> KwpSession:
    """Return a started mock KwpSession loaded with Volvo fixtures."""
    return KwpSession(FakeConnection(), mock_responses=MOCK_KWP_RESPONSES)


# ── Decoder unit tests ────────────────────────────────────────────────────────

def test_byte_minus40_typical() -> None:
    assert _byte_minus40(bytes([125])) == pytest.approx(85.0)

def test_byte_minus40_zero() -> None:
    assert _byte_minus40(bytes([0])) == pytest.approx(-40.0)

def test_byte_minus40_empty_returns_none() -> None:
    assert _byte_minus40(b"") is None

def test_byte_pct_typical() -> None:
    assert _byte_pct(bytes([128])) == pytest.approx(128 / 2.55, rel=1e-4)

def test_byte_pct_empty_returns_none() -> None:
    assert _byte_pct(b"") is None

def test_rpm_16bit_typical() -> None:
    assert _rpm_16bit(bytes([0x17, 0x70])) == pytest.approx(1500.0)

def test_rpm_16bit_zero() -> None:
    assert _rpm_16bit(bytes([0x00, 0x00])) == pytest.approx(0.0)

def test_rpm_16bit_short_returns_none() -> None:
    assert _rpm_16bit(bytes([0x17])) is None

def test_byte_kmh_typical() -> None:
    assert _byte_kmh(bytes([40])) == pytest.approx(40.0)

def test_byte_kmh_empty_returns_none() -> None:
    assert _byte_kmh(b"") is None


# ── VOLVO_BLOCKS definitions ──────────────────────────────────────────────────

def test_engine_data_block_defined() -> None:
    assert "ENGINE_DATA" in VOLVO_BLOCKS

def test_engine_data_block_id() -> None:
    assert VOLVO_BLOCKS["ENGINE_DATA"].block_id == 0x01

def test_engine_data_block_has_expected_fields() -> None:
    fields = {f.name for f in VOLVO_BLOCKS["ENGINE_DATA"].fields}
    assert fields == {"rpm", "coolant_temp", "intake_temp", "throttle", "engine_load"}


# ── read_kwp_block ────────────────────────────────────────────────────────────

async def test_read_engine_data_rpm() -> None:
    session = _volvo_session()
    await session.start_diagnostic_session()
    result = await read_kwp_block(session, "ENGINE_DATA")
    assert result is not None
    assert result["rpm"] == pytest.approx(1500.0)


async def test_read_engine_data_temperatures() -> None:
    session = _volvo_session()
    await session.start_diagnostic_session()
    result = await read_kwp_block(session, "ENGINE_DATA")
    assert result is not None
    assert result["coolant_temp"] == pytest.approx(85.0)
    assert result["intake_temp"]  == pytest.approx(35.0)


async def test_read_engine_data_all_fields_present() -> None:
    session = _volvo_session()
    await session.start_diagnostic_session()
    result = await read_kwp_block(session, "ENGINE_DATA")
    assert result is not None
    assert set(result.keys()) == {"rpm", "coolant_temp", "intake_temp", "throttle", "engine_load"}


async def test_read_unknown_block_key_returns_none() -> None:
    session = _volvo_session()
    await session.start_diagnostic_session()
    assert await read_kwp_block(session, "DOES_NOT_EXIST") is None


async def test_read_block_with_no_mock_response_returns_none() -> None:
    """A block defined in VOLVO_BLOCKS but absent from the mock dict returns None."""
    empty_session = KwpSession(FakeConnection(), mock_responses={})
    await empty_session.start_diagnostic_session()
    result = await read_kwp_block(empty_session, "ENGINE_DATA")
    assert result is None


async def test_mock_responses_match_block_definitions() -> None:
    """Every block ID in VOLVO_BLOCKS should have a corresponding mock entry."""
    for key, block in VOLVO_BLOCKS.items():
        assert block.block_id in MOCK_KWP_RESPONSES, (
            f"VOLVO_BLOCKS[{key!r}].block_id=0x{block.block_id:02X} "
            f"has no entry in MOCK_KWP_RESPONSES"
        )
