"""Log pane — scrolling Python logging output."""

from __future__ import annotations

import logging

from textual.app import ComposeResult
from textual.widget import Widget
from textual.widgets import RichLog

_warn_error_count: int = 0


def get_warn_error_count() -> int:
    return _warn_error_count


_LEVEL_MARKUP: dict[int, tuple[str, str]] = {
    logging.DEBUG:    ("[dim]",          "[/dim]"),
    logging.INFO:     ("",               ""),
    logging.WARNING:  ("[yellow]",       "[/yellow]"),
    logging.ERROR:    ("[red]",          "[/red]"),
    logging.CRITICAL: ("[bold red]",     "[/bold red]"),
}

_FMT = logging.Formatter("%(asctime)s  %(name)s  %(levelname)-8s  %(message)s")


class _RichLogHandler(logging.Handler):
    """Routes log records to a Textual RichLog widget."""

    def __init__(self, target: RichLog) -> None:
        super().__init__()
        self._target = target
        self.setFormatter(_FMT)

    def emit(self, record: logging.LogRecord) -> None:
        global _warn_error_count
        if record.levelno >= logging.WARNING:
            _warn_error_count += 1
        try:
            text = self.format(record)
            open_tag, close_tag = _LEVEL_MARKUP.get(record.levelno, ("", ""))
            text = text.replace("[", r"\[")
            line = f"{open_tag}{text}{close_tag}" if open_tag else text
            # RichLog.write must run on the main thread; call_from_thread is a no-op
            # when already on the main thread, so this is safe either way.
            self._target.app.call_from_thread(self._target.write, line)
        except Exception:
            self.handleError(record)


class LogPane(Widget):
    """Scrolling view of Python logging output captured from the root logger."""

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

    def __init__(self, **kwargs: object) -> None:
        super().__init__(**kwargs)
        self._handler: _RichLogHandler | None = None

    def compose(self) -> ComposeResult:
        yield RichLog(highlight=False, markup=True, id="raw-log")

    def on_mount(self) -> None:
        rich_log = self.query_one(RichLog)
        rich_log.border_title = "Log"
        rich_log.border_subtitle = "live application output"
        rich_log.write("[dim]  —  waiting for log output  —[/dim]")
        self._handler = _RichLogHandler(rich_log)
        logging.getLogger().addHandler(self._handler)

    def on_unmount(self) -> None:
        if self._handler is not None:
            logging.getLogger().removeHandler(self._handler)
            self._handler = None
