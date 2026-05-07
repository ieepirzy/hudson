"""DTC (Diagnostic Trouble Code) encoding and decoding per SAE J2012.

A DTC is a 2-byte value with the following bit layout:

    [SS GG | TTTT TTTT TTTT]
     |  |    |
     |  |    └── 12 bits, last three hex digits of the code
     |  └─────── 2 bits, first digit (0-3)
     └────────── 2 bits, system letter (P/C/B/U)

The system letter mapping:

    00 -> P (Powertrain)
    01 -> C (Chassis)
    10 -> B (Body)
    11 -> U (Network)

So the bytes 0x01 0x33 decode to "P0133":
    0x01 = 0000 0001
            ^^         -> 00 -> P
              ^^       -> 00 -> 0
                  0001 -> first nibble of the 3-hex-digit tail
    0x33 = 0011 0011 -> 33 -> tail "33"
    full tail: "133"
    -> P0133
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

_SYSTEM_LETTERS: Final[dict[int, str]] = {
    0b00: "P",
    0b01: "C",
    0b10: "B",
    0b11: "U",
}


@dataclass(frozen=True, slots=True)
class DTC:
    """A single Diagnostic Trouble Code."""

    code: str  # canonical form, e.g. "P0133"

    @property
    def system(self) -> str:
        """Powertrain / Chassis / Body / Network."""
        return {
            "P": "Powertrain",
            "C": "Chassis",
            "B": "Body",
            "U": "Network",
        }[self.code[0]]

    @property
    def is_manufacturer_specific(self) -> bool:
        """P1xxx and P3xxx are manufacturer-defined; P0xxx and P2xxx are SAE-defined."""
        if self.code[0] != "P":
            # For C/B/U, the convention varies; we conservatively call non-P
            # system codes generic and let the manufacturer layer override.
            return False
        return self.code[1] in ("1", "3")

    def __str__(self) -> str:
        return self.code


def decode_dtc(byte_a: int, byte_b: int) -> DTC | None:
    """Decode a 2-byte DTC payload to a `DTC` object.

    Returns `None` if both bytes are zero, which is the conventional
    "no code" filler in fixed-width DTC list responses.
    """
    if byte_a == 0 and byte_b == 0:
        return None
    if not (0 <= byte_a <= 0xFF and 0 <= byte_b <= 0xFF):
        raise ValueError(f"DTC bytes out of range: {byte_a:#04x} {byte_b:#04x}")

    system_bits = (byte_a >> 6) & 0b11
    first_digit = (byte_a >> 4) & 0b11
    tail_high = byte_a & 0x0F
    tail_low = byte_b

    letter = _SYSTEM_LETTERS[system_bits]
    code = f"{letter}{first_digit}{tail_high:X}{tail_low:02X}"
    return DTC(code=code)


def decode_dtc_list(payload: bytes) -> list[DTC]:
    """Decode a sequence of DTC pairs from a mode 03/07/0A response payload.

    The payload here is the *data portion* after the mode echo byte
    (e.g. for mode 03, after the leading 0x43). Each DTC is 2 bytes.
    Trailing zero pairs are stripped.
    """
    if len(payload) % 2 != 0:
        raise ValueError(f"DTC payload length must be even, got {len(payload)}")

    codes: list[DTC] = []
    for i in range(0, len(payload), 2):
        dtc = decode_dtc(payload[i], payload[i + 1])
        if dtc is not None:
            codes.append(dtc)
    return codes


def encode_dtc(code: str) -> tuple[int, int]:
    """Inverse of `decode_dtc` — useful for tests and round-trip validation."""
    if len(code) != 5:
        raise ValueError(f"DTC code must be 5 chars, got {code!r}")
    letter = code[0]
    if letter not in "PCBU":
        raise ValueError(f"DTC system letter must be P/C/B/U, got {letter!r}")
    if code[1] not in "0123":
        raise ValueError(f"DTC first digit must be 0-3, got {code[1]!r}")

    system_bits = {v: k for k, v in _SYSTEM_LETTERS.items()}[letter]
    first_digit = int(code[1])
    tail_high = int(code[2], 16)
    tail_low = int(code[3:5], 16)

    byte_a = (system_bits << 6) | (first_digit << 4) | tail_high
    byte_b = tail_low
    return byte_a, byte_b
