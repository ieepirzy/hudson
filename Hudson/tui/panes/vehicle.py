"""Vehicle info pane — identity header + scrollable PID discovery table."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widget import Widget
from textual.widgets import DataTable, Static

from ...core.init import InitResult


class VehiclePane(Widget):
    """Vehicle identity block + full PID table."""

    DEFAULT_CSS = """
    VehiclePane {
        height: 1fr;
        layout: vertical;
        padding: 0;
    }

    #vehicle-header {
        height: auto;
        border: round $primary 50%;
        padding: 0 2;
        margin: 1 2;
    }

    #pid-table {
        height: 1fr;
        margin: 0 2 1 2;
        border: round $primary 40%;
    }
    """

    def __init__(self, init_result: InitResult, id: str | None = None) -> None:
        super().__init__(id=id)
        self._init = init_result

    def compose(self) -> ComposeResult:
        vin = self._init.vin or "—"
        mfr = self._init.manufacturer_name
        proto = self._init.protocol_name or "—"
        n = len(self._init.supported_commands)
        yield Static(
            f"[bold]VIN[/]  [cyan]{vin}[/]   "
            f"[bold]Mfr[/]  [cyan]{mfr}[/]   "
            f"[bold]Protocol[/]  [cyan]{proto}[/]   "
            f"[bold]PIDs[/]  [cyan]{n}[/]",
            id="vehicle-header",
        )
        yield DataTable(id="pid-table", cursor_type="row", zebra_stripes=True)

    def on_mount(self) -> None:
        table = self.query_one(DataTable)
        table.add_column("Mode", width=6)
        table.add_column("PID ", width=6)
        table.add_column("Name", width=22)
        table.add_column("Description")

        cmds = sorted(
            self._init.supported_commands,
            key=lambda c: (
                c.command[0] if c.command else 0xFF,
                c.command[1] if len(c.command) > 1 else 0xFF,
            ),
        )
        for cmd in cmds:
            mode_byte = cmd.command[0] if cmd.command else None
            pid_byte = cmd.command[1] if len(cmd.command) > 1 else None
            mode = f"{mode_byte:02X}" if mode_byte is not None else "—"
            pid = f"{pid_byte:02X}" if pid_byte is not None else "—"
            table.add_row(mode, pid, cmd.name, cmd.desc)
