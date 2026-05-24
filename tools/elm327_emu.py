#!/usr/bin/env python3
"""ELM327 ECU emulator on a pseudo-terminal.

Creates a PTY, prints the slave device path, then emulates an ELM327 adapter
backed by a configurable ECU profile. All AT commands python-obd sends during
init are handled; UDS priority-1 discovery responds instantly (no 150 ms
timeout per identifier).

Usage:
    python tools/elm327_emu.py [--vin VW2ZZZ7HZ8H123456] [--protocol 6]
    # In a second terminal:
    hudson --port /dev/pts/N   (N is printed by the emulator)

The default profile mirrors FakeConnection's VW Passat data so the two
test paths stay consistent.
"""

from __future__ import annotations

import argparse
import os
import pty
import select
import sys
from dataclasses import dataclass, field


# ── ECU profile ───────────────────────────────────────────────────────────────

@dataclass
class EcuProfile:
    vin: str = "WV2ZZZ7HZ8H123456"
    ecu_can_id: int = 0x7E8
    protocol_name: str = "ISO 15765-4 (CAN 11/500)"
    # "6" is what ATDPN returns and what python-obd maps to ISO_15765_4_11bit_500k.
    protocol_number: str = "6"

    # Mode 01 supported-PID bitmasks (same values as a real VW Passat / FakeConnection).
    # Bit 0 of each word = "next range available" flag, which prompts python-obd
    # to continue querying 0120 / 0140.
    supported_01_20: int = 0xBE3FB811
    supported_21_40: int = 0x80018001
    supported_41_60: int = 0x44000000

    # Mode 01 PID → raw response bytes (the bytes AFTER "41 XX" in the OBD response).
    mode01: dict[int, bytes] = field(default_factory=lambda: {
        0x04: bytes([0x4D]),              # engine load ~30 %
        0x05: bytes([0x7D]),              # coolant 85 °C
        0x0C: bytes([0x17, 0x70]),        # RPM 1500
        0x0D: bytes([0x28]),              # speed 40 km/h
        0x0F: bytes([0x4B]),              # intake temp 35 °C
        0x11: bytes([0x26]),              # throttle ~15 %
    })

    # UDS identifier → payload bytes (what follows "62 HH LL" in a positive response).
    uds: dict[int, bytes] = field(default_factory=lambda: {
        0xF189: b"0001\x00",
        0xF190: b"WV2ZZZ7HZ8H123456",
        0xF400: bytes([0x00, 0x0F, 0x00, 0x00]),
        0xF401: bytes([0x00, 0x12, 0x00, 0x00]),
        0xF40B: bytes([0x01, 0x2C]),
        0xF40C: bytes([0x01, 0x40]),
    })


# ── ISO-TP frame encoder ──────────────────────────────────────────────────────

def _isotp_frames(payload: bytes) -> list[bytes]:
    """Encode *payload* into a list of ISO-TP CAN frame data blobs (8 bytes each)."""
    n = len(payload)
    if n <= 7:
        return [(bytes([n]) + payload).ljust(8, b"\x00")]

    frames: list[bytes] = []
    # First frame: 2-byte PCI (0x1H 0xLL where H:L = 12-bit length) + 6 data bytes.
    frames.append(
        (bytes([0x10 | ((n >> 8) & 0x0F), n & 0xFF]) + payload[:6]).ljust(8, b"\x00")
    )
    idx, sn = 6, 1
    while idx < n:
        frames.append(
            (bytes([0x20 | (sn & 0x0F)]) + payload[idx : idx + 7]).ljust(8, b"\x00")
        )
        idx += 7
        sn += 1
    return frames


# ── ELM327 command emulator ───────────────────────────────────────────────────

class ELM327Emulator:
    """Stateful ELM327 command/response processor."""

    def __init__(self, profile: EcuProfile) -> None:
        self.profile = profile
        self._echo = True
        self._headers = False
        self._linefeeds = True
        self._buf = b""

    # ── response helpers ──────────────────────────────────────────────────────

    def _eol(self) -> bytes:
        return b"\r\n" if self._linefeeds else b"\r"

    def _simple(self, text: str) -> bytes:
        """Single-line response ending with the ELM prompt."""
        return text.encode() + self._eol() + b">"

    def _can_lines(self, payload: bytes) -> bytes:
        """Format ISO-TP frames as ELM327 response (CAN header optional)."""
        frames = _isotp_frames(payload)
        lines: list[bytes] = []
        for f in frames:
            hex_data = " ".join(f"{b:02X}" for b in f)
            if self._headers:
                lines.append(f"{self.profile.ecu_can_id:03X} {hex_data}".encode())
            else:
                lines.append(hex_data.encode())
        return self._eol().join(lines) + self._eol() + b">"

    # ── command dispatch ──────────────────────────────────────────────────────

    def process(self, raw: bytes) -> bytes:
        """Process one command line (without trailing \\r), return response bytes."""
        stripped = raw.strip(b"\r\n")
        echo = (stripped + self._eol()) if self._echo else b""
        return echo + self._dispatch(stripped)

    def _dispatch(self, raw: bytes) -> bytes:
        # Try ASCII decode first — covers AT commands and ASCII-hex OBD queries.
        try:
            cmd = raw.decode("ascii").strip().upper()
        except UnicodeDecodeError:
            # python-obd sends binary bytes for custom OBDCommands (e.g. query_uds
            # creates OBDCommand with bytes([0x22, hi, lo])).  Pass them directly.
            return self._obd(raw)

        if cmd.startswith("AT"):
            return self._at(cmd[2:].strip())

        try:
            data = bytes.fromhex(cmd.replace(" ", ""))
        except ValueError:
            return self._simple("?")
        return self._obd(data)

    # ── AT command handler ────────────────────────────────────────────────────

    def _at(self, sub: str) -> bytes:
        # ── state-changing commands ───────────────────────────────────────────
        if sub == "Z":
            self._echo = True
            self._headers = False
            self._linefeeds = True
            # ELM327 reset response includes a blank line then the version banner.
            return b"\r\nELM327 v1.5" + self._eol() + b">"
        if sub == "E0":
            self._echo = False
            return self._simple("OK")
        if sub == "E1":
            self._echo = True
            return self._simple("OK")
        if sub == "H0":
            self._headers = False
            return self._simple("OK")
        if sub == "H1":
            self._headers = True
            return self._simple("OK")
        if sub == "L0":
            # Turn off linefeeds.  Set the flag BEFORE generating the response
            # so the ATL0 acknowledgement itself already uses \r — python-obd
            # splits on both \r and \n anyway, so either works here.
            self._linefeeds = False
            return self._simple("OK")
        if sub == "L1":
            self._linefeeds = True
            return self._simple("OK")
        # ── information queries ───────────────────────────────────────────────
        if sub in ("DP",):
            return self._simple(self.profile.protocol_name)
        if sub in ("DPN",):
            # Return just the protocol number.  python-obd strips a leading "A"
            # (auto-detected) if present, so either "6" or "A6" is fine.
            return self._simple(self.profile.protocol_number)
        if sub in ("RV", "RV "):
            return self._simple("12.0V")
        if sub in ("I", "@1", "@2"):
            return self._simple("ELM327 v1.5")
        # ── everything else: OK (covers SP, TP, AT, ST, CAF, SH, FC, …) ─────
        return self._simple("OK")

    # ── OBD / UDS request handlers ────────────────────────────────────────────

    def _obd(self, data: bytes) -> bytes:
        if not data:
            return self._simple("?")
        svc = data[0]

        # Mode 09 PID 02 — VIN (multi-frame)
        if svc == 0x09 and len(data) >= 2 and data[1] == 0x02:
            vin = self.profile.vin.encode("ascii")
            return self._can_lines(bytes([0x49, 0x02, 0x01]) + vin)

        # Mode 09 PID 00 — supported mode-09 PIDs (VIN = PID 02)
        if svc == 0x09 and len(data) >= 2 and data[1] == 0x00:
            return self._can_lines(bytes([0x49, 0x00, 0x40, 0x00, 0x00, 0x00]))

        # Mode 01 — current data
        if svc == 0x01 and len(data) >= 2:
            return self._mode01(data[1])

        # Mode 03 — stored DTCs (empty list)
        if svc == 0x03:
            return self._can_lines(bytes([0x43, 0x00]))

        # Mode 04 — clear DTCs
        if svc == 0x04:
            return self._can_lines(bytes([0x44]))

        # UDS 0x22 — ReadDataByIdentifier
        if svc == 0x22 and len(data) >= 3:
            return self._uds22((data[1] << 8) | data[2])

        # UDS 0x3E — TesterPresent keepalive
        if svc == 0x3E:
            return self._can_lines(bytes([0x7E, 0x00]))

        return self._simple("NO DATA")

    def _mode01(self, pid: int) -> bytes:
        # Supported-PID bitmask queries
        for supported_pid, bitmask in (
            (0x00, self.profile.supported_01_20),
            (0x20, self.profile.supported_21_40),
            (0x40, self.profile.supported_41_60),
        ):
            if pid == supported_pid:
                v = bitmask
                payload = bytes(
                    [0x41, pid, (v >> 24) & 0xFF, (v >> 16) & 0xFF, (v >> 8) & 0xFF, v & 0xFF]
                )
                return self._can_lines(payload)

        data = self.profile.mode01.get(pid)
        if data is None:
            return self._simple("NO DATA")
        return self._can_lines(bytes([0x41, pid]) + data)

    def _uds22(self, identifier: int) -> bytes:
        data = self.profile.uds.get(identifier)
        if data is None:
            # No response → ELM327 times out and returns "NO DATA".
            # Returning it immediately avoids the 150 ms per-identifier wait
            # during the priority-1 discovery sweep (~1024 probes).
            return self._simple("NO DATA")
        hi, lo = (identifier >> 8) & 0xFF, identifier & 0xFF
        return self._can_lines(bytes([0x62, hi, lo]) + data)

    # ── PTY feed loop ─────────────────────────────────────────────────────────

    def feed(self, chunk: bytes) -> bytes:
        """Accumulate *chunk*, process complete CR-terminated lines, return responses."""
        self._buf += chunk
        out = b""
        while b"\r" in self._buf:
            line, self._buf = self._buf.split(b"\r", 1)
            line = line.strip(b"\n")
            if line:
                out += self.process(line)
        return out

    def run(self, master_fd: int) -> None:
        """Event loop: read from *master_fd*, write responses back."""
        while True:
            r, _, _ = select.select([master_fd], [], [], 1.0)
            if master_fd not in r:
                continue
            try:
                chunk = os.read(master_fd, 512)
            except OSError:
                break
            out = self.feed(chunk)
            if out:
                try:
                    os.write(master_fd, out)
                except OSError:
                    break


# ── entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="ELM327 ECU emulator on a PTY for Hudson integration testing"
    )
    parser.add_argument("--vin", default="WV2ZZZ7HZ8H123456", help="Simulated VIN (17 chars)")
    parser.add_argument(
        "--protocol",
        default="6",
        choices=list("123456789A"),
        help="ELM327 protocol number returned by ATDPN (default: 6 = CAN 11-bit 500k)",
    )
    args = parser.parse_args()

    if len(args.vin) != 17:
        print(f"error: VIN must be exactly 17 characters, got {len(args.vin)}", file=sys.stderr)
        sys.exit(1)

    profile = EcuProfile(vin=args.vin, protocol_number=args.protocol)
    emu = ELM327Emulator(profile)

    master, slave = pty.openpty()
    slave_name = os.ttyname(slave)

    print(f"ELM327 emulator ready  (VIN: {profile.vin})", flush=True)
    print(f"PTY slave : {slave_name}", flush=True)
    print(f"Run       : hudson --port {slave_name}", flush=True)
    print("Press Ctrl+C to stop.", flush=True)

    try:
        emu.run(master)
    except KeyboardInterrupt:
        print("\nStopped.", flush=True)
    finally:
        os.close(master)
        os.close(slave)


if __name__ == "__main__":
    main()
