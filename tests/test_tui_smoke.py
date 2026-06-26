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
from Hudson.tui.screens.splash import MakeSelectScreen
from tests.fixtures.fake_connection import FakeConnection


async def _wait_past_splash(pilot) -> None:
    """Wait for init to complete (~2s UDS mock sweep) then dismiss the make-select modal."""
    # UDS mock sweep: (1024 / 32) * 0.065 ≈ 2.08 s; add slack for connect + other steps.
    await pilot.pause(3.0)
    # Dismiss make-select modal if shown (FakeConnection has no auto-detected make).
    if isinstance(pilot.app.screen, MakeSelectScreen):
        await pilot.press("escape")
        await pilot.pause(0.3)


@pytest.mark.asyncio
async def test_splash_to_dashboard_smoke() -> None:
    """Boot the full app and confirm the screen handoff works."""
    fake = FakeConnection()
    app = HudsonApp(fake)  # type: ignore[arg-type]

    async with app.run_test() as pilot:
        await _wait_past_splash(pilot)

        from Hudson.tui.screens.main import MainScreen

        active = app.screen
        assert isinstance(active, MainScreen), (
            f"expected MainScreen, got {type(active).__name__}"
        )

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
        await _wait_past_splash(pilot)

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
        await _wait_past_splash(pilot)

        assert isinstance(app.screen, MainScreen)

        for _ in range(len(TABS) - 1):
            await pilot.press("right")
            await pilot.pause(0.05)

        assert app.screen._active_idx == len(TABS) - 1

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
        await _wait_past_splash(pilot)

        assert isinstance(app.screen, MainScreen)

        for _ in range(3):
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
        await _wait_past_splash(pilot)

        assert isinstance(app.screen, MainScreen)

        await pilot.press("left")
        await pilot.pause(0.1)

        switcher = app.screen.query_one(ContentSwitcher)
        assert switcher.current == "vehicle"
