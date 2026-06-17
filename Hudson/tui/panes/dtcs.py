"""DTC pane — read, display, and clear diagnostic trouble codes."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

import obd
from obd import OBDCommand
from obd.protocols import ECU
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widget import Widget
from textual.widgets import Button, DataTable, Label, Static

from ...core.connection import ObdConnection
from ...core.dtc import decode_dtc_list
from ...core.dtc_lookup import lookup_description as dtc_lookup_description
from ...core.dtcdecode import fetch_definition as dtcdecode_fetch
from ...core.init import InitResult

if TYPE_CHECKING:
    from ...core.telemetry import TelemetryClient


def _raw_dtc_decoder(messages: list) -> bytes:
    """Passthrough decoder for mode 07/0A responses.

    Strips the response service byte (mode + 0x40) and returns the raw
    DTC payload bytes for decode_dtc_list().
    """
    if not messages:
        return b""
    data = bytes(messages[0].data)
    return data[1:] if len(data) > 1 else b""


GET_PENDING_DTC = OBDCommand(
    "GET_PENDING_DTC",
    "Pending Diagnostic Trouble Codes (mode 07)",
    b"\x07",
    0,
    _raw_dtc_decoder,
    ECU.ALL,
    False,
)

GET_PERMANENT_DTC = OBDCommand(
    "GET_PERMANENT_DTC",
    "Permanent Diagnostic Trouble Codes (mode 0A)",
    b"\x0A",
    0,
    _raw_dtc_decoder,
    ECU.ALL,
    False,
)

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
            yield Static("Clear Stored Fault Codes?", id="confirm-title")
            yield Static(
                "Mode 04 will erase stored codes and reset\n"
                "OBD readiness monitors.\n\n"
                "Pending codes may also clear. Permanent codes\n"
                "(mode 0A) cannot be cleared by a scan tool —\n"
                "they require a completed repair and drive cycle.",
                id="confirm-body",
            )
            yield Static(
                "Incomplete readiness monitors may cause an\n"
                "emissions inspection failure.",
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
        telemetry: TelemetryClient | None = None,
        id: str | None = None,
    ) -> None:
        super().__init__(id=id)
        self._connection = connection
        self._init = init_result
        self._telemetry = telemetry
        self._auto_scanned = False

    def compose(self) -> ComposeResult:
        yield Static(" Press r to scan for DTCs", id="dtc-status")
        table: DataTable[str] = DataTable(id="dtc-table", zebra_stripes=True)
        yield table

    def on_mount(self) -> None:
        table = self.query_one(DataTable)
        table.add_column("Status", key="status")
        table.add_column("Code", key="code")
        table.add_column("System", key="system")
        table.add_column("Type", key="type")
        table.add_column("Description", key="description")
        table.add_column("2nd Opinion", key="second_opinion")

    async def on_show(self) -> None:
        if not self._auto_scanned:
            self._auto_scanned = True
            await self.action_refresh()

    async def action_refresh(self) -> None:
        self.query_one("#dtc-status", Static).update(" Scanning all modes...")
        await self._do_scan()

    async def action_clear_codes(self) -> None:
        confirmed = await self.app.push_screen_wait(ClearDtcConfirmScreen())
        if not confirmed:
            return
        self.query_one("#dtc-status", Static).update(" Clearing stored DTCs (mode 04)...")
        try:
            await self._connection.query(obd.commands.CLEAR_DTC, force=True)
            self.query_one(DataTable).clear()
            self.query_one("#dtc-status", Static).update(
                " Stored DTCs cleared. Permanent codes require a completed repair."
                "  Press r to rescan."
            )
        except Exception as exc:
            log.exception("DTC clear failed")
            self.query_one("#dtc-status", Static).update(f" Clear failed: {exc}")

    def _add_dtc_row(
        self,
        table: DataTable,  # type: ignore[type-arg]
        code: str,
        obd_desc: str | None,
        status: str,
        row_key: str,
    ) -> None:
        system = {
            "P": "Powertrain",
            "C": "Chassis",
            "B": "Body",
            "U": "Network",
        }.get(code[0], "Unknown")

        is_mfr = code[0] == "P" and code[1] in ("1", "3")
        dtype = "Manufacturer" if is_mfr else "SAE"

        # Resolution order: manufacturer module → dtc_lookup DB → python-obd str
        mfr_desc: str | None = None
        if self._init.manufacturer_module:
            mfr_desc = getattr(
                self._init.manufacturer_module, "lookup_dtc", lambda _: None
            )(code)
        db_desc = dtc_lookup_description(code, self._init.manufacturer_name)
        description = mfr_desc or db_desc or obd_desc or "—"

        table.add_row(status, code, system, dtype, description, "…", key=row_key)

    async def _do_scan(self) -> None:
        table = self.query_one(DataTable)
        table.clear()
        status_parts: list[str] = []
        total = 0
        stored_codes: list[str] = []
        pending_codes: list[str] = []
        permanent_codes: list[str] = []
        # (row_key, code) pairs for dtcdecode update pass
        row_entries: list[tuple[str, str]] = []

        # Mode 03 — stored DTCs (python-obd returns list of (code_str, desc_str))
        try:
            resp = await self._connection.query(obd.commands.GET_DTC, force=True)
            if resp.is_null() or resp.value is None:
                status_parts.append("Stored: ECU did not respond")
            elif not resp.value:
                status_parts.append("Stored: none")
            else:
                for code, obd_desc in resp.value:
                    key = f"S:{code}"
                    self._add_dtc_row(table, code, obd_desc, "Stored", key)
                    row_entries.append((key, code))
                    stored_codes.append(code)
                    total += 1
                status_parts.append(f"Stored: {len(resp.value)}")
        except Exception as exc:
            log.exception("Mode 03 scan failed")
            status_parts.append(f"Stored: error — {exc}")

        # Mode 07 — pending DTCs (custom command, raw bytes → decode_dtc_list)
        try:
            resp = await self._connection.query(GET_PENDING_DTC, force=True)
            if resp.is_null() or resp.value is None:
                status_parts.append("Pending: no response")
            else:
                dtcs = decode_dtc_list(resp.value)
                if not dtcs:
                    status_parts.append("Pending: none")
                else:
                    for dtc in dtcs:
                        key = f"P:{dtc.code}"
                        self._add_dtc_row(table, dtc.code, None, "Pending", key)
                        row_entries.append((key, dtc.code))
                        pending_codes.append(dtc.code)
                        total += 1
                    status_parts.append(f"Pending: {len(dtcs)}")
        except Exception as exc:
            log.exception("Mode 07 scan failed")
            status_parts.append(f"Pending: error — {exc}")

        # Mode 0A — permanent DTCs (custom command, raw bytes → decode_dtc_list)
        try:
            resp = await self._connection.query(GET_PERMANENT_DTC, force=True)
            if resp.is_null() or resp.value is None:
                status_parts.append("Permanent: no response")
            else:
                dtcs = decode_dtc_list(resp.value)
                if not dtcs:
                    status_parts.append("Permanent: none")
                else:
                    for dtc in dtcs:
                        key = f"M:{dtc.code}"
                        self._add_dtc_row(table, dtc.code, None, "Permanent", key)
                        row_entries.append((key, dtc.code))
                        permanent_codes.append(dtc.code)
                        total += 1
                    status_parts.append(f"Permanent: {len(dtcs)}")
        except Exception as exc:
            log.exception("Mode 0A scan failed")
            status_parts.append(f"Permanent: error — {exc}")

        if self._telemetry is not None:
            await self._telemetry.record_dtcs(stored_codes, pending_codes, permanent_codes)

        summary = "  |  ".join(status_parts)
        if total == 0:
            self.query_one("#dtc-status", Static).update(f" ✓ No codes.  {summary}")
        else:
            self.query_one("#dtc-status", Static).update(
                f" {total} code(s) found.  {summary}  |  r=refresh  c=clear stored"
            )

        # Fetch dtcdecode.com 2nd opinions concurrently for all found codes.
        make = self._init.dtcdecode_make
        if make and row_entries:
            await self._fetch_second_opinions(table, make, row_entries)

    async def _fetch_second_opinions(
        self,
        table: DataTable,  # type: ignore[type-arg]
        make: str,
        row_entries: list[tuple[str, str]],
    ) -> None:
        unique_codes = list({code for _, code in row_entries})
        results = await asyncio.gather(
            *(dtcdecode_fetch(make, code) for code in unique_codes),
            return_exceptions=True,
        )
        definitions: dict[str, str] = {}
        for code, result in zip(unique_codes, results):
            if isinstance(result, str):
                definitions[code] = result

        for row_key, code in row_entries:
            defn = definitions.get(code, "—")
            try:
                table.update_cell(row_key, "second_opinion", defn or "—")
            except Exception:
                log.debug("Could not update 2nd opinion cell for %s", code)
