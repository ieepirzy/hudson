"""Tests for UDS service 0x19 multi-ECU DTC scanner and tiered ECU discovery."""

from __future__ import annotations

import pytest

from Hudson.core.connection import MODE22_TIMEOUT_S, UdsResponse, UdsResponseStatus
from Hudson.core.uds_dtc import (
    DiscoveryTier,
    discover_ecus,
    discover_ecus_brute_force,
    discover_ecus_functional,
    discover_ecus_known_table,
    scan_ecus_for_dtcs,
)
from tests.fixtures.fake_connection import FakeConnection, FakeKlineConnection


# ── Test fixtures for FIX #5 and MITIGATE #1 ─────────────────────────────────

class FakeNrConnection(FakeConnection):
    """FakeConnection that returns NEGATIVE_RESPONSE (NRC 0x31) for all 0x19 queries."""

    async def query_uds_dtc_at_addr(
        self, ecu_addr: int, sub_fn: int, params: bytes = b""
    ) -> UdsResponse:
        import asyncio
        await asyncio.sleep(0.01)
        return UdsResponse(UdsResponseStatus.NEGATIVE_RESPONSE, nrc=0x31)


class FakeTruncatedMfConnection(FakeConnection):
    """FakeConnection that returns LIKELY_TRUNCATED_MULTIFRAME for ECU 0x7E0.

    Payload has 1 complete 4-byte record (P0300 / 0x8C) plus 2 trailing bytes —
    simulating an ELM327 clone that failed to send an ISO-TP Flow Control frame.
    """

    async def query_uds_dtc_at_addr(
        self, ecu_addr: int, sub_fn: int, params: bytes = b""
    ) -> UdsResponse:
        import asyncio
        await asyncio.sleep(0.01)
        if ecu_addr == 0x7E0 and sub_fn in (0x02, 0x0A):
            return UdsResponse(
                UdsResponseStatus.LIKELY_TRUNCATED_MULTIFRAME,
                data=bytes([0x03, 0x00, 0x00, 0x8C, 0x01, 0x71]),
            )
        return UdsResponse(UdsResponseStatus.NO_RESPONSE)


@pytest.mark.asyncio
async def test_scan_returns_dtcs_for_responding_ecu() -> None:
    conn = FakeConnection()
    await conn.connect()

    results = await scan_ecus_for_dtcs(conn, addresses=[0x7E0])

    assert 0x7E0 in results
    codes = [r.dtc.code for r in results[0x7E0]]
    assert "P0300" in codes
    assert "P0171" in codes


@pytest.mark.asyncio
async def test_scan_skips_non_responding_ecus() -> None:
    conn = FakeConnection()
    await conn.connect()

    # Only 0x7E0 is wired in FakeConnection; 0x7E1–0x7E7 return None
    results = await scan_ecus_for_dtcs(conn, addresses=[0x7E0, 0x7E1, 0x7E2])

    assert 0x7E0 in results
    assert 0x7E1 not in results
    assert 0x7E2 not in results


@pytest.mark.asyncio
async def test_scan_empty_when_no_ecus_respond() -> None:
    conn = FakeConnection()
    await conn.connect()

    results = await scan_ecus_for_dtcs(conn, addresses=[0x7E1, 0x7E2])

    assert results == {}


@pytest.mark.asyncio
async def test_scan_restores_at_state() -> None:
    """ATAT 0 must be followed by ATAT 1 and ATSH 7E0 regardless of errors."""
    conn = FakeConnection()
    await conn.connect()

    await scan_ecus_for_dtcs(conn, addresses=[0x7E0])

    history = conn._send_at_history
    assert "ATAT 0" in history
    assert "ATAT 1" in history
    assert "ATSH 7E0" in history
    # ATAT 1 and ATSH 7E0 must come after ATAT 0
    idx_atat0 = next(i for i, c in enumerate(history) if c == "ATAT 0")
    idx_atat1 = next(i for i, c in enumerate(history) if c == "ATAT 1")
    idx_atsh  = next(i for i, c in enumerate(history) if c == "ATSH 7E0")
    assert idx_atat0 < idx_atat1
    assert idx_atat0 < idx_atsh


@pytest.mark.asyncio
async def test_scan_status_bits_decoded_correctly() -> None:
    conn = FakeConnection()
    await conn.connect()

    results = await scan_ecus_for_dtcs(conn, addresses=[0x7E0])
    records = {r.dtc.code: r for r in results[0x7E0]}

    # P0300 has status=0x8C: confirmed + pending + MIL, not testFailed
    p0300 = records["P0300"]
    assert p0300.status.confirmed
    assert p0300.status.pending
    assert p0300.status.mil_on
    assert not p0300.status.test_failed

    # P0171 has status=0x09: testFailed + confirmed
    p0171 = records["P0171"]
    assert p0171.status.test_failed
    assert p0171.status.confirmed
    assert not p0171.status.mil_on


@pytest.mark.asyncio
async def test_scan_subfn_0a_also_works() -> None:
    """sub-function 0x0A (reportSupportedDTC) uses same mock payload."""
    conn = FakeConnection()
    await conn.connect()

    results = await scan_ecus_for_dtcs(conn, sub_fn=0x0A, addresses=[0x7E0])

    assert 0x7E0 in results
    assert len(results[0x7E0]) == 2


@pytest.mark.asyncio
async def test_scan_raises_on_non_can_protocol() -> None:
    """Scanning must be rejected when the active protocol is not CAN.

    Transmitting ISO-TP frames at the wrong bus speed (e.g. 500 Kbps on a
    250 Kbps bus) can cause CAN bus errors on the vehicle network.  The guard
    must raise, not merely log a warning.
    """
    conn = FakeKlineConnection()
    await conn.connect()

    with pytest.raises(RuntimeError, match="CAN protocol"):
        await scan_ecus_for_dtcs(conn, addresses=[0x7E0])


# ── Tier A: discover_ecus_functional ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_functional_empty_when_no_responders() -> None:
    """0 functional-broadcast responders → empty list, not an error."""
    conn = FakeConnection(functional_responders=[])
    await conn.connect()

    result = await discover_ecus_functional(conn)

    assert result == []


@pytest.mark.asyncio
async def test_functional_returns_all_responding_ids() -> None:
    """Response CAN IDs are converted to physical request addresses (response − 8)."""
    conn = FakeConnection(functional_responders=[0x7E8, 0x7E9])
    await conn.connect()

    result = await discover_ecus_functional(conn)

    # 0x7E8 → 0x7E0 (ECM request addr), 0x7E9 → 0x7E1 (TCM request addr)
    assert sorted(result) == [0x7E0, 0x7E1]


@pytest.mark.asyncio
async def test_functional_raises_on_non_can() -> None:
    """Tier A must reject K-line protocol with RuntimeError."""
    conn = FakeKlineConnection()
    await conn.connect()

    with pytest.raises(RuntimeError, match="CAN protocol"):
        await discover_ecus_functional(conn)


@pytest.mark.asyncio
async def test_functional_restores_at_state() -> None:
    """ATAT 0 → ATAT 1 + ATSH 7E0 must be issued regardless of responder count."""
    conn = FakeConnection(functional_responders=[0x7E8])
    await conn.connect()

    await discover_ecus_functional(conn)

    history = conn._send_at_history
    assert "ATAT 0" in history
    assert "ATAT 1" in history
    assert "ATSH 7E0" in history


# ── Tier B: discover_ecus_known_table ────────────────────────────────────────

@pytest.mark.asyncio
async def test_known_table_no_table_returns_none() -> None:
    """No table for the vehicle's make → None, not an error, not an empty list."""
    conn = FakeConnection()
    await conn.connect()

    result = await discover_ecus_known_table(conn, make="generic")

    assert result is None


@pytest.mark.asyncio
async def test_known_table_ford_all_respond() -> None:
    """Ford table present; all probed addresses respond → all marked responded=True."""
    # Make every Ford table address respond to TesterPresent.
    from Hudson.core.ecu_tables import ECU_TABLES
    all_ford_addrs = {e.address for e in ECU_TABLES["ford"]}
    conn = FakeConnection(present_ecus=all_ford_addrs)
    await conn.connect()

    result = await discover_ecus_known_table(conn, make="Ford")

    assert result is not None
    assert all(e.responded for e in result)
    assert {e.address for e in result} == all_ford_addrs


@pytest.mark.asyncio
async def test_known_table_some_no_response_reported_not_dropped() -> None:
    """Non-responding Tier B entries appear with responded=False, not silently dropped."""
    # Only PCM (0x7E0) responds; all others are silent.
    conn = FakeConnection(present_ecus={0x7E0})
    await conn.connect()

    result = await discover_ecus_known_table(conn, make="Ford")

    assert result is not None
    from Hudson.core.ecu_tables import ECU_TABLES
    expected_count = len(ECU_TABLES["ford"])
    # Every table entry is present in the result.
    assert len(result) == expected_count

    responding = [e for e in result if e.responded]
    silent = [e for e in result if not e.responded]
    assert len(responding) == 1
    assert responding[0].address == 0x7E0
    assert len(silent) == expected_count - 1


@pytest.mark.asyncio
async def test_known_table_tier_tag_is_b() -> None:
    """All Tier B entries are tagged DiscoveryTier.B."""
    conn = FakeConnection(present_ecus={0x7E0})
    await conn.connect()

    result = await discover_ecus_known_table(conn, make="Ford")

    assert result is not None
    assert all(e.tier == DiscoveryTier.B for e in result)


@pytest.mark.asyncio
async def test_known_table_label_propagated() -> None:
    """Labels from the ECU table are carried through to DiscoveredEcu."""
    conn = FakeConnection(present_ecus={0x7E0})
    await conn.connect()

    result = await discover_ecus_known_table(conn, make="Ford")

    assert result is not None
    pcm = next((e for e in result if e.address == 0x7E0), None)
    assert pcm is not None
    assert pcm.label == "PCM"


@pytest.mark.asyncio
async def test_known_table_raises_on_non_can() -> None:
    """Tier B must reject K-line protocol with RuntimeError."""
    conn = FakeKlineConnection()
    await conn.connect()

    with pytest.raises(RuntimeError, match="CAN protocol"):
        await discover_ecus_known_table(conn, make="Ford")


# ── Tier C: discover_ecus_brute_force ────────────────────────────────────────

@pytest.mark.asyncio
async def test_brute_force_finds_only_responding_addresses() -> None:
    """Brute-force over a small range finds only the addresses that respond."""
    responding = {0x710, 0x720}
    conn = FakeConnection(present_ecus=responding)
    await conn.connect()

    result = await discover_ecus_brute_force(conn, candidate_range=range(0x710, 0x730))

    assert set(result) == responding


@pytest.mark.asyncio
async def test_brute_force_empty_when_none_respond() -> None:
    conn = FakeConnection(present_ecus=set())
    await conn.connect()

    result = await discover_ecus_brute_force(conn, candidate_range=range(0x710, 0x720))

    assert result == []


@pytest.mark.asyncio
async def test_brute_force_skips_functional_address() -> None:
    """0x7DF must not be probed — it is a functional broadcast ID, not a physical ECU."""
    probed: list[int] = []
    original = FakeConnection.probe_ecu_tester_present

    async def _recording(self, addr: int) -> bool:
        probed.append(addr)
        return await original(self, addr)

    conn = FakeConnection(present_ecus=set())
    await conn.connect()

    # Range that straddles 0x7DF
    FakeConnection.probe_ecu_tester_present = _recording  # type: ignore[method-assign]
    try:
        await discover_ecus_brute_force(conn, candidate_range=range(0x7DE, 0x7E1))
    finally:
        FakeConnection.probe_ecu_tester_present = original  # type: ignore[method-assign]

    assert 0x7DF not in probed


@pytest.mark.asyncio
async def test_brute_force_raises_on_non_can() -> None:
    """Tier C must reject K-line protocol with RuntimeError."""
    conn = FakeKlineConnection()
    await conn.connect()

    with pytest.raises(RuntimeError, match="CAN protocol"):
        await discover_ecus_brute_force(conn, candidate_range=range(0x700, 0x710))


# ── Orchestrator: discover_ecus ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_orchestrator_uses_tier_b_when_table_exists() -> None:
    """When a Tier B table exists, Tier C must not run."""
    probed_by_tester_present: list[int] = []
    original = FakeConnection.probe_ecu_tester_present

    async def _recording(self, addr: int) -> bool:
        probed_by_tester_present.append(addr)
        return await original(self, addr)

    conn = FakeConnection(present_ecus={0x7E0})
    await conn.connect()
    FakeConnection.probe_ecu_tester_present = _recording  # type: ignore[method-assign]
    try:
        result = await discover_ecus(conn, make="Ford")
    finally:
        FakeConnection.probe_ecu_tester_present = original  # type: ignore[method-assign]

    # Tier B table has 10 addresses; Tier C would probe 0x700–0x7FF.
    # If Tier C ran, probed_by_tester_present would be >> 10.
    from Hudson.core.ecu_tables import ECU_TABLES
    assert len(probed_by_tester_present) == len(ECU_TABLES["ford"])
    assert result.tier_b_entries is not None


@pytest.mark.asyncio
async def test_orchestrator_falls_to_tier_c_when_no_table() -> None:
    """When no Tier B table exists, Tier C path is selected (tier_b_entries is None).

    FakeConnection.is_mock=True so actual brute-force probing is skipped here
    (avoid 256 × asyncio.sleep per test).  The Tier C probe logic itself is
    exercised directly in test_brute_force_finds_only_responding_addresses.
    """
    conn = FakeConnection(present_ecus={0x712})
    await conn.connect()

    result = await discover_ecus(conn, make="Generic")

    assert result.tier_b_entries is None


@pytest.mark.asyncio
async def test_orchestrator_tier_a_always_runs() -> None:
    """Tier A functional broadcast result is always included regardless of tier B/C."""
    conn = FakeConnection(
        functional_responders=[0x7E8],
        present_ecus={0x7E0},
    )
    await conn.connect()

    result = await discover_ecus(conn, make="Ford")

    # 0x7E8 (response ID) is converted to 0x7E0 (ECM request address).
    assert 0x7E0 in result.found
    assert result.found[0x7E0].tier == DiscoveryTier.A


@pytest.mark.asyncio
async def test_orchestrator_tier_tags_correctly() -> None:
    """Each found address is tagged with the tier that first discovered it."""
    # 0x7E9 → converted to TCM request address 0x7E1 (Tier A)
    # 0x7E0: Tier B hit (in Ford table + responds to TesterPresent)
    conn = FakeConnection(
        functional_responders=[0x7E9],
        present_ecus={0x7E0},
    )
    await conn.connect()

    result = await discover_ecus(conn, make="Ford")

    assert result.found[0x7E1].tier == DiscoveryTier.A
    assert result.found[0x7E0].tier == DiscoveryTier.B


@pytest.mark.asyncio
async def test_orchestrator_tier_a_hit_not_overwritten_by_tier_b() -> None:
    """If Tier A already found an address, Tier B must not overwrite its tier tag."""
    # ECM (0x7E0) responds to functional broadcast as 0x7E8 (response CAN ID).
    # After conversion, 0x7E0 is in found before Tier B probes it.
    conn = FakeConnection(
        functional_responders=[0x7E8],
        present_ecus={0x7E0},
    )
    await conn.connect()

    result = await discover_ecus(conn, make="Ford")

    # Tier A found ECM first (0x7E8 → 0x7E0) — Tier B must not overwrite its tag.
    assert result.found[0x7E0].tier == DiscoveryTier.A


@pytest.mark.asyncio
async def test_orchestrator_raises_on_non_can() -> None:
    """Top-level discover_ecus must reject K-line with RuntimeError."""
    conn = FakeKlineConnection()
    await conn.connect()

    with pytest.raises(RuntimeError, match="CAN protocol"):
        await discover_ecus(conn, make="Ford")


# ── FIX #5: UdsResponse — negative response and truncation handling ───────────

@pytest.mark.asyncio
async def test_scan_handles_negative_response() -> None:
    """NRC 0x31 from an ECU is logged and skipped — address absent from results."""
    conn = FakeNrConnection()
    await conn.connect()

    results = await scan_ecus_for_dtcs(conn, addresses=[0x7E0, 0x7E1])

    assert results == {}


@pytest.mark.asyncio
async def test_scan_decodes_partial_records_on_likely_truncated() -> None:
    """LIKELY_TRUNCATED_MULTIFRAME: complete 4-byte records are decoded; address is included."""
    conn = FakeTruncatedMfConnection()
    await conn.connect()

    results = await scan_ecus_for_dtcs(conn, addresses=[0x7E0])

    assert 0x7E0 in results
    # 6-byte payload → 1 complete record (P0300); trailing 2 bytes ignored
    codes = [r.dtc.code for r in results[0x7E0]]
    assert codes == ["P0300"]
    p0300 = results[0x7E0][0]
    assert p0300.status.confirmed
    assert p0300.status.mil_on


# ── FIX #4: ATST set/restore via query_uds_at_addr ───────────────────────────

@pytest.mark.asyncio
async def test_query_uds_at_addr_sets_and_restores_atst() -> None:
    """When timeout differs from config default, ATST is set before and restored after."""
    conn = FakeConnection()
    await conn.connect()
    conn._send_at_history.clear()

    await conn.query_uds_at_addr(0x7E0, 0xF189, timeout=MODE22_TIMEOUT_S)

    history = conn._send_at_history
    # MODE22_TIMEOUT_S=0.25 → ATST 3F (63 units × 4ms = 252ms)
    # config default=0.1   → ATST 19 (25 units × 4ms = 100ms)
    assert "ATST 3F" in history, f"ATST 3F not found in {history}"
    assert "ATST 19" in history, f"ATST 19 not found in {history}"
    idx_set = next(i for i, c in enumerate(history) if c == "ATST 3F")
    idx_restore = next(i for i, c in enumerate(history) if c == "ATST 19")
    assert idx_set < idx_restore


@pytest.mark.asyncio
async def test_query_uds_at_addr_no_atst_when_timeout_matches_config() -> None:
    """When timeout equals config default (0.1s), no ATST commands are issued."""
    conn = FakeConnection()
    await conn.connect()
    conn._send_at_history.clear()

    await conn.query_uds_at_addr(0x7E0, 0xF189, timeout=0.1)

    atst_cmds = [c for c in conn._send_at_history if c.startswith("ATST")]
    assert atst_cmds == [], f"unexpected ATST commands: {atst_cmds}"
