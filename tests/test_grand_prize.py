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


# ===========================================================================
# Tuning the fill light from the GM console
# ===========================================================================

def test_set_run_level_applies_to_the_state_we_are_already_in():
    """
    The whole point of the console control: it lands during the run you are
    watching, not at the next transition. A level that waited would be
    untunable, because the thing you are tuning against is the camera.
    """
    from iobackend.dmx import DmxController
    from iobackend.lightshow import LightCuePlayer

    dmx = DmxController(artnet_ip="127.0.0.1", universe=1, lights={
        "fade_ms": 800,
        "fixtures": {"left": {"channel": 3, "default": 255},
                     "right": {"channel": 4, "default": 255},
                     "entrance": {"channel": 5, "default": 255,
                                  "always_on": True}}})
    dmx._sock = type("S", (), {"sendto": lambda s, *a: 0, "close": lambda s: None})()

    p = LightCuePlayer(dmx, {}, run_level=2)
    p.set_state("RUN_SEG_2")
    assert dmx.light_level("left") == 2

    assert p.set_run_level(5) == 5
    assert dmx.light_level("left") == 5, "must land without a state change"
    assert dmx.light_level("entrance") == 0, "the entrance is never the fill"

    assert p.set_run_level(0) == 0
    assert dmx.light_level("left") == 0, "0 puts the run back to dark"


def test_set_run_level_clamps_whatever_it_is_given():
    """The console is not the only caller; tools/ and tests reach it too."""
    from iobackend.lightshow import LightCuePlayer
    p = LightCuePlayer(object(), {})
    assert p.set_run_level(900) == 255
    assert p.set_run_level(-5) == 0
    assert p.set_run_level("bright") == 0


def test_the_run_light_body_is_bounded_to_the_documented_band():
    """
    0-16, not the DMX 0-255.

    The ceiling is the failure mode worth guarding: anything past ~10 on these
    decoders is the house lights up through a run.
    """
    import pydantic
    from web.routes_admin import RunLightBody

    assert RunLightBody(level=0).level == 0
    assert RunLightBody(level=16).level == 16
    for bad in (-1, 17, 255):
        with pytest.raises(pydantic.ValidationError):
            RunLightBody(level=bad)


def test_persisting_the_level_keeps_the_comments(tmp_path):
    """
    A yaml round-trip would delete most of hardware.yaml.

    The file is mostly prose — the camera path notes, the haze dose reasoning,
    the decoder range — and losing it to a light nudge would be a silent,
    unreviewable deletion of the reasoning the next person needs.
    """
    import re
    src = (loader.CONFIG_DIR / "hardware.yaml").read_text()
    pattern = re.compile(r"^(\s*run_level:[ \t]*)(\d+)(.*)$", re.M)
    assert len(pattern.findall(src)) == 1, "exactly one run_level line to edit"

    out = pattern.sub(lambda m: f"{m.group(1)}7{m.group(3)}", src, count=1)
    assert "run_level: 7" in out
    assert out.count("#") == src.count("#"), "every comment survives"
    assert len(out.splitlines()) == len(src.splitlines())

    # And it still loads, with the new value.
    (tmp_path / "hardware.yaml").write_text(out)
    for name in ("game.yaml", "mazes.yaml", "beams.json"):
        shutil.copy(loader.CONFIG_DIR / name, tmp_path / name)
    original = loader.CONFIG_DIR
    try:
        loader.CONFIG_DIR = tmp_path
        hw = loader.load_hardware()
    finally:
        loader.CONFIG_DIR = original
    assert (hw.hazer or {})["lights"]["run_level"] == 7


@pytest.mark.asyncio
async def test_the_run_light_route_is_registered():
    """The console POSTs it; a 404 would read as a dead button."""
    import web.routes_admin as ra
    import inspect
    src = inspect.getsource(ra.register_routes)
    assert '"/api/gm/run-light"' in src
    # Unauthenticated on purpose, like the rest of /api/gm/* — assert it did
    # not accidentally pick up the admin dependency the routes around it use.
    i = src.index('"/api/gm/run-light"')
    assert "_require_admin" not in src[i - 200:i + 200]


def test_the_console_has_a_run_light_stepper():
    """Cheap guard on a frontend with no build step: button, handler, both."""
    from pathlib import Path
    html = (Path(__file__).resolve().parents[1]
            / "web/static/gm/index.html").read_text()
    for needle in ('id="hRunLightUp"', 'id="hRunLightDown"',
                   'id="hRunLightValue"', '"/api/gm/run-light"',
                   "nudgeRunLight(+1)", "nudgeRunLight(-1)"):
        assert needle in html, needle


def test_the_sealed_run_scene_scales_the_segment_up():
    """
    Sealed, the segment is the RUN scene's only content.

    It is sized as a subtitle for life under a 520px clock; left that way it
    was unreadable from the far end of a dark container.
    """
    from pathlib import Path
    html = (Path(__file__).resolve().parents[1]
            / "web/static/display_in/index.html").read_text()
    assert "body.hide-times .segment {" in html
    assert "font-size: clamp(40px, 8.5vw, 200px);" in html
