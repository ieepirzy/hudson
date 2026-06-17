"""Tests for Hudson.cli — argument parsing and _amain entry point."""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from Hudson.cli import _parse_args, _amain


# ── _parse_args ───────────────────────────────────────────────────────────────

def test_defaults() -> None:
    """All optional flags default to their documented values."""
    args = _parse_args([])
    assert args.port is None
    assert args.baudrate is None
    assert args.protocol is None
    assert args.voltage_check is False
    assert args.mock is False
    assert args.debug is False
    assert args.telemetry is False
    assert args.vcan is None


def test_port_flag() -> None:
    args = _parse_args(["--port", "/dev/rfcomm0"])
    assert args.port == "/dev/rfcomm0"


def test_baudrate_flag() -> None:
    args = _parse_args(["--baudrate", "115200"])
    assert args.baudrate == 115200


def test_protocol_flag() -> None:
    args = _parse_args(["--protocol", "6"])
    assert args.protocol == "6"


def test_voltage_check_flag() -> None:
    args = _parse_args(["--voltage-check"])
    assert args.voltage_check is True


def test_mock_flag() -> None:
    args = _parse_args(["--mock"])
    assert args.mock is True


def test_debug_flag() -> None:
    args = _parse_args(["--debug"])
    assert args.debug is True


def test_telemetry_flag() -> None:
    args = _parse_args(["--telemetry"])
    assert args.telemetry is True


def test_vcan_flag() -> None:
    args = _parse_args(["--vcan", "vcan0"])
    assert args.vcan == "vcan0"


def test_combined_flags() -> None:
    """Multiple flags can be combined on a single invocation."""
    args = _parse_args(["--mock", "--debug", "--protocol", "6"])
    assert args.mock is True
    assert args.debug is True
    assert args.protocol == "6"


# ── _amain ────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_amain_telemetry_without_token_returns_1(monkeypatch) -> None:
    """--telemetry without HUDSON_TELEMETRY_TOKEN exits with code 1."""
    monkeypatch.delenv("HUDSON_TELEMETRY_TOKEN", raising=False)
    args = _parse_args(["--telemetry", "--mock"])
    result = await _amain(args)
    assert result == 1


@pytest.mark.asyncio
async def test_amain_telemetry_with_token_starts(monkeypatch) -> None:
    """--telemetry with a token creates a TelemetryClient and returns 0."""
    monkeypatch.setenv("HUDSON_TELEMETRY_TOKEN", "test-token-xyz")
    args = _parse_args(["--telemetry", "--mock"])
    with patch("Hudson.cli.HudsonApp") as MockApp:
        mock_instance = MagicMock()
        mock_instance.run_async = AsyncMock()
        MockApp.return_value = mock_instance
        with patch("Hudson.core.telemetry.TelemetryClient") as MockTelemetry:
            mock_tel = MagicMock()
            mock_tel.stop = AsyncMock()
            MockTelemetry.return_value = mock_tel
            result = await _amain(args)
    assert result == 0


@pytest.mark.asyncio
async def test_amain_mock_uses_fake_connection() -> None:
    """--mock creates a FakeConnection (not an ObdConnection)."""
    args = _parse_args(["--mock"])
    with patch("Hudson.cli.HudsonApp") as MockApp:
        mock_instance = MagicMock()
        mock_instance.run_async = AsyncMock()
        MockApp.return_value = mock_instance
        result = await _amain(args)
    assert result == 0
    # HudsonApp was constructed with a FakeConnection (not None)
    call_conn = MockApp.call_args.args[0]
    assert call_conn is not None
    from tests.fixtures.fake_connection import FakeConnection
    assert isinstance(call_conn, FakeConnection)


@pytest.mark.asyncio
async def test_amain_returns_zero_on_success() -> None:
    """_amain returns 0 when the app runs and exits cleanly."""
    args = _parse_args(["--mock"])
    with patch("Hudson.cli.HudsonApp") as MockApp:
        mock_instance = MagicMock()
        mock_instance.run_async = AsyncMock(return_value=None)
        MockApp.return_value = mock_instance
        result = await _amain(args)
    assert result == 0
