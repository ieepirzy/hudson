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
from textual.message import Message
from textual.screen import ModalScreen
from textual.widget import Widget
from textual.widgets import Button, DataTable, Static

from ...core.connection import ObdConnection
from ...core.dtc import DtcRecord, decode_dtc_list
from ...core.dtc_lookup import lookup_description as dtc_lookup_description
from ...core.dtcdecode import fetch_definition as dtcdecode_fetch
from ...core.init import InitResult
from ...core.uds_dtc import scan_ecus_for_dtcs

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
        width: 70%;
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


class LastClearStats(Static):
    """Sidebar box — ECU metrics since the last DTC clear + codes cleared this session."""

    DEFAULT_CSS = """
    LastClearStats {
        border: round $primary 40%;
        height: auto;
        padding: 0 1;
        margin-bottom: 1;
    }
    """

    def __init__(self) -> None:
        super().__init__("")
        self._dist: float | None = None
        self._warmups: int | None = None
        self._mil_dist: float | None = None
        self._mil_time: float | None = None
        self._cleared_codes: list[str] = []
        self._refresh_display()

    def set_ecu_metrics(
        self,
        dist: float | None,
        warmups: int | None,
        mil_dist: float | None,
        mil_time: float | None,
    ) -> None:
        self._dist = dist
        self._warmups = warmups
        self._mil_dist = mil_dist
        self._mil_time = mil_time
        self._refresh_display()

    def set_cleared_codes(self, codes: list[str]) -> None:
        self._cleared_codes = codes
        self._refresh_display()

    def _refresh_display(self) -> None:
        def fmt_km(v: float | None) -> str:
            return f"{v:.0f} km" if v is not None else "—"

        def fmt_int(v: int | None) -> str:
            return str(v) if v is not None else "—"

        def fmt_min(v: float | None) -> str:
            if v is None:
                return "—"
            h, m = divmod(int(v), 60)
            return f"{h}h {m}m" if h else f"{m} min"

        dist_color = "limegreen" if (self._dist is not None and self._dist == 0) else "white"

        codes_line = ""
        if self._cleared_codes:
            codes_line = "\n[dim]Cleared[/]   [gold]" + "  ".join(self._cleared_codes) + "[/]"

        self.update(
            f"[bold dim]SINCE LAST CLEAR[/]\n"
            f"[dim]Distance[/]  [{dist_color}]{fmt_km(self._dist)}[/]\n"
            f"[dim]Warm-ups[/]  [white]{fmt_int(self._warmups)}[/]\n"
            f"[dim]MIL dist[/]  [white]{fmt_km(self._mil_dist)}[/]\n"
            f"[dim]MIL time[/]  [white]{fmt_min(self._mil_time)}[/]"
            f"{codes_line}"
        )


class DtcPane(Widget):
    """Scan for DTCs and display them in a table."""

    class ScanPhase(Message):
        """Posted when the DTC scan enters a new phase. Empty phase = done."""
        def __init__(self, phase: str) -> None:
            super().__init__()
            self.phase = phase

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

    #dtc-main {
        height: 1fr;
        layout: horizontal;
    }

    #dtc-table {
        width: 2fr;
        border: round $primary 50%;
        margin: 1 0 1 1;
    }

    #dtc-sidebar {
        width: 26;
        padding: 1 1 1 0;
        layout: vertical;
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
        with Horizontal(id="dtc-main"):
            table: DataTable[str] = DataTable(id="dtc-table", zebra_stripes=True)
            yield table
            with Vertical(id="dtc-sidebar"):
                yield LastClearStats()

    def on_mount(self) -> None:
        table = self.query_one(DataTable)
        table.add_column("Status", key="status")
        table.add_column("Code", key="code")
        table.add_column("System", key="system")
        table.add_column("Type", key="type")
        table.add_column("ECU", key="ecu")
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

        # Snapshot current codes before erasing so we can show what was cleared.
        table = self.query_one(DataTable)
        codes_before: list[str] = []
        for row_key in table.rows:
            try:
                code_cell = table.get_cell(row_key, "code")
                status_cell = table.get_cell(row_key, "status")
                if status_cell in ("Stored", "Pending"):
                    codes_before.append(str(code_cell))
            except Exception:
                pass

        self.query_one("#dtc-status", Static).update(" Clearing stored DTCs (mode 04)...")
        try:
            await self._connection.query(obd.commands.CLEAR_DTC, force=True)
            table.clear()
            if codes_before:
                self.query_one(LastClearStats).set_cleared_codes(codes_before)
            self.query_one("#dtc-status", Static).update(
                " Stored DTCs cleared. Permanent codes require a completed repair."
                "  Press r to rescan."
            )
        except Exception as exc:
            log.exception("DTC clear failed")
            self.query_one("#dtc-status", Static).update(f" Clear failed: {exc}")

    def _lookup_description(self, code: str, obd_desc: str | None = None) -> str:
        mfr_desc: str | None = None
        if self._init.manufacturer_module:
            mfr_desc = getattr(
                self._init.manufacturer_module, "lookup_dtc", lambda _: None
            )(code)
        db_desc = dtc_lookup_description(code, self._init.manufacturer_name)
        return mfr_desc or db_desc or obd_desc or "—"

    def _add_dtc_row(
        self,
        table: DataTable,  # type: ignore[type-arg]
        code: str,
        obd_desc: str | None,
        status: str,
        row_key: str,
        ecu: str = "Std",
    ) -> None:
        system = {
            "P": "Powertrain",
            "C": "Chassis",
            "B": "Body",
            "U": "Network",
        }.get(code[0], "Unknown")

        is_mfr = code[0] == "P" and code[1] in ("1", "3")
        dtype = "Manufacturer" if is_mfr else "SAE"
        description = self._lookup_description(code, obd_desc)
        table.add_row(status, code, system, dtype, ecu, description, "…", key=row_key)

    def _add_dtc_record_row(
        self,
        table: DataTable,  # type: ignore[type-arg]
        record: DtcRecord,
        row_key: str,
        ecu: str,
    ) -> None:
        code = record.dtc.code
        system = {
            "P": "Powertrain",
            "C": "Chassis",
            "B": "Body",
            "U": "Network",
        }.get(code[0], "Unknown")

        is_mfr = code[0] == "P" and code[1] in ("1", "3")
        dtype = "Manufacturer" if is_mfr else "SAE"
        description = self._lookup_description(code)
        status = record.status.flags_str()
        table.add_row(status, code, system, dtype, ecu, description, "…", key=row_key)

    async def _do_scan(self) -> None:
        table = self.query_one(DataTable)
        table.clear()
        status_parts: list[str] = []
        total = 0
        stored_codes: list[str] = []
        pending_codes: list[str] = []
        permanent_codes: list[str] = []
        row_entries: list[tuple[str, str]] = []

        # Mode 03 — stored DTCs
        self.post_message(self.ScanPhase("DTC Mode 03"))
        try:
            resp = await self._connection.query(obd.commands.GET_DTC, force=True)
            if resp.is_null() or resp.value is None:
                status_parts.append("Stored: ECU no response")
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

        # Mode 07 — pending DTCs
        self.post_message(self.ScanPhase("DTC Mode 07"))
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

        # Mode 0A — permanent DTCs
        self.post_message(self.ScanPhase("DTC Mode 0A"))
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

        # UDS 0x19 — multi-ECU sweep (CAN only; UDS 0x19 requires ISO-TP framing)
        if self._connection.is_can_protocol:
            self.post_message(self.ScanPhase("DTC UDS 0x19"))
            try:
                self.query_one("#dtc-status", Static).update(" Scanning UDS 0x19 (multi-ECU)…")
                # Use addresses from tiered discovery when available; the discovered
                # set includes proprietary addresses (e.g. Ford BCM 0x733, ABS 0x726)
                # that the standard J1979 sweep would miss.
                _disc = self._init.discovered_ecus
                if _disc and _disc.found:
                    # Always probe ECM (0x7E0) and TCM (0x7E1) directly — they are
                    # mandatory OBD2 addresses and may not have responded during
                    # discovery (e.g. Tier A broadcast only found them by response ID).
                    _addrs = sorted({0x7E0, 0x7E1} | set(_disc.found.keys()))
                else:
                    _addrs = None
                uds_results = await scan_ecus_for_dtcs(
                    self._connection, sub_fn=0x02, addresses=_addrs
                )
                uds_total = 0
                for addr, records in uds_results.items():
                    ecu_label = f"{addr:03X}"
                    for record in records:
                        key = f"UDS:{addr:03X}:{record.dtc.code}"
                        try:
                            self._add_dtc_record_row(table, record, key, ecu_label)
                            row_entries.append((key, record.dtc.code))
                            uds_total += 1
                            total += 1
                        except Exception:
                            log.debug("Duplicate or invalid UDS row key %s — skipping", key)
                if uds_total:
                    status_parts.append(f"UDS: {uds_total}")
                else:
                    status_parts.append("UDS: none")
            except Exception as exc:
                log.exception("UDS 0x19 scan failed")
                status_parts.append(f"UDS: error — {exc}")

        # KWP 0x18 — if a K-line session is active from init
        if self._init.kwp_session is not None:
            self.post_message(self.ScanPhase("DTC KWP 0x18"))
            try:
                kwp_records = await self._init.kwp_session.read_dtcs()
                if kwp_records:
                    for record in kwp_records:
                        key = f"KWP:{record.dtc.code}"
                        try:
                            self._add_dtc_record_row(table, record, key, "KWP")
                            row_entries.append((key, record.dtc.code))
                            total += 1
                        except Exception:
                            log.debug("Duplicate or invalid KWP row key %s — skipping", key)
                    status_parts.append(f"KWP: {len(kwp_records)}")
                else:
                    status_parts.append("KWP: none")
            except Exception as exc:
                log.exception("KWP 0x18 scan failed")
                status_parts.append(f"KWP: error — {exc}")

        self.post_message(self.ScanPhase(""))  # done

        if self._telemetry is not None:
            await self._telemetry.record_dtcs(stored_codes, pending_codes, permanent_codes)

        summary = "  |  ".join(status_parts)
        if total == 0:
            self.query_one("#dtc-status", Static).update(f" ✓ No codes.  {summary}")
        else:
            self.query_one("#dtc-status", Static).update(
                f" {total} code(s) found.  {summary}  |  r=refresh  c=clear stored"
            )

        make = self._init.dtcdecode_make
        if make and row_entries:
            await self._fetch_second_opinions(table, make, row_entries)

        await self._refresh_clear_stats()

    async def _refresh_clear_stats(self) -> None:
        """Query ECU metrics about the last DTC clear and update the sidebar."""

        async def _query_float(cmd: OBDCommand) -> float | None:
            try:
                r = await self._connection.query(cmd)
                if not r.is_null() and r.value is not None:
                    return float(r.value.magnitude)
            except Exception:
                pass
            return None

        dist = await _query_float(obd.commands.DISTANCE_SINCE_DTC_CLEAR)
        warmups_raw = await _query_float(obd.commands.WARMUPS_SINCE_DTC_CLEAR)
        mil_dist = await _query_float(obd.commands.DISTANCE_W_MIL)
        mil_time = await _query_float(obd.commands.RUN_TIME_MIL)

        warmups = int(warmups_raw) if warmups_raw is not None else None
        self.query_one(LastClearStats).set_ecu_metrics(dist, warmups, mil_dist, mil_time)

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
