"""Tiered async poller for live PIDs.

Different PIDs have different useful sampling rates:

  - RPM, throttle, speed: 10 Hz (fast-changing, drive feel)
  - Coolant, intake air, voltage: 1 Hz (slow physical processes)
  - Fuel level, ambient temp: 0.2 Hz (essentially static during a session)

The poller schedules each command on its own cadence rather than blasting
them all at the highest rate. This respects ELM327 UART bandwidth and CAN
bus politeness without us thinking about it per-screen.

Each command emits readings into an asyncio.Queue that the TUI consumes.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from time import monotonic
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from obd import OBDCommand, OBDResponse

    from .core.connection import ObdConnection

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class PollSpec:
    """How often to poll a single command."""

    command: OBDCommand
    period_s: float  # 0.1 = 10 Hz, 1.0 = 1 Hz, 5.0 = 0.2 Hz


@dataclass(frozen=True, slots=True)
class Reading:
    """A single sample emitted by the poller."""

    command: OBDCommand
    response: OBDResponse
    received_at: float  # monotonic seconds


class Poller:
    """Drive a `ObdConnection` against a list of `PollSpec`s, emitting Readings."""

    def __init__(
        self,
        connection: ObdConnection,
        specs: list[PollSpec],
        out_queue: asyncio.Queue[Reading],
        *,
        on_reading: Callable[[Reading], None] | None = None,
    ) -> None:
        self._conn = connection
        self._specs = specs
        self._out = out_queue
        self._on_reading = on_reading
        self._tasks: list[asyncio.Task[None]] = []
        self._stopped = asyncio.Event()

    async def start(self) -> None:
        if self._tasks:
            raise RuntimeError("poller already started")
        self._stopped.clear()
        self._tasks = [asyncio.create_task(self._run_one(s)) for s in self._specs]
        log.info("poller started with %d commands", len(self._specs))

    async def stop(self) -> None:
        self._stopped.set()
        for task in self._tasks:
            task.cancel()
        # Drain cancellations.
        for task in self._tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._tasks.clear()

    async def _run_one(self, spec: PollSpec) -> None:
        """Poll a single command on its own cadence."""
        period = spec.period_s
        next_deadline = monotonic()
        while not self._stopped.is_set():
            try:
                response = await self._conn.query(spec.command)
            except Exception:
                log.exception("query failed for %s", spec.command.name)
                # Backoff so a sick PID doesn't spam logs.
                await asyncio.sleep(min(period * 5, 30.0))
                next_deadline = monotonic() + period
                continue

            reading = Reading(command=spec.command, response=response, received_at=monotonic())
            await self._out.put(reading)
            if self._on_reading is not None:
                try:
                    self._on_reading(reading)
                except Exception:
                    log.warning("on_reading callback raised for %s", spec.command.name, exc_info=True)

            next_deadline += period
            sleep_for = next_deadline - monotonic()
            if sleep_for > 0:
                await asyncio.sleep(sleep_for)
            else:
                # We're behind; reset the schedule rather than busy-loop catching up.
                next_deadline = monotonic()
