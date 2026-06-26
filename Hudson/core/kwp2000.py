"""KWP2000 (ISO 14230) session — pure transport layer.

Handles protocol framing only: session lifecycle and service 0x21
ReadDataByLocalIdentifier.  Manufacturer-specific block definitions
(IDs, byte offsets, field decoders) belong in the manufacturer module.

K-line transport via ELM327:
  ATSP3  ISO 14230-4 KWP fast init  (preferred)
  ATSP4  ISO 14230-4 KWP slow init  (5-baud legacy)

Protocol flow on real hardware:
  1. Select K-line protocol (ATSP3 / ATSP4)
  2. ATH1 to include response headers
  3. StartDiagnosticSession [80 F1 10 01 10 92] → positive 0x50
  4. ReadDataByLocalIdentifier [80 F1 10 21 <id> <cs>] → positive 0x61 <id> <data…>

Safety: only read services (0x21, 0x22) are present.  No write,
routine control, or security access services.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from Hudson.core.dtc import DtcRecord, decode_kwp_dtc_list

if TYPE_CHECKING:
    from Hudson.core.connection import ObdConnection

# ISO 14230-2 W3/W4: minimum delay after protocol switch before first message.
_KLINE_SWITCH_DELAY = 0.05

log = logging.getLogger(__name__)


# ── Generic data types (used by manufacturer modules) ─────────────────────────

@dataclass(frozen=True, slots=True)
class KwpField:
    """One named field within a KWP2000 measuring block."""

    name: str
    offset: int                              # byte offset within the block data
    count: int                               # number of bytes to read
    unit: str
    decode: Callable[[bytes], float | None]  # raw slice → physical value


@dataclass(frozen=True, slots=True)
class KwpBlock:
    """Definition for one KWP2000 ReadDataByLocalIdentifier block.

    Instances belong in manufacturer modules, not here.
    """

    block_id: int          # 1-byte local identifier
    name: str
    fields: tuple[KwpField, ...]


# ── Session ───────────────────────────────────────────────────────────────────

class KwpSession:
    """KWP2000 diagnostic session, supporting both K-line and CAN transport.

    ``transport`` selects the physical layer:

    - ``"kline"`` — ISO 14230-4 via ELM327 ATSP3/ATSP4 (K-line fast/slow init).
    - ``"can"``   — KWP2000 application layer wrapped in ISO-TP (ISO 15765-2)
                    over the already-active CAN protocol.  The ELM327 handles
                    ISO-TP framing automatically; only the ECU header (ATSH)
                    needs to be set.
    - ``None``    — auto-detect from ``connection.is_can_protocol`` at
                    construction time (default).

    ``ecu_addr`` is the CAN ECU header address used with ``ATSH`` for CAN
    transport; it is ignored on K-line.

    Pass ``mock_responses`` to enable mock mode for unit tests.  The dict
    maps block_id → raw data bytes (positive-response header already
    stripped).  Pass ``None`` for real hardware.

    .. note::
        KWP2000 over CAN (``transport="can"``) has not been validated on
        real hardware.  The ``0x81`` standardDiagnosticMode sub-function in
        ``StartDiagnosticSession`` is portable in practice, but behaviour
        on specific ECUs should be verified before relying on it.

    Example::

        from Hudson.manufacturers.volvo import MOCK_VOLVO_KWP_RESPONSES

        session = KwpSession(
            connection,
            mock_responses=MOCK_VOLVO_KWP_RESPONSES if connection.is_mock else None,
        )
        if await session.start_diagnostic_session():
            data = await session.query_block(0x01)
        await session.close()
    """

    def __init__(
        self,
        connection: ObdConnection,
        *,
        transport: str | None = None,
        ecu_addr: int = 0x7E0,
        mock_responses: dict[int, bytes] | None = None,
    ) -> None:
        if transport is None:
            transport = "can" if connection.is_can_protocol else "kline"
        if transport not in ("kline", "can"):
            raise ValueError(f"transport must be 'kline' or 'can', got {transport!r}")
        self._connection = connection
        self._transport = transport
        self._ecu_addr = ecu_addr
        self._mock_responses = mock_responses
        self._started = False

    @property
    def is_mock(self) -> bool:
        return self._mock_responses is not None

    @property
    def transport(self) -> str:
        return self._transport

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start_diagnostic_session(self) -> bool:
        """Send KWP2000 StartDiagnosticSession (service 0x10).

        Returns True on success, False if the ECU did not respond.
        """
        if self.is_mock:
            self._started = True
            log.debug("KWP2000 mock session started (transport=%s)", self._transport)
            return True

        if self._transport == "kline":
            return await self._start_kline()
        return await self._start_can()

    async def _start_kline(self) -> bool:
        try:
            await self._connection.send_at("ATSP3")
            await asyncio.sleep(_KLINE_SWITCH_DELAY)
            await self._connection.send_at("ATH1")
            resp = await self._connection.query_kwp_service(0x10, b"\x81")
            if resp is not None:
                self._started = True
                log.info("KWP2000 K-line session started")
                return True
            log.warning("KWP2000 StartDiagnosticSession (K-line): no positive response")
            return False
        finally:
            if not self._started:
                await self._connection.send_at("ATSP0")

    async def _start_can(self) -> bool:
        await self._connection.send_at(f"ATSH {self._ecu_addr:03X}")
        resp = await self._connection.query_kwp_service(0x10, b"\x81")
        if resp is not None:
            self._started = True
            log.info("KWP2000 CAN session started (ECU 0x%03X)", self._ecu_addr)
            return True
        log.warning(
            "KWP2000 StartDiagnosticSession (CAN @ 0x%03X): no positive response",
            self._ecu_addr,
        )
        return False

    async def close(self) -> None:
        """Close the session — best-effort, never raises."""
        if self._started and not self.is_mock:
            try:
                if self._transport == "can":
                    # Re-assert ATSH: intervening code (e.g. UDS DTC scanner) may
                    # have changed the header since _start_can() set it.
                    await self._connection.send_at(f"ATSH {self._ecu_addr:03X}")
                await self._connection.query_kwp_service(0x20)
            except Exception as exc:
                log.warning("KWP2000 StopDiagnosticSession failed: %s", exc)
            finally:
                if self._transport == "kline":
                    await self._connection.send_at("ATSP0")
        self._started = False
        log.debug("KWP2000 session closed (transport=%s)", self._transport)

    # ── Queries ───────────────────────────────────────────────────────────────

    async def query_block(self, block_id: int) -> bytes | None:
        """ReadDataByLocalIdentifier (service 0x21).

        Returns the data payload with the 0x61 + block_id response header
        already stripped, or None on negative response or no reply.
        """
        if not self._started:
            raise RuntimeError(
                "KwpSession not started — call start_diagnostic_session() first"
            )

        if self.is_mock:
            return self._mock_responses.get(block_id)  # type: ignore[union-attr]

        return await self._connection.query_enhanced_local(block_id)

    async def read_dtcs(self, status_mask: int = 0xFF) -> list[DtcRecord]:
        """ReadDiagnosticTroubleCodesByStatus (KWP2000 service 0x18).

        ``status_mask=0xFF`` requests all DTCs regardless of status.
        Returns an empty list if the session is not started, is in mock mode,
        or the ECU does not respond.
        """
        if not self._started or self.is_mock:
            return []

        # Payload: [status_mask, group_hi, group_lo] — group 0x0000 = all groups.
        # query_kwp_service strips the positive-response byte (0x58).
        # Response after strip: [numberOfDTC, hi, lo, status, ...]
        resp = await self._connection.query_kwp_service(0x18, bytes([status_mask, 0x00, 0x00]))
        if resp is None or len(resp) < 1:
            return []
        # resp[0] = numberOfDTC; resp[1:] = 3-byte records
        return decode_kwp_dtc_list(resp[1:])

    # ── Parsing ───────────────────────────────────────────────────────────────

    def parse_block(self, defn: KwpBlock, data: bytes) -> dict[str, float | None]:
        """Parse raw block bytes using a KwpBlock field definition.

        Fields whose byte range falls outside the data return None rather
        than raising, so a truncated response degrades gracefully.
        """
        result: dict[str, float | None] = {}
        for field in defn.fields:
            chunk = data[field.offset : field.offset + field.count]
            result[field.name] = field.decode(chunk) if len(chunk) == field.count else None
        return result
