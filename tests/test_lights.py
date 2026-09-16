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


def test_work_lights_return_in_attract_not_over_the_result(dmx):
    """
    The work lights come back when the player is walking OUT, not the instant
    the run ends.

    They used to return at the outcome, which meant a GM who had the house
    lights switched on got a flat 255 over the top of the finished/busted cue:
    the room lit up at the end of every run, stayed lit through the result and
    back into attract, and only went dark again when the next run forced it.
    """
    # A distinctive level, so "the cue played" is not confusable with the
    # work lights' own 255.
    cues = {**CUES,
            "finished": {"steps": [{"left": 150, "right": 150, "ms": 10}]},
            "result": {"steps": [{"left": 0, "right": 0, "ms": 0}]}}
    p = LightCuePlayer(dmx, cues)
    p.set_work_lights(True)
    p.set_state("RUN_SEG_1")
    assert dmx.light_level("left") == 0

    p.set_state("FINISHED")
    assert dmx.lights_state()["left"]["target"] == 150, \
        "the work lights painted over the outcome cue"
    p.set_state("RESULT")
    assert dmx.lights_state()["left"]["target"] == 0, \
        "the work lights painted over the result"

    p.set_state("ATTRACT")
    # Coming back up FADES, so the target is set immediately and the level
    # follows. Going dark is deliberately instant — see the dark-state tests.
    assert dmx.lights_state()["left"]["target"] == 255, \
        "the work lights never came back for load-out"


def test_an_aborted_run_still_gets_the_gm_work_light(dmx):
    """ABORTED is not an outcome to be read — it is somebody coming out of a
    dark container early, and the GM's light should win there."""
    p = LightCuePlayer(dmx, CUES)
    p.set_work_lights(True)
    p.set_state("RUN_SEG_2")
    p.set_state("ABORTED")
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


# ---------------------------------------------------------------------------
# The cue must follow EVERY state change
# ---------------------------------------------------------------------------

def test_a_state_the_player_never_heard_about_leaves_the_room_dark(dmx):
    """
    Why the attract pulse did not run after a force reset.

    RESET is transient: the runner sets self.state = ATTRACT directly and
    executes its own side effects rather than going through dispatch(). The cue
    hook lived only in dispatch, so the player's last known state stayed RESET —
    a state with no cue — and fell through to all-off. Nothing looked broken:
    the FSM was in ATTRACT, the WS said ATTRACT, and the room was simply dark.
    """
    p = LightCuePlayer(dmx, CUES)
    p.set_work_lights(False)
    p.set_state("RESET")                       # no cue for this
    # Target, not level: leaving a non-run state FADES to dark, so the level
    # follows over fade_ms. Only COUNTDOWN and the RUN states snap.
    assert dmx.lights_state()["left"]["target"] == 0

    p.set_state("ATTRACT")                     # the hop the runner must report
    assert dmx.lights_state()["left"]["target"] > 0


def test_the_attract_cue_alternates_the_two_sides(dmx):
    """Left and right must not move together — the whole point of the pulse."""
    cue = CUES["attract"]["steps"]
    a, b = cue[0], cue[1]
    assert (a["left"] > a["right"]) != (b["left"] > b["right"]), \
        "the two steps must swap which side is brighter"


def test_work_lights_off_in_attract_starts_the_cue_not_darkness(dmx):
    p = LightCuePlayer(dmx, CUES)
    p.set_state("ATTRACT")
    p.set_work_lights(True)
    assert dmx.light_level("left") == 255
    p.set_work_lights(False)
    # Without a running loop the player applies the cue's first step, which is
    # enough to prove it chose the cue rather than all-off.
    assert dmx.lights_state()["left"]["target"] == CUES["attract"]["steps"][0]["left"]


# ---------------------------------------------------------------------------
# Show pass — entrance control and the MASTER override
# ---------------------------------------------------------------------------

def test_the_entrance_goes_dark_once_we_leave_attract(dmx):
    """It leaks straight down the container.

    Everything from sign-in to the result is meant to be read by laser light,
    so the entrance is dark there — and lit again in ATTRACT, MASTER and FAULT,
    the states where somebody is walking in or out.
    """
    import config.loader as loader
    from iobackend.lightshow import LightCuePlayer

    cues = loader.load_all().mazes.light_cues
    p = LightCuePlayer(dmx, cues)
    p.set_work_lights(None)

    p.set_state("ATTRACT")
    assert dmx.lights_state()["entrance"]["target"] > 0, "attract should be lit"
    for state in ("REGISTERED", "ARM", "RUN_SEG_1", "RESULT"):
        p.set_state(state)
        assert dmx.lights_state()["entrance"]["target"] == 0, (
            f"the entrance is still leaking in {state}")
    p.set_state("FAULT")
    assert dmx.lights_state()["entrance"]["target"] > 0, "fault must light the way out"


def test_the_gm_can_black_out_master_mode(dmx):
    """MASTER's cue is full working light, which used to override the switch —
    so there was no way to stand in the container and look at the lasers."""
    import config.loader as loader
    from iobackend.lightshow import LightCuePlayer

    p = LightCuePlayer(dmx, loader.load_all().mazes.light_cues)
    p.set_state("MASTER")
    p.set_work_lights(True)
    assert dmx.lights_state()["left"]["target"] == 255
    p.set_work_lights(False)
    st = dmx.lights_state()
    assert st["left"]["target"] == 0 and st["right"]["target"] == 0
    assert st["entrance"]["target"] == 0, "the entrance should follow the house lights"


def test_a_dying_process_still_relights_the_way_out(dmx):
    """Cues may take the entrance dark; a blackout must give it back."""
    dmx.set_light("entrance", 0, fade=False, allow_always_on=True)
    assert dmx.lights_state()["entrance"]["level"] == 0
    dmx.blackout()
    assert dmx.lights_state()["entrance"]["level"] > 0


def test_the_entrance_comes_back_when_the_run_ends(dmx):
    """The cue table is the ONLY thing that sets the entrance now.

    A state that does not name it inherits whatever the last one left, and
    every state from sign-in to the result deliberately sets it to 0 — so
    without an explicit value in the ATTRACT cue the first run of the day took
    the entrance dark and it never came back.
    """
    import config.loader as loader
    from iobackend.lightshow import LightCuePlayer

    p = LightCuePlayer(dmx, loader.load_all().mazes.light_cues)
    p.set_work_lights(None)
    for state in ("ATTRACT", "REGISTERED", "ARM", "COUNTDOWN",
                  "RUN_SEG_1", "FINISHED", "RESULT", "RESET"):
        p.set_state(state)
    p.set_state("ATTRACT")
    assert dmx.lights_state()["entrance"]["target"] > 0, (
        "the entrance stayed dark after a run — it is the way in")


def test_arm_box_lights_row_one_not_row_three():
    """`arm_box` holds BEAM ids 12/13/14 translated to global channels.

    A beam id encodes row+strip and maps to a per-board relay, so the global
    channel is board_index*16 + relay_channel. Using the ids as channel numbers
    lit beams 32/33/34 — row 3, the far end of the container — instead of row 1
    where the player is standing.
    """
    import json
    from pathlib import Path
    import yaml
    import config.loader as loader

    root = Path(__file__).resolve().parent.parent
    beams = json.loads((root / "config" / "beams.json").read_text())["beams"]
    boards = [b["id"] for b in
              yaml.safe_load((root / "config" / "hardware.yaml").read_text())["relay_boards"]]
    idx = {b: i for i, b in enumerate(boards)}
    expected = sorted(idx[e["board_id"]] * 16 + e["relay_channel"]
                      for e in beams if e["id"] in ("12", "13", "14"))

    got = sorted(loader.load_all().mazes.presets["arm_box"].channels)
    assert got == expected, f"arm_box is {got}, beams 12/13/14 are {expected}"
    # And they must all be in row 1, which is what "boxed in at the plate" means.
    rows = {e["row"] for e in beams if e["id"] in ("12", "13", "14")}
    assert rows == {1}, f"arm_box beams are not all row 1: {rows}"


def test_the_outcome_flashes_then_holds_the_verdict():
    """
    Stop pressed -> room and maze flash together -> the verdict shape stays lit
    over a very dim room until RESET swaps in the attract show.

    Two earlier versions of this were wrong in opposite directions. First the
    shows looped a solid preset, and since nothing between the outcome and
    RESET stops a show (FINISHED emits only BroadcastState) the maze stayed lit
    for result_display_ms TWICE over with the room at 0. Then they ended on
    blackout, which killed the verdict entirely. The shape is the result and
    has to survive the whole result window; the room is what comes down.
    """
    from config import loader
    cfg = loader.load_all().mazes
    verdict = {"clean": "vertical", "bust": "horizontal"}

    for name, shape in verdict.items():
        show = cfg.shows[name]
        assert show.loop is False, \
            f"{name} loops, so the reconciler never gets to hold it still"
        # A show that ends leaves its last preset applied — so the LAST step is
        # the hold, and it has to be the verdict rather than darkness.
        assert show.steps[-1].preset == shape, \
            f"{name} ends on {show.steps[-1].preset!r}, so the verdict vanishes"
        # ...and it has to actually flash on the way there.
        assert sum(1 for st in show.steps if st.preset == "blackout") >= 2, \
            f"{name} does not flash before it settles"

    # The room settles very dim, NOT off, and RESULT must match what
    # finished/busted settled at — they are one continuous moment to a player.
    levels = {}
    for name in ("finished", "busted", "result"):
        last = cfg.light_cues[name]["steps"][-1]
        levels[name] = (last["left"], last["right"])
        assert 0 < last["left"] <= 20, \
            f"{name} settles at {last['left']}, which is not 'very dim'"
    assert levels["finished"] == levels["result"] == levels["busted"], \
        f"the room changes level partway through the result: {levels}"


def test_the_override_defaults_to_the_cue_table_not_to_on(dmx):
    """
    A fresh player hands the room to the cue table.

    It defaulted True, so on a fresh boot the cue table never ran at all until
    somebody pressed the GM switch off — no attract breathing, no outcome
    settle, just a flat 255 in every state that is not forced dark.
    """
    p = LightCuePlayer(dmx, CUES)
    assert p.work_lights is None, "the work-light override is on by default"
    p.set_state("ATTRACT")
    assert dmx.lights_state()["left"]["target"] != 255, \
        "attract is at full work light on a fresh boot"
