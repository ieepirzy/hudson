"""Vehicle info pane — static display of init result."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widget import Widget
from textual.widgets import Static

from ...core.init import InitResult


class VehiclePane(Widget):
    """Display vehicle identity and connection details."""

    DEFAULT_CSS = """
    VehiclePane {
        height: 1fr;
        padding: 1 2;
    }

    .vehicle-section {
        border: round $primary 50%;
        padding: 1 2;
        margin-bottom: 1;
        height: auto;
    }

    .vehicle-key {
        color: $text-muted;
        width: 20;
    }

    .vehicle-value {
        color: $accent;
        text-style: bold;
    }

    .vehicle-row {
        height: 1;
        layout: horizontal;
    }
    """

    def __init__(self, init_result: InitResult, id: str | None = None) -> None:
        super().__init__(id=id)
        self._init = init_result

    def compose(self) -> ComposeResult:
        with Vertical(classes="vehicle-section"):
            yield Static("[bold]Vehicle Identity[/bold]")
            yield self._row("VIN", self._init.vin or "—")
            yield self._row("Manufacturer", self._init.manufacturer_name)

        with Vertical(classes="vehicle-section"):
            yield Static("[bold]Connection[/bold]")
            yield self._row("Protocol", self._init.protocol_name or "—")
            yield self._row("Supported PIDs", str(len(self._init.supported_commands)))

    def _row(self, key: str, value: str) -> Widget:
        from textual.containers import Horizontal
        row = Horizontal(classes="vehicle-row")
        row._nodes  # touch to init
        return Static(f"  {key:<20} {value}", classes="vehicle-value")
