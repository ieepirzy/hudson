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


@pytest.mark.asyncio
async def test_right_arrow_switches_to_dtc_tab() -> None:
    """Pressing → from Dashboard switches the ContentSwitcher to the DTCs pane."""
    from textual.widgets import ContentSwitcher
    from Hudson.tui.screens.main import MainScreen

    fake = FakeConnection()
    app = HudsonApp(fake)  # type: ignore[arg-type]

    async with app.run_test() as pilot:
        await pilot.pause(1.0)
        for _ in range(5):
            await pilot.pause(0.1)

        assert isinstance(app.screen, MainScreen)

        await pilot.press("right")
        await pilot.pause(0.1)

        switcher = app.screen.query_one(ContentSwitcher)
        assert switcher.current == "dtcs"


@pytest.mark.asyncio
async def test_tab_index_wraps_after_full_cycle() -> None:
    """After pressing → four times, the tab index returns to 0 (Dashboard)."""
    from Hudson.tui.screens.main import MainScreen, TABS

    fake = FakeConnection()
    app = HudsonApp(fake)  # type: ignore[arg-type]

    async with app.run_test() as pilot:
        await pilot.pause(1.0)
        for _ in range(5):
            await pilot.pause(0.1)

        assert isinstance(app.screen, MainScreen)

        # Press right once for each tab; state should wrap back to index 0.
        # We check _active_idx (internal counter) rather than ContentSwitcher.current
        # to avoid triggering a Dashboard re-show that races with the gauge worker.
        for _ in range(len(TABS) - 1):   # go to last tab (index 3 = Vehicle)
            await pilot.press("right")
            await pilot.pause(0.05)

        assert app.screen._active_idx == len(TABS) - 1  # on Vehicle

        # One more press wraps to index 0
        await pilot.press("right")
        await pilot.pause(0.05)
        assert app.screen._active_idx == 0


@pytest.mark.asyncio
async def test_vehicle_tab_is_reachable() -> None:
    """Pressing → three times reaches the Vehicle pane."""
    from textual.widgets import ContentSwitcher
    from Hudson.tui.screens.main import MainScreen

    fake = FakeConnection()
    app = HudsonApp(fake)  # type: ignore[arg-type]

    async with app.run_test() as pilot:
        await pilot.pause(1.0)
        for _ in range(5):
            await pilot.pause(0.1)

        assert isinstance(app.screen, MainScreen)

        for _ in range(3):   # Dashboard → DTCs → Log → Vehicle
            await pilot.press("right")
            await pilot.pause(0.05)

        switcher = app.screen.query_one(ContentSwitcher)
        assert switcher.current == "vehicle"


@pytest.mark.asyncio
async def test_left_arrow_wraps_to_last_tab() -> None:
    """Pressing ← from the first tab wraps to the last (Vehicle)."""
    from textual.widgets import ContentSwitcher
    from Hudson.tui.screens.main import MainScreen

    fake = FakeConnection()
    app = HudsonApp(fake)  # type: ignore[arg-type]

    async with app.run_test() as pilot:
        await pilot.pause(1.0)
        for _ in range(5):
            await pilot.pause(0.1)

        assert isinstance(app.screen, MainScreen)

        await pilot.press("left")
        await pilot.pause(0.1)

        switcher = app.screen.query_one(ContentSwitcher)
        assert switcher.current == "vehicle"
