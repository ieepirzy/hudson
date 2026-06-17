"""Quick diagnostic: connect to the ELM327 and poll RPM + other basic PIDs.

Usage:
    python tools/query_live.py
    python tools/query_live.py --port /dev/rfcomm0
    python tools/query_live.py --port /dev/rfcomm0 --count 10
"""

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import obd

from Hudson.core.connection import ConnectionConfig, ObdConnection

PIDS = [
    obd.commands.RPM,
    obd.commands.SPEED,
    obd.commands.COOLANT_TEMP,
    obd.commands.INTAKE_TEMP,
    obd.commands.ENGINE_LOAD,
    obd.commands.THROTTLE_POS,
]


async def main(port: str | None, count: int) -> None:
    config = ConnectionConfig(portstr=port, check_voltage=False)
    conn = ObdConnection(config)

    print("Connecting…")
    await conn.connect()
    print(f"Connected: {conn.protocol_name}")

    rv = await conn.send_at("ATRV")
    ati = await conn.send_at("ATI")
    print(f"Adapter: {ati.strip()!r}  Voltage: {rv.strip()!r}")

    supported = await conn.supported_commands()
    print(f"Supported PIDs: {len(supported)}")

    for i in range(count):
        print(f"\n--- poll {i + 1}/{count} ---")
        for cmd in PIDS:
            resp = await conn.query(cmd, force=True)
            val = resp.value
            if val is None:
                print(f"  {cmd.name:<20} NULL (is_null={resp.is_null()})")
            else:
                try:
                    print(f"  {cmd.name:<20} {float(val.magnitude):.1f} {val.units}")
                except AttributeError:
                    print(f"  {cmd.name:<20} {val}")
        await asyncio.sleep(0.5)

    await conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", default=None)
    parser.add_argument("--count", type=int, default=5)
    args = parser.parse_args()
    asyncio.run(main(args.port, args.count))
