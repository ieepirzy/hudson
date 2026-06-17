"""ISO-TP (ISO 15765-2) frame reassembly.

When the ELM327 is in CAN Auto Formatting mode (ATCAF1, the default), it
reassembles ISO-TP for us and we never need this module. We only reach
for it when:

  1. We've explicitly turned auto-formatting off (ATCAF0) for debugging.
  2. We're doing custom UDS work where the auto-reassembly layer mangles
     responses we want to inspect frame-by-frame.
  3. We're talking to a transport that doesn't do reassembly (raw SocketCAN).

The four frame types, identified by the high nibble of byte 0:

    0x0X  Single Frame (SF)    - whole message in one CAN frame
    0x1X  First Frame (FF)     - start of multi-frame message
    0x2X  Consecutive Frame    - continuation
    0x3X  Flow Control (FC)    - receiver pacing the sender

This module currently handles inbound reassembly only. Outbound segmentation
(needed if we ever bypass the ELM327 entirely) is left for later.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum


class FrameType(IntEnum):
    SINGLE = 0x0
    FIRST = 0x1
    CONSECUTIVE = 0x2
    FLOW_CONTROL = 0x3


class IsoTpError(Exception):
    """Raised on any ISO-TP framing error."""


@dataclass(slots=True)
class Reassembler:
    """Stateful inbound ISO-TP reassembler for a single source.

    Feed CAN frame payloads (the 8 data bytes, no CAN ID) one at a time.
    `feed()` returns a complete reassembled message when one is finished,
    or `None` while a multi-frame transfer is still in progress.
    """

    _expected_length: int | None = None
    _next_seq: int = 1
    _buffer: bytearray = field(default_factory=bytearray)

    def reset(self) -> None:
        self._expected_length = None
        self._next_seq = 1
        self._buffer.clear()

    def feed(self, frame: bytes) -> bytes | None:
        if len(frame) < 1:
            raise IsoTpError("empty frame")

        frame_type = (frame[0] >> 4) & 0x0F

        if frame_type == FrameType.SINGLE:
            return self._handle_single(frame)
        if frame_type == FrameType.FIRST:
            self._handle_first(frame)
            return None
        if frame_type == FrameType.CONSECUTIVE:
            return self._handle_consecutive(frame)
        if frame_type == FrameType.FLOW_CONTROL:
            # We never receive FCs as the requester in OBD2 reads. Ignore.
            return None
        raise IsoTpError(f"unknown frame type: {frame_type:#x}")

    def _handle_single(self, frame: bytes) -> bytes:
        length = frame[0] & 0x0F
        if length > 7:
            raise IsoTpError(f"single frame length out of range: {length}")
        if len(frame) < 1 + length:
            raise IsoTpError("single frame shorter than declared length")
        self.reset()
        return bytes(frame[1 : 1 + length])

    def _handle_first(self, frame: bytes) -> None:
        if len(frame) < 8:
            raise IsoTpError("first frame must be 8 bytes")
        # 12-bit length: low nibble of byte 0 + all of byte 1
        self._expected_length = ((frame[0] & 0x0F) << 8) | frame[1]
        if self._expected_length < 8:
            raise IsoTpError(
                f"first frame length {self._expected_length} would fit in single frame"
            )
        self._next_seq = 1
        self._buffer = bytearray(frame[2:8])  # 6 data bytes

    def _handle_consecutive(self, frame: bytes) -> bytes | None:
        if self._expected_length is None:
            raise IsoTpError("consecutive frame without first frame")
        seq = frame[0] & 0x0F
        if seq != self._next_seq:
            self.reset()
            raise IsoTpError(
                f"out-of-order CF: expected seq {self._next_seq}, got {seq}"
            )
        self._next_seq = (self._next_seq + 1) & 0x0F
        remaining = self._expected_length - len(self._buffer)
        take = min(7, remaining)
        self._buffer.extend(frame[1 : 1 + take])

        if len(self._buffer) >= self._expected_length:
            result = bytes(self._buffer[: self._expected_length])
            self.reset()
            return result
        return None
