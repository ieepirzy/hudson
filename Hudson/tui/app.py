"""Main Textual application."""

from __future__ import annotations

import logging

from textual.app import App, ComposeResult
from textual.binding import Binding

from .core.connection import ObdConnection
from .tui.screens.splash import SplashScreen
from .tui.screens.main import MainScreen

log = logging.getLogger(__name__)


class HudsonApp(App[None]):
    """Hudson — OBD2 diagnostic TUI."""

    TITLE = "Hudson"
    CSS_PATH = "tui/app.tcss"

    BINDINGS = [
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self, connection: ObdConnection) -> None:
        super().__init__()
        self._connection = connection

    def compose(self) -> ComposeResult:
        return iter([])

    async def on_mount(self) -> None:
        self.run_worker(self._init_flow(), exclusive=True)

    async def _init_flow(self) -> None:
        result = await self.push_screen_wait(SplashScreen(self._connection))
        if result is None:
            return
        await self.push_screen(MainScreen(self._connection, result))
