"""
tests/test_lights.py — DMX room lights and the state-driven cues.

Inputs:  HazerController with a lights config; LightCuePlayer with cues
Outputs: assertions on channel mapping, the entrance guard, fades, and the
         rule that a run is always dark
Invariant: the entrance is never switched off, and COUNTDOWN/RUN states are
           dark regardless of cue or GM override.
"""

from __future__ import annotations

import pytest

from iobackend.dmx import DmxController
from iobackend.lightshow import LightCuePlayer, _DARK_STATES

# A 3-channel decoder addressed at DMX 3: its own ch1/ch2/ch3 land on 3/4/5.
LIGHTS = {
    "fade_ms": 800,
    "fixtures": {
        "left": {"channel": 3, "default": 255},
        "right": {"channel": 4, "default": 255},
        "entrance": {"channel": 5, "default": 255, "always_on": True},
    },
}


@pytest.fixture
def dmx():
    h = DmxController(artnet_ip="127.0.0.1", universe=1, lights=LIGHTS)
    h._sock = _NullSocket()          # never touch the network in a test
    return h


class _NullSocket:
    def sendto(self, *a, **k): return 0
    def close(self): pass


# ---------------------------------------------------------------------------
# Channel mapping — one object owns the universe
# ---------------------------------------------------------------------------

def test_lights_share_the_hazers_universe(dmx):
    """
    Every Art-Net frame carries all 512 channels, so a second sender on this
    universe would zero these twice a second. That is why they live here.
    """
    st = dmx.lights_state()
    assert st["left"]["channel"] == 3
    assert st["right"]["channel"] == 4
    assert st["entrance"]["channel"] == 5


def test_the_frame_carries_hazer_and_lights_together(dmx):
    sent = []
    dmx._sock = type("S", (), {"sendto": lambda self, p, a: sent.append(p)})()
    dmx.set_light("left", 100, fade=False)
    assert sent, "a level change must transmit"
    # ArtDmx header is 18 bytes, then channel 1 at offset 18
    frame = sent[-1][18:]
    assert frame[0] == dmx.fan          # ch1 hazer fan
    assert frame[2] == 100              # ch3 left
    assert frame[4] == 255              # ch5 entrance


# ---------------------------------------------------------------------------
# The entrance is not a show effect
# ---------------------------------------------------------------------------

def test_the_entrance_cannot_be_switched_off(dmx):
    assert dmx.set_light("entrance", 0) is False
    assert dmx.light_level("entrance") == 255


def test_blackout_leaves_the_entrance_lit(dmx):
    """
    An Art-Net node holds the last frame it received, so this is the state the
    container is left in when the process exits. A dark box with people in it
    and no lit way out is the one state worth hard-coding against.
    """
    dmx.blackout()
    assert dmx.light_level("entrance") == 255
    assert dmx.light_level("left") == 0
    assert dmx.light_level("right") == 0


def test_maze_lights_off_spares_the_entrance(dmx):
    dmx.set_maze_lights(False, fade=False)
    assert dmx.light_level("entrance") == 255
    assert dmx.light_level("left") == 0


def test_an_unknown_light_is_rejected(dmx):
    assert dmx.set_light("ceiling", 128) is False


# ---------------------------------------------------------------------------
# Fading
# ---------------------------------------------------------------------------

def _ramp(dmx, name, to, seconds=1.2, hz=50.0):
    """Drive a fade against SIMULATED time, so the test is deterministic."""
    import time as _t
    t0 = _t.monotonic()
    dmx.set_light(name, to)
    out = []
    for i in range(int(seconds * hz)):
        dmx._step_fades(t0 + i / hz)
        out.append(dmx.light_level(name))
    return out


def test_a_fade_moves_towards_the_target_rather_than_snapping(dmx):
    out = _ramp(dmx, "left", 0)
    assert out[0] == 255, "a fade must not jump on the first tick"
    assert 0 < out[len(out) // 8] < 255, "it must be part-way early on"


def test_a_fade_reaches_its_target_and_stops(dmx):
    out = _ramp(dmx, "left", 0)
    assert out[-1] == 0
    import time as _t
    assert dmx._step_fades(_t.monotonic() + 99) is False, \
        "a settled fade must stop reporting movement"


def test_a_short_move_still_takes_the_whole_fade_time(dmx):
    """
    The regression that made the attract pulse look jumpy.

    The step used to be derived from full 0-255 travel and applied whatever the
    distance — so a 14->2 pulse, twelve levels, completed in ONE tick. It was
    not a coarse fade, it was no fade at all.
    """
    dmx.set_light("left", 14, fade=False)
    out = _ramp(dmx, "left", 2)
    steps = [abs(b - a) for a, b in zip(out, out[1:]) if b != a]
    assert max(steps) == 1, f"a 12-level move must not step by {max(steps)}"
    assert len(set(out)) >= 10, "it must pass through the intermediate levels"


def test_a_long_fade_uses_more_levels_than_a_short_one(dmx):
    """8-bit DMX cannot give 255 distinct levels in 800 ms; time is the dial."""
    dmx.set_light("left", 0, fade=False)
    short = len(set(_ramp(dmx, "left", 255)))
    dmx._fade_ms = 4000
    dmx.set_light("left", 0, fade=False)
    long_ = len(set(_ramp(dmx, "left", 255, seconds=4.5)))
    assert long_ > short


def test_fade_false_snaps(dmx):
    dmx.set_light("left", 42, fade=False)
    assert dmx.light_level("left") == 42


# ---------------------------------------------------------------------------
# Cues — the run must be dark
# ---------------------------------------------------------------------------

CUES = {
    "attract": {"loop": True, "steps": [{"left": 14, "right": 2, "ms": 10},
                                        {"left": 2, "right": 14, "ms": 10}]},
    "finished": {"steps": [{"left": 255, "right": 255, "ms": 10, "fade": False}]},
}


def test_every_run_state_is_dark_whatever_the_cue_says(dmx):
    """Ambient light raises the reading inside every dot's ROI, so a broken
    beam can still read above break_ratio — a MISSED break."""
    p = LightCuePlayer(dmx, {**CUES, "run_seg_1": {"steps": [{"left": 255, "ms": 10}]}})
    p.set_work_lights(False)
    for state in _DARK_STATES:
        dmx.set_light("left", 200, fade=False)
        p.set_state(state)
        assert dmx.light_level("left") == 0, f"{state} must be dark"
        assert dmx.light_level("right") == 0, f"{state} must be dark"


def test_a_run_is_dark_even_with_the_gm_work_lights_on(dmx):
    """A forgotten tap must not be able to invalidate a run."""
    p = LightCuePlayer(dmx, CUES)
    p.set_work_lights(True)
    assert dmx.light_level("left") == 255
    p.set_state("RUN_SEG_2")
    assert dmx.light_level("left") == 0
    assert p.work_lights is True, "the override is remembered, not cancelled"


def test_going_dark_is_instant_not_a_fade(dmx):
    """A fade into a run would leave the room lit for part of the countdown."""
    p = LightCuePlayer(dmx, CUES)
    p.set_work_lights(True)
    p.set_state("COUNTDOWN")
    assert dmx.light_level("left") == 0, "must already be there, not fading"


def test_work_lights_return_after_the_run(dmx):
    p = LightCuePlayer(dmx, CUES)
    p.set_work_lights(True)
    p.set_state("RUN_SEG_1")
    assert dmx.light_level("left") == 0
    p.set_state("RESULT")
    # Coming back up FADES, so the target is set immediately and the level
    # follows. Going dark is deliberately instant — see the dark-state tests.
    assert dmx.lights_state()["left"]["target"] == 255


def test_the_entrance_stays_lit_through_a_run(dmx):
    p = LightCuePlayer(dmx, CUES)
    p.set_work_lights(False)
    for state in ("ATTRACT", "COUNTDOWN", "RUN_SEG_1", "FINISHED"):
        p.set_state(state)
        assert dmx.light_level("entrance") == 255, f"entrance dark in {state}"


def test_work_lights_suspend_the_cue(dmx):
    p = LightCuePlayer(dmx, CUES)
    p.set_work_lights(False)
    p.set_state("ATTRACT")
    p.set_work_lights(True)
    assert dmx.light_level("left") == 255


def test_a_state_with_no_cue_goes_dark_rather_than_holding_the_last_one(dmx):
    p = LightCuePlayer(dmx, CUES)
    p.set_work_lights(False)
    p.set_state("FINISHED")
    p.set_state("SOME_STATE_NOBODY_WROTE_A_CUE_FOR")
    assert dmx.lights_state()["left"]["target"] == 0


def test_setting_the_same_state_twice_does_not_restart_the_cue(dmx):
    p = LightCuePlayer(dmx, CUES)
    p.set_work_lights(False)
    p.set_state("ATTRACT")
    dmx.set_light("left", 99, fade=False)
    p.set_state("ATTRACT")
    assert dmx.light_level("left") == 99, "a repeat state change must be a no-op"


# ---------------------------------------------------------------------------
# Haze duty cycle
# ---------------------------------------------------------------------------

def _duty(**kw):
    d = DmxController(artnet_ip="127.0.0.1", universe=1, default_haze=3,
                      lights=LIGHTS, **kw)
    d._sock = _NullSocket()
    return d


def test_haze_is_off_between_bursts():
    """Continuous output at any usable level is too much haze."""
    d = _duty(haze_burst_s=2.0, haze_interval_s=90.0)
    d._step_duty(0.0)
    assert d.hazing is True, "a cycle starts with its burst"
    d._step_duty(3.0)
    assert d.hazing is False


def test_the_duty_cycle_repeats():
    d = _duty(haze_burst_s=2.0, haze_interval_s=10.0)
    on = 0.0
    dt = 0.04
    for _ in range(int(60 / dt)):
        d._step_duty(dt)
        if d.hazing:
            on += dt
    assert 0.15 < on / 60 < 0.25, f"expected ~20% duty, got {on/60:.0%}"


def test_zero_interval_means_continuous():
    """The old behaviour, still reachable."""
    d = _duty(haze_burst_s=0.0, haze_interval_s=0.0)
    d._step_duty(0.04)
    assert d.hazing is True
    d._step_duty(999.0)
    assert d.hazing is True


def test_the_gm_switch_beats_the_duty_cycle():
    d = _duty(haze_burst_s=2.0, haze_interval_s=10.0)
    d._step_duty(0.0)
    assert d.hazing is True
    d.set_enabled(False)
    assert d.hazing is False, "the GM switch must win mid-burst"


def test_the_blower_never_stops_with_the_haze():
    """Stopping the blower lets settled haze pool unevenly."""
    d = _duty(haze_burst_s=1.0, haze_interval_s=10.0)
    d.set_enabled(False)
    assert d.fan > 0


def test_duty_can_be_retuned_live():
    d = _duty(haze_burst_s=2.0, haze_interval_s=90.0)
    d.set_duty(burst_s=5.0, interval_s=30.0)
    assert d.duty["burst_s"] == 5.0 and d.duty["interval_s"] == 30.0
