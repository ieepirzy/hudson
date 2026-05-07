"""Log pane — scrolling raw event log."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.widget import Widget
from textual.widgets import RichLog


class LogPane(Widget):
    """Scrolling log of raw OBD events and errors."""

    DEFAULT_CSS = """
    LogPane {
        height: 1fr;
        padding: 1;
    }

    RichLog {
        height: 1fr;
        border: round $primary 50%;
        padding: 0 1;
    }
    """

    def compose(self) -> ComposeResult:
        yield RichLog(highlight=True, markup=True, id="raw-log")

    def write(self, message: str) -> None:
        """Write a line to the log. Call from anywhere in the app."""
        self.query_one(RichLog).write(message)
