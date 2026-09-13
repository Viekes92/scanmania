"""
tests/test_watchlist.py — a maze shape change must never read as a beam break.

Inputs:  DotDetector driven by hand; watch-lists joined from mazes.yaml + beams.json
Outputs: assertions that dropped channels are ignored, continuing channels stay
         live, and newly-lit channels are graced
Invariant: when the maze switches, the watch-list switches with it. A dot that
           goes dark because its relay opened is not being looked at, so it
           cannot produce a break.
"""

from __future__ import annotations

import time

import numpy as np
import pytest

import config.loader as loader
from vision.detect import DotDetector


@pytest.fixture
def cfg():
    return loader.load_all()


def _detector(cfg, breaks, faults=None):
    return DotDetector(
        cfg.beams,
        on_break=lambda bid, ratio, ts: breaks.append(bid),
        on_clear=lambda bid, ts: None,
        metrics_emit=lambda *a, **k: None,
        on_fault=(lambda bid, ts: faults.append(bid)) if faults is not None else None,
    )


# ---------------------------------------------------------------------------
# The join
# ---------------------------------------------------------------------------

def test_watchlists_are_built_from_the_two_existing_files(cfg):
    """No new config to author — mazes.yaml joined with beams.json."""
    assert cfg.watchlists["blackout"] == frozenset()
    assert len(cfg.watchlists["all_on"]) == len(cfg.beams.beams)
    for shape in ("maze_1", "maze_2", "maze_3"):
        assert cfg.watchlists[shape], f"{shape} lit no channels"


def test_watchlist_ids_are_real_channels(cfg):
    known = {b.id for b in cfg.beams.beams}
    for name, ids in cfg.watchlists.items():
        assert ids <= known, f"{name} references unknown channel ids"


# ---------------------------------------------------------------------------
# Swapping the list
# ---------------------------------------------------------------------------

def test_channels_leaving_the_shape_are_not_watched(cfg):
    d = _detector(cfg, [])
    d.set_watchlist(cfg.watchlists["maze_1"])
    leaving = next(iter(cfg.watchlists["maze_1"] - cfg.watchlists["maze_2"]))

    d.set_watchlist(cfg.watchlists["maze_2"])

    assert d._is_watched(leaving, time.monotonic_ns()) is False


def test_channels_entering_the_shape_are_graced(cfg):
    d = _detector(cfg, [])
    d.set_watchlist(cfg.watchlists["maze_1"])
    entering = next(iter(cfg.watchlists["maze_2"] - cfg.watchlists["maze_1"]))

    d.set_watchlist(cfg.watchlists["maze_2"], settle_ms=200)
    now = time.monotonic_ns()

    assert d._is_watched(entering, now) is False, "newly-lit channel reported too early"
    assert d._is_watched(entering, now + 300 * 1_000_000) is True


def test_channels_in_both_shapes_are_never_interrupted(cfg, monkeypatch):
    """
    The point of the design. At ~80% overlap most of the maze keeps being
    watched straight through a transition, so a real break during the switch is
    still caught.
    """
    a = set(list(cfg.watchlists["all_on"])[:30])
    b = set(list(cfg.watchlists["all_on"])[10:40])
    continuing = a & b
    assert continuing, "fixture must overlap for this to prove anything"

    d = _detector(cfg, [])
    d.set_watchlist(a)
    d.set_watchlist(b, settle_ms=5000)

    now = time.monotonic_ns()
    for cid in continuing:
        assert d._is_watched(cid, now) is True, f"{cid} was paused despite staying lit"


def test_none_means_watch_everything(cfg):
    d = _detector(cfg, [])
    d.set_watchlist(cfg.watchlists["maze_1"])
    d.set_watchlist(None)
    for beam in cfg.beams.beams:
        assert d._is_watched(beam.id, time.monotonic_ns()) is True


# ---------------------------------------------------------------------------
# The group rule — a body blocks part of an array, a fault kills all of it
# ---------------------------------------------------------------------------

def _five_dot_channel(cfg, lit_value=200.0):
    """Give one channel 5 dots with real baselines, on a synthetic frame."""
    beam = cfg.beams.beams[0]
    frame = np.zeros((240, 320, 3), dtype=np.uint8)
    beam.dots = [loader.DotROI(cx=20 + i * 40, cy=100, r=6, baseline=lit_value)
                 for i in range(5)]
    return beam, frame


def _light(frame, dot, value=255):
    """Paint a red dot so the red-isolation sample reads high."""
    import cv2
    cv2.circle(frame, (dot.cx, dot.cy), dot.r, (0, 0, value), -1)


def test_one_blocked_dot_in_five_is_a_break(cfg):
    breaks, faults = [], []
    beam, frame = _five_dot_channel(cfg)
    d = _detector(cfg, breaks, faults)
    d.set_watchlist([beam.id], settle_ms=0)
    d.arm("run-1", grace_ms=0)

    for dot in beam.dots[1:]:          # four lit, one dark
        _light(frame, dot)

    ts = time.monotonic_ns()
    for i in range(cfg.beams.detection.consecutive_frames):
        d.process_frame(frame, ts + i * 40_000_000, beam.camera)

    assert breaks == [beam.id], "a single blocked laser must still bust"
    assert faults == []


def test_all_five_dark_is_a_fault_not_a_break(cfg):
    """
    A body cannot extinguish five colinear dots at once. That is the relay
    failing, the PSU dropping, or the view being occluded — reporting it as a
    break would end a run for a hardware fault.
    """
    breaks, faults = [], []
    beam, frame = _five_dot_channel(cfg)
    d = _detector(cfg, breaks, faults)
    d.set_watchlist([beam.id], settle_ms=0)
    d.arm("run-1", grace_ms=0)

    ts = time.monotonic_ns()
    for i in range(cfg.beams.detection.consecutive_frames + 2):
        d.process_frame(frame, ts + i * 40_000_000, beam.camera)   # nothing lit

    assert breaks == [], "a dead channel must not bust the player"
    assert faults == [beam.id]


def test_all_dots_lit_is_no_event(cfg):
    breaks, faults = [], []
    beam, frame = _five_dot_channel(cfg)
    d = _detector(cfg, breaks, faults)
    d.set_watchlist([beam.id], settle_ms=0)
    d.arm("run-1", grace_ms=0)

    for dot in beam.dots:
        _light(frame, dot)

    ts = time.monotonic_ns()
    for i in range(cfg.beams.detection.consecutive_frames + 2):
        d.process_frame(frame, ts + i * 40_000_000, beam.camera)

    assert breaks == []
    assert faults == []


def test_a_shape_change_produces_no_break(cfg):
    """
    End to end, the thing that was actually broken.

    Every channel in maze_1 goes dark because the relays opened. Under the old
    code that fired a break on the first one and busted the player.
    """
    breaks, faults = [], []
    frame = np.zeros((240, 320, 3), dtype=np.uint8)
    for beam in cfg.beams.beams:
        beam.dots = [loader.DotROI(cx=10 + i * 5, cy=10, r=3, baseline=200.0)
                     for i in range(5)]

    d = _detector(cfg, breaks, faults)
    d.set_watchlist(cfg.watchlists["maze_1"], settle_ms=0)
    d.arm("run-1", grace_ms=0)

    # The maze switches. Everything maze_1 lit is now dark.
    d.set_watchlist(cfg.watchlists["maze_2"], settle_ms=250)

    ts = time.monotonic_ns()
    for i in range(10):
        d.process_frame(frame, ts + i * 40_000_000, cfg.beams.beams[0].camera)

    assert breaks == [], f"maze change reported breaks: {breaks}"


# ---------------------------------------------------------------------------
# Per-dot camera routing and the fault threshold
# ---------------------------------------------------------------------------

def test_a_channels_dots_can_straddle_two_cameras(cfg):
    """
    The fields of view overlap, so one channel's 5 colinear dots can land on
    two different cameras. Routing per channel could not express that: the dots
    on the other camera were sampled against coordinates meaningless there.
    """
    beam = cfg.beams.beams[0]
    frame = np.zeros((240, 320, 3), dtype=np.uint8)
    beam.dots = [
        loader.DotROI(cx=30 + i * 40, cy=100, r=6, baseline=200.0,
                      camera="cam_1" if i < 3 else "cam_2")
        for i in range(5)
    ]
    d = _detector(cfg, [])

    on_cam1 = d._channel_signal(frame, beam, "cam_1")
    on_cam2 = d._channel_signal(frame, beam, "cam_2")

    assert on_cam1 is not None and on_cam1[1] == 3, "cam_1 should see 3 of the 5"
    assert on_cam2 is not None and on_cam2[1] == 2, "cam_2 should see the other 2"


def test_all_dark_on_a_short_channel_is_a_break_not_a_fault(cfg):
    """
    The fault rule assumes a body cannot cover every dot on a channel. That
    holds for five colinear dots; it does not hold for two. A channel that
    calibration could only find 2 dots for must still bust the player, or a
    real break reads as a hardware fault and they sail through.
    """
    breaks, faults = [], []
    beam, frame = _five_dot_channel(cfg)
    beam.dots = beam.dots[:2]                      # calibration found only 2
    d = _detector(cfg, breaks, faults)
    d.set_watchlist([beam.id], settle_ms=0)
    d.arm("run-1", grace_ms=0)

    ts = time.monotonic_ns()
    for i in range(cfg.beams.detection.consecutive_frames + 1):
        d.process_frame(frame, ts + i * 40_000_000, beam.camera)

    assert breaks == [beam.id], "a short channel going dark must still bust"
    assert faults == [], "2 dark dots is not enough to call a hardware fault"
