"""
tests/test_grand_prize.py — the grand-prize branch's three changes.

Inputs:  config/loader.py over a temp config dir; the GameRunner fixture
Outputs: assertions on the display seal (display.hide_times), the fill-light
         level (hazer.lights.run_level) and the GM's manual start plate
Invariant: all three default to OFF. A checkout that says nothing about them
           behaves exactly like normal play — times on screen, runs dark, the
           plate the only way into ARM. Each is opt-in from config or a tap.

The seal is a DISPLAY property, never a game one: the stopwatch still runs and
the run row still records both elapsed_ms and raw_elapsed_ms, because the
operator has to be able to settle the prize afterwards. The tests below assert
that too — hiding a time and not measuring it are different bugs.
"""

from __future__ import annotations

import asyncio
import shutil

import pytest
import yaml

import config.loader as loader
from config.loader import DisplayConfig, GameConfig, _validate_hazer
from core.events import ARM, REGISTERED, RUN_SEG_2, PlateHigh
from core.runner import GameRunner


# ---------------------------------------------------------------------------
# Helper: run the real loader over a doctored copy of the real config
# ---------------------------------------------------------------------------

def _load_game_with(tmp_path, mutate) -> GameConfig:
    """
    Copy config/ to tmp_path, let `mutate` edit the parsed game.yaml, reload.

    Copies the whole directory rather than writing a minimal game.yaml:
    load_game() cross-checks count_in.preset against mazes.yaml, so a stub
    would be testing the stub.
    """
    src = loader.CONFIG_DIR
    for name in ("game.yaml", "hardware.yaml", "mazes.yaml", "beams.json"):
        shutil.copy(src / name, tmp_path / name)

    path = tmp_path / "game.yaml"
    with open(path) as f:
        d = yaml.safe_load(f)
    mutate(d)
    path.write_text(yaml.safe_dump(d))

    original = loader.CONFIG_DIR
    try:
        loader.CONFIG_DIR = tmp_path
        return loader.load_game()
    finally:
        loader.CONFIG_DIR = original


# ===========================================================================
# The seal — display.hide_times
# ===========================================================================

def test_the_seal_is_off_unless_asked_for():
    """
    The default is normal play.

    A branch is a bad place to keep a default. Anyone merging this, or reading
    config/loader.py on its own, gets times on screen until game.yaml says
    otherwise.
    """
    assert DisplayConfig().hide_times is False


def test_a_config_with_no_display_block_shows_times(tmp_path):
    assert _load_game_with(tmp_path, lambda d: d.pop("display", None)) \
        .display.hide_times is False


def test_the_seal_parses(tmp_path):
    cfg = _load_game_with(tmp_path, lambda d: d.update(
        display={"hide_times": True}))
    assert cfg.display.hide_times is True


def test_a_junk_seal_value_does_not_stop_the_box_booting(tmp_path):
    """
    A typo must not be a crash at a venue.

    Same reasoning the audio and hazer blocks are parsed under: config is
    edited on site, in a hurry, by whoever is there.
    """
    cfg = _load_game_with(tmp_path, lambda d: d.update(display={"hide_times": "yes"}))
    assert cfg.display.hide_times is True
    cfg = _load_game_with(tmp_path, lambda d: d.update(display=None))
    assert cfg.display.hide_times is False


@pytest.mark.asyncio
async def test_the_state_broadcast_carries_the_seal(runner):
    """
    The displays learn about it from the broadcast, not from a page reload.

    Both display pages ship sealed in their markup and unseal only on a
    message that says false, so this field is what lets a screen come back to
    normal play without anyone touching the kiosk.
    """
    assert runner._get_state_message()["hide_times"] is False

    runner.config.game.display.hide_times = True
    assert runner._get_state_message()["hide_times"] is True


@pytest.mark.asyncio
async def test_sealing_the_screens_does_not_stop_the_clock(runner):
    """
    Hiding a time and not measuring one are different bugs.

    The whole point of a sealed final is that the operator reads the times off
    the console afterwards and awards the prize. If the seal reached the
    stopwatch, the prize could not be settled at all.
    """
    runner.config.game.display.hide_times = True
    runner.stopwatch.start()
    await asyncio.sleep(0.02)
    msg = runner._get_state_message()
    assert msg["hide_times"] is True
    assert msg["elapsed_ms"] > 0, "the server clock still runs while sealed"
    assert msg["raw_elapsed_ms"] is not None


# ===========================================================================
# The fill light — hazer.lights.run_level
# ===========================================================================

def test_the_fill_level_is_range_checked():
    """
    Checked against the documented band, not the full DMX one.

    _ranged clamps, so the ceiling IS the failure mode: against 0-255 a
    fat-fingered `run_level: 200` would clamp to 255 and put the house lights
    up through every run. 16 is already past full on these decoders, so the
    clamp lands somewhere bright but survivable.
    """
    out = _validate_hazer({"artnet_ip": "1.2.3.4",
                           "lights": {"run_level": 900}})
    assert out["lights"]["run_level"] == 16, "clamped to the documented band"
    out = _validate_hazer({"artnet_ip": "1.2.3.4",
                           "lights": {"run_level": 2}})
    assert out["lights"]["run_level"] == 2


def test_a_junk_fill_level_leaves_the_run_dark():
    """Not a number is the one case that can fall all the way back to 0."""
    out = _validate_hazer({"artnet_ip": "1.2.3.4",
                           "lights": {"run_level": "bright"}})
    assert out["lights"]["run_level"] == 0


def test_a_hazer_block_with_no_fill_level_is_left_alone():
    out = _validate_hazer({"artnet_ip": "1.2.3.4", "lights": {"fade_ms": 800}})
    assert "run_level" not in out["lights"]


# ===========================================================================
# The GM's manual start plate
# ===========================================================================

@pytest.mark.asyncio
async def test_the_gm_plate_action_sends_the_plate_event(runner):
    """
    The same event the Opta sends, not a second way into ARM.

    That is the whole design: the FSM has one PlateHigh handler, in REGISTERED,
    and it stays the only way in. A new event type would have needed its own
    handler in every state that must ignore it.
    """
    runner.on_gm_action("plate", {})
    event = runner._event_queue.get_nowait()
    assert isinstance(event, PlateHigh)


@pytest.mark.asyncio
async def test_the_gm_plate_action_arms_a_signed_in_player(runner):
    """REGISTERED → ARM, which is the transition the real plate is wired to."""
    runner.state = REGISTERED
    runner.on_gm_action("plate", {})
    await runner.dispatch(runner._event_queue.get_nowait())
    assert runner.state == ARM


@pytest.mark.asyncio
async def test_the_gm_plate_action_cannot_touch_a_run(runner):
    """
    It is live in REGISTERED only, and the FSM is the thing enforcing that.

    The console greys the button out everywhere else, but a greyed-out button
    is a courtesy — the iPad can be stale, and /api/gm/plate is reachable from
    anything on the venue network. A mis-tap mid-run has to be a no-op in the
    state machine, not just in the markup.
    """
    for state in (RUN_SEG_2, "COUNTDOWN", "ATTRACT", "MASTER", "FINISHED"):
        runner.state = state
        runner.on_gm_action("plate", {})
        await runner.dispatch(runner._event_queue.get_nowait())
        assert runner.state == state, f"PlateHigh moved the FSM out of {state}"


@pytest.mark.asyncio
async def test_an_unknown_gm_action_is_still_rejected(runner):
    """
    The new branch in on_gm_action must not have swallowed the else.

    A silently-accepted typo in an action string is a console button that does
    nothing and reports success.
    """
    runner.on_gm_action("plaet", {})
    assert runner._event_queue.empty()


@pytest.mark.asyncio
async def test_the_plate_route_is_registered():
    """The console POSTs /api/gm/plate; a 404 would show as a failed tap."""
    from fastapi import APIRouter
    from web.routes_gm import register_routes

    router = APIRouter()
    register_routes(router, lambda action, payload: None)
    paths = {r.path for r in router.routes}
    assert "/api/gm/plate" in paths


@pytest.mark.asyncio
async def test_the_plate_route_dispatches_the_plate_action(runner):
    """End to end from the HTTP handler to a PlateHigh on the queue."""
    from fastapi import APIRouter
    from web.routes_gm import register_routes

    router = APIRouter()
    register_routes(router, runner.on_gm_action)
    handler = next(r.endpoint for r in router.routes
                   if r.path == "/api/gm/plate")
    assert (await handler())["ok"] is True
    assert isinstance(runner._event_queue.get_nowait(), PlateHigh)


# ===========================================================================
# The GM console markup — the button exists and is wired
# ===========================================================================

def test_the_console_has_a_plate_button():
    """
    Cheap guard on a single-file frontend with no build step and no unit tests.

    It catches the two ways this silently half-lands: a button with no handler,
    and a handler for a button that was renamed.
    """
    from pathlib import Path
    html = (Path(__file__).resolve().parents[1]
            / "web/static/gm/index.html").read_text()
    assert 'id="btnPlate"' in html
    assert 'gmPost("plate")' in html
    # Seven slots now, and the row is fixed-width on purpose: a control that
    # moves between two broadcasts moves under the operator's thumb.
    assert "grid-template-columns: repeat(7, 1fr)" in html


@pytest.mark.parametrize("page", ["display_in", "display_out"])
def test_both_displays_ship_sealed(page):
    """
    <body> carries the class and JS only ever removes it.

    This is the property that makes a kiosk safe to restart mid-final: a page
    that has not heard from the server yet shows no time, and a dropped
    WebSocket fails sealed rather than revealing one.
    """
    from pathlib import Path
    html = (Path(__file__).resolve().parents[1]
            / f"web/static/{page}/index.html").read_text()
    assert '<body class="hide-times">' in html
    assert "body.hide-times .sealed-hide { display: none !important; }" in html
    assert "applySeal(!!msg.hide_times)" in html


def test_the_outdoor_leaderboard_is_part_of_the_seal():
    """A ranked board with blanked times still publishes the order."""
    from pathlib import Path
    html = (Path(__file__).resolve().parents[1]
            / "web/static/display_out/index.html").read_text()
    assert '<div id="leaderboard" class="sealed-hide">' in html
