"""Tests for mode 07 / 0A DTC decoding, _raw_dtc_decoder, and FakeConnection simulation.

Covers the regression where _raw_dtc_decoder stripped only one header byte instead of
two, causing an odd-length payload that made decode_dtc_list raise ValueError, which
was silently swallowed — meaning pending and permanent DTCs were NEVER shown in the UI.
"""

from __future__ import annotations

import pytest

from Hudson.core.dtc import decode_dtc_list
from Hudson.tui.panes.dtcs import GET_PENDING_DTC, GET_PERMANENT_DTC, _raw_dtc_decoder


# ── _Msg helper ───────────────────────────────────────────────────────────────

class _Msg:
    """Minimal stand-in for a python-obd Message exposing .data as bytes."""

    def __init__(self, data: bytes) -> None:
        self.data = data


# ── Unit tests: _raw_dtc_decoder ─────────────────────────────────────────────

def test_raw_dtc_decoder_empty_messages() -> None:
    assert _raw_dtc_decoder([]) == b""


def test_raw_dtc_decoder_count_zero_response() -> None:
    """[0x47, 0x00] is a valid mode 07 response with zero DTCs — produces empty bytes."""
    result = _raw_dtc_decoder([_Msg(bytes([0x47, 0x00]))])
    assert result == b""


def test_raw_dtc_decoder_single_byte_message_skipped() -> None:
    """A one-byte message has no DTC data after stripping the 2-byte header."""
    result = _raw_dtc_decoder([_Msg(bytes([0x47]))])
    assert result == b""


def test_raw_dtc_decoder_strips_two_header_bytes() -> None:
    """Service echo (0x47) AND count byte are both stripped; DTC pairs remain.

    This is the regression: the old code stripped only one byte (service echo),
    leaving the count byte in the payload, producing an odd-length input that
    raised ValueError inside decode_dtc_list.
    """
    # 0x47 = service echo (0x07 + 0x40), 0x01 = DTC count, (0x03, 0x00) = P0300
    msg = _Msg(bytes([0x47, 0x01, 0x03, 0x00]))
    result = _raw_dtc_decoder([msg])
    assert result == bytes([0x03, 0x00])


def test_raw_dtc_decoder_multiple_messages_concatenated() -> None:
    """Functional broadcast (0x7DF): multiple ECUs respond as separate Message objects."""
    # ECU 1 (PCM): P0300 = (0x03, 0x00)
    msg1 = _Msg(bytes([0x47, 0x01, 0x03, 0x00]))
    # ECU 2 (ABS): C0035 = (0x40, 0x35)
    msg2 = _Msg(bytes([0x47, 0x01, 0x40, 0x35]))
    result = _raw_dtc_decoder([msg1, msg2])
    assert result == bytes([0x03, 0x00, 0x40, 0x35])


def test_raw_dtc_decoder_two_dtcs_in_one_message() -> None:
    """Multiple DTC pairs from a single ECU's response."""
    msg = _Msg(bytes([0x47, 0x02, 0x03, 0x00, 0x01, 0x71]))
    result = _raw_dtc_decoder([msg])
    assert result == bytes([0x03, 0x00, 0x01, 0x71])


# ── Integration: _raw_dtc_decoder → decode_dtc_list ──────────────────────────

def test_decode_chain_produces_correct_dtcs() -> None:
    """Full chain: _raw_dtc_decoder → decode_dtc_list gives expected codes."""
    msg = _Msg(bytes([0x47, 0x02, 0x03, 0x00, 0x01, 0x71]))
    payload = _raw_dtc_decoder([msg])
    dtcs = decode_dtc_list(payload)
    assert [d.code for d in dtcs] == ["P0300", "P0171"]


def test_decode_chain_empty_count_zero() -> None:
    """Count=0 response: after stripping, empty bytes → empty DTC list, no ValueError."""
    msg = _Msg(bytes([0x47, 0x00]))
    payload = _raw_dtc_decoder([msg])
    assert decode_dtc_list(payload) == []


def test_decode_chain_multi_ecu_broadcast() -> None:
    """Two ECUs each report one DTC — concatenation produces two codes."""
    msg1 = _Msg(bytes([0x47, 0x01, 0x03, 0x00]))  # P0300
    msg2 = _Msg(bytes([0x47, 0x01, 0x40, 0x35]))  # C0035
    payload = _raw_dtc_decoder([msg1, msg2])
    codes = [d.code for d in decode_dtc_list(payload)]
    assert "P0300" in codes
    assert "C0035" in codes


# ── FakeConnection mode 07 / 0A simulation ───────────────────────────────────

@pytest.mark.asyncio
async def test_fake_connection_pending_dtcs_non_null() -> None:
    """FakeConnection with pending_dtcs returns a non-null response for GET_PENDING_DTC."""
    from tests.fixtures.fake_connection import FakeConnection

    conn = FakeConnection(pending_dtcs=["P0420"])
    await conn.connect()

    resp = await conn.query(GET_PENDING_DTC)
    assert not resp.is_null()
    assert resp.value is not None


@pytest.mark.asyncio
async def test_fake_connection_pending_dtcs_decode_correctly() -> None:
    """Pending DTC bytes decode to the correct codes via decode_dtc_list."""
    from tests.fixtures.fake_connection import FakeConnection

    conn = FakeConnection(pending_dtcs=["P0420", "C0035"])
    await conn.connect()

    resp = await conn.query(GET_PENDING_DTC)
    assert not resp.is_null()

    dtcs = decode_dtc_list(resp.value)
    codes = [d.code for d in dtcs]
    assert "P0420" in codes
    assert "C0035" in codes


@pytest.mark.asyncio
async def test_fake_connection_empty_pending_is_non_null() -> None:
    """FakeConnection with no pending DTCs returns non-null response (not 'no response')."""
    from tests.fixtures.fake_connection import FakeConnection

    conn = FakeConnection(pending_dtcs=[])
    await conn.connect()

    resp = await conn.query(GET_PENDING_DTC)
    assert not resp.is_null()
    assert resp.value == b""
    assert decode_dtc_list(resp.value) == []


@pytest.mark.asyncio
async def test_fake_connection_default_no_pending() -> None:
    """Default FakeConnection (no pending_dtcs arg) has empty pending DTC list."""
    from tests.fixtures.fake_connection import FakeConnection

    conn = FakeConnection()
    await conn.connect()

    resp = await conn.query(GET_PENDING_DTC)
    assert not resp.is_null()
    assert decode_dtc_list(resp.value) == []


@pytest.mark.asyncio
async def test_fake_connection_permanent_dtcs_decode_correctly() -> None:
    """Permanent DTC bytes decode to the correct code via decode_dtc_list."""
    from tests.fixtures.fake_connection import FakeConnection

    conn = FakeConnection(permanent_dtcs=["P0300"])
    await conn.connect()

    resp = await conn.query(GET_PERMANENT_DTC)
    assert not resp.is_null()

    dtcs = decode_dtc_list(resp.value)
    assert [d.code for d in dtcs] == ["P0300"]


@pytest.mark.asyncio
async def test_fake_connection_multiple_permanent_dtcs() -> None:
    """Multiple permanent DTCs are all returned and correctly encoded."""
    from tests.fixtures.fake_connection import FakeConnection

    conn = FakeConnection(permanent_dtcs=["P0171", "B0100", "U0100"])
    await conn.connect()

    resp = await conn.query(GET_PERMANENT_DTC)
    dtcs = decode_dtc_list(resp.value)
    codes = [d.code for d in dtcs]
    assert "P0171" in codes
    assert "B0100" in codes
    assert "U0100" in codes


# ── DtcPane regression: pending DTC reaches the DataTable ────────────────────

@pytest.mark.asyncio
async def test_do_scan_pending_dtc_appears_in_table() -> None:
    """Regression: a pending DTC from mode 07 must appear in the DataTable with status 'Pending'.

    Before the fix, _raw_dtc_decoder stripped only 1 byte (service echo) instead of
    2 (service echo + count byte). The leftover count byte made the payload odd-length,
    decode_dtc_list raised ValueError, the exception was silently swallowed, and the
    DataTable never received any pending DTC rows.
    """
    from textual.app import App, ComposeResult
    from textual.widgets import DataTable

    from Hudson.core.init import InitResult
    from Hudson.tui.panes.dtcs import DtcPane
    from tests.fixtures.fake_connection import FakeConnection

    conn = FakeConnection(pending_dtcs=["P0420"])
    init = InitResult()

    class _TestApp(App):
        def compose(self) -> ComposeResult:
            yield DtcPane(conn, init)

    app = _TestApp()
    async with app.run_test() as pilot:
        await pilot.pause(1.5)

        table = app.query_one(DataTable)
        pending_rows = []
        for row_key in table.rows:
            try:
                status = table.get_cell(row_key, "status")
                code = table.get_cell(row_key, "code")
                if status == "Pending":
                    pending_rows.append(code)
            except Exception:
                pass

        assert "P0420" in pending_rows, (
            f"P0420 not found in pending rows. Pending rows: {pending_rows}"
        )
