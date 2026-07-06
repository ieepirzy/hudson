"""UDS service 0x19 (ReadDTCInformation) multi-ECU scanner.

Sweeps a set of standard CAN ECU addresses, issuing service 0x19 sub-function
0x02 (reportDTCByStatusMask) or 0x0A (reportSupportedDTC) to each one.

Transport notes:
  - ATSH + the UDS query are atomic (one lock acquisition) inside
    query_uds_dtc_at_addr — no TOCTOU risk across concurrent callers.
  - ATAT 0 disables adaptive timing before the scan so all ECUs are polled
    with a fixed ELM327 timeout.  ATAT is restored to 1 in the finally block.
  - ATST is managed per-query by query_uds_at_addr (set to MODE22_TIMEOUT_S
    when timeout differs from config, restored in finally).
  - Per-ECU errors are caught and logged individually so a non-responding ECU
    does not abort the sweep of remaining addresses.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

from Hudson.core.connection import UdsResponse, UdsResponseStatus
from Hudson.core.dtc import DtcRecord, decode_uds_dtc_list
from Hudson.core.ecu_tables import lookup_table

if TYPE_CHECKING:
    from Hudson.core.connection import ObdConnection
    from Hudson.core.ecu_cache import EcuCache

log = logging.getLogger(__name__)

# Standard J1979 CAN ECU addresses (ECM through TCM + gateway)
STANDARD_ECU_ADDRESSES: list[int] = [
    0x7E0,  # ECM / PCM
    0x7E1,  # TCM
    0x7E2,  # ABS / ESC
    0x7E3,  # Body control
    0x7E4,  # Supplemental restraints
    0x7E5,  # Instrument cluster
    0x7E6,  # HVAC
    0x7E7,  # Auxiliary
    0x7D9,  # OBD gateway (J1979-2 extended)
]


async def scan_ecus_for_dtcs(
    connection: ObdConnection,
    *,
    sub_fn: int = 0x02,
    addresses: list[int] | None = None,
) -> dict[int, list[DtcRecord]]:
    """Query each ECU address for DTCs via UDS service 0x19.

    ``sub_fn=0x02`` (reportDTCByStatusMask) requests all DTCs regardless of
    status.  Pass ``sub_fn=0x0A`` (reportSupportedDTC) to enumerate
    factory-supported codes instead.

    Returns a dict mapping ECU address → list of DtcRecord.  Addresses that do
    not respond are absent from the result.

    **CAN bus speed prerequisite** — the caller is responsible for selecting the
    correct CAN protocol (via ``ConnectionConfig.protocol`` or a prior ``ATSP``
    command) *before* calling this function.  Transmitting ISO-TP frames at the
    wrong bit rate (e.g. 500 Kbps on a 250 Kbps bus) causes CAN bus errors and
    may put ECUs into bus-off state.  This function raises ``RuntimeError`` if
    the active ELM327 protocol is not a CAN variant.
    """
    if not connection.is_can_protocol:
        raise RuntimeError(
            f"UDS 0x19 scan requires a CAN protocol, but the active protocol is "
            f"{connection.protocol_name!r}. Set ConnectionConfig.protocol to the "
            f"correct CAN variant for this vehicle before scanning "
            f"(wrong CAN bus speed can cause bus errors on the vehicle network)."
        )

    if addresses is None:
        addresses = STANDARD_ECU_ADDRESSES

    # 0x02 requires a DTCStatusMask parameter; 0x0A takes no parameters.
    params = bytes([0xFF]) if sub_fn == 0x02 else b""

    results: dict[int, list[DtcRecord]] = {}

    # Disable adaptive timing for the sweep so we poll each ECU at a fixed
    # rate.  ATST is left untouched (see module docstring).
    try:
        await connection.send_at("ATAT 0")
    except Exception as exc:
        log.warning("ATAT 0 failed: %s — continuing without disabling adaptive timing", exc)

    try:
        for addr in addresses:
            try:
                response = await connection.query_uds_dtc_at_addr(addr, sub_fn, params)
                if response.status == UdsResponseStatus.NO_RESPONSE:
                    log.debug("UDS 0x19 sub_fn=0x%02X @ 0x%03X: no response", sub_fn, addr)
                    continue
                if response.status == UdsResponseStatus.NEGATIVE_RESPONSE:
                    log.debug(
                        "UDS 0x19 sub_fn=0x%02X @ 0x%03X: negative response (NRC 0x%02X)",
                        sub_fn, addr, response.nrc,
                    )
                    continue
                # OK or LIKELY_TRUNCATED_MULTIFRAME — decode whatever complete records arrived
                records = decode_uds_dtc_list(response.data)
                if records:
                    suffix = " (response may be truncated)" if response.status == UdsResponseStatus.LIKELY_TRUNCATED_MULTIFRAME else ""
                    log.info(
                        "UDS 0x19 sub_fn=0x%02X @ 0x%03X: %d DTC(s)%s",
                        sub_fn, addr, len(records), suffix,
                    )
                    results[addr] = records
                else:
                    log.debug("UDS 0x19 sub_fn=0x%02X @ 0x%03X: responded, 0 DTCs", sub_fn, addr)
            except Exception as exc:
                log.warning(
                    "UDS 0x19 sub_fn=0x%02X @ 0x%03X: query error: %s",
                    sub_fn, addr, exc,
                )
    finally:
        # Restore adaptive timing and default ECM header regardless of errors.
        # ATST intentionally not modified — scan uses connection's configured timeout.
        try:
            await connection.send_at("ATAT 1")
        except Exception as exc:
            log.warning("ATAT 1 restore failed: %s", exc)
        try:
            await connection.send_at("ATSH 7E0")
        except Exception as exc:
            log.warning("ATSH 7E0 restore failed: %s", exc)

    return results


# ── Tiered ECU discovery ──────────────────────────────────────────────────────

# Tier C brute-force candidate range for 11-bit CAN physical addresses.
# 0x7DF is the J1979 functional broadcast address — not a physical ECU ID —
# and is excluded from probing.  The 0x700–0x7FF convention is not universally
# followed (some vehicles use lower ranges; J1939 uses 29-bit IDs entirely),
# but it covers the overwhelming majority of standard OBD2 CAN networks.
BRUTE_FORCE_CANDIDATE_RANGE: range = range(0x700, 0x800)
_BRUTE_FORCE_SKIP: frozenset[int] = frozenset({0x7DF})


class DiscoveryTier(Enum):
    """Which discovery tier first found a given ECU address."""

    A = "A"  # J1979 functional broadcast — legally mandated emissions-relevant ECUs
    B = "B"  # Manufacturer known-address table — vendor-specific, more comprehensive
    C = "C"  # Brute-force probe — universal fallback, intentionally slow


@dataclass(frozen=True, slots=True)
class DiscoveredEcu:
    """One ECU entry from the tiered discovery process."""

    address: int
    tier: DiscoveryTier
    label: str = ""
    responded: bool = True  # False for Tier B table entries that did not respond


@dataclass(slots=True)
class EcuDiscoveryResult:
    """Merged output of the tiered ECU discovery orchestrator.

    ``found`` contains every ECU that responded, keyed by address, tagged with
    the tier that first found it.

    ``tier_b_entries`` is ``None`` when no Tier B table exists for the vehicle
    (the caller can infer that Tier C was used instead).  When a table does
    exist, this holds all table entries — both responding and non-responding —
    so "in table but no response" is visible rather than silently dropped.
    """

    found: dict[int, DiscoveredEcu]
    tier_b_entries: list[DiscoveredEcu] | None  # None = no table; list includes non-responders


async def discover_ecus_functional(connection: ObdConnection) -> list[int]:
    """Tier A — J1979 Mode 01 functional broadcast to detect emissions-relevant ECUs.

    Sends Mode 01 PID 0x00 (supported-PID bitmask) to the standard J1979
    functional CAN ID 0x7DF and returns the physical CAN IDs that answer.

    By design this tier finds only OBD2-mandated ECUs — typically the ECM
    (engine control) and sometimes the TCM.  Body, comfort, and infotainment
    modules have no legal obligation to respond to functional broadcasts and
    most will not.  A Tier A hit is a legal guarantee that the ECU is
    present and OBD2-compliant; absence from Tier A does not mean the ECU is
    absent from the network.

    Response-window duration is determined by the ELM327's ATST setting at
    call time (ConnectionConfig.timeout, default 100 ms).  This is a judgment
    call — long enough for well-behaved OBD2 ECUs over ISO-TP, short enough to
    not add perceptible latency when used as Tier A of a multi-tier scan.
    Tune ConnectionConfig.timeout if needed for slow CAN buses.

    Raises ``RuntimeError`` if the active protocol is not CAN — see
    ``ObdConnection.is_can_protocol``.  Transmitting at the wrong CAN bus speed
    can cause bus errors on the vehicle network.
    """
    if not connection.is_can_protocol:
        raise RuntimeError(
            f"Tier A ECU discovery requires a CAN protocol, but the active protocol is "
            f"{connection.protocol_name!r}.  Set ConnectionConfig.protocol to the correct "
            f"CAN variant for this vehicle before scanning."
        )

    try:
        await connection.send_at("ATAT 0")
    except Exception as exc:
        log.warning("Tier A: ATAT 0 failed: %s — continuing", exc)

    try:
        raw_ids = await connection.query_functional_mode01(pid=0x00)
        # raw_ids are response CAN IDs (e.g. 0x7E8 from ECM at 0x7E0).
        # Convert to physical request addresses so ATSH commands work correctly.
        ids = [a - 8 if 0x7E8 <= a <= 0x7EF else a for a in raw_ids]
        log.info(
            "Tier A: functional broadcast → %d responder(s): %s (raw: %s)",
            len(ids),
            [f"0x{a:03X}" for a in ids],
            [f"0x{a:03X}" for a in raw_ids],
        )
        return ids
    except Exception as exc:
        log.warning("Tier A: functional broadcast failed: %s", exc)
        return []
    finally:
        try:
            await connection.send_at("ATAT 1")
        except Exception as exc:
            log.warning("Tier A: ATAT 1 restore failed: %s", exc)
        try:
            await connection.send_at("ATSH 7E0")
        except Exception as exc:
            log.warning("Tier A: ATSH 7E0 restore failed: %s", exc)


async def discover_ecus_known_table(
    connection: ObdConnection,
    make: str,
) -> list[DiscoveredEcu] | None:
    """Tier B — probe each address from the manufacturer's known ECU table.

    Looks up the address table for *make* (case-insensitive, e.g. ``"Ford"``).
    If no table exists, returns ``None`` — the explicit signal that the caller
    should fall to Tier C.  An empty list means the table exists but nothing
    responded.

    All table entries are included in the return value regardless of whether
    they responded, so the caller can distinguish "in table, responded" from
    "in table, no response" — neither is silently dropped.

    Raises ``RuntimeError`` if the active protocol is not CAN.
    """
    if not connection.is_can_protocol:
        raise RuntimeError(
            f"Tier B ECU discovery requires a CAN protocol, but the active protocol is "
            f"{connection.protocol_name!r}."
        )

    entries = lookup_table(make)
    if entries is None:
        log.debug("Tier B: no address table for make %r — returning None", make)
        return None

    log.info("Tier B: probing %d known address(es) for make %r", len(entries), make)

    try:
        await connection.send_at("ATAT 0")
    except Exception as exc:
        log.warning("Tier B: ATAT 0 failed: %s — continuing", exc)

    results: list[DiscoveredEcu] = []

    try:
        for entry in entries:
            try:
                present = await connection.probe_ecu_tester_present(entry.address)
                results.append(
                    DiscoveredEcu(
                        address=entry.address,
                        tier=DiscoveryTier.B,
                        label=entry.label,
                        responded=present,
                    )
                )
                if present:
                    log.info("Tier B: 0x%03X (%s) responded", entry.address, entry.label)
                else:
                    log.debug("Tier B: 0x%03X (%s) — no response", entry.address, entry.label)
            except Exception as exc:
                log.warning("Tier B: probe 0x%03X (%s): %s", entry.address, entry.label, exc)
                results.append(
                    DiscoveredEcu(
                        address=entry.address,
                        tier=DiscoveryTier.B,
                        label=entry.label,
                        responded=False,
                    )
                )
    finally:
        try:
            await connection.send_at("ATAT 1")
        except Exception as exc:
            log.warning("Tier B: ATAT 1 restore failed: %s", exc)
        try:
            await connection.send_at("ATSH 7E0")
        except Exception as exc:
            log.warning("Tier B: ATSH 7E0 restore failed: %s", exc)

    return results


async def discover_ecus_brute_force(
    connection: ObdConnection,
    *,
    candidate_range: range | None = None,
) -> list[int]:
    """Tier C — brute-force probe across a candidate physical CAN ID range.

    Sends TesterPresent (UDS 0x3E 0x00) to each address in *candidate_range*
    and collects the addresses that return a positive response (0x7E 0x00).
    Addresses that timeout or return a negative response are silently skipped.

    This is intentionally slow — probing every address in the default range
    (0x700–0x7FF, 255 addresses after excluding 0x7DF) takes on the order of
    tens of seconds to minutes depending on the configured timeout per probe.
    Use Tier B when an address table is available.

    The conventional 11-bit physical CAN address space is ~0x700–0x7FF, but
    this convention is not universally followed — some vehicles use lower
    ranges, and SAE J1939 heavy-truck networks use 29-bit extended IDs
    entirely.  The default range here is a best-effort starting point for
    standard passenger-vehicle CAN networks.

    Raises ``RuntimeError`` if the active protocol is not CAN.
    """
    if not connection.is_can_protocol:
        raise RuntimeError(
            f"Tier C ECU discovery requires a CAN protocol, but the active protocol is "
            f"{connection.protocol_name!r}."
        )

    if candidate_range is None:
        candidate_range = BRUTE_FORCE_CANDIDATE_RANGE

    probe_count = sum(1 for a in candidate_range if a not in _BRUTE_FORCE_SKIP)
    log.info("Tier C: brute-force probing %d candidate address(es) — this will be slow", probe_count)

    try:
        await connection.send_at("ATAT 0")
    except Exception as exc:
        log.warning("Tier C: ATAT 0 failed: %s — continuing", exc)

    found: list[int] = []

    try:
        for addr in candidate_range:
            if addr in _BRUTE_FORCE_SKIP:
                continue
            try:
                if await connection.probe_ecu_tester_present(addr):
                    found.append(addr)
                    log.info("Tier C: ECU found at 0x%03X", addr)
            except Exception as exc:
                log.warning("Tier C: probe 0x%03X: %s", addr, exc)
    finally:
        try:
            await connection.send_at("ATAT 1")
        except Exception as exc:
            log.warning("Tier C: ATAT 1 restore failed: %s", exc)
        try:
            await connection.send_at("ATSH 7E0")
        except Exception as exc:
            log.warning("Tier C: ATSH 7E0 restore failed: %s", exc)

    return found


async def discover_ecus(
    connection: ObdConnection,
    make: str,
    *,
    cache: EcuCache | None = None,
    vin_prefix: str = "",
) -> EcuDiscoveryResult:
    """Top-level ECU discovery orchestrator — runs tiers in order, merges results.

    Always runs Tier A (functional broadcast).  Runs Tier B if an address
    table exists for *make*; if no table exists, runs Tier C (brute-force).
    Tier C results are cached in *cache* under *vin_prefix* when both are
    provided, so subsequent starts skip the slow sweep.

    ``make`` should be the vehicle's manufacturer name as it appears in
    ``InitResult.manufacturer_name`` (e.g. ``"Ford"``, ``"Generic"``).
    Case-insensitive.

    Returns an ``EcuDiscoveryResult`` where ``found`` maps address →
    ``DiscoveredEcu`` for each responding ECU, tagged with the tier that
    first found it.  ``tier_b_entries`` is ``None`` when Tier C was used, or
    the full Tier B table (responding and non-responding entries) when Tier B
    ran.

    DTCs are **not** fetched here — call ``scan_ecus_for_dtcs()`` separately
    on the discovered addresses.

    Raises ``RuntimeError`` if the active protocol is not CAN.
    """
    if not connection.is_can_protocol:
        raise RuntimeError(
            f"ECU discovery requires a CAN protocol, but the active protocol is "
            f"{connection.protocol_name!r}.  Set ConnectionConfig.protocol to the correct "
            f"CAN variant for this vehicle before scanning."
        )

    found: dict[int, DiscoveredEcu] = {}

    # ── Tier A — always run ───────────────────────────────────────────────────
    tier_a_ids = await discover_ecus_functional(connection)
    for addr in tier_a_ids:
        found[addr] = DiscoveredEcu(address=addr, tier=DiscoveryTier.A, label="")

    # ── Tier B or C ───────────────────────────────────────────────────────────
    tier_b_entries = await discover_ecus_known_table(connection, make)

    if tier_b_entries is not None:
        # Table exists — use Tier B; skip Tier C entirely.
        for ecu in tier_b_entries:
            if ecu.responded and ecu.address not in found:
                found[ecu.address] = ecu
        log.info(
            "discover_ecus: Tier B — %d/%d address(es) responded",
            sum(1 for e in tier_b_entries if e.responded),
            len(tier_b_entries),
        )
    else:
        # No table for this vehicle — check cache before brute-force.
        # Mock connections skip brute-force (no hardware to probe; avoids 254
        # artificial probes in tests while still exercising Tier A and B paths).
        log.info("discover_ecus: no Tier B table for %r — checking Tier C cache", make)
        if connection.is_mock:
            tier_c_ids = []
        elif cache and vin_prefix and await cache.tier_c_complete(vin_prefix):
            tier_c_ids = await cache.get_tier_c_addresses(vin_prefix)
            log.info("discover_ecus: Tier C — %d address(es) from cache", len(tier_c_ids))
        else:
            log.info("discover_ecus: running Tier C brute-force (this will be slow)")
            tier_c_ids = await discover_ecus_brute_force(connection)
            if cache and vin_prefix:
                await cache.save_tier_c_results(vin_prefix, tier_c_ids)
        for addr in tier_c_ids:
            if addr not in found:
                found[addr] = DiscoveredEcu(address=addr, tier=DiscoveryTier.C, label="")

    log.info("discover_ecus: %d ECU(s) found in total", len(found))
    return EcuDiscoveryResult(found=found, tier_b_entries=tier_b_entries)
