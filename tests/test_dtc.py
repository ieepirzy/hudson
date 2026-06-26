"""Tests for DTC encoding/decoding.

Round-trip tests are particularly valuable here because the bit-packing
is the kind of code that's easy to get subtly wrong (off-by-one nibble
shifts, wrong system-bit position).
"""

from __future__ import annotations

import pytest

from Hudson.core.dtc import (
    DTC,
    DtcRecord,
    DtcStatus,
    decode_dtc,
    decode_dtc_list,
    decode_kwp_dtc_list,
    decode_uds_dtc_list,
    encode_dtc,
)


@pytest.mark.parametrize(
    ("byte_a", "byte_b", "expected"),
    [
        # P0133 - the canonical Wikipedia example
        (0x01, 0x33, "P0133"),
        # P0000 is filler / no code
        # (handled separately in the None test below)
        # Top of P-range
        (0x00, 0x01, "P0001"),
        # First C code
        (0x40, 0x00, "C0000"),
        # First B code
        (0x80, 0x00, "B0000"),
        # First U code
        (0xC0, 0x00, "U0000"),
        # P3xxx
        (0x30, 0x00, "P3000"),
        # P10A0 (touches all the nibbles)
        (0x10, 0xA0, "P10A0"),
        # Last possible P code
        (0x3F, 0xFF, "P3FFF"),
    ],
)
def test_decode_known_codes(byte_a: int, byte_b: int, expected: str) -> None:
    decoded = decode_dtc(byte_a, byte_b)
    assert decoded is not None
    assert decoded.code == expected


def test_decode_zero_bytes_returns_none() -> None:
    """0x00 0x00 is the conventional 'no code' filler."""
    assert decode_dtc(0x00, 0x00) is None


@pytest.mark.parametrize(
    "code",
    ["P0133", "P0001", "C0000", "B0000", "U0000", "P3000", "P10A0", "P3FFF"],
)
def test_encode_decode_roundtrip(code: str) -> None:
    a, b = encode_dtc(code)
    decoded = decode_dtc(a, b)
    assert decoded is not None
    assert decoded.code == code


def test_decode_dtc_list_strips_trailing_zeros() -> None:
    # Two real codes, then padding
    payload = bytes([0x01, 0x33, 0x01, 0x71, 0x00, 0x00, 0x00, 0x00])
    codes = decode_dtc_list(payload)
    assert [c.code for c in codes] == ["P0133", "P0171"]


def test_decode_dtc_list_rejects_odd_length() -> None:
    with pytest.raises(ValueError):
        decode_dtc_list(b"\x01\x33\x01")


def test_dtc_classification() -> None:
    p0133 = DTC("P0133")
    p1296 = DTC("P1296")  # VW-specific
    assert p0133.system == "Powertrain"
    assert not p0133.is_manufacturer_specific
    assert p1296.is_manufacturer_specific


def test_invalid_letter_rejected_by_encoder() -> None:
    with pytest.raises(ValueError):
        encode_dtc("X0000")


def test_invalid_first_digit_rejected() -> None:
    with pytest.raises(ValueError):
        encode_dtc("P4000")  # only 0-3 valid for the first digit


# ── DtcStatus tests ──────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    ("raw", "expected_flags"),
    [
        (0x00, "—"),
        (0x01, "T"),           # testFailed only
        (0x04, "P"),           # pending only
        (0x08, "C"),           # confirmed only
        (0x80, "M"),           # MIL only
        (0x8C, "PCM"),         # pending + confirmed + MIL (no testFailed)
        (0x09, "TC"),          # testFailed + confirmed
        (0xFF, "TPCM"),        # all relevant flags
    ],
)
def test_dtc_status_flags_str(raw: int, expected_flags: str) -> None:
    assert DtcStatus(raw=raw).flags_str() == expected_flags


def test_dtc_status_individual_bits() -> None:
    s = DtcStatus(raw=0xFF)
    assert s.test_failed
    assert s.test_failed_this_op_cycle
    assert s.pending
    assert s.confirmed
    assert s.test_not_completed_since_clear
    assert s.test_failed_since_clear
    assert s.test_not_completed_this_op_cycle
    assert s.mil_on


def test_dtc_status_all_clear() -> None:
    s = DtcStatus(raw=0x00)
    assert not s.test_failed
    assert not s.pending
    assert not s.confirmed
    assert not s.mil_on


# ── decode_uds_dtc_list tests ─────────────────────────────────────────────────

def test_decode_uds_dtc_list_basic() -> None:
    # P0300 = (0x03, 0x00), P0171 = (0x01, 0x71)
    payload = bytes([
        0x03, 0x00, 0x00, 0x8C,  # P0300 — confirmed + pending + MIL
        0x01, 0x71, 0x00, 0x09,  # P0171 — testFailed + confirmed
    ])
    records = decode_uds_dtc_list(payload)
    assert len(records) == 2
    assert records[0].dtc.code == "P0300"
    assert records[0].status.raw == 0x8C
    assert records[0].failure_type == 0x00
    assert records[1].dtc.code == "P0171"
    assert records[1].status.raw == 0x09


def test_decode_uds_dtc_list_empty() -> None:
    assert decode_uds_dtc_list(b"") == []


def test_decode_uds_dtc_list_skips_null_pair() -> None:
    # A 4-byte record where hi=0 mid=0 (null DTC) should be skipped
    payload = bytes([0x00, 0x00, 0x00, 0x00, 0x03, 0x00, 0x00, 0x8C])
    records = decode_uds_dtc_list(payload)
    assert len(records) == 1
    assert records[0].dtc.code == "P0300"


def test_decode_uds_dtc_list_truncated_trailing_ignored() -> None:
    # 5 bytes — only one full 4-byte record, trailing byte ignored
    payload = bytes([0x03, 0x00, 0x00, 0x8C, 0xFF])
    records = decode_uds_dtc_list(payload)
    assert len(records) == 1


def test_decode_uds_dtc_list_failure_type_preserved() -> None:
    # lo byte = 0x23 (manufacturer failure type)
    payload = bytes([0x03, 0x00, 0x23, 0x09])
    records = decode_uds_dtc_list(payload)
    assert len(records) == 1
    assert records[0].failure_type == 0x23


# ── decode_kwp_dtc_list tests ─────────────────────────────────────────────────

def test_decode_kwp_dtc_list_basic() -> None:
    # P0401 = encode_dtc("P0401") = (0x04, 0x01)
    payload = bytes([
        0x04, 0x01, 0x08,  # P0401 — confirmed
        0x04, 0x02, 0x04,  # P0402 — pending
    ])
    records = decode_kwp_dtc_list(payload)
    assert len(records) == 2
    assert records[0].dtc.code == "P0401"
    assert records[0].status.confirmed
    assert not records[0].status.pending
    assert records[1].dtc.code == "P0402"
    assert records[1].status.pending


def test_decode_kwp_dtc_list_empty() -> None:
    assert decode_kwp_dtc_list(b"") == []


def test_decode_kwp_dtc_list_truncated_trailing_ignored() -> None:
    # 4 bytes — one full 3-byte record, trailing byte ignored
    payload = bytes([0x04, 0x01, 0x08, 0xFF])
    records = decode_kwp_dtc_list(payload)
    assert len(records) == 1


def test_decode_kwp_dtc_list_skips_null_pair() -> None:
    payload = bytes([0x00, 0x00, 0x00, 0x04, 0x01, 0x08])
    records = decode_kwp_dtc_list(payload)
    assert len(records) == 1
    assert records[0].dtc.code == "P0401"


# ── DtcRecord tests ───────────────────────────────────────────────────────────

def test_dtc_record_default_failure_type() -> None:
    dtc = DTC(code="P0300")
    status = DtcStatus(raw=0x08)
    record = DtcRecord(dtc=dtc, status=status)
    assert record.failure_type == 0
