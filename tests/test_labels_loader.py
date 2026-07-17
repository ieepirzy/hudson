"""Tests for the named-byte formula DSL in hudson_data/labels_loader.py."""

from __future__ import annotations

from hudson_data.labels_loader import _apply_formula


def test_named_byte_formula_within_a_to_h() -> None:
    # (A*256+B)/1280 with raw = [0x01, 0x00] -> (1*256+0)/1280 = 0.2
    assert _apply_formula("(A*256+B)/1280", bytes([0x01, 0x00])) == 0.2


def test_named_byte_formula_letter_past_h() -> None:
    # I is raw[8]; formula should resolve it instead of silently returning None.
    raw = bytes(range(1, 10))  # raw[8] == 9
    assert _apply_formula("I", raw) == 9.0


def test_named_byte_formula_letter_z() -> None:
    raw = bytes([0] * 25 + [42])  # raw[25] == 42 -> Z
    assert _apply_formula("Z", raw) == 42.0
