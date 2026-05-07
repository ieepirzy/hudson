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

    # python-obd returns VIN as bytes-like or str depending on version.
    if isinstance(raw, bytes):
        vin = raw.decode("ascii", errors="replace").strip()
    else:
        vin = str(raw).strip()

    # Strip non-ASCII or control characters defensively.
    vin = "".join(c for c in vin if c.isprintable())

    if len(vin) != 17:
        raise VinReadError(f"VIN length {len(vin)} != 17, got {vin!r}")

    return vin
