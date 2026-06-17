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

    @asynccontextmanager
    async def session(self) -> AsyncIterator[ObdConnection]:
        await self.connect()
        try:
            yield self
        finally:
            await self.close()
