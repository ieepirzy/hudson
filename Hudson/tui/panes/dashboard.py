"""Dashboard pane — dynamic gauge layout driven by PID discovery."""

from __future__ import annotations

import asyncio
import logging
from time import monotonic

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widget import Widget
from textual.widgets import Static

from Hudson.core.connection import ObdConnection
from Hudson.core.init import InitResult
from Hudson.core.poller import Reading
from Hudson.tui.widgets.gauge import GAUGE_CATALOG, Gauge

log = logging.getLogger(__name__)


class SessionStats(Static):
    """Small stat box — uptime, polls, errors, DTCs."""

    DEFAULT_CSS = """
    SessionStats {
        border: round $primary 40%;
        height: auto;
        padding: 0 1;
    }
    """

    def __init__(self, dtc_count: int = 0) -> None:
        super().__init__("")
        self._start = monotonic()
        self._polls = 0
        self._dtc_count = dtc_count

    def on_mount(self) -> None:
        self._refresh_display()
        self.set_interval(1, self._refresh_display)

    def increment_polls(self) -> None:
        self._polls += 1

    def _refresh_display(self) -> None:
        elapsed = int(monotonic() - self._start)
        h = elapsed // 3600
        m = (elapsed % 3600) // 60
        s = elapsed % 60
        uptime = f"{h:02d}:{m:02d}:{s:02d}"
        dtc_color = "gold" if self._dtc_count > 0 else "limegreen"
        self.update(
            f"[bold dim]SESSION[/]\n"
            f"[dim]Uptime[/]  [white]{uptime}[/]\n"
            f"[dim]Polls[/]   [white]{self._polls:,}[/]\n"
            f"[dim]Errors[/]  [limegreen]0[/]\n"
            f"[dim]DTCs[/]    [{dtc_color}]{self._dtc_count}[/]"
        )


class ConnectionStats(Static):
    """Small stat box — port, dongle, status."""

    DEFAULT_CSS = """
    ConnectionStats {
        border: round $primary 40%;
        height: auto;
        padding: 0 1;
    }
    """

    def __init__(self, init_result: InitResult) -> None:
        super().__init__("")
        self._init = init_result

    def on_mount(self) -> None:
        self.update(
            f"[bold dim]CONNECTION[/]\n"
            f"[dim]Port[/]    [white]—[/]\n"
            f"[dim]Dongle[/]  [white]ELM327[/]\n"
            f"[dim]Status[/]  [limegreen]Connected[/]"
        )


class DashboardPane(Widget):
    """Split dashboard — gauges auto-discovered from supported PIDs."""

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

        supported_names = {c.name for c in init_result.supported_commands}
        # Preserve catalog order; only include PIDs the car actually supports.
        self._active: list[tuple[str, object]] = [
            (pid, cfg)
            for pid, cfg in GAUGE_CATALOG.items()
            if pid in supported_names
        ]

    def compose(self) -> ComposeResult:
        mid = (len(self._active) + 1) // 2  # left column gets the larger half
        left_pids = self._active[:mid]
        right_pids = self._active[mid:]

        with Vertical(id="dash-left"):
            for pid, cfg in left_pids:
                g = Gauge(pid, cfg, widget_id=f"g-{pid.lower()}")
                self._gauges[pid] = g
                yield g

        with Vertical(id="dash-right"):
            for pid, cfg in right_pids:
                g = Gauge(pid, cfg, widget_id=f"g-{pid.lower()}")
                self._gauges[pid] = g
                yield g
            self._session = SessionStats()
            yield self._session
            yield ConnectionStats(self._init)

    async def on_mount(self) -> None:
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
