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
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, AsyncIterator

import obd

if TYPE_CHECKING:
    from obd import OBDCommand, OBDResponse

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ConnectionConfig:
    """Settings for opening an ELM327 connection."""

    portstr: str | None = None  # e.g. "/dev/rfcomm0"; None = auto-detect
    baudrate: int | None = None  # None = auto-detect
    protocol: str | None = None  # None = auto-detect; "6" = ISO 15765-4 (CAN 11/500)
    fast: bool = True  # python-obd's "fast" mode reuses the last response if cached
    timeout: float = 0.1
    check_voltage: bool = True


class ObdConnection:
    """Async-friendly wrapper around `obd.OBD`."""

    def __init__(self, config: ConnectionConfig | None = None) -> None:
        self._config = config or ConnectionConfig()
        self._conn: obd.OBD | None = None
        self._lock = asyncio.Lock()

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
        if not self._conn.is_connected():
            status = self._conn.status()
            raise ConnectionError(f"failed to connect to ELM327: {status}")

        log.info("connected: protocol=%s port=%s", self._conn.protocol_name(), self._conn.port_name())

    async def close(self) -> None:
        if self._conn is None:
            return
        await asyncio.to_thread(self._conn.close)
        self._conn = None

    async def query(self, cmd: OBDCommand, force: bool = False) -> OBDResponse:
        """Send a single OBD command, awaiting the response."""
        if self._conn is None:
            raise RuntimeError("not connected")
        async with self._lock:
            return await asyncio.to_thread(self._conn.query, cmd, force=force)

    async def supported_commands(self) -> set[OBDCommand]:
        """Return the set of commands the connected vehicle reports as supported."""
        if self._conn is None:
            raise RuntimeError("not connected")
        # `supported_commands` is a property that reads pre-probed state, no I/O.
        return set(self._conn.supported_commands)

    @property
    def is_connected(self) -> bool:
        return self._conn is not None and self._conn.is_connected()

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
