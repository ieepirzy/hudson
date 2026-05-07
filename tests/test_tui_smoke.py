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

        # After init, the dashboard should be on top.
        from Hudson.tui.screens.dashboard import DashboardScreen

        active = app.screen
        assert isinstance(active, DashboardScreen), (
            f"expected DashboardScreen, got {type(active).__name__}"
        )

        # Vehicle info strip should be populated with the fake VIN.
        info_widget = active.query_one("#vehicle-info")
        rendered = str(info_widget.render())
        assert "WV2ZZZ7HZ8H123456" in rendered, f"VIN not in info strip: {rendered!r}"
        assert "VW/Audi" in rendered, f"Manufacturer not detected: {rendered!r}"

        # Let a few poll cycles happen so gauges populate.
        await pilot.pause(0.5)

        # RPM gauge should have a value by now.
        rpm_gauge = active.query_one("#g-rpm")
        rpm_value_widget = rpm_gauge.query_one("#value")
        rpm_text = str(rpm_value_widget.render())
        assert rpm_text != "--", f"RPM gauge still empty: {rpm_text!r}"
        assert "rpm" in rpm_text, f"RPM gauge missing unit: {rpm_text!r}"
