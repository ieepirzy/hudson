"""Splash screen — runs the init sequence with live step-by-step progress.

Visually:

  ┌─ Hudson ─────────────────────────────────────────┐
  │                                                  │
  │   ✓  Connection      ISO 15765-4 (CAN 11/500)    │
  │   ✓  Protocol        ISO 15765-4 (CAN 11/500)    │
  │   ✓  VIN             WV2ZZZ7HZ8H123456            │
  │   ✓  Manufacturer    VW/Audi                     │
  │   ⏳  Supported PIDs  probing...                   │
  │   ·  Ready                                       │
  │                                                  │
  └──────────────────────────────────────────────────┘
"""

from __future__ import annotations

import asyncio
import logging

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import Screen
from textual.widgets import Label, Static

from src.hudson.core.connection import ObdConnection
from src.hudson.core.init import InitEvent, InitResult, InitStep, run_init

log = logging.getLogger(__name__)


_STEP_LABELS: dict[InitStep, str] = {
    InitStep.CONNECT: "Connection",
    InitStep.PROTOCOL: "Protocol",
    InitStep.VIN: "VIN",
    InitStep.MANUFACTURER: "Manufacturer",
    InitStep.SUPPORTED_PIDS: "Supported PIDs",
    InitStep.READY: "Ready",
}


class SplashScreen(Screen[InitResult]):
    """Run init, show progress, dismiss with the result on completion."""

    DEFAULT_CSS = """
    SplashScreen {
        align: center middle;
    }
    SplashScreen > Vertical {
        width: 70;
        height: auto;
        border: round $primary;
        padding: 1 2;
    }
    .splash-title {
        text-align: center;
        text-style: bold;
        color: $accent;
        margin-bottom: 1;
    }
    .splash-step {
        height: 1;
    }
    .splash-step-error {
        color: $warning;
    }
    """

    def __init__(self, connection: ObdConnection) -> None:
        super().__init__()
        self._connection = connection
        self._events: asyncio.Queue[InitEvent] = asyncio.Queue()
        self._labels: dict[InitStep, Label] = {}

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static("HUDSON — pre-flight check", classes="splash-title")
            for step, label_text in _STEP_LABELS.items():
                lbl = Label(f" ·  {label_text}", classes="splash-step")
                self._labels[step] = lbl
                yield lbl

    async def on_mount(self) -> None:
        # Drive the init sequence and the consumer concurrently.
        consumer = asyncio.create_task(self._consume_events())
        try:
            result = await run_init(self._connection, self._events)
        except Exception as exc:  # noqa: BLE001
            log.exception("init failed")
            await self._events.put(
                InitEvent(InitStep.CONNECT, "init aborted", error=str(exc), done=True)
            )
            await asyncio.sleep(2)  # let user see the error
            consumer.cancel()
            self.app.exit(message=f"Init failed: {exc}")
            return

        # Let final event render, then dismiss.
        await asyncio.sleep(0.2)
        consumer.cancel()
        self.dismiss(result)

    async def _consume_events(self) -> None:
        while True:
            event = await self._events.get()
            label = self._labels.get(event.step)
            if label is None:
                continue
            base_text = _STEP_LABELS[event.step]
            if event.error:
                marker = "✗"
                text = f"{base_text}  —  {event.error}"
                label.add_class("splash-step-error")
            elif event.done:
                marker = "✓"
                text = f"{base_text}  —  {event.detail}" if event.detail else base_text
            else:
                marker = "⏳"
                text = f"{base_text}  —  {event.detail}" if event.detail else base_text
            label.update(f" {marker}  {text}")
