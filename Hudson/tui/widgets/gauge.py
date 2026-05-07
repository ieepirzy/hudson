"""A simple value-and-label gauge for live PIDs."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import Static


class Gauge(Widget):
    """Display a labelled scalar value with a unit suffix."""

    value: reactive[float | None] = reactive(None)
    unit: reactive[str] = reactive("")

    def __init__(self, label: str, *, unit: str = "", widget_id: str | None = None) -> None:
        super().__init__(id=widget_id)
        self._label = label
        self.unit = unit
        self._disabled = False

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(self._label, classes="gauge--label")
            yield Static("--", id="value", classes="gauge--value")

    def watch_value(self, value: float | None) -> None:
        if self._disabled:
            return
        text = "--" if value is None else f"{value:.1f} {self.unit}".strip()
        self.query_one("#value", Static).update(text)

    def disable(self) -> None:
        """Mark this gauge as not-applicable (PID unsupported by vehicle)."""
        self._disabled = True
        self.add_class("gauge--disabled")
        try:
            self.query_one("#value", Static).update("n/a")
        except Exception:  # noqa: BLE001 - widget may not be mounted yet
            pass
