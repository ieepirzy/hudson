"""Vehicle info pane — identity header + scrollable PID discovery table."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widget import Widget
from textual.widgets import DataTable, Static

from ...core.init import InitResult
from ...core.uds_dtc import DiscoveryTier


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

    #ecu-table {
        height: auto;
        max-height: 10;
        margin: 0 2 1 2;
        border: round $accent 40%;
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
        disc = self._init.discovered_ecus
        if disc and disc.found:
            yield DataTable(id="ecu-table", cursor_type="row", zebra_stripes=True)
        yield DataTable(id="pid-table", cursor_type="row", zebra_stripes=True)

    def on_mount(self) -> None:
        disc = self._init.discovered_ecus
        if disc and disc.found:
            ecu_table = self.query_one("#ecu-table", DataTable)
            ecu_table.add_column("Addr", width=6)
            ecu_table.add_column("Tier", width=6)
            ecu_table.add_column("Label")
            _tier_color = {
                DiscoveryTier.A: "green",
                DiscoveryTier.B: "cyan",
                DiscoveryTier.C: "yellow",
            }
            for addr, ecu in sorted(disc.found.items()):
                color = _tier_color.get(ecu.tier, "white")
                ecu_table.add_row(
                    f"0x{addr:03X}",
                    f"[{color}]{ecu.tier.value}[/]",
                    ecu.label or "—",
                )

        pid_table = self.query_one("#pid-table", DataTable)
        pid_table.add_column("Mode", width=6)
        pid_table.add_column("PID ", width=6)
        pid_table.add_column("Name", width=22)
        pid_table.add_column("Description")

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
            pid_table.add_row(mode, pid, cmd.name, cmd.desc)
