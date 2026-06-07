"""Main Textual application."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from textual.app import App, ComposeResult
from textual.binding import Binding

from Hudson.core.connection import ObdConnection
from Hudson.tui.screens.splash import SplashScreen
from Hudson.tui.screens.main import MainScreen

if TYPE_CHECKING:
    from Hudson.core.telemetry import TelemetryClient

log = logging.getLogger(__name__)


class HudsonApp(App[None]):
    """Hudson — OBD2 diagnostic TUI."""

    TITLE = "Hudson"
    CSS_PATH = "app.tcss"

    BINDINGS = [
        Binding("q", "quit", "Quit", priority=True),
    ]

    def __init__(
        self,
        connection: ObdConnection,
        telemetry: TelemetryClient | None = None,
    ) -> None:
        super().__init__()
        self._connection = connection
        self._telemetry = telemetry

    def compose(self) -> ComposeResult:
        return iter([])

    async def on_mount(self) -> None:
        self.run_worker(self._init_flow(), exclusive=True)

    async def _init_flow(self) -> None:
        result = await self.push_screen_wait(SplashScreen(self._connection))
        if result is None:
            return
        if self._telemetry is not None:
            try:
                await self._telemetry.start(result)
            except Exception:
                log.exception("telemetry start failed")
        await self.push_screen(MainScreen(self._connection, result, self._telemetry))
