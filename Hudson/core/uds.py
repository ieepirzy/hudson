"""UDS discovery engine — read-only service 0x22 (ReadDataByIdentifier) only.

Safety contract:
  Only service 0x22 is ever sent.  Services 0x27 (security access), 0x2E
  (write data), 0x2F (I/O control), 0x31 (routine control), and 0x34–0x36
  (memory transfer) are intentionally absent from this module.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from time import monotonic

from Hudson.core.connection import ObdConnection
from Hudson.core.ecu_cache import EcuCache

log = logging.getLogger(__name__)

# ── Identifier ranges ─────────────────────────────────────────────────────────

PRIORITY1_RANGES: list[tuple[int, int]] = [
    (0xF100, 0xF1FF),  # standardised vehicle info (VIN, SW versions, …)
    (0xF400, 0xF4FF),  # VAG engine measuring blocks
    (0xF600, 0xF6FF),  # VAG transmission data
    (0x0600, 0x06FF),  # common UDS extended data
]

PRIORITY2_RANGES: list[tuple[int, int]] = [
    (0x0000, 0xFFFF),  # full sweep — background only, 5 req/sec
]

# Fake responding identifiers returned in --mock mode.
MOCK_UDS_IDENTIFIERS: list[int] = [0xF189, 0xF190, 0xF400, 0xF401, 0xF40B, 0xF40C]
_MOCK_IDENTIFIER_SET: frozenset[int] = frozenset(MOCK_UDS_IDENTIFIERS)

# Mock responses keyed by identifier — data bytes after stripping UDS header.
_MOCK_RESPONSES: dict[int, bytes] = {
    0xF189: b"0001\x00",                          # ECU SW version "0001"
    0xF190: b"WV2ZZZ7HZ8H123456",                 # VIN
    0xF400: bytes([0x00, 0x0F, 0x00, 0x00]),       # boost actual
    0xF401: bytes([0x00, 0x12, 0x00, 0x00]),       # boost setpoint
    0xF40B: bytes([0x01, 0x2C]),                   # boost pressure actual (kPa)
    0xF40C: bytes([0x01, 0x40]),                   # boost pressure specified (kPa)
}

_SAVE_BATCH_SIZE = 50
_PRIORITY2_RATE = 5.0          # max requests per second for background sweep
_DEFAULT_TIMEOUT = 0.15        # seconds per identifier probe
_KEEPALIVE_INTERVAL = 4.0      # seconds between TesterPresent keepalives

ProgressCallback = Callable[[int, int, int, bool], Awaitable[None]]


def _build_identifiers(ranges: list[tuple[int, int]]) -> list[int]:
    ids: list[int] = []
    for start, end in ranges:
        ids.extend(range(start, end + 1))
    return ids


class UdsDiscovery:
    """Discover which UDS identifiers an ECU responds to via service 0x22."""

    def __init__(
        self,
        connection: ObdConnection,
        cache: EcuCache,
        ecu_version: str,
    ) -> None:
        self._connection = connection
        self._cache = cache
        self.ecu_version = ecu_version
        self._p1_responding: list[int] = []

    async def read_ecu_version(self) -> str | None:
        """Query identifier 0xF189 (SW version string) and return decoded text."""
        data = await self._connection.query_uds(0x22, 0xF189)
        if not data:
            return None
        return data.decode("ascii", errors="replace").strip("\x00 ")

    # ── Priority 1 ───────────────────────────────────────────────────────────

    async def run_priority1(self, on_progress: ProgressCallback) -> list[int]:
        """Probe PRIORITY1_RANGES.  Returns list of responding identifier ints."""
        if self._connection.is_mock:
            return await self._run_priority1_mock(on_progress)
        return await self._run_priority1_real(on_progress)

    async def _run_priority1_real(self, on_progress: ProgressCallback) -> list[int]:
        identifiers = _build_identifiers(PRIORITY1_RANGES)
        total = len(identifiers)

        # Resume from a previous interrupted run if possible.
        start_idx = 0
        saved = await self._cache.get_progress(self.ecu_version)
        if saved and saved[1] == "priority1":
            last = saved[0]
            for i, ident in enumerate(identifiers):
                if ident > last:
                    start_idx = i
                    break

        responding: list[int] = []
        batch: list[tuple[int, bool, bytes | None]] = []
        last_keepalive = monotonic()

        for i, identifier in enumerate(identifiers[start_idx:], start=start_idx):
            responded, raw = await self._probe_identifier(identifier)
            if responded:
                responding.append(identifier)
            batch.append((identifier, responded, raw))

            if len(batch) >= _SAVE_BATCH_SIZE:
                await self._cache.save_identifiers_batch(self.ecu_version, batch)
                await self._cache.save_progress(self.ecu_version, identifier, "priority1")
                batch.clear()

            now = monotonic()
            if now - last_keepalive >= _KEEPALIVE_INTERVAL:
                await self._connection.send_tester_present()
                last_keepalive = now

            await on_progress(i + 1, total, identifier, responded)

        if batch:
            await self._cache.save_identifiers_batch(self.ecu_version, batch)

        vin_prefix = self.ecu_version[:3] if len(self.ecu_version) >= 3 else self.ecu_version
        await self._cache.mark_priority1_complete(self.ecu_version, vin_prefix)
        self._p1_responding = responding
        return responding

    async def _run_priority1_mock(self, on_progress: ProgressCallback) -> list[int]:
        """Fake priority-1 sweep with animated progress (~2 seconds)."""
        identifiers = _build_identifiers(PRIORITY1_RANGES)
        total = len(identifiers)
        responding: list[int] = []

        # Sleep every 32 iterations so the progress bar animates smoothly.
        # (total / 32) * 0.065 ≈ 2.08 s for 1024 identifiers.
        for i, identifier in enumerate(identifiers):
            responded = identifier in _MOCK_IDENTIFIER_SET
            if responded:
                responding.append(identifier)
            if i % 32 == 0:
                await asyncio.sleep(0.065)
            await on_progress(i + 1, total, identifier, responded)

        self._p1_responding = responding
        return responding

    # ── Priority 2 (background) ───────────────────────────────────────────────

    async def run_priority2_background(
        self,
        on_progress: ProgressCallback | None = None,
    ) -> None:
        """Full 0x0000–0xFFFF sweep at ≤5 req/sec.  Designed for asyncio.create_task()."""
        if self._connection.is_mock:
            return

        if not self._p1_responding:
            # ECU didn't respond to any priority-1 identifier; skip the full sweep.
            log.info("priority1 found no responses — skipping priority2 sweep")
            return

        identifiers = _build_identifiers(PRIORITY2_RANGES)
        total = len(identifiers)

        start_idx = 0
        saved = await self._cache.get_progress(self.ecu_version)
        if saved and saved[1] == "priority2":
            last = saved[0]
            for i, ident in enumerate(identifiers):
                if ident > last:
                    start_idx = i
                    break

        batch: list[tuple[int, bool, bytes | None]] = []
        delay = 1.0 / _PRIORITY2_RATE
        last_keepalive = monotonic()

        for i, identifier in enumerate(identifiers[start_idx:], start=start_idx):
            responded, raw = await self._probe_identifier(identifier)
            batch.append((identifier, responded, raw))

            if len(batch) >= _SAVE_BATCH_SIZE:
                await self._cache.save_identifiers_batch(self.ecu_version, batch)
                await self._cache.save_progress(self.ecu_version, identifier, "priority2")
                batch.clear()

            if on_progress is not None:
                await on_progress(i + 1, total, identifier, responded)

            now = monotonic()
            if now - last_keepalive >= _KEEPALIVE_INTERVAL:
                await self._connection.send_tester_present()
                last_keepalive = now

            await asyncio.sleep(delay)

        if batch:
            await self._cache.save_identifiers_batch(self.ecu_version, batch)

    # ── Internal ──────────────────────────────────────────────────────────────

    async def _probe_identifier(self, identifier: int) -> tuple[bool, bytes | None]:
        try:
            data = await asyncio.wait_for(
                self._connection.query_uds(0x22, identifier),
                timeout=_DEFAULT_TIMEOUT,
            )
            return (data is not None, data)
        except asyncio.TimeoutError:
            return (False, None)
        except Exception:
            log.debug("UDS probe 0x%04X raised", identifier, exc_info=True)
            return (False, None)
