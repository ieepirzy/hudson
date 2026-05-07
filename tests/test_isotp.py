"""Tests for ISO-TP inbound reassembly.

The VIN response example from the protocol primer is the gold-standard
end-to-end test. Anything that can reassemble that correctly will handle
DTCs and UDS reads too.
"""

from __future__ import annotations

import pytest

from Hudson.core.isotp import IsoTpError, Reassembler


def test_single_frame_returns_immediately() -> None:
    r = Reassembler()
    # SF, 3 bytes payload, mode 01 PID 0C response = "41 0C 1A F8" (4 bytes actually)
    # Let's use a real 3-byte SF: speed response "41 0D 32"
    frame = bytes([0x03, 0x41, 0x0D, 0x32, 0x00, 0x00, 0x00, 0x00])
    out = r.feed(frame)
    assert out == bytes([0x41, 0x0D, 0x32])


def test_vin_multiframe_reassembly() -> None:
    """Mode 09 PID 02 VIN response: 20-byte payload across FF + 2 CFs.

    6 (FF) + 7 (CF1) + 7 (CF2) = 20 bytes exactly. CF3 with padding only
    appears if the last CF would otherwise be empty — not the case here.
    """
    r = Reassembler()

    # First Frame: total length 20 (0x014), first 6 data bytes
    ff = bytes([0x10, 0x14, 0x49, 0x02, 0x01, 0x31, 0x56, 0x57])
    assert r.feed(ff) is None

    # CF1, seq 1, 7 data bytes
    cf1 = bytes([0x21, 0x31, 0x5A, 0x5A, 0x5A, 0x37, 0x37, 0x5A])
    assert r.feed(cf1) is None

    # CF2, seq 2, 7 data bytes — this completes the 20-byte transfer
    cf2 = bytes([0x22, 0x38, 0x35, 0x30, 0x30, 0x30, 0x30, 0x30])
    out = r.feed(cf2)

    assert out is not None
    assert len(out) == 20
    assert out[:3] == bytes([0x49, 0x02, 0x01])  # mode 09 echo + VIN count
    # The remaining 17 bytes are the ASCII VIN
    vin = out[3:].decode("ascii")
    assert vin == "1VW1ZZZ77Z8500000"


def test_consecutive_frame_without_first_raises() -> None:
    r = Reassembler()
    cf = bytes([0x21, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00])
    with pytest.raises(IsoTpError, match="without first frame"):
        r.feed(cf)


def test_out_of_order_consecutive_raises() -> None:
    r = Reassembler()
    ff = bytes([0x10, 0x14, 0x49, 0x02, 0x01, 0x31, 0x56, 0x57])
    r.feed(ff)
    # Skip seq 1, send seq 2 directly
    cf2 = bytes([0x22, 0x38, 0x35, 0x30, 0x30, 0x30, 0x30, 0x30])
    with pytest.raises(IsoTpError, match="out-of-order"):
        r.feed(cf2)


def test_reset_after_complete_message() -> None:
    """After a full reassembly, the reassembler should be ready for the next message."""
    r = Reassembler()
    # Single frame, then another single frame
    sf1 = bytes([0x03, 0x41, 0x0D, 0x32, 0x00, 0x00, 0x00, 0x00])
    sf2 = bytes([0x04, 0x41, 0x0C, 0x1A, 0xF8, 0x00, 0x00, 0x00])
    r.feed(sf1)
    out = r.feed(sf2)
    assert out == bytes([0x41, 0x0C, 0x1A, 0xF8])
