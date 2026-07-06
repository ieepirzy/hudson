"""Main screen — btop-style tab bar with swappable content panes.

Layout:
    ┌─ HUDSON ──── VIN · Mfr · Protocol · PIDs ──────────────── HH:MM ─┐
    │ [ Dashboard ]  [ DTCs ]  [ Log ]  [ Vehicle ]                      │
    ├────────────────────────────────────────────────────────────────────┤
    │  (active pane)                                                      │
    └────────────────────────────────────────────────────────────────────┘
      ←/→ switch   q quit   c clear DTCs (DTC pane only)
"""

from __future__ import annotations

import asyncio
import logging
from time import monotonic
from typing import TYPE_CHECKING

from textual.app import ComposeResult
from textual.binding import Binding
from textual.widgets import ContentSwitcher
from textual.containers import Horizontal
from textual.screen import Screen
from textual.widget import Widget
from textual.widgets import Static

from Hudson.core.connection import ObdConnection
from Hudson.core.init import InitResult
from Hudson.core.poller import Poller, PollSpec, Reading
from Hudson.core.uds import UdsDiscovery
from Hudson.tui.panes.dashboard import DashboardPane
from Hudson.tui.panes.dtcs import DtcPane
from Hudson.tui.panes.log import LogPane
from Hudson.tui.panes.vehicle import VehiclePane
from Hudson.tui.widgets.gauge import GAUGE_CATALOG

import obd

if TYPE_CHECKING:
    from Hudson.core.telemetry import TelemetryClient

log = logging.getLogger(__name__)

_SCAN_BAR_WIDTH = 14
_PROGRESS_UPDATE_INTERVAL = 50  # update UI every N identifiers to avoid flooding


class StatusBar(Static):
    """Persistent one-line footer: protocol · transport | active ECU | scan | warnings."""

    DEFAULT_CSS = """
    StatusBar {
        height: 1;
        background: $surface-darken-2;
        color: $text-muted;
        padding: 0 1;
    }
    """

    def __init__(
        self,
        connection: ObdConnection,
        init_result: InitResult,
        *,
        id: str | None = None,
    ) -> None:
        super().__init__("", id=id)
        self._connection = connection
        self._proto = init_result.protocol_name or "—"
        self._transport = getattr(connection, "transport_label", "")
        self._scanning = False
        self._scan_label = ""
        self._scan_current = 0
        self._scan_total = 0
        self._scan_responding = 0
        self._scan_elapsed = 0.0
        self._scan_done = False
        self._scan_done_responding = 0

    def on_mount(self) -> None:
        self.set_interval(0.5, self._refresh_display)

    # ── scan state setters (called by MainScreen) ─────────────────────────────

    def show_scanning(
        self,
        current: int,
        total: int,
        responding: int,
        elapsed: float,
        label: str = "",
    ) -> None:
        self._scanning = True
        self._scan_done = False
        self._scan_label = label
        self._scan_current = current
        self._scan_total = total
        self._scan_responding = responding
        self._scan_elapsed = elapsed
        self._refresh_display()

    def show_scan_phase(self, phase: str) -> None:
        """Text-only phase label, no progress bar — e.g. 'DTC Mode 03'."""
        self._scanning = True
        self._scan_done = False
        self._scan_label = phase
        self._scan_total = 0
        self._refresh_display()

    def show_complete(self, responding: int) -> None:
        self._scanning = False
        self._scan_done = True
        self._scan_done_responding = responding
        self._refresh_display()
        self.set_timer(12.0, self.hide_scan)

    def hide_scan(self) -> None:
        self._scanning = False
        self._scan_done = False
        self._refresh_display()

    # ── rendering ─────────────────────────────────────────────────────────────

    def _refresh_display(self) -> None:
        from Hudson.tui.panes.log import get_warn_error_count

        # Protocol / transport
        transport = self._transport or getattr(self._connection, "transport_label", "")
        proto_part = self._proto
        if transport:
            proto_part += f" · {transport}"

        # Active ECU
        ecu_addr = getattr(self._connection, "active_ecu_addr", None)
        ecu_part = f"ECU {ecu_addr:03X}" if ecu_addr is not None else "ECU —"

        # Scan progress
        if self._scanning and self._scan_total > 0:
            pct = int(self._scan_current / self._scan_total * 100)
            filled = round(self._scan_current / self._scan_total * _SCAN_BAR_WIDTH)
            bar = "█" * filled + "░" * (_SCAN_BAR_WIDTH - filled)
            rate = self._scan_current / self._scan_elapsed if self._scan_elapsed > 0 else 1.0
            eta_s = (self._scan_total - self._scan_current) / rate if rate > 0 else 0.0
            if eta_s < 60:
                eta = f"{int(eta_s)}s"
            elif eta_s < 3600:
                eta = f"{int(eta_s / 60)}m"
            else:
                eta = f"{int(eta_s / 3600)}h{int((eta_s % 3600) / 60):02d}m"
            scan_part = f"⟳ {self._scan_label}  \\[{bar}]  {pct}%  ETA {eta}  {self._scan_responding} found"
        elif self._scanning:
            scan_part = f"⟳ {self._scan_label}"
        elif self._scan_done:
            scan_part = f"✓ scan  {self._scan_done_responding} found"
        else:
            scan_part = ""

        # Warning / error count
        err = get_warn_error_count()
        err_part = f"⚠ {err}" if err > 0 else ""

        # Assemble line with dim separators
        line = f"[dim]{proto_part}[/]  [white]│[/]  {ecu_part}"
        if scan_part:
            line += f"  [white]│[/]  [cyan]{scan_part}[/]"
        if err_part:
            line += f"  [white]│[/]  [red]{err_part}[/]"

        try:
            self.update(line)
        except Exception:
            pass


TABS: list[tuple[str, str]] = [
    ("dashboard", "Dashboard"),
    ("dtcs", "DTCs"),
    ("log", "Log"),
    ("vehicle", "Vehicle"),
]


class TabBar(Widget):
    """btop-style bracketed tab bar."""

    DEFAULT_CSS = """
    TabBar {
        height: 1;
        background: $surface;
        padding: 0 1;
    }
    .tab {
        padding: 0 1;
        color: $text-muted;
    }
    .tab--active {
        color: $accent;
        text-style: bold;
    }
    """

    def __init__(self, tabs: list[tuple[str, str]], active: str) -> None:
        super().__init__()
        self._tabs = tabs
        self._active = active

    def compose(self) -> ComposeResult:
        with Horizontal():
            for tab_id, label in self._tabs:
                classes = "tab tab--active" if tab_id == self._active else "tab"
                yield Static(f"[ {label} ]", classes=classes, id=f"tab-{tab_id}")

    def set_active(self, tab_id: str) -> None:
        for tid, _ in self._tabs:
            widget = self.query_one(f"#tab-{tid}", Static)
            if tid == tab_id:
                widget.add_class("tab--active")
                widget.remove_class("tab")
            else:
                widget.remove_class("tab--active")
                if "tab" not in widget.classes:
                    widget.add_class("tab")
        self._active = tab_id


class MainScreen(Screen[None]):
    """Single persistent screen housing all panes."""

    BINDINGS = [
        Binding("left", "prev_tab", "← prev", show=True, priority=True),
        Binding("right", "next_tab", "→ next", show=True, priority=True),
        Binding("q", "app.quit", "Quit", show=True, priority=True),
    ]

    DEFAULT_CSS = """
    MainScreen {
        layout: vertical;
    }

    #header-strip {
        height: 1;
        background: $primary;
        color: $text;
        padding: 0 1;
        text-style: bold;
    }

    #tab-bar-container {
        height: 1;
        background: $surface-darken-1;
    }

    #content {
        height: 1fr;
    }
    """

    def __init__(
        self,
        connection: ObdConnection,
        init_result: InitResult,
        telemetry: TelemetryClient | None = None,
    ) -> None:
        super().__init__()
        self._connection = connection
        self._init = init_result
        self._telemetry = telemetry
        self._queue: asyncio.Queue[Reading] = asyncio.Queue()
        self._poller: Poller | None = None
        self._tab_ids = [t[0] for t in TABS]
        self._active_idx = 0
        self._uds_task: asyncio.Task[None] | None = None
        self._uds_responding = 0
        self._uds_start = 0.0
        self._uds_label = ""

    def compose(self) -> ComposeResult:
        transport = getattr(self._connection, "transport_label", "")
        proto_display = self._init.protocol_name or "—"
        if transport:
            proto_display += f" [{transport}]"
        info = (
            f" HUDSON   "
            f"VIN: {self._init.vin or '—'}   "
            f"{self._init.manufacturer_name}   "
            f"{proto_display}   "
            f"PIDs: {len(self._init.supported_commands)}"
        )
        yield Static(info, id="header-strip")

        with Horizontal(id="tab-bar-container"):
            yield TabBar(TABS, self._tab_ids[0])

        with ContentSwitcher(initial="dashboard", id="content"):
            yield DashboardPane(self._connection, self._queue, self._init, id="dashboard")
            yield DtcPane(self._connection, self._init, telemetry=self._telemetry, id="dtcs")
            yield LogPane(id="log")
            yield VehiclePane(self._init, id="vehicle")

        yield StatusBar(self._connection, self._init, id="status-bar")

    async def on_mount(self) -> None:
        supported_names = {c.name for c in self._init.supported_commands}
        active_specs = [
            PollSpec(getattr(obd.commands, pid), cfg.interval)
            for pid, cfg in GAUGE_CATALOG.items()
            if hasattr(obd.commands, pid) and (not supported_names or pid in supported_names)
        ]

        if active_specs:
            on_reading = self._telemetry.record_reading if self._telemetry is not None else None
            self._poller = Poller(self._connection, active_specs, self._queue, on_reading=on_reading)
            await self._poller.start()

        has_uds = self._init.uds_discovery is not None
        has_secondary = (
            self._init.discovered_ecus is not None
            and any(a != 0x7E0 for a in self._init.discovered_ecus.found)
            and self._init.cache is not None
        )
        if has_uds or has_secondary:
            self._uds_task = asyncio.create_task(self._run_uds_background())
            self._uds_task.add_done_callback(self._on_uds_done)

    async def on_unmount(self) -> None:
        if self._poller:
            await self._poller.stop()
        if self._uds_task is not None and not self._uds_task.done():
            self._uds_task.cancel()
            try:
                await self._uds_task
            except (asyncio.CancelledError, Exception):
                pass

    async def _on_uds_progress(
        self, current: int, total: int, _identifier: int, responded: bool
    ) -> None:
        if responded:
            self._uds_responding += 1
        if current % _PROGRESS_UPDATE_INTERVAL == 0 or current == total:
            elapsed = monotonic() - self._uds_start
            try:
                self.query_one(StatusBar).show_scanning(
                    current, total, self._uds_responding, elapsed, self._uds_label
                )
            except Exception:
                pass

    async def _run_uds_background(self) -> None:
        """Sequential UDS background sweep: ECM priority-2, then secondary ECUs."""
        # ── ECM priority-2 ───────────────────────────────────────────────────
        if self._init.uds_discovery is not None:
            self._uds_label = "ECM 7E0"
            self._uds_start = monotonic()
            self._uds_responding = 0
            await self._init.uds_discovery.run_priority2_background(
                on_progress=self._on_uds_progress,
            )

        # ── Secondary ECUs: P1 then P2, one at a time ────────────────────────
        disc = self._init.discovered_ecus
        cache = self._init.cache
        if disc is None or cache is None:
            return

        make = self._init.manufacturer_name
        vin_prefix = self._init.vin[:8] if self._init.vin else ""

        for addr, ecu_info in sorted(disc.found.items()):
            if addr == 0x7E0:
                continue
            label = ecu_info.label or f"ECU {addr:03X}"
            self._uds_label = f"{label} {addr:03X}"
            self._uds_start = monotonic()
            self._uds_responding = 0

            fallback = f"vin:{vin_prefix}" if vin_prefix else f"addr:{addr:03X}"
            secondary = UdsDiscovery(
                self._connection, cache,
                ecu_version=fallback,
                make=make,
                ecu_addr=addr,
            )
            try:
                version = await secondary.read_ecu_version()
                if version:
                    secondary.ecu_version = version
                await secondary.run_priority1(self._on_uds_progress)
                self._uds_start = monotonic()
                self._uds_responding = 0
                await secondary.run_priority2_background(self._on_uds_progress)
            except Exception as exc:
                log.warning("UDS sweep for ECU 0x%03X failed: %s", addr, exc)

        # Restore ATSH to ECM after secondary sweeps leave it at the last ECU's address.
        try:
            await self._connection.send_at("ATSH 7E0")
        except Exception as exc:
            log.warning("ATSH 7E0 restore after secondary ECU sweep failed: %s", exc)

    def _on_uds_done(self, task: asyncio.Task[None]) -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            log.error("UDS priority-2 background scan failed", exc_info=exc)
            return
        try:
            self.query_one(StatusBar).show_complete(self._uds_responding)
        except Exception:
            log.debug("StatusBar update skipped (widget not available)")

    def on_dtc_pane_scan_phase(self, msg: DtcPane.ScanPhase) -> None:
        try:
            bar = self.query_one(StatusBar)
            if msg.phase:
                bar.show_scan_phase(msg.phase)
            else:
                bar.hide_scan()
        except Exception:
            pass

    def action_next_tab(self) -> None:
        self._active_idx = (self._active_idx + 1) % len(self._tab_ids)
        self._switch_to(self._tab_ids[self._active_idx])

    def action_prev_tab(self) -> None:
        self._active_idx = (self._active_idx - 1) % len(self._tab_ids)
        self._switch_to(self._tab_ids[self._active_idx])

    def _switch_to(self, tab_id: str) -> None:
        self.query_one(ContentSwitcher).current = tab_id
        self.query_one(TabBar).set_active(tab_id)
