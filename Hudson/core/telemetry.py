"""Telemetry client — POST live sensor data to the Hudson telemetry endpoint.

Activated via `--telemetry` on the CLI. Token is read from the
HUDSON_TELEMETRY_TOKEN environment variable.

Three event types are sent:
  session_start  — once on init completion (VIN, manufacturer, protocol)
  readings       — batched PID values every BATCH_INTERVAL seconds
  dtc_scan       — after each DTC scan in the DTCs pane
  session_end    — on clean shutdown

All network I/O is best-effort: failures are logged at WARNING and discarded.
The TUI and OBD2 collection paths are never blocked by telemetry errors.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    from Hudson.core.init import InitResult
    from Hudson.core.poller import Reading

log = logging.getLogger(__name__)

_TELEMETRY_URL = "https://api.muutto365.fi/api/telemetry"
_BATCH_INTERVAL = 5.0   # seconds between reading POSTs
_MAX_BATCH = 500        # max readings per POST (guard against oversized payloads)
_USER_AGENT = "Hudson/0.1 (OBD2 diagnostic)"


class TelemetryClient:
    """Async telemetry sender.

    Lifecycle::

        client = TelemetryClient(token)
        await client.start(init_result)   # after init completes
        ...
        await client.stop()               # on shutdown, flushes buffered readings
    """

    def __init__(self, token: str) -> None:
        self._session_id = str(uuid.uuid4())
        self._http = httpx.AsyncClient(
            base_url=_TELEMETRY_URL,
            headers={
                "Authorization": f"Bearer {token}",
                "User-Agent": _USER_AGENT,
            },
            timeout=10.0,
        )
        self._reading_queue: asyncio.Queue[dict] = asyncio.Queue(maxsize=_MAX_BATCH * 2)
        self._batch_task: asyncio.Task[None] | None = None

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(self, init_result: InitResult) -> None:
        """Emit session_start and begin the background batch sender."""
        payload = {
            "event": "session_start",
            "session_id": self._session_id,
            "vin": init_result.vin,
            "manufacturer": init_result.manufacturer_name,
            "protocol": init_result.protocol_name,
            "timestamp": _utcnow(),
        }
        await self._post(payload)
        self._batch_task = asyncio.create_task(self._batch_loop())
        log.info("telemetry started: session_id=%s", self._session_id)

    async def stop(self) -> None:
        """Flush buffered readings, emit session_end, close HTTP client."""
        if self._batch_task is not None:
            self._batch_task.cancel()
            try:
                await self._batch_task
            except asyncio.CancelledError:
                pass
        await self._flush()
        await self._post(
            {"event": "session_end", "session_id": self._session_id, "timestamp": _utcnow()}
        )
        await self._http.aclose()
        log.info("telemetry stopped: session_id=%s", self._session_id)

    # ── Data ingestion ─────────────────────────────────────────────────────────

    def record_reading(self, reading: Reading) -> None:
        """Enqueue a single PID reading for the next batch POST.

        Called from the Poller's on_reading callback — synchronous and
        non-blocking. Readings are dropped silently if the queue is full
        (the batch loop isn't keeping up, which means the network is slow).
        """
        value = getattr(reading.response.value, "magnitude", None)
        if value is None:
            return
        try:
            self._reading_queue.put_nowait(
                {"pid": reading.command.name, "value": float(value), "ts": time.time()}
            )
        except asyncio.QueueFull:
            pass

    async def record_dtcs(
        self,
        stored: list[str],
        pending: list[str],
        permanent: list[str],
    ) -> None:
        """POST a DTC scan result. DTC scans are infrequent — not batched.

        Fire-and-forget: creates a task and returns immediately so the DTC
        pane is not blocked waiting for the network.
        """
        payload = {
            "event": "dtc_scan",
            "session_id": self._session_id,
            "timestamp": _utcnow(),
            "stored": stored,
            "pending": pending,
            "permanent": permanent,
        }
        asyncio.create_task(self._post(payload))

    # ── Background batch loop ─────────────────────────────────────────────────

    async def _batch_loop(self) -> None:
        while True:
            await asyncio.sleep(_BATCH_INTERVAL)
            await self._flush()

    async def _flush(self) -> None:
        batch: list[dict] = []
        while not self._reading_queue.empty() and len(batch) < _MAX_BATCH:
            batch.append(self._reading_queue.get_nowait())
        if not batch:
            return
        await self._post(
            {
                "event": "readings",
                "session_id": self._session_id,
                "timestamp": _utcnow(),
                "readings": batch,
            }
        )

    # ── HTTP ──────────────────────────────────────────────────────────────────

    async def _post(self, payload: dict) -> None:
        """POST JSON payload. Never raises — all errors go to the log file."""
        event = payload.get("event", "?")
        try:
            resp = await self._http.post("", json=payload)
            if resp.status_code >= 400:
                log.warning("telemetry %r → HTTP %d: %s", event, resp.status_code, resp.text[:120])
            else:
                log.debug("telemetry %r → HTTP %d", event, resp.status_code)
        except Exception as exc:
            log.warning("telemetry %r POST failed: %s", event, exc)


def _utcnow() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
