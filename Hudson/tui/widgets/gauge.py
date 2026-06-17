"""Gauge widget — colored PID display with sparkline history and bar indicator."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass

from textual.app import ComposeResult
from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import Static

SPARK_CHARS = " ▁▂▃▄▅▆▇█"
BAR_FULL = "█"
BAR_EMPTY = "░"
BAR_WIDTH = 20


def _spark(history: deque[float], max_val: float) -> str:
    if not history:
        return ""
    peak = max(history) or max_val
    out = []
    for v in history:
        idx = int((v / peak) * (len(SPARK_CHARS) - 1))
        out.append(SPARK_CHARS[max(0, min(idx, len(SPARK_CHARS) - 1))])
    return "".join(out)


def _bar(value: float, max_val: float) -> str:
    if max_val == 0:
        return BAR_EMPTY * BAR_WIDTH
    pct = min(1.0, max(0.0, value) / max_val)
    filled = round(pct * BAR_WIDTH)
    return BAR_FULL * filled + BAR_EMPTY * (BAR_WIDTH - filled)


@dataclass(frozen=True)
class GaugeConfig:
    """Display + polling config for one PID."""

    label: str
    unit: str
    max_val: float
    color: str
    interval: float  # poll interval in seconds


# Single source of truth for every PID that can appear on the dashboard.
# Keys must match obd.commands.<name> exactly.
# Order determines left-to-right, top-to-bottom display priority.
GAUGE_CATALOG: dict[str, GaugeConfig] = {
    # ── fast (0.1 s) ────────────────────────────────────────────────
    "RPM":                      GaugeConfig("RPM",           "rpm",  6000, "dodgerblue",     0.1),
    "SPEED":                    GaugeConfig("Speed",         "km/h",  200, "cyan",            0.1),
    "THROTTLE_POS":             GaugeConfig("Throttle",      "%",     100, "gold",            0.1),
    "RELATIVE_THROTTLE_POS":    GaugeConfig("Throttle Rel",  "%",     100, "khaki",           0.1),
    "ACCELERATOR_POS_D":        GaugeConfig("Accel D",       "%",     100, "lightseagreen",   0.1),
    "ACCELERATOR_POS_E":        GaugeConfig("Accel E",       "%",     100, "mediumseagreen",  0.1),
    "THROTTLE_ACTUATOR":        GaugeConfig("Throttle Act",  "%",     100, "lightsalmon",     0.1),
    # ── medium (0.5 s) ──────────────────────────────────────────────
    "ENGINE_LOAD":              GaugeConfig("Engine Load",   "%",     100, "mediumpurple",    0.5),
    "ABSOLUTE_LOAD":            GaugeConfig("Abs Load",      "%",     100, "plum",            0.5),
    "MAF":                      GaugeConfig("MAF",           "g/s",   655, "deepskyblue",     0.5),
    "SHORT_FUEL_TRIM_1":        GaugeConfig("Fuel Trim S1",  "%",      50, "orange",          0.5),
    "LONG_FUEL_TRIM_1":         GaugeConfig("Fuel Trim L1",  "%",      50, "darkorange",      0.5),
    # ── slow (1 s) ──────────────────────────────────────────────────
    "COOLANT_TEMP":             GaugeConfig("Coolant",       "°C",    130, "tomato",          1.0),
    "INTAKE_TEMP":              GaugeConfig("Intake Air",    "°C",     80, "limegreen",       1.0),
    "AMBIENT_AIR_TEMP":         GaugeConfig("Ambient",       "°C",     60, "palegreen",       1.0),
    "INTAKE_PRESSURE":          GaugeConfig("Intake MAP",    "kPa",   255, "slateblue",       1.0),
    "TIMING_ADVANCE":           GaugeConfig("Timing Adv",    "°",      64, "yellow",          1.0),
    "COMMANDED_EQUIV_RATIO":    GaugeConfig("Lambda",        "",        2, "orchid",          1.0),
    "COMMANDED_EGR":            GaugeConfig("EGR Cmd",       "%",     100, "rosybrown",       1.0),
    # ── very slow (5 s) ─────────────────────────────────────────────
    "FUEL_LEVEL":               GaugeConfig("Fuel Level",    "%",     100, "darkorange",      5.0),
    "BAROMETRIC_PRESSURE":      GaugeConfig("Baro",          "kPa",   110, "steelblue",       5.0),
    "CONTROL_MODULE_VOLTAGE":   GaugeConfig("Battery",       "V",      16, "greenyellow",     5.0),
    "CATALYST_TEMP_B1S1":       GaugeConfig("Cat B1S1",      "°C",   1300, "firebrick",       5.0),
    "CATALYST_TEMP_B2S1":       GaugeConfig("Cat B2S1",      "°C",   1300, "crimson",         5.0),
    "OIL_TEMP":                 GaugeConfig("Oil Temp",      "°C",    150, "coral",           5.0),
    "ENGINE_FUEL_RATE":         GaugeConfig("Fuel Rate",     "L/h",   200, "lightblue",       5.0),
    "HYBRID_BATTERY_REMAINING": GaugeConfig("HV Battery",    "%",     100, "lime",            5.0),
}


class Gauge(Widget):
    """PID gauge with sparkline history, bar indicator, and per-type color."""

    value: reactive[float | None] = reactive(None)

    DEFAULT_CSS = """
    Gauge {
        border: round $primary 40%;
        padding: 0;
        height: 7;
        layout: vertical;
    }
    Gauge.gauge--disabled {
        border: round $surface-lighten-1 30%;
        opacity: 0.35;
    }
    .gauge--header {
        height: 1;
        padding: 0 1;
    }
    .gauge--spark {
        height: 1fr;
        padding: 0 1;
        opacity: 0.5;
        overflow: hidden;
    }
    .gauge--bar {
        height: 1;
        padding: 0 1;
    }
    .gauge--minmax {
        height: 1;
        padding: 0 1;
        opacity: 0.3;
        text-align: right;
    }
    """

    def __init__(
        self,
        pid_name: str,
        config: GaugeConfig,
        *,
        widget_id: str | None = None,
    ) -> None:
        super().__init__(id=widget_id)
        self._pid_name = pid_name
        self._label = config.label
        self._unit = config.unit
        self._max = config.max_val
        self._color = config.color
        self._history: deque[float] = deque(maxlen=40)
        self._disabled = False

    def compose(self) -> ComposeResult:
        yield Static(classes="gauge--header", id="header")
        yield Static("", classes="gauge--spark", id="spark")
        yield Static("", classes="gauge--bar", id="bar")
        yield Static(f"0 – {self._max:.0f} {self._unit}", classes="gauge--minmax")

    def on_mount(self) -> None:
        self.styles.border = ("round", self._color)
        self._refresh_display(None)

    def watch_value(self, value: float | None) -> None:
        if self._disabled:
            return
        if value is not None:
            self._history.append(value)
        self._refresh_display(value)

    def _refresh_display(self, value: float | None) -> None:
        color = self._color
        if value is None:
            reading = "[dim]--[/]"
            bar_str = f"[dim]{BAR_EMPTY * BAR_WIDTH}[/]"
            spark_str = ""
        else:
            reading = f"[bold {color}]{value:.0f} {self._unit}[/]"
            bar_str = f"[{color}]{_bar(value, self._max)}[/]"
            spark_str = f"[{color}]{_spark(self._history, self._max)}[/]"

        try:
            self.query_one("#header", Static).update(
                f"[bold {color} dim]{self._label.upper()}[/]  {reading}"
            )
            self.query_one("#spark", Static).update(spark_str)
            self.query_one("#bar", Static).update(bar_str)
        except Exception:  # noqa: BLE001
            # Widget not yet composed (race between poller and mount); next reading will update.
            pass

    def disable(self) -> None:
        self._disabled = True
        self.add_class("gauge--disabled")
        try:
            self.query_one("#header", Static).update(
                f"[dim]{self._label.upper()}  n/a[/]"
            )
        except Exception:  # noqa: BLE001
            pass
