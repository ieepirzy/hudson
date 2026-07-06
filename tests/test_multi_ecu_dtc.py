"""Multi-ECU discovery and DTC scan integration tests.

Scenario: a Ford vehicle with two active ECUs on the CAN bus.
  - PCM (0x7E0): found via Tier A functional broadcast (responds as 0x7E8)
                 reports P0300 (confirmed + pending + MIL)
  - ABS (0x726): found via Tier B Ford address table (responds to TesterPresent)
                 reports C0035 (testFailed + confirmed)
  - All other Ford table entries: present in Tier B list but do not respond.

Tests cover:
  1. ECU discovery — both ECUs found, correct tier tags
  2. Per-ECU DTC scan — correct codes and status bits per ECU
  3. Full discover-then-scan flow — addresses from discovery feed into DTC scan
  4. Attribution — no cross-contamination between ECU DTC sets
"""

from __future__ import annotations

import asyncio

import pytest

from Hudson.core.connection import UdsResponse, UdsResponseStatus
from Hudson.core.uds_dtc import (
    DiscoveryTier,
    discover_ecus,
    scan_ecus_for_dtcs,
)
from tests.fixtures.fake_connection import FakeConnection


# PCM (0x7E0): P0300 — confirmed + pending + MIL (status=0x8C)
_PCM_DTC_PAYLOAD = bytes([0x03, 0x00, 0x00, 0x8C])

# ABS (0x726): C0035 — testFailed + confirmed (status=0x09)
# C0035 encoding: system=C → bits7-6=01; first digit=0; tail=035
# byte_a = (0b01 << 6) | (0 << 4) | 0x0 = 0x40, byte_b = 0x35
_ABS_DTC_PAYLOAD = bytes([0x40, 0x35, 0x00, 0x09])


class FakeMultiEcuConnection(FakeConnection):
    """Ford multi-ECU fixture: PCM (0x7E0) via Tier A + ABS (0x726) via Tier B.

    PCM responds to the J1979 functional broadcast as response CAN ID 0x7E8.
    PCM (0x7E0) and ABS (0x726) respond to UDS TesterPresent probes.
    All other Ford table addresses are silent.
    """

    def __init__(self) -> None:
        super().__init__(
            vin="WF0EXXGCD7YH12345",        # Ford Transit VIN (WF0 = Ford Genk)
            functional_responders=[0x7E8],  # PCM response CAN ID → request addr 0x7E0
            present_ecus={0x7E0, 0x726},    # PCM + ABS respond to TesterPresent
        )

    async def query_uds_dtc_at_addr(
        self,
        ecu_addr: int,
        sub_fn: int,
        params: bytes = b"",
    ) -> UdsResponse:
        await asyncio.sleep(0.01)
        if ecu_addr == 0x7E0 and sub_fn in (0x02, 0x0A):
            return UdsResponse(UdsResponseStatus.OK, data=_PCM_DTC_PAYLOAD)
        if ecu_addr == 0x726 and sub_fn in (0x02, 0x0A):
            return UdsResponse(UdsResponseStatus.OK, data=_ABS_DTC_PAYLOAD)
        return UdsResponse(UdsResponseStatus.NO_RESPONSE)


# ── ECU discovery tests ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_discovery_finds_pcm_and_abs() -> None:
    """discover_ecus finds both PCM (0x7E0) and ABS (0x726)."""
    conn = FakeMultiEcuConnection()
    await conn.connect()

    result = await discover_ecus(conn, make="Ford")

    assert 0x7E0 in result.found, "PCM (0x7E0) not found"
    assert 0x726 in result.found, "ABS (0x726) not found"


@pytest.mark.asyncio
async def test_discovery_pcm_tagged_tier_a() -> None:
    """PCM is tagged Tier A — it responded to the J1979 functional broadcast."""
    conn = FakeMultiEcuConnection()
    await conn.connect()

    result = await discover_ecus(conn, make="Ford")

    assert result.found[0x7E0].tier == DiscoveryTier.A


@pytest.mark.asyncio
async def test_discovery_abs_tagged_tier_b() -> None:
    """ABS is tagged Tier B — not in functional broadcast, found via Ford address table."""
    conn = FakeMultiEcuConnection()
    await conn.connect()

    result = await discover_ecus(conn, make="Ford")

    assert result.found[0x726].tier == DiscoveryTier.B


@pytest.mark.asyncio
async def test_discovery_tier_b_entries_include_non_responding() -> None:
    """All Ford table entries appear in tier_b_entries, including non-responding ones."""
    from Hudson.core.ecu_tables import ECU_TABLES

    conn = FakeMultiEcuConnection()
    await conn.connect()

    result = await discover_ecus(conn, make="Ford")

    assert result.tier_b_entries is not None
    assert len(result.tier_b_entries) == len(ECU_TABLES["ford"])

    responding_addrs = {e.address for e in result.tier_b_entries if e.responded}
    silent_addrs = {e.address for e in result.tier_b_entries if not e.responded}

    # ABS (0x726) must appear as responding in Tier B
    assert 0x726 in responding_addrs
    # Other addresses like BCM, IPC, HVAC are silent
    assert 0x733 in silent_addrs   # BCM
    assert 0x737 in silent_addrs   # IPC


@pytest.mark.asyncio
async def test_discovery_tier_b_abs_label_correct() -> None:
    """ABS entry from the Ford table carries its label through DiscoveredEcu."""
    conn = FakeMultiEcuConnection()
    await conn.connect()

    result = await discover_ecus(conn, make="Ford")

    abs_ecu = result.found.get(0x726)
    assert abs_ecu is not None
    assert abs_ecu.label == "ABS"


# ── Per-ECU DTC scan tests ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_dtc_scan_both_ecus_respond() -> None:
    """DTC scan returns results for both PCM and ABS when addresses are explicitly listed."""
    conn = FakeMultiEcuConnection()
    await conn.connect()

    results = await scan_ecus_for_dtcs(conn, addresses=[0x7E0, 0x726])

    assert 0x7E0 in results, "PCM missing from DTC scan results"
    assert 0x726 in results, "ABS missing from DTC scan results"


@pytest.mark.asyncio
async def test_dtc_scan_pcm_p0300_with_correct_status() -> None:
    """PCM (0x7E0) reports P0300 with confirmed + pending + MIL status."""
    conn = FakeMultiEcuConnection()
    await conn.connect()

    results = await scan_ecus_for_dtcs(conn, addresses=[0x7E0])

    assert 0x7E0 in results
    records = {r.dtc.code: r for r in results[0x7E0]}
    assert "P0300" in records

    p0300 = records["P0300"]
    assert p0300.status.confirmed
    assert p0300.status.pending
    assert p0300.status.mil_on
    assert not p0300.status.test_failed


@pytest.mark.asyncio
async def test_dtc_scan_abs_c0035_with_correct_status() -> None:
    """ABS (0x726) reports C0035 with testFailed + confirmed status."""
    conn = FakeMultiEcuConnection()
    await conn.connect()

    results = await scan_ecus_for_dtcs(conn, addresses=[0x726])

    assert 0x726 in results
    records = {r.dtc.code: r for r in results[0x726]}
    assert "C0035" in records

    c0035 = records["C0035"]
    assert c0035.status.test_failed
    assert c0035.status.confirmed
    assert not c0035.status.mil_on
    assert not c0035.status.pending


@pytest.mark.asyncio
async def test_dtc_scan_silent_ecus_excluded() -> None:
    """ECUs that do not respond are absent from the DTC scan results."""
    conn = FakeMultiEcuConnection()
    await conn.connect()

    results = await scan_ecus_for_dtcs(conn, addresses=[0x7E0, 0x726, 0x733, 0x737])

    assert 0x733 not in results, "BCM (silent) unexpectedly present in results"
    assert 0x737 not in results, "IPC (silent) unexpectedly present in results"


# ── Full discover-then-scan flow ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_discover_then_scan_full_flow() -> None:
    """Integration: discover ECUs first, then scan DTCs using discovered addresses."""
    conn = FakeMultiEcuConnection()
    await conn.connect()

    # Step 1: discover ECUs
    discovery = await discover_ecus(conn, make="Ford")
    assert len(discovery.found) >= 2

    # Step 2: scan DTCs for all discovered addresses
    addresses = sorted(discovery.found.keys())
    dtc_results = await scan_ecus_for_dtcs(conn, addresses=addresses)

    # Both responding ECUs should have DTCs
    assert 0x7E0 in dtc_results
    assert 0x726 in dtc_results

    pcm_codes = {r.dtc.code for r in dtc_results[0x7E0]}
    abs_codes = {r.dtc.code for r in dtc_results[0x726]}
    assert "P0300" in pcm_codes
    assert "C0035" in abs_codes

    # Silent Ford table entries must not appear
    for addr in (0x733, 0x737, 0x740, 0x741, 0x745, 0x764, 0x7D9):
        assert addr not in dtc_results, f"Silent ECU 0x{addr:03X} in DTC results"


@pytest.mark.asyncio
async def test_discover_then_scan_no_cross_contamination() -> None:
    """Each DTC is attributed only to the ECU that reported it."""
    conn = FakeMultiEcuConnection()
    await conn.connect()

    discovery = await discover_ecus(conn, make="Ford")
    addresses = sorted(discovery.found.keys())
    dtc_results = await scan_ecus_for_dtcs(conn, addresses=addresses)

    if 0x7E0 in dtc_results:
        pcm_codes = {r.dtc.code for r in dtc_results[0x7E0]}
        assert "C0035" not in pcm_codes, "C0035 (ABS fault) misattributed to PCM"

    if 0x726 in dtc_results:
        abs_codes = {r.dtc.code for r in dtc_results[0x726]}
        assert "P0300" not in abs_codes, "P0300 (engine fault) misattributed to ABS"


@pytest.mark.asyncio
async def test_discover_then_scan_tier_tags_match_expected() -> None:
    """After discover-then-scan, Tier tags on found ECUs are unchanged and correct."""
    conn = FakeMultiEcuConnection()
    await conn.connect()

    discovery = await discover_ecus(conn, make="Ford")

    # DTC scan must not alter the discovery result's tier tags.
    _ = await scan_ecus_for_dtcs(conn, addresses=sorted(discovery.found.keys()))

    assert discovery.found[0x7E0].tier == DiscoveryTier.A
    assert discovery.found[0x726].tier == DiscoveryTier.B
