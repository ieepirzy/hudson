"""Init sequence orchestration.

The startup flow is a state machine:

    NEW
     ├── connect()                     → CONNECTED
     ├── read_protocol()               → PROTOCOL_KNOWN
     ├── resolve_vin_chain()           → VIN_KNOWN  (or VIN_FAILED → generic fallback)
     ├── select_manufacturer()         → MANUFACTURER_KNOWN
     ├── read_ecu_version()            → ECU_VERSION_KNOWN  (UDS strategy only)
     ├── run_priority1_discovery()     → UDS_DISCOVERY done (or skipped)
     ├── probe_supported_pids()        → PIDS_KNOWN
     └── ready                          → READY

Each step emits an `InitEvent` so the splash screen can render progress live.
Non-fatal errors surface as InitEvent(error=...) so the UI can report them
without raising into the event loop.

After run_init() returns the caller should fire:
    asyncio.create_task(result.uds_discovery.run_priority2_background())
if result.uds_discovery is not None, to continue the background sweep.
"""

from __future__ import annotations

import asyncio
import importlib
import logging
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import TYPE_CHECKING

from Hudson.core.connection import ObdConnection
from Hudson.core.ecu_cache import EcuCache
from Hudson.core.kwp2000 import KwpSession
from Hudson.core.uds import UdsDiscovery
from Hudson.core.vin import resolve_vin_chain
from Hudson.manufacturers.registry import select_decoder

if TYPE_CHECKING:
    from obd import OBDCommand

log = logging.getLogger(__name__)


class InitStep(Enum):
    CONNECT = auto()
    PROTOCOL = auto()
    VIN = auto()
    MANUFACTURER = auto()
    ECU_VERSION = auto()     # UDS: query 0xF189 for ECU software version
    UDS_DISCOVERY = auto()   # UDS: priority-1 identifier sweep
    KWP_SESSION = auto()     # KWP2000: K-line session attempt (pre-2007 vehicles)
    SUPPORTED_PIDS = auto()
    READY = auto()


@dataclass(frozen=True, slots=True)
class InitEvent:
    """One step of progress emitted by `run_init`."""

    step: InitStep
    detail: str = ""
    error: str | None = None       # None = success; set = failure
    done: bool = False             # True when the step finished (success or error)
    progress: float | None = None  # 0.0–1.0, only set during UDS_DISCOVERY sweep


@dataclass(slots=True)
class InitResult:
    """Aggregate state produced by a successful init."""

    protocol_name: str = ""
    vin: str | None = None
    manufacturer_name: str = "Generic"
    manufacturer_module: object | None = None
    supported_commands: set[OBDCommand] = field(default_factory=set)
    ecu_version: str | None = None
    uds_identifiers: list[int] = field(default_factory=list)
    uds_discovery: UdsDiscovery | None = None
    kwp_session: KwpSession | None = None
    dtcdecode_make: str | None = None  # set by splash screen after make selection


async def run_init(
    connection: ObdConnection,
    events: asyncio.Queue[InitEvent],
) -> InitResult:
    """Run the full init sequence, emitting progress events.

    Returns the populated InitResult on success.
    Raises only if the connection itself fails — everything else is reported
    as a non-fatal error event and execution continues.
    """
    result = InitResult()

    # ── 1. Connect ───────────────────────────────────────────────────────────
    await events.put(InitEvent(InitStep.CONNECT, "opening serial connection"))
    await connection.connect()
    await events.put(InitEvent(InitStep.CONNECT, "connected", done=True))

    # ── 2. Protocol ──────────────────────────────────────────────────────────
    await events.put(InitEvent(InitStep.PROTOCOL, "detecting protocol"))
    result.protocol_name = connection.protocol_name
    await events.put(
        InitEvent(InitStep.PROTOCOL, result.protocol_name or "(unknown)", done=True)
    )

    # ── 3. VIN ───────────────────────────────────────────────────────────────
    await events.put(InitEvent(InitStep.VIN, "reading VIN"))
    result.vin = await resolve_vin_chain(connection)
    if result.vin:
        await events.put(InitEvent(InitStep.VIN, result.vin, done=True))
    else:
        await events.put(
            InitEvent(
                InitStep.VIN,
                "unavailable",
                error="all VIN protocols failed — check hudson.log for details",
                done=True,
            )
        )

    # ── 4. Manufacturer ──────────────────────────────────────────────────────
    await events.put(InitEvent(InitStep.MANUFACTURER, "loading manufacturer module"))
    module_path = select_decoder(result.vin) if result.vin else "Hudson.manufacturers.generic"
    try:
        result.manufacturer_module = importlib.import_module(module_path)
        result.manufacturer_name = getattr(result.manufacturer_module, "name", "Generic")
        await events.put(
            InitEvent(InitStep.MANUFACTURER, result.manufacturer_name, done=True)
        )
    except Exception as exc:  # noqa: BLE001
        log.exception("manufacturer module load failed")
        await events.put(
            InitEvent(InitStep.MANUFACTURER, "generic fallback", error=str(exc), done=True)
        )
        result.manufacturer_module = importlib.import_module("Hudson.manufacturers.generic")
        result.manufacturer_name = "Generic"

    # ── 5 & 6. UDS discovery ─────────────────────────────────────────────────
    strategy = getattr(result.manufacturer_module, "DISCOVERY_STRATEGY", "probe")

    # UDS (service 0x22) only makes sense over CAN. Sending it over a K-line
    # protocol (ISO 9141-2, KWP2000) confuses the ELM327 state machine and
    # corrupts subsequent mode 01 queries — skip entirely for K-line.
    _proto = result.protocol_name.lower()
    _is_kline = any(kw in _proto for kw in ("9141", "14230", "kwp"))
    if _is_kline:
        strategy = "mode01_only"
        log.info("K-line protocol detected (%s) — skipping UDS discovery", result.protocol_name)

    # "probe"  → ask the ECU at runtime (handles uncertain transition-era vehicles)
    # "uds"    → manufacturer is certain; skip the probe gate, go straight to discovery
    # anything else → mode01_only, skip UDS entirely
    if strategy in ("uds", "probe"):
        await _run_uds_steps(connection, events, result, probe_only=(strategy == "probe"))
    else:
        await events.put(InitEvent(InitStep.ECU_VERSION, "not applicable", done=True))
        await events.put(InitEvent(InitStep.UDS_DISCOVERY, "not applicable", done=True))
        await events.put(InitEvent(InitStep.KWP_SESSION, "not applicable", done=True))

    # ── 7. Supported PIDs ────────────────────────────────────────────────────
    await events.put(InitEvent(InitStep.SUPPORTED_PIDS, "probing supported PIDs"))
    result.supported_commands = await connection.supported_commands()
    await events.put(
        InitEvent(
            InitStep.SUPPORTED_PIDS,
            f"{len(result.supported_commands)} commands supported",
            done=True,
        )
    )

    # ── 8. Ready ─────────────────────────────────────────────────────────────
    await events.put(InitEvent(InitStep.READY, "ready", done=True))
    return result


async def _run_uds_steps(
    connection: ObdConnection,
    events: asyncio.Queue[InitEvent],
    result: InitResult,
    *,
    probe_only: bool = False,
) -> None:
    """Execute ECU_VERSION and UDS_DISCOVERY init steps.

    When probe_only=True (strategy="probe"), 0xF189 is the gate: a response
    promotes to full discovery; no response falls back to mode 01 gracefully.
    When probe_only=False (strategy="uds"), we trust the manufacturer and
    proceed to full discovery regardless — but still read the ECU version first.
    """

    # ── 5. ECU version ───────────────────────────────────────────────────────
    detail = "probing ECU software version (0xF189)"
    if probe_only:
        detail = "probing for UDS capability (0xF189)"
    await events.put(InitEvent(InitStep.ECU_VERSION, detail))

    cache = EcuCache()
    await cache.init()

    fallback_version = f"vin:{result.vin[:8]}" if result.vin else "unknown"
    discovery = UdsDiscovery(connection, cache, fallback_version)

    ecu_version = await discovery.read_ecu_version()
    if ecu_version is None:
        # ECU didn't respond to 0xF189 — not UDS capable on this transport.
        await events.put(
            InitEvent(InitStep.ECU_VERSION, "no response — ECU does not speak UDS", done=True)
        )
        await events.put(
            InitEvent(InitStep.UDS_DISCOVERY, "skipped — falling back to mode 01", done=True)
        )
        kwp_blocks = getattr(result.manufacturer_module, "kwp_blocks", None)
        if kwp_blocks is not None:
            # K-line session (ATSP3) is only safe when the vehicle is actually
            # on a K-line protocol. Issuing ATSP3 on a CAN vehicle corrupts the
            # ELM327 state machine and poisons subsequent mode 01 queries.
            _proto_lower = connection.protocol_name.lower()
            _is_can = not any(kw in _proto_lower for kw in ("9141", "14230", "kwp"))
            if _is_can:
                log.warning(
                    "KWP blocks are defined for %s but protocol is CAN (%s) — "
                    "K-line session blocked to protect adapter state",
                    result.manufacturer_name,
                    connection.protocol_name,
                )
                await events.put(
                    InitEvent(
                        InitStep.KWP_SESSION,
                        "blocked — CAN protocol detected, K-line not safe",
                        error="K-line session suppressed on CAN vehicle",
                        done=True,
                    )
                )
            else:
                await _run_kwp_session(connection, events, result)
        else:
            await events.put(InitEvent(InitStep.KWP_SESSION, "not applicable", done=True))
        return

    discovery.ecu_version = ecu_version
    result.ecu_version = ecu_version
    result.uds_discovery = discovery
    await events.put(InitEvent(InitStep.ECU_VERSION, ecu_version, done=True))

    # ── 6. UDS discovery ─────────────────────────────────────────────────────
    await events.put(
        InitEvent(
            InitStep.UDS_DISCOVERY,
            "Starting Hudson UDS discovery system (service 0x22, read-only)…",
        )
    )

    if await cache.priority1_complete(discovery.ecu_version):
        cached = await cache.get_discovered_identifiers(discovery.ecu_version)
        ids = [r["identifier"] for r in cached if r["responded"]]
        result.uds_identifiers = ids
        discovery._p1_responding = ids  # allow priority-2 background sweep to proceed
        await events.put(
            InitEvent(
                InitStep.UDS_DISCOVERY,
                f"{len(ids)} identifiers found (cached)",
                done=True,
            )
        )
        await events.put(InitEvent(InitStep.KWP_SESSION, "not applicable", done=True))
        return

    async def _on_progress(current: int, total: int, identifier: int, responded: bool) -> None:
        await events.put(
            InitEvent(
                InitStep.UDS_DISCOVERY,
                detail=f"{current}/{total}",
                progress=current / total,
            )
        )

    try:
        responding = await discovery.run_priority1(_on_progress)
        result.uds_identifiers = responding
        await events.put(
            InitEvent(
                InitStep.UDS_DISCOVERY,
                f"{len(responding)} identifiers found",
                done=True,
            )
        )
    except Exception as exc:  # noqa: BLE001
        log.exception("UDS priority-1 discovery failed")
        await events.put(
            InitEvent(InitStep.UDS_DISCOVERY, "discovery failed", error=str(exc), done=True)
        )
    await events.put(InitEvent(InitStep.KWP_SESSION, "not applicable", done=True))


async def _run_kwp_session(
    connection: ObdConnection,
    events: asyncio.Queue[InitEvent],
    result: InitResult,
) -> None:
    """Attempt a KWP2000 K-line session for manufacturers that expose kwp_blocks."""
    await events.put(InitEvent(InitStep.KWP_SESSION, "attempting KWP2000 session (ATSP3)"))
    session = KwpSession(connection)
    try:
        started = await session.start_diagnostic_session()
        if started:
            result.kwp_session = session
            await events.put(InitEvent(InitStep.KWP_SESSION, "K-line session active", done=True))
        else:
            await session.close()
            await events.put(
                InitEvent(InitStep.KWP_SESSION, "no response — K-line unavailable", done=True)
            )
    except Exception as exc:  # noqa: BLE001
        log.exception("KWP2000 session failed")
        await session.close()
        await events.put(
            InitEvent(InitStep.KWP_SESSION, "session failed", error=str(exc), done=True)
        )
