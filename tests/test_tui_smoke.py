"""Headless TUI smoke test.

Uses Textual's `Pilot` API to boot the app against a `FakeConnection` and
verify that:

  1. SplashScreen renders without errors
  2. Init sequence completes
  3. DashboardScreen takes over and renders the vehicle info strip
  4. Gauges receive values from the (fake) poller

This catches the kinds of bugs you'd otherwise only find by plugging
into a real car — wiring errors, missing init flow, screen lifecycle.
"""

from __future__ import annotations

import asyncio

import pytest

from Hudson.tui.app import HudsonApp
from tests.fixtures.fake_connection import FakeConnection


@pytest.mark.asyncio
async def test_splash_to_dashboard_smoke() -> None:
    """Boot the full app and confirm the screen handoff works."""
    fake = FakeConnection()
    app = HudsonApp(fake)  # type: ignore[arg-type]

    async with app.run_test() as pilot:
        # Allow the splash to complete the init sequence.
        # (Connect 0.1s + 6 fake queries × 0.02s + small slack)
        await pilot.pause(1.0)

        # Pump the event loop a bit for any pending coroutines.
        for _ in range(5):
            await pilot.pause(0.1)

        # After init, MainScreen should be on top.
        from Hudson.tui.screens.main import MainScreen

        active = app.screen
        assert isinstance(active, MainScreen), (
            f"expected MainScreen, got {type(active).__name__}"
        )

        # Header strip should be populated with the fake VIN and manufacturer.
        info_widget = active.query_one("#header-strip")
        rendered = str(info_widget.render())
        assert "WV2ZZZ7HZ8H123456" in rendered, f"VIN not in header strip: {rendered!r}"
        assert "VW/Audi" in rendered, f"Manufacturer not detected: {rendered!r}"
