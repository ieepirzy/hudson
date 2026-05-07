"""Mock ObdConnection for headless TUI testing.

Replaces the real serial connection with synthesized responses so the
splash → dashboard flow can be exercised in CI / sandbox environments
where no ELM327 is connected.
"""

from __future__ import annotations

import asyncio
import math
from time import monotonic
from typing import TYPE_CHECKING

import obd
from obd import OBDResponse

if TYPE_CHECKING:
    from obd import OBDCommand


class _FakeQuantity:
    """Minimal stand-in for a Pint quantity exposing `.magnitude`."""

    __slots__ = ("magnitude",)

    def __init__(self, magnitude: float) -> None:
        self.magnitude = magnitude


class FakeConnection:
    """Implements the same surface as `ObdConnection` for tests."""

    # Realistic set of stored DTCs — cleared by CLEAR_DTC, readable by GET_DTC.
    # Format matches what python-obd returns: list of (code_str, description_str).
    _INITIAL_DTCS: list[tuple[str, str]] = [
        ("P0300", "Random/Multiple Cylinder Misfire Detected"),
        ("P0171", "System Too Lean (Bank 1)"),
        ("C0035", "Left Front Wheel Speed Sensor Circuit"),
    ]

    def __init__(self, vin: str = "WV2ZZZ7HZ8H123456") -> None:
        self._vin = vin
        self._connected = False
        self._t0 = monotonic()
        self._dtcs: list[tuple[str, str]] = list(self._INITIAL_DTCS)
        self._supported = {
            obd.commands.RPM,
            obd.commands.SPEED,
            obd.commands.THROTTLE_POS,
            obd.commands.COOLANT_TEMP,
            obd.commands.INTAKE_TEMP,
            obd.commands.ENGINE_LOAD,
            obd.commands.VIN,
            obd.commands.GET_DTC,
            obd.commands.CLEAR_DTC,
        }

    async def connect(self) -> None:
        await asyncio.sleep(0.1)
        self._connected = True

    async def close(self) -> None:
        self._connected = False

    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def protocol_name(self) -> str:
        return "ISO 15765-4 (CAN 11/500)"

    async def supported_commands(self) -> set[OBDCommand]:
        return set(self._supported)

    async def query(self, cmd: OBDCommand, force: bool = False) -> OBDResponse:
        await asyncio.sleep(0.02)  # simulate UART round-trip

        if cmd is obd.commands.VIN:
            return _make_response(cmd, self._vin)

        if cmd is obd.commands.GET_DTC:
            return _make_response(cmd, list(self._dtcs))

        if cmd is obd.commands.CLEAR_DTC:
            self._dtcs.clear()
            return _make_response(cmd, [])

        t = monotonic() - self._t0
        value = _synthetic_value(cmd, t)
        return _make_response(cmd, value)


def _synthetic_value(cmd: OBDCommand, t: float) -> object:
    """Toy waveform generator so the dashboard moves."""
    if cmd is obd.commands.RPM:
        return _FakeQuantity(800 + 1500 * (0.5 + 0.5 * math.sin(t * 0.7)))
    if cmd is obd.commands.SPEED:
        return _FakeQuantity(40 + 30 * math.sin(t * 0.3))
    if cmd is obd.commands.THROTTLE_POS:
        return _FakeQuantity(15 + 25 * (0.5 + 0.5 * math.sin(t * 1.1)))
    if cmd is obd.commands.COOLANT_TEMP:
        return _FakeQuantity(85 + 5 * math.sin(t * 0.05))
    if cmd is obd.commands.INTAKE_TEMP:
        return _FakeQuantity(35 + 3 * math.sin(t * 0.07))
    if cmd is obd.commands.ENGINE_LOAD:
        return _FakeQuantity(30 + 20 * math.sin(t * 0.5))
    return None


def _make_response(cmd: OBDCommand, value: object) -> OBDResponse:
    """Build an OBDResponse with a value attached.

    `OBDResponse.is_null()` checks both that `messages` is non-empty AND that
    `value is not None`, so we pass a sentinel message so the response reads
    as "real" without us needing to construct a full `Message` object.
    """
    resp = OBDResponse(command=cmd, messages=[_FakeMessage()])
    resp.value = value
    return resp


class _FakeMessage:
    """Sentinel — `is_null` only checks truthiness of the messages list."""
