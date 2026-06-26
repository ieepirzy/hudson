"""Async connection layer.

python-obd's API is synchronous and blocks on serial I/O. python-obd ships
its own `Async` class but it's threaded, not asyncio-based, and would fight
Textual's event loop. So we run python-obd's blocking `query()` calls in
a thread pool via `asyncio.to_thread`, exposing a clean async interface.

The connection itself lives on a single dedicated thread (because pyserial
is not thread-safe), serialized through a single asyncio.Lock to prevent
overlapping queries.
"""

from __future__ import annotations

import asyncio
import logging
import re
from contextlib import asynccontextmanager
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, AsyncIterator

import obd
from obd import OBDCommand, OBDStatus
from obd.protocols import ECU

if TYPE_CHECKING:
    from obd import OBDResponse

log = logging.getLogger(__name__)


def _uds_passthrough(messages: list) -> bytes | None:
    """Decoder that returns raw payload bytes from the first CAN message."""
    if not messages:
        return None
    return bytes(messages[0].data)


@dataclass(frozen=True, slots=True)
class ConnectionConfig:
    """Settings for opening an ELM327 connection."""

    portstr: str | None = None  # e.g. "/dev/rfcomm0"; None = auto-detect
    baudrate: int | None = None  # None = auto-detect
    protocol: str | None = None  # None = auto-detect; "6" = ISO 15765-4 (CAN 11/500)
    fast: bool = True  # python-obd's "fast" mode reuses the last response if cached
    timeout: float = 0.1
    check_voltage: bool = True


_VOLTAGE_RE = re.compile(r"^\d+\.?\d*\s*[Vv]?$")

# ── UDS response types ────────────────────────────────────────────────────────

_ATST_UNIT: float = 0.004  # ELM327 ATST step: 4 ms per unit
MODE22_TIMEOUT_S: float = 0.25  # generous timeout for Mode 22 on clone hardware


def _atst_for(seconds: float) -> str:
    """Format an ELM327 ATST command for the given timeout in seconds."""
    val = max(1, min(0xFF, int(seconds / _ATST_UNIT + 0.5)))
    return f"ATST {val:02X}"


_NRC_NAMES: dict[int, str] = {
    0x10: "generalReject",
    0x11: "serviceNotSupported",
    0x12: "subFunctionNotSupported",
    0x13: "incorrectMessageLengthOrInvalidFormat",
    0x14: "responseTooLong",
    0x21: "busyRepeatRequest",
    0x22: "conditionsNotCorrect",
    0x24: "requestSequenceError",
    0x25: "noResponseFromSubnetComponent",
    0x26: "failurePreventsExecutionOfRequestedAction",
    0x31: "requestOutOfRange",
    0x33: "securityAccessDenied",
    0x35: "invalidKey",
    0x36: "exceededNumberOfAttempts",
    0x37: "requiredTimeDelayNotExpired",
    0x70: "uploadDownloadNotAccepted",
    0x71: "transferDataSuspended",
    0x72: "generalProgrammingFailure",
    0x73: "wrongBlockSequenceCounter",
    0x78: "requestCorrectlyReceivedResponsePending",
    0x7E: "subFunctionNotSupportedInActiveSession",
    0x7F: "serviceNotSupportedInActiveSession",
}


class UdsResponseStatus(Enum):
    OK = "ok"
    NO_RESPONSE = "no_response"
    NEGATIVE_RESPONSE = "negative_response"
    TRUNCATED = "truncated"
    LIKELY_TRUNCATED_MULTIFRAME = "likely_truncated_multiframe"


@dataclass(frozen=True, slots=True)
class UdsResponse:
    """Typed result of a UDS query — callers check .status before using .data."""

    status: UdsResponseStatus
    data: bytes = b""
    nrc: int = 0


class ObdConnection:
    """Async-friendly wrapper around `obd.OBD`."""

    def __init__(self, config: ConnectionConfig | None = None) -> None:
        self._config = config or ConnectionConfig()
        self._conn: obd.OBD | None = None
        self._lock = asyncio.Lock()
        self._reconnect_lock = asyncio.Lock()

    async def connect(self) -> None:
        """Open the underlying serial connection in a worker thread."""

        def _open() -> obd.OBD:
            return obd.OBD(
                portstr=self._config.portstr,
                baudrate=self._config.baudrate,
                protocol=self._config.protocol,
                fast=self._config.fast,
                timeout=self._config.timeout,
                check_voltage=self._config.check_voltage,
            )

        self._conn = await asyncio.to_thread(_open)
        status = self._conn.status()
        # Accept OBD_CONNECTED (protocol found) as well as CAR_CONNECTED (mode 01 works).
        # Hudson uses UDS/manufacturer protocols — mode 01 responsiveness is not required.
        if status in (OBDStatus.NOT_CONNECTED, OBDStatus.ELM_CONNECTED):
            raise ConnectionError(f"failed to connect to ELM327: {status}")

        log.info("connected: protocol=%s port=%s", self._conn.protocol_name(), self._conn.port_name())
        await self._log_adapter_state()

    async def _log_adapter_state(self) -> None:
        """Read and log key ELM327 AT registers after connection.

        These are read-only queries that don't affect adapter state. They
        produce a timestamped snapshot in the log file so we can diagnose
        adapter misconfiguration without re-plugging.
        """
        ati = await self.send_at("ATI")
        dp = await self.send_at("ATDP")
        rv = await self.send_at("ATRV")
        log.info(
            "ELM327 state after init — firmware: %r  protocol: %r  voltage: %r",
            ati.strip(),
            dp.strip(),
            rv.strip(),
        )
        voltage = rv.strip()
        if not voltage or not _VOLTAGE_RE.match(voltage):
            log.warning(
                "ELM327: voltage read returned %r — "
                "adapter may not be seeing ignition power",
                voltage,
            )

    async def close(self) -> None:
        if self._conn is None:
            return
        await asyncio.to_thread(self._conn.close)
        self._conn = None

    async def _reconnect(self) -> None:
        """Reconnect with exponential backoff. Returns only when connected.

        Uses a separate lock so concurrent callers all wait for the same
        reconnect attempt rather than racing to open multiple connections.
        The UDS discovery cache (keyed by ECU version string) remains valid
        across reconnects as long as the same vehicle is connected.
        """
        async with self._reconnect_lock:
            if self.is_connected:
                return
            delay = 1.0
            attempt = 0
            while True:
                attempt += 1
                log.warning("reconnecting (attempt %d)…", attempt)
                try:
                    if self._conn is not None:
                        await asyncio.to_thread(self._conn.close)
                        self._conn = None
                except Exception:
                    pass
                try:
                    await self.connect()
                    log.info("reconnected after %d attempt(s)", attempt)
                    return
                except Exception as exc:
                    log.warning("reconnect attempt %d failed: %s — retrying in %.0fs", attempt, exc, delay)
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, 60.0)

    async def query(self, cmd: OBDCommand, force: bool = False) -> OBDResponse:
        """Send a single OBD command, awaiting the response.

        If the adapter disconnects mid-session, transparently reconnects with
        exponential backoff before returning. Callers block until the adapter
        is back online.
        """
        if self._conn is None:
            raise RuntimeError("not connected")
        async with self._lock:
            resp = await asyncio.to_thread(self._conn.query, cmd, force=force)
        if not self.is_connected:
            await self._reconnect()
            async with self._lock:
                resp = await asyncio.to_thread(self._conn.query, cmd, force=force)
        return resp

    async def supported_commands(self) -> set[OBDCommand]:
        """Return the set of commands the connected vehicle reports as supported."""
        if self._conn is None:
            raise RuntimeError("not connected")
        # `supported_commands` is a property that reads pre-probed state, no I/O.
        return set(self._conn.supported_commands)

    @property
    def is_mock(self) -> bool:
        return False

    async def query_uds(self, service: int, identifier: int, timeout: float = 0.15) -> bytes | None:
        """Send a UDS ReadDataByIdentifier (0x22) request via OBDCommand.

        Only service 0x22 is permitted. Returns the data bytes on a positive
        response (0x62), or None on negative response (0x7F) or no reply.
        """
        if service != 0x22:
            raise ValueError(f"only UDS service 0x22 is permitted; got {service:#04x}")
        if self._conn is None:
            raise RuntimeError("not connected")

        high = (identifier >> 8) & 0xFF
        low = identifier & 0xFF
        cmd = OBDCommand(
            f"UDS_{identifier:04X}",
            f"UDS ReadDataByIdentifier 0x{identifier:04X}",
            bytes([0x22, high, low]),
            0,
            _uds_passthrough,
            ECU.ALL,
            False,
        )

        async with self._lock:
            resp = await asyncio.to_thread(self._conn.query, cmd, force=True)

        if resp.is_null() or resp.value is None:
            return None

        raw: bytes = resp.value
        if len(raw) >= 3 and raw[0] == 0x62 and raw[1] == high and raw[2] == low:
            return bytes(raw[3:])
        return None

    async def query_uds_at_addr(
        self,
        ecu_addr: int,
        identifier: int,
        timeout: float = MODE22_TIMEOUT_S,
    ) -> UdsResponse:
        """Send UDS 0x22 ReadDataByIdentifier to a specific ECU address.

        ATSH and the query are performed atomically under one lock acquisition to
        prevent TOCTOU races between concurrent callers. When *timeout* differs
        from the connection's configured timeout, ATST is updated via send_at()
        before the query and restored in a finally block — matching the pattern
        used by scan_ecus_for_dtcs for ATAT.

        Returns a UdsResponse; callers should check .status before using .data.
        """
        if self._conn is None:
            raise RuntimeError("not connected")

        high = (identifier >> 8) & 0xFF
        low = identifier & 0xFF
        cmd = OBDCommand(
            f"UDS22_{ecu_addr:03X}_{identifier:04X}",
            f"UDS ReadDataByIdentifier 0x{identifier:04X} @ ECU 0x{ecu_addr:03X}",
            bytes([0x22, high, low]),
            0,
            _uds_passthrough,
            ECU.ALL,
            False,
        )

        conn = self._conn

        def _atomic() -> bytes | None:
            iface = getattr(conn, "interface", getattr(conn, "_interface", None))
            if iface is None:
                return None
            try:
                iface.send_and_parse(f"ATSH {ecu_addr:03X}")
            except Exception as exc:
                log.warning("ATSH %03X failed: %s", ecu_addr, exc)
                return None
            resp = conn.query(cmd, force=True)
            if resp.is_null() or resp.value is None:
                return None
            return bytes(resp.value)

        config_timeout = self._config.timeout
        atst_changed = abs(timeout - config_timeout) > 1e-4
        if atst_changed:
            await self.send_at(_atst_for(timeout))
        try:
            async with self._lock:
                raw = await asyncio.to_thread(_atomic)
        finally:
            if atst_changed:
                await self.send_at(_atst_for(config_timeout))

        if raw is None or len(raw) < 3:
            return UdsResponse(UdsResponseStatus.NO_RESPONSE)
        if raw[0] == 0x7F and len(raw) >= 3:
            nrc = raw[2]
            nrc_name = _NRC_NAMES.get(nrc, f"0x{nrc:02X}")
            log.info(
                "UDS 0x22 NR 0x%04X @ 0x%03X: NRC 0x%02X (%s)",
                identifier, ecu_addr, nrc, nrc_name,
            )
            return UdsResponse(UdsResponseStatus.NEGATIVE_RESPONSE, nrc=nrc)
        if raw[0] == 0x62 and raw[1] == high and raw[2] == low:
            return UdsResponse(UdsResponseStatus.OK, data=bytes(raw[3:]))
        return UdsResponse(UdsResponseStatus.NO_RESPONSE)

    async def query_uds_dtc_at_addr(
        self,
        ecu_addr: int,
        sub_fn: int,
        params: bytes = b"",
    ) -> UdsResponse:
        """Send a UDS 0x19 (ReadDTCInformation) request to a specific ECU address.

        ATSH and the query are performed atomically under one lock acquisition to
        prevent TOCTOU races between concurrent callers.

        Returns a UdsResponse. LIKELY_TRUNCATED_MULTIFRAME is returned when the
        record payload is not a multiple of 4 bytes — this indicates ISO-TP
        multi-frame truncation caused by ELM327 clone FC (flow control) failure;
        the .data field still contains whatever bytes were received so the caller
        can decode the complete 4-byte records that arrived.
        """
        if self._conn is None:
            raise RuntimeError("not connected")

        request = bytes([0x19, sub_fn]) + params
        cmd = OBDCommand(
            f"UDS19_{ecu_addr:03X}_{sub_fn:02X}",
            f"UDS ReadDTCInformation 0x{sub_fn:02X} @ ECU 0x{ecu_addr:03X}",
            request,
            0,
            _uds_passthrough,
            ECU.ALL,
            False,
        )

        conn = self._conn

        def _atomic() -> bytes | None:
            iface = getattr(conn, "interface", getattr(conn, "_interface", None))
            if iface is None:
                return None
            try:
                iface.send_and_parse(f"ATSH {ecu_addr:03X}")
            except Exception as exc:
                log.warning("ATSH %03X failed: %s", ecu_addr, exc)
                return None
            resp = conn.query(cmd, force=True)
            if resp.is_null() or resp.value is None:
                return None
            return bytes(resp.value)

        async with self._lock:
            raw = await asyncio.to_thread(_atomic)

        if raw is None:
            return UdsResponse(UdsResponseStatus.NO_RESPONSE)
        if raw[0] == 0x7F and len(raw) >= 3:
            nrc = raw[2]
            nrc_name = _NRC_NAMES.get(nrc, f"0x{nrc:02X}")
            log.info(
                "UDS 0x19 NR sub_fn=0x%02X @ 0x%03X: NRC 0x%02X (%s)",
                sub_fn, ecu_addr, nrc, nrc_name,
            )
            return UdsResponse(UdsResponseStatus.NEGATIVE_RESPONSE, nrc=nrc)
        # Response layout: [0x59, sub_fn, DTCStatusAvailabilityMask, record…]
        # ISO 14229-1: mask byte always present for sub_fn 0x02 and 0x0A.
        if len(raw) < 3 or raw[0] != 0x59 or raw[1] != sub_fn:
            return UdsResponse(UdsResponseStatus.NO_RESPONSE)
        records_payload = bytes(raw[3:])
        if records_payload and len(records_payload) % 4 != 0:
            # Non-multiple-of-4 record payload: ISO-TP multi-frame response likely
            # truncated. ELM327 v1.5 clones commonly fail to send the Flow Control
            # (FC 0x30 CTS) frame, causing the ECU to stop after the first CAN frame
            # (~7 usable bytes). Cannot fix in software over the AT-command interface.
            log.warning(
                "UDS 0x19 @ 0x%03X: payload length %d is not a multiple of 4 — "
                "ISO-TP multi-frame likely truncated (ELM327 clone FC failure); "
                "%d complete record(s) decodable",
                ecu_addr, len(records_payload), len(records_payload) // 4,
            )
            return UdsResponse(UdsResponseStatus.LIKELY_TRUNCATED_MULTIFRAME, data=records_payload)
        return UdsResponse(UdsResponseStatus.OK, data=records_payload)

    async def query_enhanced_local(self, local_id: int, timeout: float = 0.15) -> bytes | None:
        """Send a mode 0x21 ReadDataByLocalIdentifier request.

        Used for Toyota enhanced data streams over CAN/ISO-TP. Returns the data
        bytes following the positive-response header (0x61 + local_id), or None
        on a negative response or no reply.

        Not to be confused with KWP2000 query_block — that uses K-line transport.
        """
        if self._conn is None:
            raise RuntimeError("not connected")

        cmd = OBDCommand(
            f"ENH_{local_id:02X}",
            f"Enhanced Local Identifier 0x{local_id:02X}",
            bytes([0x21, local_id]),
            0,
            _uds_passthrough,
            ECU.ALL,
            False,
        )

        async with self._lock:
            resp = await asyncio.to_thread(self._conn.query, cmd, force=True)

        if resp.is_null() or resp.value is None:
            return None

        raw: bytes = resp.value
        # Positive response: 0x61 (= 0x21 + 0x40) + local_id echo + data
        if len(raw) >= 2 and raw[0] == 0x61 and raw[1] == local_id:
            return bytes(raw[2:])
        return None

    async def send_at(self, cmd: str) -> str:
        """Send an ELM327 AT command; returns response string best-effort."""
        if self._conn is None:
            raise RuntimeError("not connected")

        def _send() -> str:
            iface = getattr(self._conn, "interface", getattr(self._conn, "_interface", None))
            if iface is None:
                log.warning("send_at(%r): no ELM327 interface available", cmd)
                return ""
            try:
                msgs = iface.send_and_parse(cmd)
                return "".join(str(m) for m in msgs) if msgs else ""
            except Exception as exc:
                log.warning("AT command %r failed: %s", cmd, exc)
                return ""

        async with self._lock:
            return await asyncio.to_thread(_send)

    async def query_kwp_service(
        self,
        service: int,
        payload: bytes = b"",
        timeout: float = 0.3,
    ) -> bytes | None:
        """Send a KWP2000 service request over the active K-line transport.

        Returns the response bytes (positive-response byte stripped), or None
        on negative response or no reply.  Only call after send_at("ATSP3").
        """
        if self._conn is None:
            raise RuntimeError("not connected")

        cmd = OBDCommand(
            f"KWP_{service:02X}",
            f"KWP2000 service 0x{service:02X}",
            bytes([service]) + payload,
            0,
            _uds_passthrough,
            ECU.ALL,
            False,
        )

        async with self._lock:
            resp = await asyncio.to_thread(self._conn.query, cmd, force=True)

        if resp.is_null() or resp.value is None:
            return None

        raw: bytes = resp.value
        positive = service + 0x40
        if len(raw) >= 1 and raw[0] == positive:
            return bytes(raw[1:])
        return None

    async def query_functional_mode01(self, pid: int = 0x00) -> list[int]:
        """Broadcast Mode 01 PID to the J1979 functional address (0x7DF).

        Sets ATSH to 7DF, sends Mode 01 PID *pid*, and returns the physical
        CAN IDs of every ECU that responded.

        Source address extraction: python-obd initialises the ELM327 with
        ATH1 (headers on), so each raw frame string starts with the 3-char
        hex CAN ID (e.g. "7E806410...").  python-obd's internal ``frame.rx_id``
        field is NOT the source address — for 11-bit physical responses it is
        always set to the made-up tester address (0xF1), regardless of which
        ECU sent the frame.  Instead we read ``msg.frames[0].raw[:3]`` which
        contains the actual response CAN ID.  ``msg.parsed()`` filters out
        non-OBD error lines ("NO DATA", "CAN ERROR") before extraction.

        Only call this on a CAN protocol; see ``is_can_protocol``.
        """
        if self._conn is None:
            raise RuntimeError("not connected")

        def _decode_physical_ids(msgs: list) -> list[int]:
            ids = []
            for msg in msgs:
                if not (msg.frames and msg.parsed()):
                    continue
                raw = msg.frames[0].raw  # e.g. "7E806410..." (no spaces, headers on)
                if len(raw) >= 3:
                    try:
                        ids.append(int(raw[:3], 16))
                    except ValueError:
                        pass
            return ids

        cmd = OBDCommand(
            f"MODE01_{pid:02X}_FNC",
            f"Mode 01 PID 0x{pid:02X} functional broadcast",
            bytes([0x01, pid]),
            0,
            _decode_physical_ids,
            ECU.ALL,
            False,
        )

        conn = self._conn

        def _send() -> list[int]:
            iface = getattr(conn, "interface", getattr(conn, "_interface", None))
            if iface is None:
                return []
            try:
                iface.send_and_parse("ATSH 7DF")
            except Exception as exc:
                log.warning("ATSH 7DF failed: %s", exc)
                return []
            resp = conn.query(cmd, force=True)
            if resp.is_null() or resp.value is None:
                return []
            return list(resp.value)

        async with self._lock:
            return await asyncio.to_thread(_send)

    async def probe_ecu_tester_present(self, addr: int) -> bool:
        """Send TesterPresent (0x3E 0x00) to *addr*; return True on positive response.

        Uses the same atomic ATSH-then-request pattern as
        ``query_uds_dtc_at_addr`` so the header change and UDS request are
        serialised under one lock acquisition.

        A positive TesterPresent response is ``0x7E 0x00``; a negative
        response (``0x7F …``) or no reply returns ``False``.

        Only call this on a CAN protocol; see ``is_can_protocol``.
        """
        if self._conn is None:
            raise RuntimeError("not connected")

        cmd = OBDCommand(
            f"TP_{addr:03X}",
            f"TesterPresent @ 0x{addr:03X}",
            bytes([0x3E, 0x00]),
            0,
            _uds_passthrough,
            ECU.ALL,
            False,
        )

        conn = self._conn

        def _atomic() -> bool:
            iface = getattr(conn, "interface", getattr(conn, "_interface", None))
            if iface is None:
                return False
            try:
                iface.send_and_parse(f"ATSH {addr:03X}")
            except Exception as exc:
                log.warning("ATSH %03X failed: %s", addr, exc)
                return False
            resp = conn.query(cmd, force=True)
            if resp.is_null() or resp.value is None:
                return False
            raw = bytes(resp.value)
            return len(raw) >= 1 and raw[0] == 0x7E

        async with self._lock:
            return await asyncio.to_thread(_atomic)

    async def send_tester_present(self) -> None:
        """Send UDS TesterPresent (0x3E 0x00) keepalive — best-effort, never raises."""
        if self._conn is None:
            return
        cmd = OBDCommand(
            "TESTER_PRESENT",
            "UDS TesterPresent keepalive",
            bytes([0x3E, 0x00]),
            0,
            lambda msgs: None,
            ECU.ALL,
            False,
        )
        try:
            async with self._lock:
                await asyncio.to_thread(self._conn.query, cmd, force=True)
        except Exception as exc:
            log.warning("TesterPresent keepalive failed: %s", exc)

    @property
    def is_connected(self) -> bool:
        if self._conn is None:
            return False
        return self._conn.status() not in (OBDStatus.NOT_CONNECTED, OBDStatus.ELM_CONNECTED)

    @property
    def protocol_name(self) -> str:
        return self._conn.protocol_name() if self._conn else ""

    @property
    def is_can_protocol(self) -> bool:
        """True only when the active ELM327 protocol is a CAN variant (ISO 15765 / SAE J1939).

        Transmitting CAN frames at the wrong bit rate can cause bus errors on the
        vehicle network.  Always verify this before issuing any CAN-layer request.
        """
        return "CAN" in self.protocol_name

    @asynccontextmanager
    async def session(self) -> AsyncIterator[ObdConnection]:
        await self.connect()
        try:
            yield self
        finally:
            await self.close()
