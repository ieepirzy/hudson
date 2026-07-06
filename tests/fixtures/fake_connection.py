"""Mock ObdConnection for headless TUI testing.

Replaces the real serial connection with synthesized responses so the
splash → dashboard flow can be exercised in CI / sandbox environments
where no ELM327 is connected.
"""

from __future__ import annotations

import asyncio
import math
from time import monotonic
from typing import TYPE_CHECKING

import obd
from obd import OBDResponse

from Hudson.core.connection import (
    MODE22_TIMEOUT_S,
    UdsResponse,
    UdsResponseStatus,
    _atst_for,
)
from Hudson.core.dtc import encode_dtc

if TYPE_CHECKING:
    from obd import OBDCommand


class _FakeQuantity:
    """Minimal stand-in for a Pint quantity exposing `.magnitude`."""

    __slots__ = ("magnitude",)

    def __init__(self, magnitude: float) -> None:
        self.magnitude = magnitude


class FakeConnection:
    """Implements the same surface as `ObdConnection` for tests."""

    # Realistic set of stored DTCs — cleared by CLEAR_DTC, readable by GET_DTC.
    # Format matches what python-obd returns: list of (code_str, description_str).
    _INITIAL_DTCS: list[tuple[str, str]] = [
        ("P0300", "Random/Multiple Cylinder Misfire Detected"),
        ("P0171", "System Too Lean (Bank 1)"),
        ("C0035", "Left Front Wheel Speed Sensor Circuit"),
        # VAG-specific — no description from python-obd; resolved via vw_audi.lookup_dtc
        ("P1176", ""),
    ]

    def __init__(
        self,
        vin: str = "WV2ZZZ7HZ8H123456",
        *,
        functional_responders: list[int] | None = None,
        present_ecus: set[int] | None = None,
        pending_dtcs: list[str] | None = None,
        permanent_dtcs: list[str] | None = None,
    ) -> None:
        self._vin = vin
        self._connected = False
        self._t0 = monotonic()
        self._dtcs: list[tuple[str, str]] = list(self._INITIAL_DTCS)
        self._protocol_kline = False
        self._send_at_history: list[str] = []
        # Tier A: addresses that respond to Mode 01 functional broadcast.
        self._functional_responders: list[int] = list(functional_responders or [])
        # Tier B/C: addresses that respond to TesterPresent probe.
        # Defaults to {0x7E0} to match query_uds_dtc_at_addr's existing behaviour.
        self._present_ecus: set[int] = (
            set(present_ecus) if present_ecus is not None else {0x7E0}
        )
        # Mode 07 (pending) and 0A (permanent) DTC simulation.
        self._pending_dtcs: list[str] = list(pending_dtcs) if pending_dtcs is not None else []
        self._permanent_dtcs: list[str] = list(permanent_dtcs) if permanent_dtcs is not None else []
        self._supported = {
            obd.commands.RPM,
            obd.commands.SPEED,
            obd.commands.THROTTLE_POS,
            obd.commands.COOLANT_TEMP,
            obd.commands.INTAKE_TEMP,
            obd.commands.ENGINE_LOAD,
            obd.commands.VIN,
            obd.commands.GET_DTC,
            obd.commands.CLEAR_DTC,
        }

    async def connect(self) -> None:
        await asyncio.sleep(0.1)
        self._connected = True

    async def close(self) -> None:
        self._connected = False

    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def protocol_name(self) -> str:
        return "ISO 15765-4 (CAN 11/500)"

    @property
    def is_can_protocol(self) -> bool:
        return "CAN" in self.protocol_name

    @property
    def active_ecu_addr(self) -> int | None:
        return 0x7E0

    @property
    def transport_label(self) -> str:
        return "CAN"

    async def supported_commands(self) -> set[OBDCommand]:
        return set(self._supported)

    @property
    def is_mock(self) -> bool:
        return True

    async def query(self, cmd: OBDCommand, force: bool = False) -> OBDResponse:
        await asyncio.sleep(0.02)  # simulate UART round-trip

        if cmd is obd.commands.VIN:
            return _make_response(cmd, self._vin)

        if cmd is obd.commands.GET_DTC:
            return _make_response(cmd, list(self._dtcs))

        if cmd is obd.commands.CLEAR_DTC:
            self._dtcs = list(self._INITIAL_DTCS)
            return _make_response(cmd, [])

        # Mode 07 (pending) and 0A (permanent) — custom OBDCommand instances,
        # detected by command byte since they are not in the obd.commands namespace.
        if cmd.command == b"\x07":
            return _make_response(cmd, _encode_dtc_payload(self._pending_dtcs))
        if cmd.command == b"\x0A":
            return _make_response(cmd, _encode_dtc_payload(self._permanent_dtcs))

        t = monotonic() - self._t0
        value = _synthetic_value(cmd, t)
        return _make_response(cmd, value)

    async def query_uds(self, service: int, identifier: int, timeout: float = 0.15) -> bytes | None:
        """Return fake positive UDS responses for known mock identifiers."""
        await asyncio.sleep(0.01)
        return _MOCK_UDS_RESPONSES.get(identifier)

    async def query_enhanced_local(self, local_id: int, timeout: float = 0.15) -> bytes | None:
        """Return None by default — override in subclasses for manufacturer mocks."""
        await asyncio.sleep(0.01)
        return None

    async def send_tester_present(self) -> None:
        pass

    async def send_at(self, cmd: str) -> str:
        await asyncio.sleep(0.01)
        self._send_at_history.append(cmd)
        if cmd in ("ATSP3", "ATSP4"):
            self._protocol_kline = True
        elif cmd == "ATSP0":
            self._protocol_kline = False
        return "OK"

    async def query_uds_at_addr(
        self,
        ecu_addr: int,
        identifier: int,
        timeout: float = MODE22_TIMEOUT_S,
    ) -> UdsResponse:
        """Return fake UDS 0x22 responses; tracks ATST commands in _send_at_history."""
        _config_timeout = 0.1  # ConnectionConfig.timeout default
        _atst_changed = abs(timeout - _config_timeout) > 1e-4
        if _atst_changed:
            await self.send_at(_atst_for(timeout))
        try:
            await asyncio.sleep(0.01)
            data = _MOCK_UDS_RESPONSES.get(identifier)
        finally:
            if _atst_changed:
                await self.send_at(_atst_for(_config_timeout))
        if data is None:
            return UdsResponse(UdsResponseStatus.NO_RESPONSE)
        return UdsResponse(UdsResponseStatus.OK, data=data)

    async def query_uds_dtc_at_addr(
        self,
        ecu_addr: int,
        sub_fn: int,
        params: bytes = b"",
    ) -> UdsResponse:
        """Return fake UDS 0x19 payloads for ECU 0x7E0 (header already stripped)."""
        await asyncio.sleep(0.01)
        if ecu_addr == 0x7E0 and sub_fn in (0x02, 0x0A):
            return UdsResponse(UdsResponseStatus.OK, data=_MOCK_UDS19_PAYLOAD)
        return UdsResponse(UdsResponseStatus.NO_RESPONSE)

    async def query_kwp_service(
        self,
        service: int,
        payload: bytes = b"",
        timeout: float = 0.15,
    ) -> bytes | None:
        await asyncio.sleep(0.01)
        return None

    async def query_functional_mode01(self, pid: int = 0x00) -> list[int]:
        """Return addresses configured as functional-broadcast responders."""
        await asyncio.sleep(0.01)
        return list(self._functional_responders)

    async def probe_ecu_tester_present(self, addr: int) -> bool:
        """Return True if *addr* is in the set of present ECUs."""
        await asyncio.sleep(0.01)
        return addr in self._present_ecus

    @property
    def protocol_kline(self) -> bool:
        return self._protocol_kline


# UDS 0x19 payload for ECU 0x7E0 (0x59 + sub_fn header already stripped).
# Records: [hi, mid, lo, status_byte] per DTC.
#   P0300 = (0x03, 0x00) — confirmed + pending + MIL  (0x8C)
#   P0171 = (0x01, 0x71) — testFailed + confirmed     (0x09)
_MOCK_UDS19_PAYLOAD: bytes = bytes([
    0x03, 0x00, 0x00, 0x8C,
    0x01, 0x71, 0x00, 0x09,
])

# Fake UDS positive-response payloads (data bytes only, UDS header stripped).
_MOCK_UDS_RESPONSES: dict[int, bytes] = {
    0xF189: b"0001\x00",                     # ECU SW version → "0001"
    0xF190: b"WV2ZZZ7HZ8H123456",            # VIN via UDS
    0xF400: bytes([0x00, 0x0F, 0x00, 0x00]), # boost pressure actual
    0xF401: bytes([0x00, 0x12, 0x00, 0x00]), # boost pressure setpoint
    0xF40B: bytes([0x01, 0x2C]),             # boost actual (kPa)
    0xF40C: bytes([0x01, 0x40]),             # boost specified (kPa)
}

# Toyota mode 0x22 enhanced PID responses.
# Identifiers mirror standard mode 01 PID numbers (0x01xx range).
# Values match the waveform-generator defaults so tests stay consistent:
#   coolant=85°C → raw 125 (125-40=85), intake=35°C → raw 75,
#   load≈30% → raw 77 (77/2.55≈30), RPM=1500 → 0x1770 (/4=1500),
#   speed=40 km/h → 0x28, throttle≈15% → raw 38 (38/2.55≈15)
_MOCK_TOYOTA_UDS_RESPONSES: dict[int, bytes] = {
    0xF189: b"1AZ\x00",           # ECU SW version → "1AZ" (engine family)
    0x0105: bytes([0x7D]),         # coolant:  125 - 40 = 85 °C
    0x010F: bytes([0x4B]),         # intake:    75 - 40 = 35 °C
    0x0110: bytes([0x4D]),         # load:      77 / 2.55 ≈ 30 %
    0x010C: bytes([0x17, 0x70]),   # RPM:    0x1770 / 4 = 1500 rpm
    0x010D: bytes([0x28]),         # speed:   40 km/h
    0x0111: bytes([0x26]),         # throttle: 38 / 2.55 ≈ 15 %
}

# Toyota mode 0x21 local block responses (data bytes, header stripped).
# Block 0x10: RPM(2) coolant(1) intake(1) throttle(1) load(1)
_MOCK_TOYOTA_ENHANCED_LOCAL: dict[int, bytes] = {
    0x10: bytes([0x17, 0x70, 0x7D, 0x4B, 0x26, 0x4D]),
}


# Volvo K-line block responses (data bytes, header stripped).
# Block 0x01: RPM(2) coolant(1) intake(1) throttle(1) load(1) — same values as
# MOCK_KWP_RESPONSES in volvo.py so cross-layer assertions stay consistent.
_MOCK_VOLVO_KWP_DATA: dict[int, bytes] = {
    0x01: bytes([0x17, 0x70, 0x7D, 0x4B, 0x26, 0x4D]),
}


class FakeKlineConnection(FakeConnection):
    """FakeConnection that reports a K-line protocol — use for transport auto-detect tests."""

    @property
    def protocol_name(self) -> str:
        return "ISO 14230-4 (KWP fast init)"


class FakeVolvoConnection(FakeKlineConnection):
    """FakeConnection variant with a Volvo VIN, no UDS, and a working K-line session.

    UDS probe (0xF189) returns None so the init sequence falls through to
    _run_kwp_session.  query_kwp_service(0x10) returns positive so the
    KwpSession starts.  query_enhanced_local returns Volvo block data.
    """

    def __init__(self) -> None:
        super().__init__(vin="YV1RS61T242397765")  # Volvo S60

    async def query_uds(self, service: int, identifier: int, timeout: float = 0.15) -> bytes | None:
        await asyncio.sleep(0.01)
        return None  # no UDS — triggers KWP fallback

    async def query_uds_at_addr(
        self, ecu_addr: int, identifier: int, timeout: float = MODE22_TIMEOUT_S
    ) -> UdsResponse:
        await asyncio.sleep(0.01)
        return UdsResponse(UdsResponseStatus.NO_RESPONSE)  # K-line only — no CAN UDS

    async def query_kwp_service(
        self,
        service: int,
        payload: bytes = b"",
        timeout: float = 0.15,
    ) -> bytes | None:
        await asyncio.sleep(0.01)
        if service == 0x10:  # StartDiagnosticSession → positive
            return b""
        if service == 0x20:  # StopDiagnosticSession → positive
            return b""
        return None

    async def query_enhanced_local(self, local_id: int, timeout: float = 0.15) -> bytes | None:
        await asyncio.sleep(0.01)
        return _MOCK_VOLVO_KWP_DATA.get(local_id)


class FakeToyotaConnection(FakeConnection):
    """FakeConnection variant with a Toyota VIN and Toyota-specific mock responses.

    Use this fixture for tests that exercise the Toyota enhanced PID flow
    (mode 0x22 via query_uds_at_addr and mode 0x21 via query_enhanced_local).
    """

    def __init__(self) -> None:
        super().__init__(vin="JT000000000000001")

    async def query_uds(self, service: int, identifier: int, timeout: float = 0.15) -> bytes | None:
        await asyncio.sleep(0.01)
        return _MOCK_TOYOTA_UDS_RESPONSES.get(identifier)

    async def query_uds_at_addr(
        self, ecu_addr: int, identifier: int, timeout: float = MODE22_TIMEOUT_S
    ) -> UdsResponse:
        await asyncio.sleep(0.01)
        data = _MOCK_TOYOTA_UDS_RESPONSES.get(identifier)
        if data is None:
            return UdsResponse(UdsResponseStatus.NO_RESPONSE)
        return UdsResponse(UdsResponseStatus.OK, data=data)

    async def query_enhanced_local(self, local_id: int, timeout: float = 0.15) -> bytes | None:
        await asyncio.sleep(0.01)
        return _MOCK_TOYOTA_ENHANCED_LOCAL.get(local_id)


def _encode_dtc_payload(codes: list[str]) -> bytes:
    """Encode DTC code strings to raw 2-byte pairs for mode 07/0A simulation."""
    result = b""
    for code in codes:
        a, b = encode_dtc(code)
        result += bytes([a, b])
    return result


def _synthetic_value(cmd: OBDCommand, t: float) -> object:
    """Toy waveform generator so the dashboard moves."""
    if cmd is obd.commands.RPM:
        return _FakeQuantity(800 + 1500 * (0.5 + 0.5 * math.sin(t * 0.7)))
    if cmd is obd.commands.SPEED:
        return _FakeQuantity(40 + 30 * math.sin(t * 0.3))
    if cmd is obd.commands.THROTTLE_POS:
        return _FakeQuantity(15 + 25 * (0.5 + 0.5 * math.sin(t * 1.1)))
    if cmd is obd.commands.COOLANT_TEMP:
        return _FakeQuantity(85 + 5 * math.sin(t * 0.05))
    if cmd is obd.commands.INTAKE_TEMP:
        return _FakeQuantity(35 + 3 * math.sin(t * 0.07))
    if cmd is obd.commands.ENGINE_LOAD:
        return _FakeQuantity(30 + 20 * math.sin(t * 0.5))
    return None


def _make_response(cmd: OBDCommand, value: object) -> OBDResponse:
    """Build an OBDResponse with a value attached.

    `OBDResponse.is_null()` checks both that `messages` is non-empty AND that
    `value is not None`, so we pass a sentinel message so the response reads
    as "real" without us needing to construct a full `Message` object.
    """
    resp = OBDResponse(command=cmd, messages=[_FakeMessage()])
    resp.value = value
    return resp


class FakeNoMode09VinConnection(FakeConnection):
    """FakeConnection where mode 09 VIN returns null, forcing chain fallback to UDS.

    Used to exercise the VIN resolution chain beyond the first step without
    real hardware. UDS 0xF190 returns the VIN (step 2 succeeds).
    """

    async def query(self, cmd: "OBDCommand", force: bool = False) -> OBDResponse:
        import obd as _obd
        if cmd is _obd.commands.VIN:
            # Return a null response — is_null() returns True when messages is empty.
            return OBDResponse(command=cmd, messages=[])
        return await super().query(cmd, force=force)


class _FakeMessage:
    """Sentinel — `is_null` only checks truthiness of the messages list."""
