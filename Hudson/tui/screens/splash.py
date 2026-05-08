"""Splash screen — runs the init sequence with live step-by-step progress.

Visually (UDS path):

  ┌─ HUDSON ─────────────────────────────────────────────────────────────┐
  │                                                                       │
  │   ✓  Connection      connected                                        │
  │   ✓  Protocol        ISO 15765-4 (CAN 11/500)                        │
  │   ✓  VIN             WV2ZZZ7HZ8H123456                               │
  │   ✓  Manufacturer    VW/Audi                                         │
  │   ✓  ECU version     0001                                            │
  │   ⏳  UDS discovery   [████████████░░░░░░░░]  61%   (624/1024)       │
  │   ·  Supported PIDs                                                  │
  │   ·  Ready                                                           │
  │                                                                       │
  └───────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import asyncio
import logging

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import Screen
from textual.widgets import Label, Static

from Hudson.core.connection import ObdConnection
from Hudson.core.init import InitEvent, InitResult, InitStep, run_init

log = logging.getLogger(__name__)

_BAR_WIDTH = 20

_STEP_LABELS: dict[InitStep, str] = {
    InitStep.CONNECT:        "Connection",
    InitStep.PROTOCOL:       "Protocol",
    InitStep.VIN:            "VIN",
    InitStep.MANUFACTURER:   "Manufacturer",
    InitStep.ECU_VERSION:    "ECU version",
    InitStep.UDS_DISCOVERY:  "UDS discovery",
    InitStep.KWP_SESSION:    "KWP2000 session",
    InitStep.SUPPORTED_PIDS: "Supported PIDs",
    InitStep.READY:          "Ready",
}


def _progress_bar(progress: float) -> str:
    filled = round(progress * _BAR_WIDTH)
    return f"[{'█' * filled}{'░' * (_BAR_WIDTH - filled)}]"


class SplashScreen(Screen[InitResult]):
    """Run init, show live progress, dismiss with the result on completion."""

    DEFAULT_CSS = """
    SplashScreen {
        align: center middle;
    }
    SplashScreen > Vertical {
        width: 72;
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
            yield Static("HUDSON — initializing", classes="splash-title")
            for step, label_text in _STEP_LABELS.items():
                lbl = Label(f" ·  {label_text}", classes="splash-step")
                self._labels[step] = lbl
                yield lbl

    async def on_mount(self) -> None:
        consumer = asyncio.create_task(self._consume_events())
        try:
            result = await run_init(self._connection, self._events)
        except Exception as exc:  # noqa: BLE001
            log.exception("init failed")
            await self._events.put(
                InitEvent(InitStep.CONNECT, "init aborted", error=str(exc), done=True)
            )
            await asyncio.sleep(2)
            consumer.cancel()
            self.app.exit(message=f"Init failed: {exc}")
            return

        # Priority-2 background sweep is started by MainScreen after it mounts,
        # so it holds the task reference and can cancel on exit.

        await asyncio.sleep(0.2)
        consumer.cancel()
        self.dismiss(result)

    async def _consume_events(self) -> None:
        while True:
            event = await self._events.get()
            label = self._labels.get(event.step)
            if label is None:
                continue

            base = _STEP_LABELS[event.step]

            # Progress bar — only during UDS_DISCOVERY sweep
            if event.step == InitStep.UDS_DISCOVERY and event.progress is not None:
                bar = _progress_bar(event.progress)
                pct = int(event.progress * 100)
                label.update(f" ⏳  {base}  {bar}  {pct:3d}%   ({event.detail})")
                continue

            if event.error:
                label.add_class("splash-step-error")
                label.update(f" ✗  {base}  —  {event.error}")
            elif event.done:
                detail = f"  —  {event.detail}" if event.detail else ""
                label.update(f" ✓  {base}{detail}")
            else:
                detail = f"  —  {event.detail}" if event.detail else ""
                label.update(f" ⏳  {base}{detail}")
