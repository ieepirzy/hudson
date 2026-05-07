"""Dashboard pane — live PID gauges in a btop-style grid."""

from __future__ import annotations

import asyncio
import logging

from textual.app import ComposeResult
from textual.containers import Grid
from textual.widget import Widget
from textual.widgets import Static

from ..widgets.gauge import Gauge
from ...core.connection import ObdConnection
from ...core.init import InitResult
from ...core.poller import Reading

log = logging.getLogger(__name__)


class DashboardPane(Widget):
    """Live readings grid."""

    DEFAULT_CSS = """
    DashboardPane {
        height: 1fr;
        layout: vertical;
    }

    #pid-count {
        height: 1;
        color: $text-muted;
        padding: 0 1;
    }

    #dashboard-grid {
        grid-size: 3 2;
        grid-gutter: 1;
        padding: 1;
        height: 1fr;
    }

    Gauge {
        border: round $primary 50%;
        padding: 0 1;
        height: 100%;
    }

    Gauge.gauge--disabled {
        border: round $surface-lighten-1;
        color: $text-muted 50%;
    }

    .gauge--label {
        color: $text-muted;
        text-style: bold;
    }

    .gauge--value {
        color: $accent;
        text-style: bold;
        text-align: right;
    }
    """

    def __init__(
        self,
        connection: ObdConnection,
        queue: asyncio.Queue[Reading],
        init_result: InitResult,
        id: str | None = None,
    ) -> None:
        super().__init__(id=id)
        self._connection = connection
        self._queue = queue
        self._init = init_result
        self._gauges: dict[str, Gauge] = {}

    def compose(self) -> ComposeResult:
        n = len(self._init.supported_commands)
        yield Static(f" {n} PIDs supported", id="pid-count")
        with Grid(id="dashboard-grid"):
            specs = [
                ("RPM", "RPM", "rpm"),
                ("SPEED", "Speed", "km/h"),
                ("THROTTLE_POS", "Throttle", "%"),
                ("COOLANT_TEMP", "Coolant", "°C"),
                ("INTAKE_TEMP", "Intake air", "°C"),
                ("ENGINE_LOAD", "Engine load", "%"),
            ]
            for cmd_name, label, unit in specs:
                g = Gauge(label, unit=unit, widget_id=f"g-{cmd_name.lower()}")
                self._gauges[cmd_name] = g
                yield g

    async def on_mount(self) -> None:
        supported = {c.name for c in self._init.supported_commands}
        for name, gauge in self._gauges.items():
            if name not in supported:
                gauge.disable()
        self.run_worker(self._consume(), exclusive=True)

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
