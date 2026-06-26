#!/usr/bin/env python3
"""Build Hudson/data/ford.db from a FORScan DTC TSV export.

Usage:
    python3 tools/build_ford_dtc_db.py /path/to/forscan_dtc_complete.tsv

Output: Hudson/data/ford.db

Schema
------
dtc_definitions
    Populated from the FORScan export.  One row per (code, description) pair
    after cleaning.  A single DTC code may have multiple rows because FORScan
    assigns several descriptions to the same code — one per module that can set
    it, plus SAE generic entries, plus free-text diagnostic hints.

    code        TEXT  — five-character DTC code, e.g. 'P0300'.  Upper-case,
                        uses hex digits (A–F) in positions 2–5.
    description TEXT  — one cleaned description string.  The raw TSV field may
                        contain pipe-separated sub-descriptions and sometimes
                        trailing garbage from the original obfuscated source;
                        both are cleaned before import (see _clean()).

    PRIMARY KEY is (code, description) via UNIQUE constraint so re-running the
    script is idempotent.  The rowid ordering preserves TSV source order, which
    roughly places the most general/useful description first.

identifier_definitions
    Empty stub for future UDS 0x22 identifier-to-value translation data.
    See Hudson/core/uds.py for context.  Will be populated separately when
    Ford-specific identifier data becomes available.

    identifier  INTEGER  — UDS 0x22 identifier (0x0000–0xFFFF).
    name        TEXT     — human-readable signal name.
    unit        TEXT     — display unit string, e.g. 'kPa', '°C', 'rpm'.
    scale       REAL     — multiply raw integer by this to get physical value.
    offset      REAL     — add to (raw * scale) to get physical value.
    byte_order  TEXT     — 'big' (default) or 'little'.
    signed      INTEGER  — 0 = unsigned, 1 = two's-complement signed.
    byte_length INTEGER  — number of bytes to read from the response payload.
    description TEXT     — optional longer description of the signal.
"""

from __future__ import annotations

import re
import sqlite3
import sys
from pathlib import Path

# ── schema ────────────────────────────────────────────────────────────────────

_SCHEMA = """
CREATE TABLE IF NOT EXISTS dtc_definitions (
    code        TEXT NOT NULL,
    description TEXT NOT NULL,
    UNIQUE (code, description)
);
CREATE INDEX IF NOT EXISTS idx_dtc_code ON dtc_definitions (code);

CREATE TABLE IF NOT EXISTS identifier_definitions (
    identifier  INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    unit        TEXT,
    scale       REAL    NOT NULL DEFAULT 1.0,
    offset      REAL    NOT NULL DEFAULT 0.0,
    byte_order  TEXT    NOT NULL DEFAULT 'big',
    signed      INTEGER NOT NULL DEFAULT 0,
    byte_length INTEGER NOT NULL DEFAULT 1,
    description TEXT
);
"""

# ── cleaning ──────────────────────────────────────────────────────────────────

# Control-character boundary: the FORScan source was partially obfuscated;
# some descriptions have binary garbage appended after the readable text.
_CTRL_RE = re.compile(r"[\x00-\x1f\x7f]")

# Descriptions that carry no diagnostic value.
_USELESS: frozenset[str] = frozenset({
    "Refer to the workshop manual for details.",
    "No Additional Fault Symptom Available for this Diagnostic Trouble Codes",
    "No Diagnostic Trouble Codes Detected at Time of Request",
    "Malfunction Indicator Lamp Off",
})


def _clean(raw: str) -> list[str]:
    """Return cleaned sub-descriptions from one raw TSV description field.

    Steps:
    1. Truncate at the first control character (garbage boundary).
    2. Split on '|' — FORScan sometimes packs multiple sub-descriptions into
       one field separated by pipe characters.
    3. Strip surrounding whitespace from each part.
    4. Drop parts shorter than 4 characters or matching known useless strings.
    """
    m = _CTRL_RE.search(raw)
    if m:
        raw = raw[: m.start()]
    parts = [p.strip() for p in raw.split("|")]
    return [p for p in parts if len(p) >= 4 and p not in _USELESS]


# ── builder ───────────────────────────────────────────────────────────────────

def build(tsv_path: Path, db_path: Path) -> None:
    rows: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()

    with tsv_path.open(encoding="utf-8", errors="replace") as f:
        for line in f:
            parts = line.rstrip("\n").split("\t")
            if len(parts) != 2:
                continue
            code = parts[0].strip().upper()
            if not code:
                continue
            for desc in _clean(parts[1]):
                key = (code, desc)
                if key not in seen:
                    seen.add(key)
                    rows.append(key)

    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.executescript(_SCHEMA)
        conn.executemany(
            "INSERT OR IGNORE INTO dtc_definitions (code, description) VALUES (?, ?)",
            rows,
        )
        conn.commit()

    unique_codes = len({r[0] for r in rows})
    print(f"ford.db: {len(rows):,} descriptions across {unique_codes:,} codes → {db_path}")


# ── entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    if len(sys.argv) != 2:
        print(f"Usage: {sys.argv[0]} <forscan_dtc_complete.tsv>")
        sys.exit(1)
    tsv = Path(sys.argv[1])
    if not tsv.exists():
        print(f"error: file not found: {tsv}", file=sys.stderr)
        sys.exit(1)
    db = Path(__file__).resolve().parent.parent / "Hudson" / "data" / "ford.db"
    build(tsv, db)


if __name__ == "__main__":
    main()
