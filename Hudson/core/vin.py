"""VIN reading via mode 09 PID 02.

python-obd has `obd.commands.VIN` built in but its parser has historically
been finicky on multi-frame responses. We use it as the primary path and
fall back to a raw query if the parsed value comes back empty.

The VIN itself is 17 ASCII characters. The first 3 are the World Manufacturer
Identifier (WMI), used by `manufacturers/registry.py` to dispatch to the
right extended-PID module.
"""

from __future__ import annotations

import logging

import obd

# SAE J17 model year encoding — position 9 (0-indexed) of the VIN.
# Letters I, O, Q, U, Z are never used in VINs.
#
# The table runs in two 30-year cycles:
#   Cycle 1: A=1980 … S=1995, T=1996 … Y=2000, 1=2001 … 9=2009
#   Cycle 2: A=2010 … S=2025, T=2026 … (ongoing)
#
# Letters A–S are unambiguous for a diagnostic tool: the first-cycle years
# (1980–1995) predate OBD-II, so we can safely return the second-cycle year.
#
# Letters T, V, W, X, Y are deliberately absent — both OBD-II-era cycles are
# plausible (e.g. T = 1996 or 2026, V = 1997 or 2027). decode_model_year()
# returns None for these so callers fall back to a runtime ECU probe.
AMBIGUOUS_YEARS: frozenset[str] = frozenset("TVWXY")

_YEAR_CHARS: dict[str, int] = {
    # A–S: return the second-cycle year (2010–2025); first cycle predates OBD-II
    "A": 2010, "B": 2011, "C": 2012, "D": 2013, "E": 2014,
    "F": 2015, "G": 2016, "H": 2017, "J": 2018, "K": 2019,
    "L": 2020, "M": 2021, "N": 2022, "P": 2023, "R": 2024,
    "S": 2025,
    # T, V, W, X, Y intentionally omitted — both cycles are OBD-II era.
    # Digits — unambiguous (2001–2009):
    "1": 2001, "2": 2002, "3": 2003, "4": 2004, "5": 2005,
    "6": 2006, "7": 2007, "8": 2008, "9": 2009,
}


def decode_model_year(vin: str) -> int | None:
    """Return the model year encoded in VIN position 9 (0-indexed).

    Returns None if the VIN is too short, the character is invalid, or the
    year is genuinely ambiguous (T/V/W/X/Y — both OBD-II era cycles).
    Callers must not use None to skip ECU probing; probe directly instead.
    """
    if len(vin) < 10:
        return None
    return _YEAR_CHARS.get(vin[9].upper())

from Hudson.core.connection import ObdConnection

log = logging.getLogger(__name__)


class VinReadError(RuntimeError):
    """Could not read VIN from the vehicle."""


async def read_vin(connection: ObdConnection) -> str:
    """Read the VIN from the connected vehicle.

    Raises VinReadError if no readable VIN comes back. This is non-fatal at
    the application level — we'd just fall back to the generic decoder — but
    the caller decides that policy.
    """
    response = await connection.query(obd.commands.VIN, force=True)
    if response.is_null():
        raise VinReadError("VIN response was null (no reply or unsupported)")

    raw = response.value
    if raw is None:
        raise VinReadError("VIN response value was None")

    # python-obd returns VIN as bytes, bytearray, or str depending on decoder version.
    if isinstance(raw, (bytes, bytearray)):
        vin = raw.decode("ascii", errors="replace").strip()
    else:
        vin = str(raw).strip()

    # Strip non-ASCII or control characters defensively.
    vin = "".join(c for c in vin if c.isprintable())

    if len(vin) != 17:
        raise VinReadError(f"VIN length {len(vin)} != 17, got {vin!r}")

    return vin
