"""Main Textual application.

Flow:
    on_mount  →  push SplashScreen
    SplashScreen runs init, returns InitResult via dismiss()
    on init done  →  push DashboardScreen with init result
"""

from __future__ import annotations

import asyncio
import logging

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.widgets import Footer, Header

from Hudson.core.connection import ObdConnection
from Hudson.core.init import InitResult
from Hudson.core.poller import Reading
from Hudson.tui.screens.dashboard import DashboardScreen
from Hudson.tui.screens.splash import SplashScreen

log = logging.getLogger(__name__)


class HudsonApp(App[None]):
    """Hudson — OBD2 diagnostic TUI."""

    TITLE = "Hudson"
    SUB_TITLE = "live PIDs · DTCs · diagnostics"
    CSS_PATH = "app.tcss"

    BINDINGS = [
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self, connection: ObdConnection) -> None:
        super().__init__()
        self._connection = connection
        self._readings: asyncio.Queue[Reading] = asyncio.Queue()

    def compose(self) -> ComposeResult:
        yield Header()
        yield Footer()

    async def on_mount(self) -> None:
        # push_screen_wait must run inside a worker.
        self.run_worker(self._init_flow(), exclusive=True)

    async def _init_flow(self) -> None:
        splash = SplashScreen(self._connection)
        result = await self.push_screen_wait(splash)
        if result is None:
            return
        await self._show_dashboard(result)

    async def _show_dashboard(self, init_result: InitResult) -> None:
        await self.push_screen(
            DashboardScreen(self._connection, self._readings, init_result)
        )
