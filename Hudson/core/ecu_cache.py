"""SQLite-backed cache for discovered UDS identifiers, keyed by ECU version string.

Database lives at ~/.hudson/ecu_cache.db so it persists across sessions and
accumulates knowledge about the specific ECU fitted to the vehicle.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

import aiosqlite

log = logging.getLogger(__name__)

_DB_PATH = Path.home() / ".hudson" / "ecu_cache.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS ecu_versions (
    ecu_version         TEXT PRIMARY KEY,
    vin_prefix          TEXT,
    discovered_at       TEXT,
    priority1_complete  INTEGER DEFAULT 0,
    priority2_complete  INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS discovered_identifiers (
    ecu_version  TEXT,
    identifier   INTEGER,
    responded    INTEGER,
    raw_value    BLOB,
    label        TEXT,
    unit         TEXT,
    PRIMARY KEY (ecu_version, identifier)
);

CREATE TABLE IF NOT EXISTS discovery_progress (
    ecu_version      TEXT PRIMARY KEY,
    last_identifier  INTEGER,
    phase            TEXT
);

-- Physical ECU addresses found by Tier C brute-force, keyed by VIN prefix.
-- Persisted so subsequent starts skip the slow brute-force sweep.
CREATE TABLE IF NOT EXISTS tier_c_complete (
    vin_prefix    TEXT PRIMARY KEY,
    discovered_at TEXT
);

CREATE TABLE IF NOT EXISTS tier_c_addresses (
    vin_prefix  TEXT,
    address     INTEGER,
    PRIMARY KEY (vin_prefix, address)
);
"""


class EcuCache:
    """Async SQLite cache for UDS discovery results."""

    def __init__(self, db_path: Path | None = None) -> None:
        self._path = db_path or _DB_PATH

    async def init(self) -> None:
        """Create database directory and tables if they do not exist."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self._path) as db:
            await db.executescript(_SCHEMA)
            await db.commit()
        log.debug("ECU cache initialised at %s", self._path)

    async def get_ecu_version_info(self, ecu_version: str) -> dict | None:
        async with aiosqlite.connect(self._path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM ecu_versions WHERE ecu_version = ?",
                (ecu_version,),
            ) as cur:
                row = await cur.fetchone()
                return dict(row) if row else None

    async def save_identifiers_batch(
        self,
        ecu_version: str,
        batch: list[tuple[int, bool, bytes | None]],
    ) -> None:
        """Batch-insert (identifier, responded, raw_value) tuples."""
        async with aiosqlite.connect(self._path) as db:
            await db.executemany(
                """
                INSERT OR REPLACE INTO discovered_identifiers
                    (ecu_version, identifier, responded, raw_value)
                VALUES (?, ?, ?, ?)
                """,
                [(ecu_version, ident, int(resp), raw) for ident, resp, raw in batch],
            )
            await db.commit()

    async def get_discovered_identifiers(self, ecu_version: str) -> list[dict]:
        async with aiosqlite.connect(self._path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """
                SELECT * FROM discovered_identifiers
                WHERE ecu_version = ?
                ORDER BY identifier
                """,
                (ecu_version,),
            ) as cur:
                return [dict(r) for r in await cur.fetchall()]

    async def get_progress(self, ecu_version: str) -> tuple[int, str] | None:
        """Return (last_identifier, phase) for a partial discovery run, or None."""
        async with aiosqlite.connect(self._path) as db:
            async with db.execute(
                "SELECT last_identifier, phase FROM discovery_progress WHERE ecu_version = ?",
                (ecu_version,),
            ) as cur:
                row = await cur.fetchone()
                return (row[0], row[1]) if row else None

    async def save_progress(self, ecu_version: str, last_identifier: int, phase: str) -> None:
        async with aiosqlite.connect(self._path) as db:
            await db.execute(
                """
                INSERT OR REPLACE INTO discovery_progress
                    (ecu_version, last_identifier, phase)
                VALUES (?, ?, ?)
                """,
                (ecu_version, last_identifier, phase),
            )
            await db.commit()

    async def mark_priority1_complete(self, ecu_version: str, vin_prefix: str = "") -> None:
        now = datetime.now(timezone.utc).isoformat()
        async with aiosqlite.connect(self._path) as db:
            await db.execute(
                """
                INSERT OR REPLACE INTO ecu_versions
                    (ecu_version, vin_prefix, discovered_at, priority1_complete)
                VALUES (?, ?, ?, 1)
                """,
                (ecu_version, vin_prefix, now),
            )
            await db.commit()

    async def priority1_complete(self, ecu_version: str) -> bool:
        async with aiosqlite.connect(self._path) as db:
            async with db.execute(
                "SELECT priority1_complete FROM ecu_versions WHERE ecu_version = ?",
                (ecu_version,),
            ) as cur:
                row = await cur.fetchone()
                return bool(row and row[0])

    async def mark_priority2_complete(self, ecu_version: str) -> None:
        async with aiosqlite.connect(self._path) as db:
            await db.execute(
                "UPDATE ecu_versions SET priority2_complete = 1 WHERE ecu_version = ?",
                (ecu_version,),
            )
            await db.commit()

    async def tier_c_complete(self, vin_prefix: str) -> bool:
        """True when a Tier C brute-force sweep has been completed for this VIN prefix."""
        async with aiosqlite.connect(self._path) as db:
            async with db.execute(
                "SELECT 1 FROM tier_c_complete WHERE vin_prefix = ?",
                (vin_prefix,),
            ) as cur:
                return await cur.fetchone() is not None

    async def get_tier_c_addresses(self, vin_prefix: str) -> list[int]:
        """Return cached Tier C addresses for *vin_prefix* (may be empty list)."""
        async with aiosqlite.connect(self._path) as db:
            async with db.execute(
                "SELECT address FROM tier_c_addresses WHERE vin_prefix = ? ORDER BY address",
                (vin_prefix,),
            ) as cur:
                return [row[0] for row in await cur.fetchall()]

    async def save_tier_c_results(self, vin_prefix: str, addresses: list[int]) -> None:
        """Persist Tier C brute-force results and mark the sweep complete."""
        now = datetime.now(timezone.utc).isoformat()
        async with aiosqlite.connect(self._path) as db:
            await db.execute(
                "INSERT OR REPLACE INTO tier_c_complete (vin_prefix, discovered_at) VALUES (?, ?)",
                (vin_prefix, now),
            )
            await db.executemany(
                "INSERT OR REPLACE INTO tier_c_addresses (vin_prefix, address) VALUES (?, ?)",
                [(vin_prefix, addr) for addr in addresses],
            )
            await db.commit()
