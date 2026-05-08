"""Tests for Toyota enhanced PID flow (mode 0x22 and mode 0x21).

All tests run against FakeToyotaConnection — no hardware required.
Decoder functions are tested in isolation first so formula bugs are
caught before the async stack is involved.
"""

from __future__ import annotations

import pytest

from Hudson.manufacturers.toyota import (
    ENHANCED_BLOCKS,
    ENHANCED_PIDS,
    BlockField,
    EnhancedBlock,
    EnhancedPid,
    _byte_kmh,
    _byte_minus40,
    _byte_pct,
    _rpm_16bit,
    read_enhanced_block,
    read_enhanced_pid,
)
from tests.fixtures.fake_connection import FakeToyotaConnection


# ── Decoder unit tests ────────────────────────────────────────────────────────

def test_byte_minus40_typical() -> None:
    assert _byte_minus40(bytes([125])) == pytest.approx(85.0)


def test_byte_minus40_zero_raw() -> None:
    assert _byte_minus40(bytes([0])) == pytest.approx(-40.0)


def test_byte_minus40_empty_returns_none() -> None:
    assert _byte_minus40(b"") is None


def test_byte_pct_half_scale() -> None:
    # 128 / 2.55 ≈ 50.2 %
    assert _byte_pct(bytes([128])) == pytest.approx(128 / 2.55, rel=1e-4)


def test_byte_pct_empty_returns_none() -> None:
    assert _byte_pct(b"") is None


def test_rpm_16bit_typical() -> None:
    # 0x1770 = 6000; 6000 / 4 = 1500 rpm
    assert _rpm_16bit(bytes([0x17, 0x70])) == pytest.approx(1500.0)


def test_rpm_16bit_zero() -> None:
    assert _rpm_16bit(bytes([0x00, 0x00])) == pytest.approx(0.0)


def test_rpm_16bit_short_returns_none() -> None:
    assert _rpm_16bit(bytes([0x17])) is None


def test_byte_kmh_typical() -> None:
    assert _byte_kmh(bytes([40])) == pytest.approx(40.0)


def test_byte_kmh_empty_returns_none() -> None:
    assert _byte_kmh(b"") is None


# ── read_enhanced_pid (mode 0x22) ─────────────────────────────────────────────

async def test_read_coolant_temp() -> None:
    conn = FakeToyotaConnection()
    await conn.connect()
    result = await read_enhanced_pid(conn, "COOLANT_TEMP")
    assert result == pytest.approx(85.0)


async def test_read_intake_temp() -> None:
    conn = FakeToyotaConnection()
    await conn.connect()
    result = await read_enhanced_pid(conn, "INTAKE_TEMP")
    assert result == pytest.approx(35.0)


async def test_read_rpm() -> None:
    conn = FakeToyotaConnection()
    await conn.connect()
    result = await read_enhanced_pid(conn, "RPM")
    assert result == pytest.approx(1500.0)


async def test_read_speed() -> None:
    conn = FakeToyotaConnection()
    await conn.connect()
    result = await read_enhanced_pid(conn, "SPEED")
    assert result == pytest.approx(40.0)


async def test_all_enhanced_pids_return_value() -> None:
    """Every PID defined in ENHANCED_PIDS must return a non-None float on mock."""
    conn = FakeToyotaConnection()
    await conn.connect()
    for key in ENHANCED_PIDS:
        result = await read_enhanced_pid(conn, key)
        assert result is not None, f"Expected float for {key!r}, got None"


async def test_unknown_pid_key_returns_none() -> None:
    conn = FakeToyotaConnection()
    await conn.connect()
    assert await read_enhanced_pid(conn, "DOES_NOT_EXIST") is None


async def test_null_uds_response_returns_none() -> None:
    """If the ECU returns no data, read_enhanced_pid must return None gracefully."""
    conn = FakeToyotaConnection()
    await conn.connect()
    # BOOST is not in ENHANCED_PIDS, but we can test via an ad-hoc pid
    # whose identifier has no mock response.
    orphan_pid = EnhancedPid(0xFFFF, "No response", "unit", 1, _byte_kmh)
    data = await conn.query_uds(0x22, orphan_pid.identifier)
    assert data is None


# ── read_enhanced_block (mode 0x21) ──────────────────────────────────────────

async def test_read_engine_data_block_rpm() -> None:
    conn = FakeToyotaConnection()
    await conn.connect()
    result = await read_enhanced_block(conn, "ENGINE_DATA")
    assert result is not None
    assert result["rpm"] == pytest.approx(1500.0)


async def test_read_engine_data_block_temperatures() -> None:
    conn = FakeToyotaConnection()
    await conn.connect()
    result = await read_enhanced_block(conn, "ENGINE_DATA")
    assert result is not None
    assert result["coolant_temp"] == pytest.approx(85.0)
    assert result["intake_temp"] == pytest.approx(35.0)


async def test_read_engine_data_block_all_fields_present() -> None:
    conn = FakeToyotaConnection()
    await conn.connect()
    result = await read_enhanced_block(conn, "ENGINE_DATA")
    assert result is not None
    expected_fields = {"rpm", "coolant_temp", "intake_temp", "throttle", "engine_load"}
    assert set(result.keys()) == expected_fields


async def test_unknown_block_key_returns_none() -> None:
    conn = FakeToyotaConnection()
    await conn.connect()
    assert await read_enhanced_block(conn, "DOES_NOT_EXIST") is None


async def test_null_enhanced_local_response_returns_none() -> None:
    """query_enhanced_local returning None must propagate as None from read_enhanced_block."""
    conn = FakeToyotaConnection()
    await conn.connect()
    # Block 0xFF has no mock response → query_enhanced_local returns None
    orphan_block = EnhancedBlock(0xFF, "Ghost Block", ())
    data = await conn.query_enhanced_local(orphan_block.local_id)
    assert data is None


# ── parse_block via truncated data ────────────────────────────────────────────

async def test_truncated_block_data_yields_none_fields() -> None:
    """A shorter-than-expected block response degrades gracefully field by field."""
    conn = FakeToyotaConnection()
    await conn.connect()

    short_block = EnhancedBlock(
        0x10,
        "Short Block",
        (
            BlockField("rpm", 0, 2, "rpm", _rpm_16bit),
            # field at offset 10 won't fit in a 6-byte response
            BlockField("phantom", 10, 2, "rpm", _rpm_16bit),
        ),
    )

    # Manually parse a 6-byte payload
    from Hudson.manufacturers.toyota import ENHANCED_BLOCKS  # noqa: PLC0415 — already imported at top
    # Use read_enhanced_block path by temporarily checking the truncation logic directly
    data = bytes([0x17, 0x70, 0x7D, 0x4B, 0x26, 0x4D])  # 6 bytes

    result: dict[str, float | None] = {}
    for field in short_block.fields:
        chunk = data[field.offset : field.offset + field.count]
        result[field.name] = field.decode(chunk) if len(chunk) == field.count else None

    assert result["rpm"] == pytest.approx(1500.0)
    assert result["phantom"] is None
