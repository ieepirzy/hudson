"""Main screen — btop-style tab bar with swappable content panes.

Layout:
    ┌─ HUDSON ──── VIN · Mfr · Protocol · PIDs ──────────────── HH:MM ─┐
    │ [ Dashboard ]  [ DTCs ]  [ Log ]  [ Vehicle ]                      │
    ├────────────────────────────────────────────────────────────────────┤
    │  (active pane)                                                      │
    └────────────────────────────────────────────────────────────────────┘
      ←/→ switch   q quit   c clear DTCs (DTC pane only)
"""

from __future__ import annotations

import asyncio
import logging

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import ContentSwitcher, Horizontal, Vertical
from textual.screen import Screen
from textual.widget import Widget
from textual.widgets import Static

from .core.connection import ObdConnection
from .core.init import InitResult
from .core.poller import Poller, PollSpec, Reading
from .tui.panes.dashboard import DashboardPane
from .tui.panes.dtcs import DtcPane
from .tui.panes.log import LogPane
from .tui.panes.vehicle import VehiclePane

log = logging.getLogger(__name__)

TABS: list[tuple[str, str]] = [
    ("dashboard", "Dashboard"),
    ("dtcs", "DTCs"),
    ("log", "Log"),
    ("vehicle", "Vehicle"),
]


class TabBar(Widget):
    """btop-style bracketed tab bar."""

    DEFAULT_CSS = """
    TabBar {
        height: 1;
        background: $surface;
        padding: 0 1;
    }
    .tab {
        padding: 0 1;
        color: $text-muted;
    }
    .tab--active {
        color: $accent;
        text-style: bold;
    }
    """

    def __init__(self, tabs: list[tuple[str, str]], active: str) -> None:
        super().__init__()
        self._tabs = tabs
        self._active = active

    def compose(self) -> ComposeResult:
        with Horizontal():
            for tab_id, label in self._tabs:
                classes = "tab tab--active" if tab_id == self._active else "tab"
                yield Static(f"[ {label} ]", classes=classes, id=f"tab-{tab_id}")

    def set_active(self, tab_id: str) -> None:
        for tid, _ in self._tabs:
            widget = self.query_one(f"#tab-{tid}", Static)
            if tid == tab_id:
                widget.add_class("tab--active")
                widget.remove_class("tab")
                widget.remove_class("tab")
            else:
                widget.remove_class("tab--active")
                if "tab" not in widget.classes:
                    widget.add_class("tab")
        self._active = tab_id


class MainScreen(Screen[None]):
    """Single persistent screen housing all panes."""

    BINDINGS = [
        Binding("left", "prev_tab", "← prev", show=True),
        Binding("right", "next_tab", "→ next", show=True),
        Binding("q", "app.quit", "Quit", show=True),
    ]

    DEFAULT_CSS = """
    MainScreen {
        layout: vertical;
    }

    #header-strip {
        height: 1;
        background: $primary;
        color: $text;
        padding: 0 1;
        text-style: bold;
    }

    #tab-bar-container {
        height: 1;
        background: $surface-darken-1;
    }

    #content {
        height: 1fr;
    }
    """

    def __init__(
        self,
        connection: ObdConnection,
        init_result: InitResult,
    ) -> None:
        super().__init__()
        self._connection = connection
        self._init = init_result
        self._queue: asyncio.Queue[Reading] = asyncio.Queue()
        self._poller: Poller | None = None
        self._tab_ids = [t[0] for t in TABS]
        self._active_idx = 0

    def compose(self) -> ComposeResult:
        info = (
            f" HUDSON   "
            f"VIN: {self._init.vin or '—'}   "
            f"{self._init.manufacturer_name}   "
            f"{self._init.protocol_name or '—'}   "
            f"PIDs: {len(self._init.supported_commands)}"
        )
        yield Static(info, id="header-strip")

        with Horizontal(id="tab-bar-container"):
            yield TabBar(TABS, self._tab_ids[0])

        with ContentSwitcher(initial="dashboard", id="content"):
            yield DashboardPane(self._connection, self._queue, self._init, id="dashboard")
            yield DtcPane(self._connection, self._init, id="dtcs")
            yield LogPane(id="log")
            yield VehiclePane(self._init, id="vehicle")

    async def on_mount(self) -> None:
        from .core.poller import PollSpec
        import obd

        DEFAULT_POLL_SPECS: list[PollSpec] = [
            PollSpec(obd.commands.RPM, 0.1),
            PollSpec(obd.commands.SPEED, 0.1),
            PollSpec(obd.commands.THROTTLE_POS, 0.1),
            PollSpec(obd.commands.COOLANT_TEMP, 1.0),
            PollSpec(obd.commands.INTAKE_TEMP, 1.0),
            PollSpec(obd.commands.ENGINE_LOAD, 0.5),
        ]

        supported_names = {c.name for c in self._init.supported_commands}
        active_specs = [s for s in DEFAULT_POLL_SPECS if s.command.name in supported_names]

        if active_specs:
            self._poller = Poller(self._connection, active_specs, self._queue)
            await self._poller.start()

    async def on_unmount(self) -> None:
        if self._poller:
            await self._poller.stop()

    def action_next_tab(self) -> None:
        self._active_idx = (self._active_idx + 1) % len(self._tab_ids)
        self._switch_to(self._tab_ids[self._active_idx])

    def action_prev_tab(self) -> None:
        self._active_idx = (self._active_idx - 1) % len(self._tab_ids)
        self._switch_to(self._tab_ids[self._active_idx])

    def _switch_to(self, tab_id: str) -> None:
        self.query_one(ContentSwitcher).current = tab_id
        self.query_one(TabBar).set_active(tab_id)
