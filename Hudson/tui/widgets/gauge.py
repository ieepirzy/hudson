"""Gauge widget — colored PID display with sparkline history and bar indicator."""

from __future__ import annotations

from collections import deque

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
    pct = min(1.0, value / max_val)
    filled = round(pct * BAR_WIDTH)
    return BAR_FULL * filled + BAR_EMPTY * (BAR_WIDTH - filled)


# Color per gauge type — maps to Textual CSS color names
GAUGE_COLORS: dict[str, str] = {
    "RPM":          "dodgerblue",
    "SPEED":        "cyan",
    "THROTTLE_POS": "gold",
    "COOLANT_TEMP": "tomato",
    "INTAKE_TEMP":  "limegreen",
    "ENGINE_LOAD":  "mediumpurple",
}

GAUGE_MAXES: dict[str, float] = {
    "RPM":          6000,
    "SPEED":        200,
    "THROTTLE_POS": 100,
    "COOLANT_TEMP": 130,
    "INTAKE_TEMP":  80,
    "ENGINE_LOAD":  100,
}


class Gauge(Widget):
    """PID gauge with sparkline history, bar indicator, and per-type color."""

    value: reactive[float | None] = reactive(None)

    DEFAULT_CSS = """
    Gauge {
        border: round $primary 40%;
        padding: 0;
        height: 100%;
        layout: vertical;
    }
    Gauge.gauge--disabled {
        border: round $surface-lighten-1 30%;
        opacity: 0.35;
    }
    .gauge--header {
        height: 1;
        padding: 0 1;
        layout: horizontal;
    }
    .gauge--label {
        text-style: bold;
        width: 1fr;
        opacity: 0.6;
    }
    .gauge--reading {
        text-align: right;
        text-style: bold;
        width: auto;
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
        font-size: 80%;
    }
    """

    def __init__(
        self,
        label: str,
        pid_name: str,
        *,
        unit: str = "",
        widget_id: str | None = None,
    ) -> None:
        super().__init__(id=widget_id)
        self._label = label
        self._pid_name = pid_name
        self._unit = unit
        self._max = GAUGE_MAXES.get(pid_name, 100)
        self._color = GAUGE_COLORS.get(pid_name, "white")
        self._history: deque[float] = deque(maxlen=40)
        self._disabled = False

    def compose(self) -> ComposeResult:
        yield Static(classes="gauge--header", id="header")
        yield Static("", classes="gauge--spark", id="spark")
        yield Static("", classes="gauge--bar", id="bar")
        yield Static(f"0 – {self._max:.0f} {self._unit}", classes="gauge--minmax")

    def on_mount(self) -> None:
        color = self._color
        self.styles.border = ("round", color)
        self.query_one("#header", Static).update(
            f"[bold {color} dim]{self._label.upper()}[/]"
        )
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
            reading = f"[dim]--[/]"
            bar_str = f"[dim]{BAR_EMPTY * BAR_WIDTH}[/]"
            spark_str = ""
        else:
            reading = f"[bold {color}]{value:.0f} {self._unit}[/]"
            bar_str = f"[{color}]{_bar(value, self._max)}[/]"
            spark_str = f"[{color}]{_spark(self._history, self._max)}[/]"

        self.query_one("#header", Static).update(
            f"[bold {color} dim]{self._label.upper()}[/]  {reading}"
        )
        self.query_one("#spark", Static).update(spark_str)
        self.query_one("#bar", Static).update(bar_str)

    def disable(self) -> None:
        self._disabled = True
        self.add_class("gauge--disabled")
        try:
            self.query_one("#header", Static).update(
                f"[dim]{self._label.upper()}  n/a[/]"
            )
        except Exception:  # noqa: BLE001
            pass
