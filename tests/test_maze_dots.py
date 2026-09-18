"""
tests/test_maze_dots.py — per-maze dot detection: count rule, swap, and baselines.

Inputs:  DotDetector driven by hand against synthetic frames and a synthetic
         BeamsConfig carrying two maze captures
Outputs: assertions that a maze change never reads as a break, that a handful of
         dark dots busts the player while dozens is reported as a fault, and
         that an uncalibrated preset watches nothing
Invariant: detection is scoped to the dots captured while THAT maze was lit. A
           dot going dark because its relay opened is not in the watched set, so
           it cannot produce a break.
"""

from __future__ import annotations

import time

import cv2
import numpy as np
import pytest

import config.loader as loader
from vision.baseline import BaselineManager
from vision.detect import DotDetector

_LIT = 200.0        # baseline value that a painted dot comfortably exceeds
_W, _H = 320, 240


# ---------------------------------------------------------------------------
# Fixtures — a two-maze, two-camera capture built by hand
# ---------------------------------------------------------------------------

def _dots(cam: str, n: int, y: int) -> list[loader.Dot]:
    return [loader.Dot(id=f"{cam}:d{i}", cx=15 + i * 22, cy=y, r=6,
                       baseline=_LIT) for i in range(n)]


@pytest.fixture
def cfg():
    """Real config, with the mazes block replaced by a synthetic capture."""
    c = loader.load_all()
    c.beams.mazes = {
        "maze_1": loader.MazeROIs(name="maze_1", cameras={
            "SM-CAM-11": loader.CameraCapture("SM-CAM-11", _W, _H, {}, _dots("SM-CAM-11", 8, 60)),
            "SM-CAM-22": loader.CameraCapture("SM-CAM-22", _W, _H, {}, _dots("SM-CAM-22", 6, 120)),
        }),
        "maze_2": loader.MazeROIs(name="maze_2", cameras={
            "SM-CAM-11": loader.CameraCapture("SM-CAM-11", _W, _H, {}, _dots("SM-CAM-11", 4, 180)),
        }),
    }
    return c


def _detector(cfg, breaks, faults=None, samples=None):
    return DotDetector(
        cfg.beams,
        on_break=lambda did, ratio, ts: breaks.append(did),
        on_clear=lambda did, ts: None,
        metrics_emit=lambda *a, **k: None,
        on_fault=(lambda why, ts: faults.append(why)) if faults is not None else None,
        on_sample=(lambda did, v: samples.append((did, v))) if samples is not None else None,
    )


def _frame(lit: list[loader.Dot]) -> np.ndarray:
    """A black frame with the given dots painted red."""
    f = np.zeros((_H, _W, 3), dtype=np.uint8)
    for d in lit:
        cv2.circle(f, (d.cx, d.cy), d.r, (0, 0, 255), -1)
    return f


def _pump(d, frame, camera, frames, start=None):
    ts = start or time.monotonic_ns()
    for i in range(frames):
        d.process_frame(frame, ts + i * 40_000_000, camera)
    return ts + frames * 40_000_000


def _n(cfg) -> int:
    return cfg.beams.detection.consecutive_frames


# ---------------------------------------------------------------------------
# Which dots are watched
# ---------------------------------------------------------------------------

def test_set_maze_watches_that_mazes_dots(cfg):
    d = _detector(cfg, [])
    d.set_maze("maze_1", settle_ms=0)
    assert d.watching == 14
    d.set_maze("maze_2", settle_ms=0)
    assert d.watching == 4


def test_an_uncalibrated_preset_watches_nothing(cfg):
    """
    Silence is the safe failure. Without a capture there is no baseline, so
    every reading would be a guess, and a guess ends someone's run.
    """
    d = _detector(cfg, [])
    d.set_maze("maze_3", settle_ms=0)
    assert d.watching == 0
    d.set_maze(None, settle_ms=0)
    assert d.watching == 0


def test_masked_dots_are_never_watched(cfg):
    cfg.beams.mazes["maze_2"].cameras["SM-CAM-11"].dots[0].masked = True
    d = _detector(cfg, [])
    d.set_maze("maze_2", settle_ms=0)
    assert d.watching == 3


def test_frames_are_routed_by_camera(cfg):
    """
    An ROI is pixels in one camera's view. Sampling it against another
    camera's frame reads coordinates that mean nothing there.
    """
    breaks = []
    d = _detector(cfg, breaks)
    d.set_maze("maze_1", settle_ms=0)
    d.arm("run-1", grace_ms=0)

    cam1 = cfg.beams.mazes["maze_1"].cameras["SM-CAM-11"].dots
    # SM-CAM-11 fully lit; a black SM-CAM-22 frame must not darken SM-CAM-11's dots.
    _pump(d, _frame(cam1), "SM-CAM-11", _n(cfg) + 2)
    assert d.stats()["cameras"]["SM-CAM-22"]["dark"] == 0


# ---------------------------------------------------------------------------
# The count rule
# ---------------------------------------------------------------------------

def test_one_blocked_dot_is_a_break_only_below_the_cluster_threshold(cfg):
    """
    One dark dot busts when `min_simultaneous_breaks` is 1, and is IGNORED at 3.

    The shipped value is 3: a body crossing a curtain blocks several of its
    lasers at once, while a lone dot going dark is haze, a marginal r=4 dot or
    sensor noise — and in assisted mode each of those put the CONFIRM/VETO
    dialog in front of the GM mid-run. This pins both sides of the knob, since
    the single-dot contract changed deliberately rather than by accident.
    """
    dots = cfg.beams.mazes["maze_1"].cameras["SM-CAM-11"].dots

    cfg.beams.detection.min_simultaneous_breaks = 1
    breaks, faults = [], []
    d = _detector(cfg, breaks, faults)
    d.set_maze("maze_1", settle_ms=0)
    d.arm("run-1", grace_ms=0)
    _pump(d, _frame(dots[1:]), "SM-CAM-11", _n(cfg))       # one dark
    assert breaks == ["SM-CAM-11:d0"], "at a threshold of 1 a single dot must bust"
    assert faults == []

    cfg.beams.detection.min_simultaneous_breaks = 3
    breaks, faults = [], []
    d = _detector(cfg, breaks, faults)
    d.set_maze("maze_1", settle_ms=0)
    d.arm("run-2", grace_ms=0)
    _pump(d, _frame(dots[1:]), "SM-CAM-11", _n(cfg))       # the same one dark
    assert breaks == [], "at a threshold of 3 one dot must not raise the dialog"
    assert faults == []


def test_a_handful_of_dark_dots_is_still_a_break(cfg):
    """A body is wide. Three dark dots is a player, not a fault."""
    breaks, faults = [], []
    d = _detector(cfg, breaks, faults)
    d.set_maze("maze_1", settle_ms=0)
    d.arm("run-1", grace_ms=0)

    dots = cfg.beams.mazes["maze_1"].cameras["SM-CAM-11"].dots
    _pump(d, _frame(dots[3:]), "SM-CAM-11", _n(cfg))       # three dark

    assert len(breaks) == 1 and faults == []


def test_more_than_max_simultaneous_is_a_fault_not_a_break(cfg):
    """
    A body cannot extinguish the whole ceiling. That is a relay that did not
    fire, a preset change the detector was not told about, or a camera glitch.
    Reporting it as a break would end a run for a hardware fault.
    """
    cfg.beams.detection.max_simultaneous_breaks = 4
    breaks, faults = [], []
    d = _detector(cfg, breaks, faults)
    d.set_maze("maze_1", settle_ms=0)
    d.arm("run-1", grace_ms=0)

    black = np.zeros((_H, _W, 3), dtype=np.uint8)
    _pump(d, black, "SM-CAM-11", _n(cfg) + 2)              # all 8 of SM-CAM-11 dark

    assert breaks == [], "a dead maze must not bust the player"
    assert len(faults) == 1 and "8 dots dark" in faults[0]


def test_the_fault_is_reported_once_not_every_frame(cfg):
    cfg.beams.detection.max_simultaneous_breaks = 2
    breaks, faults = [], []
    d = _detector(cfg, breaks, faults)
    d.set_maze("maze_1", settle_ms=0)
    d.arm("run-1", grace_ms=0)

    _pump(d, np.zeros((_H, _W, 3), np.uint8), "SM-CAM-11", _n(cfg) + 20)
    assert len(faults) == 1


def test_all_dots_lit_is_no_event(cfg):
    breaks, faults = [], []
    d = _detector(cfg, breaks, faults)
    d.set_maze("maze_1", settle_ms=0)
    d.arm("run-1", grace_ms=0)

    dots = cfg.beams.mazes["maze_1"].cameras["SM-CAM-11"].dots
    _pump(d, _frame(dots), "SM-CAM-11", _n(cfg) + 2)

    assert breaks == [] and faults == []


def test_the_darkest_dot_is_the_one_reported(cfg):
    """Whatever a body is most squarely blocking is the best evidence crop."""
    # About which dot is picked, not about the cluster rule — pin the
    # threshold so the shipped value cannot change what this test means.
    cfg.beams.detection.min_simultaneous_breaks = 1
    breaks = []
    d = _detector(cfg, breaks)
    d.set_maze("maze_1", settle_ms=0)
    d.arm("run-1", grace_ms=0)

    dots = cfg.beams.mazes["maze_1"].cameras["SM-CAM-11"].dots
    f = _frame(dots[2:])
    cv2.circle(f, (dots[1].cx, dots[1].cy), dots[1].r, (0, 0, 90), -1)  # dim, not dark
    _pump(d, f, "SM-CAM-11", _n(cfg))

    assert breaks == ["SM-CAM-11:d0"], "d0 is fully dark, d1 only dim"


# ---------------------------------------------------------------------------
# The swap — the bug this whole design exists to prevent
# ---------------------------------------------------------------------------

def test_a_shape_change_produces_no_break(cfg):
    """
    Every dot of maze_1 goes dark because the relays opened. The new dot set
    does not contain them, so nothing asks about them.
    """
    breaks, faults = [], []
    d = _detector(cfg, breaks, faults)
    d.set_maze("maze_1", settle_ms=0)
    d.arm("run-1", grace_ms=0)

    m1 = cfg.beams.mazes["maze_1"].cameras["SM-CAM-11"].dots
    ts = _pump(d, _frame(m1), "SM-CAM-11", _n(cfg) + 2)

    d.set_maze("maze_2", settle_ms=0)                 # relays switch
    m2 = cfg.beams.mazes["maze_2"].cameras["SM-CAM-11"].dots
    _pump(d, _frame(m2), "SM-CAM-11", _n(cfg) + 2, start=ts)

    assert breaks == [] and faults == []


def test_settle_time_suppresses_frames_while_relays_switch(cfg):
    """Newly lit dots are still coming on; a dark reading then means nothing."""
    breaks = []
    d = _detector(cfg, breaks)
    d.arm("run-1", grace_ms=0)
    d.set_maze("maze_2", settle_ms=5000)

    _pump(d, np.zeros((_H, _W, 3), np.uint8), "SM-CAM-11", _n(cfg) + 5)
    assert breaks == []
    assert d.stats()["cameras"]["SM-CAM-11"]["dark"] == 0


def test_arming_clears_stale_dark_state(cfg):
    breaks = []
    d = _detector(cfg, breaks)
    d.set_maze("maze_2", settle_ms=0)
    _pump(d, np.zeros((_H, _W, 3), np.uint8), "SM-CAM-11", _n(cfg) + 2)   # unarmed
    assert d.stats()["cameras"]["SM-CAM-11"]["dark"] == 4

    d.arm("run-1", grace_ms=0)
    assert d.stats()["cameras"]["SM-CAM-11"]["dark"] == 0


# ---------------------------------------------------------------------------
# Suppression (invariant 5)
# ---------------------------------------------------------------------------

def test_a_stalled_camera_suppresses_everything(cfg):
    breaks, faults = [], []
    d = _detector(cfg, breaks, faults)
    d.set_maze("maze_2", settle_ms=0)
    d.arm("run-1", grace_ms=0)
    d.set_stalled(True)

    _pump(d, np.zeros((_H, _W, 3), np.uint8), "SM-CAM-11", _n(cfg) + 5)
    assert breaks == [] and faults == []


def test_unarmed_never_breaks(cfg):
    breaks = []
    d = _detector(cfg, breaks)
    d.set_maze("maze_2", settle_ms=0)
    _pump(d, np.zeros((_H, _W, 3), np.uint8), "SM-CAM-11", _n(cfg) + 5)
    assert breaks == []


def test_the_grace_period_is_the_callers_value(cfg):
    """arm()'s argument used to be logged and then dropped for the config value."""
    breaks = []
    d = _detector(cfg, breaks)
    d.set_maze("maze_2", settle_ms=0)
    d.arm("run-1", grace_ms=100_000)

    _pump(d, np.zeros((_H, _W, 3), np.uint8), "SM-CAM-11", _n(cfg) + 5)
    assert breaks == []


def test_a_dot_with_no_baseline_is_skipped(cfg):
    """Uncalibrated means unknown, and unknown must never bust anyone."""
    for dot in cfg.beams.mazes["maze_2"].cameras["SM-CAM-11"].dots:
        dot.baseline = 0.0
    breaks = []
    d = _detector(cfg, breaks)
    d.set_maze("maze_2", settle_ms=0)
    d.arm("run-1", grace_ms=0)

    _pump(d, np.zeros((_H, _W, 3), np.uint8), "SM-CAM-11", _n(cfg) + 5)
    assert breaks == []


# ---------------------------------------------------------------------------
# Baselines
# ---------------------------------------------------------------------------

def test_baselines_are_scoped_per_maze(cfg):
    """The same dot id is a different brightness under a different shape."""
    bm = BaselineManager(cfg.beams)
    bm.set_maze("maze_1")
    bm.set("SM-CAM-11:d0", 111.0)
    bm.set_maze("maze_2")
    bm.set("SM-CAM-11:d0", 222.0)

    bm.set_maze("maze_1")
    assert bm.get("SM-CAM-11:d0") == 111.0
    bm.set_maze("maze_2")
    assert bm.get("SM-CAM-11:d0") == 222.0


def test_the_ema_is_fed_only_between_runs(cfg):
    """Invariant: adapting mid-run slowly accepts a broken beam as normal."""
    samples = []
    d = _detector(cfg, [], samples=samples)
    d.set_maze("maze_2", settle_ms=0)

    dots = cfg.beams.mazes["maze_2"].cameras["SM-CAM-11"].dots
    _pump(d, _frame(dots), "SM-CAM-11", 2)
    assert samples, "ATTRACT must feed the rolling baseline"

    samples.clear()
    d.arm("run-1", grace_ms=0)
    _pump(d, _frame(dots), "SM-CAM-11", 2)
    assert samples == [], "a run must not move the baseline"


def test_a_frozen_baseline_never_moves(cfg):
    bm = BaselineManager(cfg.beams)
    bm.set_maze("maze_1")
    bm.freeze()
    for _ in range(500):
        bm.update_ema("SM-CAM-11:d0", 5.0)
    assert bm.get("SM-CAM-11:d0") == _LIT

    bm.unfreeze()
    for _ in range(500):
        bm.update_ema("SM-CAM-11:d0", 5.0)
    assert bm.get("SM-CAM-11:d0") < _LIT


def test_apply_baselines_carries_drift_across_a_maze_change(cfg):
    d = _detector(cfg, [])
    d.set_maze("maze_2", settle_ms=0)
    assert d.apply_baselines({"SM-CAM-11:d0": 90.0, "unknown:d9": 50.0}) == 1

    breaks = []
    d2 = _detector(cfg, breaks)
    d2.set_maze("maze_2", settle_ms=0)
    d2.apply_baselines({f"SM-CAM-11:d{i}": 20.0 for i in range(4)})
    d2.arm("run-1", grace_ms=0)

    # A dot painted at ~110 reads far above a 20.0 baseline: no break.
    dots = cfg.beams.mazes["maze_2"].cameras["SM-CAM-11"].dots
    _pump(d2, _frame(dots), "SM-CAM-11", _n(cfg) + 2)
    assert breaks == []


def test_arm_does_not_report_a_fault_for_the_maze_it_is_not_lighting(cfg):
    """
    ARM lights `arm_box` — three channels, so the player is boxed in rather
    than standing in the dark — while the detector stays pointed at the full
    maze, because preflight needs a calibrated dot count to check. Nearly every
    watched dot is therefore legitimately dark.

    Judging that was fatal: the mass-dark rule called it a hardware fault,
    vision dropped to manual and the FSM went ARM -> FAULT. Five consecutive
    test runs on the box never reached the count-in.
    """
    # Mirror the box: far more dark dots than the mass-dark limit. On the real
    # maze that is ~130 of 137 against a limit of 10.
    cfg.beams.detection.max_simultaneous_breaks = 5
    breaks, faults = [], []
    d = _detector(cfg, breaks, faults)
    d.set_maze("maze_1", settle_ms=0)
    # NOT armed — this is ARM, before GO.
    _pump(d, _frame([]), "SM-CAM-11", _n(cfg))       # every dot dark

    assert faults == [], f"ARM reported a hardware fault: {faults}"
    assert breaks == [], "an unarmed detector emitted a break"


def test_the_same_darkness_IS_a_fault_once_armed(cfg):
    """The rule itself is right — it just must not run before GO."""
    # The fixture camera has 8 dots; drop the limit below that so "all dark"
    # is genuinely a mass-dark rather than a large break.
    cfg.beams.detection.max_simultaneous_breaks = 5
    breaks, faults = [], []
    d = _detector(cfg, breaks, faults)
    d.set_maze("maze_1", settle_ms=0)
    d.arm("run-1", grace_ms=0)
    _pump(d, _frame([]), "SM-CAM-11", _n(cfg))       # every dot dark

    assert faults, "a genuine mass-dark during a run was not reported"
    assert breaks == [], "mass-dark must suppress, never bust"
