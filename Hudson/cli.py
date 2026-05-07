"""Command-line entry point.

Usage:
    hudson [--port /dev/rfcomm0] [--protocol auto|6] [--mock] [--debug]

Connection is opened inside the SplashScreen so init progress is visible
to the user, not hidden behind a CLI loading delay.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from Hudson.core.connection import ConnectionConfig, ObdConnection
from Hudson.tui.app import HudsonApp


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="hudson")
    parser.add_argument(
        "--port",
        default=None,
        help="Serial port to use (default: auto-detect). For BT, typically /dev/rfcomm0.",
    )
    parser.add_argument(
        "--baudrate",
        type=int,
        default=None,
        help="Serial baudrate (default: auto-detect).",
    )
    parser.add_argument(
        "--protocol",
        default=None,
        help='OBD2 protocol id ("6" = ISO 15765-4 CAN 11/500). Default: auto.',
    )
    parser.add_argument(
        "--mock",
        action="store_true",
        help="Use a fake connection with synthetic data (no hardware required).",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Verbose logging to stderr.",
    )
    return parser.parse_args(argv)


async def _amain(args: argparse.Namespace) -> int:
    if args.mock:
        from tests.fixtures.fake_connection import FakeConnection
        connection = FakeConnection()
    else:
        config = ConnectionConfig(
            portstr=args.port,
            baudrate=args.baudrate,
            protocol=args.protocol,
        )
        connection = ObdConnection(config)

    try:
        app = HudsonApp(connection)  # type: ignore[arg-type]
        await app.run_async()
    finally:
        await connection.close()
    return 0


def main() -> None:
    args = _parse_args(sys.argv[1:])
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s: %(message)s",
        stream=sys.stderr,
    )
    raise SystemExit(asyncio.run(_amain(args)))


if __name__ == "__main__":
    main()