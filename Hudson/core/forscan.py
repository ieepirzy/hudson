"""ForScan DTC database loader.

Looks for forscan_dtc_complete.tsv in the user's home directory and, if
present, provides Ford-specific DTC descriptions.  The file is not
distributed with Hudson — it is user-supplied.  When absent, lookups
silently return None.
"""

from __future__ import annotations

import logging
from pathlib import Path

log = logging.getLogger(__name__)

_DB_PATH = Path.home() / "forscan_dtc_complete.tsv"

_db: dict[str, str] | None = None
_loaded = False


def _load() -> dict[str, str]:
    global _db, _loaded
    if _loaded:
        return _db or {}
    _loaded = True
    if not _DB_PATH.exists():
        return {}
    try:
        raw: dict[str, list[str]] = {}
        with open(_DB_PATH, encoding="utf-8", errors="replace") as f:
            for line in f:
                parts = line.rstrip("\n").split("\t", 1)
                if len(parts) == 2:
                    code = parts[0].strip().upper()
                    desc = parts[1].strip()
                    if code and desc:
                        raw.setdefault(code, []).append(desc)
        _db = {k: " / ".join(dict.fromkeys(v)) for k, v in raw.items()}
        corrupted = sum(1 for v in _db.values() if "\ufffd" in v)
        if corrupted:
            log.warning(
                "ForScan DTC database: %d entr%s contain replacement characters (U+FFFD) "
                "— file may not be UTF-8 encoded",
                corrupted,
                "ies" if corrupted != 1 else "y",
            )
        log.debug("Loaded %d ForScan DTC entries from %s", len(_db), _DB_PATH)
    except Exception:
        log.warning("Failed to load ForScan DTC database from %s", _DB_PATH)
    return _db or {}


def lookup(code: str) -> str | None:
    """Return the ForScan description for *code*, or None if not found."""
    return _load().get(code.upper().strip())
