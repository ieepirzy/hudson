"""DTC pane — read, display, and clear diagnostic trouble codes."""

from __future__ import annotations

import logging

import obd
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.widget import Widget
from textual.widgets import DataTable, Static

from ...core.connection import ObdConnection
from ...core.dtc import decode_dtc_list
from ...core.init import InitResult

log = logging.getLogger(__name__)


class DtcPane(Widget):
    """Scan for DTCs and display them in a table."""

    BINDINGS = [
        Binding("r", "refresh", "Refresh", show=True),
        Binding("c", "clear_codes", "Clear DTCs", show=True),
    ]

    DEFAULT_CSS = """
    DtcPane {
        height: 1fr;
        layout: vertical;
    }

    #dtc-status {
        height: 1;
        padding: 0 1;
        color: $text-muted;
    }

    #dtc-table {
        height: 1fr;
        border: round $primary 50%;
        margin: 1;
    }
    """

    def __init__(
        self,
        connection: ObdConnection,
        init_result: InitResult,
        id: str | None = None,
    ) -> None:
        super().__init__(id=id)
        self._connection = connection
        self._init = init_result
        self._auto_scanned = False

    def compose(self) -> ComposeResult:
        yield Static(" Press r to scan for DTCs", id="dtc-status")
        table: DataTable[str] = DataTable(id="dtc-table", zebra_stripes=True)
        yield table

    def on_mount(self) -> None:
        table = self.query_one(DataTable)
        table.add_columns("Code", "System", "Type", "Description")

    async def on_show(self) -> None:
        if not self._auto_scanned:
            self._auto_scanned = True
            await self.action_refresh()

    async def action_refresh(self) -> None:
        self.query_one("#dtc-status", Static).update(" Scanning...")
        await self._do_scan()

    async def action_clear_codes(self) -> None:
        self.query_one("#dtc-status", Static).update(" Clearing DTCs...")
        try:
            await self._connection.query(obd.commands.CLEAR_DTC, force=True)
            self.query_one(DataTable).clear()
            self.query_one("#dtc-status", Static).update(" DTCs cleared. Press r to rescan.")
        except Exception as exc:
            self.query_one("#dtc-status", Static).update(f" Clear failed: {exc}")

    async def _do_scan(self) -> None:
        table = self.query_one(DataTable)
        table.clear()
        try:
            resp = await self._connection.query(obd.commands.GET_DTC, force=True)
            if resp.is_null() or resp.value is None:
                self.query_one("#dtc-status", Static).update(" No DTC data returned.")
                return

            # python-obd returns a list of (code, description) tuples for GET_DTC
            raw = resp.value
            if not raw:
                self.query_one("#dtc-status", Static).update(" ✓ No trouble codes stored.")
                return

            for code, description in raw:
                system = {"P": "Powertrain", "C": "Chassis", "B": "Body", "U": "Network"}.get(
                    code[0], "Unknown"
                )
                mfr_specific = "Manufacturer" if code[1] in ("1", "3") and code[0] == "P" else "SAE"
                # Check manufacturer module for a better description
                mfr_desc = None
                if self._init.manufacturer_module:
                    mfr_desc = getattr(self._init.manufacturer_module, "lookup_dtc", lambda c: None)(code)
                display_desc = mfr_desc or description or "—"
                table.add_row(code, system, mfr_specific, display_desc)

            self.query_one("#dtc-status", Static).update(
                f" {len(raw)} trouble code(s) found. c to clear."
            )
        except Exception as exc:
            log.exception("DTC scan failed")
            self.query_one("#dtc-status", Static).update(f" Scan failed: {exc}")
