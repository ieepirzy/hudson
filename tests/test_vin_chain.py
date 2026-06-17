"""Tests for the speculative VIN resolution chain."""

from __future__ import annotations

import pytest

from Hudson.core.vin import _parse_vin_value, resolve_vin_chain
from tests.fixtures.fake_connection import (
    FakeConnection,
    FakeNoMode09VinConnection,
)


# ── _parse_vin_value ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw,expected", [
    (b"WV2ZZZ7HZ8H123456", "WV2ZZZ7HZ8H123456"),     # clean bytes
    ("WV2ZZZ7HZ8H123456", "WV2ZZZ7HZ8H123456"),      # str passthrough
    (b"1HGBH41JXMN109186", "1HGBH41JXMN109186"),     # another valid VIN
    (b"\x00" * 17, None),                             # all non-printable
    (b"TOOSHORT", None),                              # wrong length
    # Trailing null gets stripped by the isprintable filter → 17 valid chars → VIN
    (b"WV2ZZZ7HZ8H123456\x00", "WV2ZZZ7HZ8H123456"),
    # 16 chars + null → null stripped → 16 chars → not a valid VIN
    (b"WV2ZZZ7HZ8H12345\x00", None),
])
def test_parse_vin_value(raw: object, expected: str | None) -> None:
    assert _parse_vin_value(raw) == expected


def test_parse_vin_value_strips_nulls() -> None:
    raw = b"WV2ZZZ7HZ8H123456\x00\x00"
    # After decode + strip: "WV2ZZZ7HZ8H123456" (trailing nulls stripped by .strip())
    assert _parse_vin_value(raw) == "WV2ZZZ7HZ8H123456"


# ── resolve_vin_chain ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_chain_succeeds_on_mode09() -> None:
    """Standard connection: mode 09 returns VIN immediately."""
    conn = FakeConnection()
    await conn.connect()
    vin = await resolve_vin_chain(conn)
    assert vin == "WV2ZZZ7HZ8H123456"


@pytest.mark.asyncio
async def test_chain_falls_back_to_uds_when_mode09_null() -> None:
    """When mode 09 returns null, chain falls through to UDS F190."""
    conn = FakeNoMode09VinConnection()
    await conn.connect()
    vin = await resolve_vin_chain(conn)
    # UDS 0xF190 mock returns the same VIN
    assert vin == "WV2ZZZ7HZ8H123456"


@pytest.mark.asyncio
async def test_chain_returns_none_when_all_fail() -> None:
    """If all protocols fail, resolve_vin_chain returns None (no exception)."""

    class AllFailConnection(FakeConnection):
        async def query(self, cmd, force=False):
            import obd as _obd
            if cmd is _obd.commands.VIN:
                from obd import OBDResponse
                return OBDResponse(command=cmd, messages=[])
            return await super().query(cmd, force=force)

        async def query_uds(self, service, identifier, timeout=0.15):
            return None

        async def query_kwp_service(self, service, payload=b"", timeout=0.15):
            return None

    conn = AllFailConnection()
    await conn.connect()
    vin = await resolve_vin_chain(conn)
    assert vin is None


@pytest.mark.asyncio
async def test_chain_records_at_commands() -> None:
    """AT state hygiene: ATD is issued before each attempt."""
    conn = FakeConnection()
    await conn.connect()
    await resolve_vin_chain(conn)

    # Mode 09 step issues ATD before querying.
    assert "ATD" in conn._send_at_history


@pytest.mark.asyncio
async def test_chain_restores_protocol_after_kwp_attempt() -> None:
    """After K-line attempts, ATSP0 is issued to restore auto-detect.

    Only meaningful on K-line protocols — KWP steps are skipped on CAN to
    prevent the ELM327 from hanging on K-line fast-init with no K-line signal.
    """

    class FailMode09AndUds(FakeConnection):
        @property
        def protocol_name(self) -> str:
            return "ISO 14230-4 KWP fast"  # K-line — KWP steps must run

        async def query(self, cmd, force=False):
            import obd as _obd
            if cmd is _obd.commands.VIN:
                from obd import OBDResponse
                return OBDResponse(command=cmd, messages=[])
            return await super().query(cmd, force=force)

        async def query_uds(self, service, identifier, timeout=0.15):
            return None

    conn = FailMode09AndUds()
    await conn.connect()
    await resolve_vin_chain(conn)

    # K-line steps were attempted and protocol was restored.
    assert "ATSP3" in conn._send_at_history
    assert "ATSP0" in conn._send_at_history
    # Protocol ends in auto-detect (last ATSP command is ATSP0).
    last_atsp = next(
        (cmd for cmd in reversed(conn._send_at_history) if cmd.startswith("ATSP")),
        None,
    )
    assert last_atsp == "ATSP0"


@pytest.mark.asyncio
async def test_chain_skips_kwp_on_can() -> None:
    """KWP steps are not attempted on CAN — prevents ELM327 K-line fast-init hang."""

    class CanFailMode09AndUds(FakeConnection):
        async def query(self, cmd, force=False):
            import obd as _obd
            if cmd is _obd.commands.VIN:
                from obd import OBDResponse
                return OBDResponse(command=cmd, messages=[])
            return await super().query(cmd, force=force)

        async def query_uds(self, service, identifier, timeout=0.15):
            return None

    conn = CanFailMode09AndUds()  # protocol_name = "ISO 15765-4 (CAN 11/500)"
    await conn.connect()
    await resolve_vin_chain(conn)

    assert "ATSP3" not in conn._send_at_history
