"""Command-line entry point.

Usage:
    hudson [--port /dev/rfcomm0] [--protocol 6] [--no-voltage-check] [--mock] [--debug] [--telemetry]

Connection is opened inside the SplashScreen so init progress is visible
to the user, not hidden behind a CLI loading delay.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from Hudson.core.connection import ConnectionConfig, ObdConnection
from Hudson.tui.app import HudsonApp


_LOG_FMT = "%(asctime)s %(name)s %(levelname)s: %(message)s"


def _configure_logging(debug: bool) -> None:
    log_dir = Path("logs")
    log_dir.mkdir(exist_ok=True)

    file_handler = RotatingFileHandler(
        log_dir / "hudson.log",
        maxBytes=1_000_000,
        backupCount=3,
        encoding="utf-8",
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter(_LOG_FMT))

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    root.addHandler(file_handler)

    if debug:
        stderr_handler = logging.StreamHandler(sys.stderr)
        stderr_handler.setLevel(logging.DEBUG)
        stderr_handler.setFormatter(logging.Formatter(_LOG_FMT))
        root.addHandler(stderr_handler)


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
        "--no-voltage-check",
        action="store_true",
        help="Skip ELM327 battery voltage check. Useful for BT adapters that report bad voltage.",
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
    parser.add_argument(
        "--telemetry",
        action="store_true",
        help="Send telemetry to api.muutto365.fi. Requires HUDSON_TELEMETRY_TOKEN env var.",
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
            check_voltage=not args.no_voltage_check,
        )
        connection = ObdConnection(config)

    telemetry = None
    if args.telemetry:
        token = os.environ.get("HUDSON_TELEMETRY_TOKEN")
        if not token:
            print("Error: --telemetry requires HUDSON_TELEMETRY_TOKEN environment variable", file=sys.stderr)
            return 1
        from Hudson.core.telemetry import TelemetryClient
        telemetry = TelemetryClient(token)

    try:
        app = HudsonApp(connection, telemetry=telemetry)  # type: ignore[arg-type]
        await app.run_async()
    finally:
        if telemetry is not None:
            await telemetry.stop()
        await connection.close()
    return 0


def main() -> None:
    args = _parse_args(sys.argv[1:])
    _configure_logging(args.debug)
    raise SystemExit(asyncio.run(_amain(args)))


if __name__ == "__main__":
    main()