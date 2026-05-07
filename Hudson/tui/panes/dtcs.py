"""DTC pane — read, display, and clear diagnostic trouble codes."""

from __future__ import annotations

import logging

import obd
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widget import Widget
from textual.widgets import Button, DataTable, Label, Static

from ...core.connection import ObdConnection
from ...core.dtc import decode_dtc_list
from ...core.init import InitResult

log = logging.getLogger(__name__)


class ClearDtcConfirmScreen(ModalScreen[bool]):
    """Safety confirmation before wiping stored fault codes.

    Clearing DTCs also resets OBD readiness monitors. A car with incomplete
    monitors will fail an emissions inspection even if nothing is broken.
    """

    DEFAULT_CSS = """
    ClearDtcConfirmScreen {
        align: center middle;
    }

    #confirm-dialog {
        width: 52;
        height: auto;
        border: round $warning;
        background: $surface;
        padding: 1 2;
    }

    #confirm-title {
        text-style: bold;
        color: $warning;
        margin-bottom: 1;
    }

    #confirm-body {
        color: $text;
        margin-bottom: 1;
    }

    #confirm-warning {
        color: $warning;
        opacity: 0.8;
        margin-bottom: 1;
    }

    #confirm-buttons {
        height: 3;
        align: right middle;
    }

    Button {
        margin-left: 1;
    }
    """

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm-dialog"):
            yield Static("Clear Diagnostic Trouble Codes?", id="confirm-title")
            yield Static(
                "This will erase all stored fault codes and reset\n"
                "OBD readiness monitors.",
                id="confirm-body",
            )
            yield Static(
                "A car with incomplete readiness monitors may fail\n"
                "an emissions inspection even if no fault is present.",
                id="confirm-warning",
            )
            with Horizontal(id="confirm-buttons"):
                yield Button("Cancel", variant="default", id="btn-cancel")
                yield Button("Clear DTCs", variant="warning", id="btn-confirm")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "btn-confirm")


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
        confirmed = await self.app.push_screen_wait(ClearDtcConfirmScreen())
        if not confirmed:
            return
        self.query_one("#dtc-status", Static).update(" Clearing DTCs...")
        try:
            await self._connection.query(obd.commands.CLEAR_DTC, force=True)
            self.query_one(DataTable).clear()
            self.query_one("#dtc-status", Static).update(
                " DTCs cleared. Press r to rescan."
            )
        except Exception as exc:
            log.exception("DTC clear failed")
            self.query_one("#dtc-status", Static).update(f" Clear failed: {exc}")

    async def _do_scan(self) -> None:
        table = self.query_one(DataTable)
        table.clear()
        try:
            resp = await self._connection.query(obd.commands.GET_DTC, force=True)
            if resp.is_null() or resp.value is None:
                self.query_one("#dtc-status", Static).update(
                    " No DTC data returned — ECU may not support mode 03."
                )
                return

            raw = resp.value  # list of (code_str, description_str) from python-obd
            if not raw:
                self.query_one("#dtc-status", Static).update(
                    " ✓ No trouble codes stored."
                )
                return

            for code, obd_desc in raw:
                system = {
                    "P": "Powertrain",
                    "C": "Chassis",
                    "B": "Body",
                    "U": "Network",
                }.get(code[0], "Unknown")

                is_mfr = code[0] == "P" and code[1] in ("1", "3")
                dtype = "Manufacturer" if is_mfr else "SAE"

                # Manufacturer module gets priority over python-obd's generic desc.
                mfr_desc: str | None = None
                if self._init.manufacturer_module:
                    mfr_desc = getattr(
                        self._init.manufacturer_module, "lookup_dtc", lambda _: None
                    )(code)
                description = mfr_desc or obd_desc or "—"

                table.add_row(code, system, dtype, description)

            self.query_one("#dtc-status", Static).update(
                f" {len(raw)} trouble code(s) found.  r = refresh   c = clear"
            )
        except Exception as exc:
            log.exception("DTC scan failed")
            self.query_one("#dtc-status", Static).update(f" Scan failed: {exc}")
