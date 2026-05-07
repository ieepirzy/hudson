"""Tests for DTC encoding/decoding.

Round-trip tests are particularly valuable here because the bit-packing
is the kind of code that's easy to get subtly wrong (off-by-one nibble
shifts, wrong system-bit position).
"""

from __future__ import annotations

import pytest

from Hudson.core.dtc import DTC, decode_dtc, decode_dtc_list, encode_dtc


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
