"""Init sequence orchestration.

The startup flow is essentially a state machine:

    NEW
     ├── connect()                     → CONNECTED
     ├── read_protocol()               → PROTOCOL_KNOWN
     ├── read_vin()                    → VIN_KNOWN  (or VIN_FAILED → generic fallback)
     ├── select_manufacturer()         → MANUFACTURER_KNOWN
     ├── probe_supported_pids()        → PIDS_KNOWN
     └── ready                          → READY

Each step emits a `InitEvent` on a queue so the splash screen can render
progress live. Errors at any step surface as `InitEvent` with `error=...`
so the UI can show what failed without raising into the event loop.
"""

from __future__ import annotations

import asyncio
import importlib
import logging
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import TYPE_CHECKING

from Hudson.core.connection import ObdConnection
from Hudson.core.vin import VinReadError, read_vin
from Hudson.manufacturers.registry import select_decoder

if TYPE_CHECKING:
    from obd import OBDCommand

log = logging.getLogger(__name__)


class InitStep(Enum):
    CONNECT = auto()
    PROTOCOL = auto()
    VIN = auto()
    MANUFACTURER = auto()
    SUPPORTED_PIDS = auto()
    READY = auto()


@dataclass(frozen=True, slots=True)
class InitEvent:
    """One step of progress emitted by `run_init`."""

    step: InitStep
    detail: str = ""
    error: str | None = None  # None on success; set on failure
    done: bool = False  # True when this step finished (success or error)


@dataclass(slots=True)
class InitResult:
    """The aggregate state produced by a successful init."""

    protocol_name: str = ""
    vin: str | None = None
    manufacturer_name: str = "Generic"
    manufacturer_module: object | None = None  # imported module
    supported_commands: set[OBDCommand] = field(default_factory=set)


async def run_init(
    connection: ObdConnection,
    events: asyncio.Queue[InitEvent],
) -> InitResult:
    """Run the full init sequence, emitting progress events.

    Returns the populated InitResult on success.
    Raises if connection itself fails — everything else is recoverable
    and reported as a non-fatal error event.
    """
    result = InitResult()

    # ── 1. Connect ───────────────────────────────────────────────
    await events.put(InitEvent(InitStep.CONNECT, "opening serial connection"))
    await connection.connect()
    await events.put(InitEvent(InitStep.CONNECT, "connected", done=True))

    # ── 2. Protocol ──────────────────────────────────────────────
    await events.put(InitEvent(InitStep.PROTOCOL, "detecting protocol"))
    result.protocol_name = connection.protocol_name
    await events.put(
        InitEvent(InitStep.PROTOCOL, result.protocol_name or "(unknown)", done=True)
    )

    # ── 3. VIN ───────────────────────────────────────────────────
    await events.put(InitEvent(InitStep.VIN, "reading VIN"))
    try:
        result.vin = await read_vin(connection)
        await events.put(InitEvent(InitStep.VIN, result.vin, done=True))
    except VinReadError as exc:
        log.warning("VIN read failed: %s", exc)
        await events.put(
            InitEvent(InitStep.VIN, "unavailable", error=str(exc), done=True)
        )

    # ── 4. Manufacturer ──────────────────────────────────────────
    await events.put(InitEvent(InitStep.MANUFACTURER, "loading manufacturer module"))
    if result.vin is not None:
        module_path = select_decoder(result.vin)
    else:
        module_path = "hudson.manufacturers.generic"
    try:
        result.manufacturer_module = importlib.import_module(module_path)
        result.manufacturer_name = getattr(result.manufacturer_module, "name", "Generic")
        await events.put(
            InitEvent(InitStep.MANUFACTURER, result.manufacturer_name, done=True)
        )
    except Exception as exc:  # noqa: BLE001 - we want broad failure reporting here
        log.exception("manufacturer load failed")
        await events.put(
            InitEvent(InitStep.MANUFACTURER, "generic fallback", error=str(exc), done=True)
        )
        result.manufacturer_module = importlib.import_module("hudson.manufacturers.generic")
        result.manufacturer_name = "Generic"

    # ── 5. Supported PIDs ────────────────────────────────────────
    await events.put(InitEvent(InitStep.SUPPORTED_PIDS, "probing supported PIDs"))
    result.supported_commands = await connection.supported_commands()
    await events.put(
        InitEvent(
            InitStep.SUPPORTED_PIDS,
            f"{len(result.supported_commands)} commands supported",
            done=True,
        )
    )

    # ── 6. Ready ─────────────────────────────────────────────────
    await events.put(InitEvent(InitStep.READY, "ready", done=True))
    return result
