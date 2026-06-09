"""Tests for Hudson.core.ecu_cache — async SQLite ECU discovery cache."""

from __future__ import annotations

from pathlib import Path

import pytest

from Hudson.core.ecu_cache import EcuCache


@pytest.fixture()
async def cache(tmp_path: Path) -> EcuCache:
    c = EcuCache(tmp_path / "test.db")
    await c.init()
    return c


@pytest.mark.asyncio
async def test_init_creates_tables(tmp_path: Path) -> None:
    """init() creates the database and all three tables without error."""
    db_path = tmp_path / "ecu.db"
    cache = EcuCache(db_path)
    await cache.init()
    assert db_path.exists()


@pytest.mark.asyncio
async def test_init_idempotent(tmp_path: Path) -> None:
    """Calling init() twice does not raise (CREATE TABLE IF NOT EXISTS)."""
    cache = EcuCache(tmp_path / "ecu.db")
    await cache.init()
    await cache.init()


@pytest.mark.asyncio
async def test_get_discovered_identifiers_miss(cache: EcuCache) -> None:
    """Querying an unknown ECU version returns an empty list."""
    result = await cache.get_discovered_identifiers("unknown-ecu")
    assert result == []


@pytest.mark.asyncio
async def test_save_and_get_identifiers(cache: EcuCache) -> None:
    """Saved identifiers are retrievable for the correct ECU version."""
    batch = [(0xF190, True, b"VIN_DATA"), (0xDEAD, False, None)]
    await cache.save_identifiers_batch("ECU-0001", batch)
    rows = await cache.get_discovered_identifiers("ECU-0001")
    assert len(rows) == 2
    responding = {r["identifier"]: r for r in rows}
    assert responding[0xF190]["responded"] == 1
    assert responding[0xF190]["raw_value"] == b"VIN_DATA"
    assert responding[0xDEAD]["responded"] == 0
    assert responding[0xDEAD]["raw_value"] is None


@pytest.mark.asyncio
async def test_identifiers_ordered_by_identifier(cache: EcuCache) -> None:
    """get_discovered_identifiers returns rows sorted by identifier."""
    batch = [(0x0200, True, None), (0x0100, True, None), (0x0300, True, None)]
    await cache.save_identifiers_batch("ECU-ORDER", batch)
    rows = await cache.get_discovered_identifiers("ECU-ORDER")
    ids = [r["identifier"] for r in rows]
    assert ids == sorted(ids)


@pytest.mark.asyncio
async def test_different_ecu_versions_isolated(cache: EcuCache) -> None:
    """Data saved for one ECU version is not visible under a different version."""
    await cache.save_identifiers_batch("ECU-A", [(0x1000, True, b"a")])
    await cache.save_identifiers_batch("ECU-B", [(0x2000, True, b"b")])
    rows_a = await cache.get_discovered_identifiers("ECU-A")
    rows_b = await cache.get_discovered_identifiers("ECU-B")
    assert len(rows_a) == 1 and rows_a[0]["identifier"] == 0x1000
    assert len(rows_b) == 1 and rows_b[0]["identifier"] == 0x2000


@pytest.mark.asyncio
async def test_priority1_not_complete_by_default(cache: EcuCache) -> None:
    """priority1_complete returns False before mark_priority1_complete is called."""
    assert not await cache.priority1_complete("ECU-NEW")


@pytest.mark.asyncio
async def test_mark_and_check_priority1_complete(cache: EcuCache) -> None:
    """mark_priority1_complete sets the flag; priority1_complete returns True."""
    await cache.mark_priority1_complete("ECU-0001", vin_prefix="WV2ZZZ")
    assert await cache.priority1_complete("ECU-0001")


@pytest.mark.asyncio
async def test_priority1_complete_isolated(cache: EcuCache) -> None:
    """Marking one ECU complete does not affect another."""
    await cache.mark_priority1_complete("ECU-DONE")
    assert not await cache.priority1_complete("ECU-OTHER")


@pytest.mark.asyncio
async def test_get_ecu_version_info_miss(cache: EcuCache) -> None:
    """get_ecu_version_info returns None for unknown ECU version."""
    assert await cache.get_ecu_version_info("no-such-ecu") is None


@pytest.mark.asyncio
async def test_get_ecu_version_info_hit(cache: EcuCache) -> None:
    """get_ecu_version_info returns a dict after mark_priority1_complete."""
    await cache.mark_priority1_complete("ECU-VER", vin_prefix="WVW")
    info = await cache.get_ecu_version_info("ECU-VER")
    assert info is not None
    assert info["ecu_version"] == "ECU-VER"
    assert info["vin_prefix"] == "WVW"
    assert info["priority1_complete"] == 1
    assert "discovered_at" in info


@pytest.mark.asyncio
async def test_save_and_get_progress(cache: EcuCache) -> None:
    """Progress (last_identifier, phase) round-trips correctly."""
    await cache.save_progress("ECU-PROG", 0xF000, "priority1")
    result = await cache.get_progress("ECU-PROG")
    assert result is not None
    last_id, phase = result
    assert last_id == 0xF000
    assert phase == "priority1"


@pytest.mark.asyncio
async def test_get_progress_miss(cache: EcuCache) -> None:
    """get_progress returns None when no progress has been saved."""
    assert await cache.get_progress("ECU-NOPROGRESS") is None


@pytest.mark.asyncio
async def test_save_progress_overwrites(cache: EcuCache) -> None:
    """Saving progress twice updates the record in place."""
    await cache.save_progress("ECU-OW", 0x1000, "phase1")
    await cache.save_progress("ECU-OW", 0x2000, "phase2")
    result = await cache.get_progress("ECU-OW")
    assert result == (0x2000, "phase2")
