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

After init, if the vehicle make could not be auto-detected, a make-selection
modal is shown so that dtcdecode.com lookups can work.
"""

from __future__ import annotations

import asyncio
import logging

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen, Screen
from textual.widgets import Button, Label, Select, Static

from Hudson.core.connection import ObdConnection
from Hudson.core.dtcdecode import AUTO_MAKE_MAP, MAKES
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


class MakeSelectScreen(ModalScreen[str | None]):
    """Prompt the user to pick a vehicle make for dtcdecode.com lookups.

    Shown when VIN lookup failed or the WMI-derived manufacturer is ambiguous
    (e.g. VW/Audi), so we cannot auto-map to a dtcdecode.com make slug.
    Dismisses with the chosen make string, or None if the user skips.
    """

    BINDINGS = [Binding("escape", "skip", "Skip")]

    DEFAULT_CSS = """
    MakeSelectScreen {
        align: center middle;
    }
    #make-dialog {
        width: 60;
        height: auto;
        border: round $accent;
        background: $surface;
        padding: 1 2;
    }
    #make-title {
        text-style: bold;
        color: $accent;
        margin-bottom: 1;
    }
    #make-subtitle {
        color: $text-muted;
        margin-bottom: 1;
    }
    #make-select {
        margin-bottom: 1;
    }
    #make-buttons {
        height: 3;
        align: right middle;
    }
    Button {
        margin-left: 1;
    }
    """

    def __init__(self, reason: str) -> None:
        super().__init__()
        self._reason = reason
        self._selected: str | None = None

    def compose(self) -> ComposeResult:
        with Vertical(id="make-dialog"):
            yield Static("Select Vehicle Make", id="make-title")
            yield Static(self._reason, id="make-subtitle")
            yield Select(
                [(make, make) for make in MAKES],
                prompt="Select make for dtcdecode.com lookups…",
                id="make-select",
            )
            with Horizontal(id="make-buttons"):
                yield Button("Skip", variant="default", id="btn-skip")
                yield Button("Confirm", variant="primary", id="btn-confirm")

    def on_select_changed(self, event: Select.Changed) -> None:
        self._selected = event.value if event.value != Select.BLANK else None

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-confirm":
            self.dismiss(self._selected)
        else:
            self.dismiss(None)

    def action_skip(self) -> None:
        self.dismiss(None)


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

        # Resolve dtcdecode make: try auto-map first, prompt on ambiguity/failure.
        result.dtcdecode_make = _auto_detect_make(result)
        if result.dtcdecode_make is None:
            reason = _make_prompt_reason(result)
            result.dtcdecode_make = await self.app.push_screen_wait(
                MakeSelectScreen(reason)
            )

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


def _auto_detect_make(result: InitResult) -> str | None:
    """Return a dtcdecode make slug if it can be unambiguously inferred."""
    return AUTO_MAKE_MAP.get(result.manufacturer_name)


def _make_prompt_reason(result: InitResult) -> str:
    if result.vin is None:
        return (
            "VIN could not be read — select the vehicle make so that "
            "dtcdecode.com lookups work correctly."
        )
    return (
        f"Manufacturer detected as '{result.manufacturer_name}' which maps to "
        "more than one make on dtcdecode.com — please select the correct one."
    )
