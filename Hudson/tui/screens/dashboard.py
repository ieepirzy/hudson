"""Dashboard screen — live PID gauges.

Filters the desired poll set against `init_result.supported_commands` so we
don't waste bandwidth on PIDs the car doesn't answer to.
"""

from __future__ import annotations

import asyncio
import logging

import obd
from textual.app import ComposeResult
from textual.containers import Grid, Horizontal
from textual.screen import Screen
from textual.widgets import Static

from Hudson.core.connection import ObdConnection
from Hudson.core.init import InitResult
from Hudson.core.poller import Poller, PollSpec, Reading
from Hudson.tui.widgets.gauge import Gauge

log = logging.getLogger(__name__)


# Default PID priorities. Only specs whose command is supported by the car
# will actually be polled (filtering happens in on_mount).
DEFAULT_POLL_SPECS: list[PollSpec] = [
    PollSpec(obd.commands.RPM, 0.1),
    PollSpec(obd.commands.SPEED, 0.1),
    PollSpec(obd.commands.THROTTLE_POS, 0.1),
    PollSpec(obd.commands.COOLANT_TEMP, 1.0),
    PollSpec(obd.commands.INTAKE_TEMP, 1.0),
    PollSpec(obd.commands.ENGINE_LOAD, 0.5),
]


class DashboardScreen(Screen[None]):
    """Live readings grid."""

    def __init__(
        self,
        connection: ObdConnection,
        queue: asyncio.Queue[Reading],
        init_result: InitResult,
    ) -> None:
        super().__init__()
        self._connection = connection
        self._queue = queue
        self._init = init_result
        self._poller: Poller | None = None
        self._gauges: dict[str, Gauge] = {}

    def compose(self) -> ComposeResult:
        # Vehicle info strip across the top.
        info_bits = [
            f"VIN: {self._init.vin or '—'}",
            f"Mfr: {self._init.manufacturer_name}",
            f"Proto: {self._init.protocol_name or '—'}",
            f"PIDs: {len(self._init.supported_commands)}",
        ]
        yield Static("   ·   ".join(info_bits), id="vehicle-info")

        with Grid(id="dashboard-grid"):
            self._gauges["RPM"] = Gauge("RPM", unit="rpm", widget_id="g-rpm")
            self._gauges["SPEED"] = Gauge("Speed", unit="km/h", widget_id="g-speed")
            self._gauges["THROTTLE_POS"] = Gauge("Throttle", unit="%", widget_id="g-throttle")
            self._gauges["COOLANT_TEMP"] = Gauge("Coolant", unit="°C", widget_id="g-coolant")
            self._gauges["INTAKE_TEMP"] = Gauge("Intake", unit="°C", widget_id="g-intake")
            self._gauges["ENGINE_LOAD"] = Gauge("Load", unit="%", widget_id="g-load")
            for gauge in self._gauges.values():
                yield gauge

    async def on_mount(self) -> None:
        # Filter the wishlist to commands the car actually supports.
        supported_names = {c.name for c in self._init.supported_commands}
        active_specs = [s for s in DEFAULT_POLL_SPECS if s.command.name in supported_names]
        skipped = [s.command.name for s in DEFAULT_POLL_SPECS if s.command.name not in supported_names]

        if skipped:
            log.info("skipping unsupported commands: %s", ", ".join(skipped))
            for name in skipped:
                gauge = self._gauges.get(name)
                if gauge is not None:
                    gauge.disable()

        if not active_specs:
            log.warning("no supported commands match dashboard wishlist")
            return

        self._poller = Poller(self._connection, active_specs, self._queue)
        await self._poller.start()
        self.run_worker(self._consume(), exclusive=True)

    async def on_unmount(self) -> None:
        if self._poller is not None:
            await self._poller.stop()

    async def _consume(self) -> None:
        while True:
            reading = await self._queue.get()
            gauge = self._gauges.get(reading.command.name)
            if gauge is None:
                continue
            value = reading.response.value
            if value is None:
                gauge.value = None
                continue
            try:
                gauge.value = float(value.magnitude)
            except AttributeError:
                gauge.value = float(value)
