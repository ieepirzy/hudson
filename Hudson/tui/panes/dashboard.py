"""Dashboard pane — split layout with colored gauges and session stats."""

from __future__ import annotations

import asyncio
import logging
from time import monotonic

import obd
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widget import Widget
from textual.widgets import Static

from Hudson.core.connection import ObdConnection
from Hudson.core.init import InitResult
from Hudson.core.poller import Reading
from Hudson.tui.widgets.gauge import Gauge

log = logging.getLogger(__name__)

GAUGE_SPECS = [
    ("RPM",          "RPM",         "rpm"),
    ("SPEED",        "Speed",       "km/h"),
    ("THROTTLE_POS", "Throttle",    "%"),
    ("COOLANT_TEMP", "Coolant",     "°C"),
    ("INTAKE_TEMP",  "Intake air",  "°C"),
    ("ENGINE_LOAD",  "Engine load", "%"),
]

LEFT_PIDS  = ["RPM", "SPEED", "THROTTLE_POS"]
RIGHT_PIDS = ["COOLANT_TEMP", "INTAKE_TEMP", "ENGINE_LOAD"]


class SessionStats(Widget):
    """Small stat box — uptime, polls, errors, DTCs."""

    DEFAULT_CSS = """
    SessionStats {
        border: round $primary 40%;
        height: auto;
        padding: 0 1;
    }
    .ss-title {
        color: $accent;
        text-style: bold;
        opacity: 0.5;
        height: 1;
    }
    .ss-row { height: 1; }
    .ss-ok   { color: limegreen; }
    .ss-warn { color: gold; }
    """

    def __init__(self, dtc_count: int = 0) -> None:
        super().__init__()
        self._start = monotonic()
        self._polls = 0
        self._dtc_count = dtc_count

    def compose(self) -> ComposeResult:
        yield Static("SESSION", classes="ss-title")
        yield Static(classes="ss-row", id="row-uptime")
        yield Static(classes="ss-row", id="row-polls")
        yield Static(classes="ss-row", id="row-errors")
        yield Static(classes="ss-row", id="row-dtcs")

    def on_mount(self) -> None:
        self._render()
        self.set_interval(1, self._render)

    def increment_polls(self) -> None:
        self._polls += 1

    def _render(self) -> None:
        elapsed = int(monotonic() - self._start)
        h = elapsed // 3600
        m = (elapsed % 3600) // 60
        s = elapsed % 60
        uptime = f"{h:02d}:{m:02d}:{s:02d}"
        dtc_class = "ss-warn" if self._dtc_count > 0 else "ss-ok"
        self.query_one("#row-uptime", Static).update(
            f"[dim]Uptime[/]  [white]{uptime}[/]"
        )
        self.query_one("#row-polls", Static).update(
            f"[dim]Polls[/]  [white]{self._polls:,}[/]"
        )
        self.query_one("#row-errors", Static).update(
            f"[dim]Errors[/]  [limegreen]0[/]"
        )
        self.query_one("#row-dtcs", Static).update(
            f"[dim]DTCs[/]  [{dtc_class}]{self._dtc_count}[/]"
        )


class ConnectionStats(Widget):
    """Small stat box — port, dongle, status."""

    DEFAULT_CSS = """
    ConnectionStats {
        border: round $primary 40%;
        height: auto;
        padding: 0 1;
    }
    .cs-title { color: $accent; text-style: bold; opacity: 0.5; height: 1; }
    .cs-row { height: 1; }
    """

    def __init__(self, init_result: InitResult) -> None:
        super().__init__()
        self._init = init_result

    def compose(self) -> ComposeResult:
        yield Static("CONNECTION", classes="cs-title")
        yield Static(f"[dim]Port[/]    [white]—[/]", classes="cs-row")
        yield Static(f"[dim]Dongle[/]  [white]ELM327[/]", classes="cs-row")
        yield Static(f"[dim]Status[/]  [limegreen]Connected[/]", classes="cs-row")


class DashboardPane(Widget):
    """Split dashboard — 3 tall gauges left, 3 shorter right + stat boxes."""

    DEFAULT_CSS = """
    DashboardPane {
        layout: horizontal;
        padding: 0;
        height: 1fr;
    }
    #dash-left {
        layout: vertical;
        width: 2fr;
        padding: 1;
    }
    #dash-right {
        layout: vertical;
        width: 1fr;
        padding: 1;
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
        self._session: SessionStats | None = None

    def compose(self) -> ComposeResult:
        with Vertical(id="dash-left"):
            for pid, label, unit in GAUGE_SPECS:
                if pid in LEFT_PIDS:
                    g = Gauge(label, pid, unit=unit, widget_id=f"g-{pid.lower()}")
                    self._gauges[pid] = g
                    yield g

        with Vertical(id="dash-right"):
            for pid, label, unit in GAUGE_SPECS:
                if pid in RIGHT_PIDS:
                    g = Gauge(label, pid, unit=unit, widget_id=f"g-{pid.lower()}")
                    self._gauges[pid] = g
                    yield g
            self._session = SessionStats()
            yield self._session
            yield ConnectionStats(self._init)

    async def on_mount(self) -> None:
        supported = {c.name for c in self._init.supported_commands}
        for pid, gauge in self._gauges.items():
            if pid not in supported:
                gauge.disable()
        self.run_worker(self._consume(), exclusive=True)

    async def _consume(self) -> None:
        while True:
            reading = await self._queue.get()
            gauge = self._gauges.get(reading.command.name)
            if gauge is None:
                continue
            if self._session:
                self._session.increment_polls()
            value = reading.response.value
            if value is None:
                gauge.value = None
                continue
            try:
                gauge.value = float(value.magnitude)
            except AttributeError:
                gauge.value = float(value)
