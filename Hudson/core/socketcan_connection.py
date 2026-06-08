"""Direct SocketCAN connection — no ELM327 required.

Implements the same public interface as ObdConnection but drives the CAN bus
directly via python-can + the ISO-TP transport layer (can-isotp).  Intended
for use with virtual CAN interfaces (vcan0) and the fake_ecu.py test harness.

Usage (via Hudson CLI):
    hudson --vcan vcan0

Architecture:
    asyncio.Lock serialises all CAN I/O.
    Each query creates a fresh isotp.CanStack bound to the current tx/rx pair,
    sends the payload, and polls process() until a response arrives or the
    request times out (default 2 s).  Creating a new stack per query is
    intentional: it avoids stale partial-reassembly state across retries.

send_at() handling:
    AT commands have no meaning on a raw CAN bus.  ATSH <hex> is parsed to
    update the transmit/receive address pair so VIN-chain step 2 (UDS F190 to
    the gateway at 0x7D9) works without changes to vin.py.  All other AT
    commands return "OK" immediately.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

import can
import isotp
import obd

if TYPE_CHECKING:
    from obd import OBDCommand

log = logging.getLogger(__name__)

# ── J1979 mode-01 decoder table ───────────────────────────────────────────────
# Maps PID → (decode_fn, unit_string).
# decode_fn(data: bytes) → float  (data = bytes after "41 XX" service echo)

_D = {
    0x04: (lambda d: d[0] * 100 / 255,                    "percent"),   # ENGINE_LOAD
    0x05: (lambda d: d[0] - 40,                            "degC"),      # COOLANT_TEMP
    0x06: (lambda d: (d[0] - 128) * 100 / 128,            "percent"),   # SHORT_FUEL_TRIM_1
    0x07: (lambda d: (d[0] - 128) * 100 / 128,            "percent"),   # LONG_FUEL_TRIM_1
    0x0B: (lambda d: d[0],                                 "kilopascal"), # INTAKE_PRESSURE
    0x0C: (lambda d: ((d[0] << 8) | d[1]) / 4,            "rpm"),       # RPM
    0x0D: (lambda d: d[0],                                 "kph"),       # SPEED
    0x0E: (lambda d: d[0] / 2 - 64,                        "degree"),    # TIMING_ADVANCE
    0x0F: (lambda d: d[0] - 40,                            "degC"),      # INTAKE_TEMP
    0x10: (lambda d: ((d[0] << 8) | d[1]) / 100,          "gps"),       # MAF
    0x11: (lambda d: d[0] * 100 / 255,                    "percent"),   # THROTTLE_POS
    0x1C: (lambda d: float(d[0]),                          ""),          # OBD_COMPLIANCE
    0x1F: (lambda d: (d[0] << 8) | d[1],                  "second"),    # RUN_TIME
    0x22: (lambda d: ((d[0] << 8) | d[1]) * 10,           "kilopascal"), # FUEL_RAIL_PRESSURE_VAC
    0x23: (lambda d: ((d[0] << 8) | d[1]) * 10,           "kilopascal"), # FUEL_RAIL_PRESSURE_DIRECT
    0x24: (lambda d: ((d[0] << 8) | d[1]) * 2 / 65535,   "ratio"),     # O2_S1_WR_VOLTAGE
    0x2C: (lambda d: d[0] * 100 / 255,                    "percent"),   # COMMANDED_EGR
    0x2D: (lambda d: (d[0] - 128) * 100 / 128,            "percent"),   # EGR_ERROR
    0x2E: (lambda d: d[0] * 100 / 255,                    "percent"),   # EVAPORATIVE_PURGE
    0x2F: (lambda d: d[0] * 100 / 255,                    "percent"),   # FUEL_LEVEL
    0x30: (lambda d: float(d[0]),                          "count"),     # WARMUPS_SINCE_DTC_CLEAR
    0x33: (lambda d: d[0],                                 "kilopascal"), # BAROMETRIC_PRESSURE
    0x3C: (lambda d: ((d[0] << 8) | d[1]) / 10 - 40,     "degC"),      # CATALYST_TEMP_B1S1
    0x3E: (lambda d: ((d[0] << 8) | d[1]) / 10 - 40,     "degC"),      # CATALYST_TEMP_B2S1
    0x42: (lambda d: ((d[0] << 8) | d[1]) / 1000,        "volt"),      # CONTROL_MODULE_VOLTAGE
    0x43: (lambda d: ((d[0] << 8) | d[1]) * 100 / 255,   "percent"),   # ABSOLUTE_LOAD
    0x44: (lambda d: ((d[0] << 8) | d[1]) * 2 / 65535,   "ratio"),     # COMMANDED_EQUIV_RATIO
    0x45: (lambda d: d[0] * 100 / 255,                    "percent"),   # RELATIVE_THROTTLE_POS
    0x46: (lambda d: d[0] - 40,                           "degC"),      # AMBIENT_AIR_TEMP
    0x49: (lambda d: d[0] * 100 / 255,                    "percent"),   # ACCELERATOR_POS_D
    0x4A: (lambda d: d[0] * 100 / 255,                    "percent"),   # ACCELERATOR_POS_E
    0x4C: (lambda d: d[0] * 100 / 255,                    "percent"),   # THROTTLE_ACTUATOR
    0x52: (lambda d: d[0] * 100 / 255,                    "percent"),   # ETHANOL_PERCENT
    0x5B: (lambda d: d[0] * 100 / 255,                    "percent"),   # HYBRID_BATTERY_REMAINING
    0x5C: (lambda d: d[0] - 40,                           "degC"),      # OIL_TEMP
    0x5D: (lambda d: ((d[0] << 8) | d[1]) / 128 - 210,   "degree"),    # FUEL_INJECT_TIMING
    0x5E: (lambda d: ((d[0] << 8) | d[1]) / 20,           "liters_per_hour"), # ENGINE_FUEL_RATE
}

# Build PID → OBDCommand map from python-obd's command table at import time.
# python-obd stores command bytes as ASCII hex (e.g. b'010C' for mode 01 PID 0x0C).
_MODE1_CMDS: dict[int, OBDCommand] = {}
for _name in dir(obd.commands):
    _cmd = getattr(obd.commands, _name)
    if isinstance(_cmd, obd.OBDCommand) and len(_cmd.command) >= 4:
        try:
            _hex = _cmd.command.decode("ascii")
            if int(_hex[:2], 16) == 0x01:
                _MODE1_CMDS[int(_hex[2:4], 16)] = _cmd
        except (ValueError, UnicodeDecodeError):
            pass
del _name, _cmd  # clean up module namespace


# ── Response wrappers ─────────────────────────────────────────────────────────

class _Quantity:
    """Duck-typed stand-in for pint Quantity — provides .magnitude and .unit."""

    def __init__(self, magnitude: float, unit: str = "") -> None:
        self.magnitude = magnitude
        self.unit = unit

    def __float__(self) -> float:
        return float(self.magnitude)

    def __repr__(self) -> str:
        return f"{self.magnitude} {self.unit}"


class _SocketCanResponse:
    """Duck-typed OBDResponse for callers that check .is_null() and .value."""

    def __init__(self, value: object = None) -> None:
        self._value = value

    @property
    def value(self) -> object:
        return self._value

    def is_null(self) -> bool:
        return self._value is None


# ── Wire-format conversion ────────────────────────────────────────────────────

def _to_wire(cmd_bytes: bytes) -> bytes:
    """Convert OBDCommand.command to the binary bytes sent on the CAN bus.

    Standard python-obd commands store bytes as ASCII hex (e.g. b'010C' for
    mode 01 PID 0x0C).  Custom OBDCommands built with bytes([...]) are already
    binary (e.g. b'\\x07').  We detect the format by checking whether every
    character is a hex digit; if so we decode the hex string.
    """
    try:
        text = cmd_bytes.decode("ascii")
        if len(text) % 2 == 0 and all(c in "0123456789ABCDEFabcdef" for c in text):
            return bytes.fromhex(text)
    except (UnicodeDecodeError, ValueError):
        pass
    return bytes(cmd_bytes)


# ── SocketCAN connection ──────────────────────────────────────────────────────

class SocketCanConnection:
    """OBD connection backed by a raw SocketCAN interface.

    Public interface mirrors ObdConnection so the rest of Hudson (init.py,
    vin.py, panes/dtcs.py, etc.) can use it without modification.
    """

    _DEFAULT_ECM_TX  = 0x7E0
    _DEFAULT_ECM_RX  = 0x7E8
    _QUERY_TIMEOUT   = 2.0   # seconds per ISO-TP request

    def __init__(self, interface: str = "vcan0") -> None:
        self._interface = interface
        self._bus: can.BusABC | None = None
        self._lock = asyncio.Lock()

        # Current transmit/receive pair — updated by ATSH commands
        self._tx_id = self._DEFAULT_ECM_TX
        self._rx_id = self._DEFAULT_ECM_RX

    # ── lifecycle ─────────────────────────────────────────────────────────────

    async def connect(self) -> None:
        def _open() -> can.BusABC:
            return can.Bus(interface="socketcan", channel=self._interface)

        self._bus = await asyncio.to_thread(_open)
        log.info("SocketCAN connected: interface=%s", self._interface)

    async def close(self) -> None:
        if self._bus is not None:
            await asyncio.to_thread(self._bus.shutdown)
            self._bus = None

    # ── properties ────────────────────────────────────────────────────────────

    @property
    def protocol_name(self) -> str:
        return f"ISO 15765-4 (socketcan/{self._interface})"

    @property
    def is_mock(self) -> bool:
        return False

    @property
    def is_connected(self) -> bool:
        return self._bus is not None

    # ── AT command emulation ──────────────────────────────────────────────────

    async def send_at(self, cmd: str) -> str:
        """Parse AT commands that matter on CAN; ignore the rest."""
        upper = cmd.strip().upper().replace(" ", "")

        if upper in ("ATD", "ATZ", "ATSP0"):
            # Reset to ECM defaults
            self._tx_id = self._DEFAULT_ECM_TX
            self._rx_id = self._DEFAULT_ECM_RX
            log.debug("send_at(%r) → address reset to ECM %03X/%03X", cmd, self._tx_id, self._rx_id)
            return "OK"

        if upper.startswith("ATSH"):
            # ATSH <hex>: set CAN transmit header.
            # ISO 15765-4 ECM range (0x7E0–0x7E7): response at tx + 8.
            # Other modules (gateway, BCM, etc.): response at tx + 1.
            header_hex = upper[4:].lstrip()
            try:
                self._tx_id = int(header_hex, 16)
                if 0x7E0 <= self._tx_id <= 0x7E7:
                    self._rx_id = self._tx_id + 8
                else:
                    self._rx_id = self._tx_id + 1
                log.debug("send_at(%r) → tx=%03X rx=%03X", cmd, self._tx_id, self._rx_id)
            except ValueError:
                log.warning("send_at(%r): could not parse header hex", cmd)
            return "OK"

        # All other AT commands (ATSP3, ATH1, ATL0, ATRV, ATDP, …) are no-ops.
        return "OK"

    # ── OBD queries ───────────────────────────────────────────────────────────

    async def query(self, cmd: OBDCommand, force: bool = False) -> _SocketCanResponse:
        """Send an OBD request and decode the response."""
        if self._bus is None:
            raise RuntimeError("not connected")
        if not cmd.command:
            return _SocketCanResponse()

        wire = _to_wire(cmd.command)
        svc  = wire[0]
        raw  = await self._isotp_query(wire)

        if raw is None:
            return _SocketCanResponse()

        # Mode 09 — VIN
        if svc == 0x09 and len(raw) >= 4 and raw[0] == 0x49 and raw[1] == 0x02:
            vin_bytes = raw[3:]  # strip 49 02 01
            vin = vin_bytes.decode("ascii", errors="replace").rstrip("\x00").strip()
            return _SocketCanResponse(vin if vin else None)

        # Mode 01 — current data
        if svc == 0x01 and len(raw) >= 3 and raw[0] == 0x41:
            pid  = raw[1]
            data = raw[2:]
            decoder = _D.get(pid)
            if decoder is None:
                return _SocketCanResponse(_Quantity(float(data[0]) if data else 0.0))
            fn, unit = decoder
            try:
                return _SocketCanResponse(_Quantity(fn(data), unit))
            except (IndexError, ZeroDivisionError):
                return _SocketCanResponse()

        # Mode 03 — stored DTCs: value = [(code, desc), ...]
        if svc == 0x03 and raw and raw[0] == 0x43:
            return _SocketCanResponse(_parse_dtc_pairs(raw[1:]))

        # Mode 07 — pending DTCs: value = raw bytes for decode_dtc_list()
        if svc == 0x07 and raw and raw[0] == 0x47:
            return _SocketCanResponse(bytes(raw[1:]))

        # Mode 0A — permanent DTCs: value = raw bytes for decode_dtc_list()
        if svc == 0x0A and raw and raw[0] == 0x4A:
            return _SocketCanResponse(bytes(raw[1:]))

        # Negative response or unknown — treat as null
        return _SocketCanResponse()

    async def query_uds(self, service: int, identifier: int, timeout: float = 2.0) -> bytes | None:
        """Send UDS 0x22 ReadDataByIdentifier; return payload bytes or None."""
        if service != 0x22:
            raise ValueError(f"only UDS service 0x22 is permitted; got {service:#04x}")
        if self._bus is None:
            raise RuntimeError("not connected")

        hi, lo = (identifier >> 8) & 0xFF, identifier & 0xFF
        raw = await self._isotp_query(bytes([0x22, hi, lo]), timeout=timeout)
        if raw is None or len(raw) < 3:
            return None
        if raw[0] == 0x62 and raw[1] == hi and raw[2] == lo:
            return bytes(raw[3:])
        return None

    async def query_kwp_service(
        self,
        service: int,
        payload: bytes = b"",
        timeout: float = 0.3,
    ) -> bytes | None:
        """KWP2000 is K-line only — not supported over SocketCAN."""
        log.debug("query_kwp_service: K-line not available on SocketCAN, returning None")
        return None

    async def query_enhanced_local(self, local_id: int, timeout: float = 0.15) -> bytes | None:
        """Mode 0x21 ReadDataByLocalIdentifier."""
        if self._bus is None:
            raise RuntimeError("not connected")
        raw = await self._isotp_query(bytes([0x21, local_id]), timeout=timeout)
        if raw is None or len(raw) < 2:
            return None
        if raw[0] == 0x61 and raw[1] == local_id:
            return bytes(raw[2:])
        return None

    async def send_tester_present(self) -> None:
        """Send UDS TesterPresent (0x3E 0x00) keepalive."""
        if self._bus is None:
            return
        try:
            await self._isotp_query(bytes([0x3E, 0x00]), timeout=0.5)
        except Exception as exc:
            log.debug("TesterPresent keepalive failed: %s", exc)

    async def supported_commands(self) -> set[OBDCommand]:
        """Query mode 01 supported PIDs; return matching python-obd commands."""
        if self._bus is None:
            raise RuntimeError("not connected")
        supported: set[OBDCommand] = set()

        for meta_pid in (0x00, 0x20, 0x40):
            raw = await self._isotp_query(bytes([0x01, meta_pid]))
            if raw is None or len(raw) < 6 or raw[0] != 0x41 or raw[1] != meta_pid:
                break
            bitmask = int.from_bytes(raw[2:6], "big")
            for bit in range(32):
                if bitmask & (1 << (31 - bit)):
                    pid = meta_pid + bit + 1
                    if pid in _MODE1_CMDS:
                        supported.add(_MODE1_CMDS[pid])
            if not (bitmask & 0x00000001):  # next range not available
                break

        # Always include DTC and VIN commands
        for attr in ("GET_DTC", "CLEAR_DTC", "VIN"):
            if hasattr(obd.commands, attr):
                supported.add(getattr(obd.commands, attr))

        log.info("SocketCAN: %d supported commands", len(supported))
        return supported

    # ── ISO-TP core ───────────────────────────────────────────────────────────

    async def _isotp_query(self, payload: bytes, timeout: float = _QUERY_TIMEOUT) -> bytes | None:
        """Send ISO-TP payload and await response (serialised by lock)."""
        tx_id, rx_id = self._tx_id, self._rx_id

        def _blocking() -> bytes | None:
            addr  = isotp.Address(isotp.AddressingMode.Normal_11bits, txid=tx_id, rxid=rx_id)
            stack = isotp.CanStack(bus=self._bus, address=addr)
            stack.send(payload)
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                stack.process()
                if stack.available():
                    return stack.recv()
                time.sleep(0.001)
            return None

        async with self._lock:
            return await asyncio.to_thread(_blocking)


# ── DTC helpers ───────────────────────────────────────────────────────────────

def _parse_dtc_pairs(data: bytes) -> list[tuple[str, str]]:
    """Parse raw DTC byte pairs into (code, description) tuples."""
    result: list[tuple[str, str]] = []
    i = 0
    while i + 1 < len(data):
        byte_a, byte_b = data[i], data[i + 1]
        if byte_a == 0 and byte_b == 0:
            break
        system = ["P", "C", "B", "U"][(byte_a >> 6) & 0x03]
        d1 = (byte_a >> 4) & 0x03
        d2 = byte_a & 0x0F
        tail = byte_b
        code = f"{system}{d1}{d2:X}{tail:02X}"
        result.append((code, ""))
        i += 2
    return result
