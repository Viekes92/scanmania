"""
tests/test_sweep.py — the calibration sweep's pure logic, without hardware.

Inputs:  synthetic frames and hand-built sweep results
Outputs: assertions on dot finding, validation verdicts, and the atomic write
Invariant: a failed sweep must never overwrite a good beams.json. The service
           refuses to boot on a malformed one, so a bad write is an outage.

The relay/camera passes need the maze powered and are verified on the NUC. This
covers everything that does not.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import cv2
import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import sweep  # noqa: E402

import config.loader as loader  # noqa: E402


def _frame(dots, size=(240, 320)):
    f = np.zeros((*size, 3), np.uint8)
    for (x, y) in dots:
        cv2.circle(f, (x, y), 4, (60, 60, 255), -1)
    return f


# ---------------------------------------------------------------------------
# Dot finding
# ---------------------------------------------------------------------------

def test_finds_five_colinear_dots():
    f = _frame([(40 + i * 45, 120) for i in range(5)])
    found = sweep.find_dots(f)
    assert len(found) == 5, f"expected 5 dots, got {len(found)}"


def test_finds_nothing_in_a_dark_frame():
    assert sweep.find_dots(np.zeros((240, 320, 3), np.uint8)) == []


def test_dot_positions_are_close_to_truth():
    truth = [(50, 100), (120, 100), (190, 100)]
    found = sorted(sweep.find_dots(_frame(truth)))
    for (tx, ty), (fx, fy, _) in zip(truth, found):
        assert abs(fx - tx) <= 2 and abs(fy - ty) <= 2


def test_match_dot_finds_the_nearest_within_tolerance():
    cands = [(100, 100, 5), (200, 100, 5)]
    assert sweep.match_dot((103, 101, 5), cands)[0] == 100
    assert sweep.match_dot((150, 100, 5), cands) is None


# ---------------------------------------------------------------------------
# Colinearity — reported, never enforced
# ---------------------------------------------------------------------------

def test_straight_line_has_near_zero_residual():
    dots = [(10 + i * 20, 50, 5) for i in range(5)]
    assert sweep.colinearity_residual(dots) < 1.0


def test_a_lens_bent_line_still_reads_low():
    """
    Barrel distortion bows a physically straight array. The check must tolerate
    that — it exists to catch a ghost far off the line, not to police optics.
    """
    dots = [(10 + i * 20, 50 + int(2 * (i - 2) ** 2), 5) for i in range(5)]
    assert sweep.colinearity_residual(dots) < 25


def test_an_outlier_shows_up():
    dots = [(10 + i * 20, 50, 5) for i in range(4)] + [(50, 160, 5)]
    assert sweep.colinearity_residual(dots) > 25


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

@pytest.fixture
def channels():
    return loader.load_all().beams.beams[:3]


MAZE = "maze_1"


def _result(beam_ids, n_dots=5, baseline=200.0, floor=10.0, cam="cam_1"):
    """A sweep result in the per-maze shape the tool now produces."""
    res = {bid: {"dots": [{"cx": 10 + i * 20, "cy": 40, "r": 5, "camera": cam,
                           "baselines": {MAZE: baseline},
                           "dark_floors": {MAZE: floor}}
                          for i in range(n_dots)]} for bid in beam_ids}
    res["_mazes"] = [MAZE]
    res["_maze_counts"] = {MAZE: {"expected": n_dots * len(beam_ids),
                                  "found": {cam: n_dots * len(beam_ids)}}}
    return res


def test_a_clean_sweep_passes(channels):
    errors, warnings = sweep.validate(None, _result([c.id for c in channels]), channels)
    assert errors == []
    assert warnings == []


def test_an_empty_channel_is_an_error(channels):
    res = _result([c.id for c in channels])
    res[channels[0].id]["dots"] = []
    errors, _ = sweep.validate(None, res, channels)
    assert any("produced no dots" in e for e in errors)


def test_a_short_channel_warns_about_fault_discrimination(channels):
    """
    Under 4 dots, detect.py can no longer tell a real break from a dead channel
    — see _MIN_DOTS_FOR_FAULT. That must be surfaced, not silently written.
    """
    res = _result([c.id for c in channels])
    res[channels[0].id]["dots"] = res[channels[0].id]["dots"][:2]
    _, warnings = sweep.validate(None, res, channels)
    assert any("under 4 dots" in w for w in warnings)


def test_a_dot_that_can_never_fire_is_flagged(channels):
    """
    dark_floor at or above break_ratio means a neighbour blooms into the ROI and
    holds it above threshold. The dot looks fine in the label pass and is a dead
    sensor in the maze. Only the measure pass catches it.
    """
    res = _result([c.id for c in channels], baseline=200.0, floor=150.0)
    _, warnings = sweep.validate(None, res, channels)
    assert any("can never fire" in w for w in warnings)


def test_no_dots_anywhere_is_an_error(channels):
    res = _result([c.id for c in channels], n_dots=0)
    res["_maze_counts"] = {MAZE: {"expected": 15, "found": {"cam_1": 0}}}
    errors, _ = sweep.validate(None, res, channels)
    assert any("no dots detected at all" in e for e in errors)


def test_a_maze_short_of_its_expected_count_warns(channels):
    """Per maze, not all_on: a maze has a known expected dot count."""
    res = _result([c.id for c in channels])
    res["_maze_counts"] = {MAZE: {"expected": 100, "found": {"cam_1": 40}}}
    _, warnings = sweep.validate(None, res, channels)
    assert any("of 100 dots found" in w for w in warnings)


def test_a_dot_can_be_blind_in_one_maze_and_fine_in_another(channels):
    """
    The reason baselines are per maze: neighbours differ between shapes, so the
    same dot can be perfectly detectable in one and swamped in another.
    """
    res = _result([c.id for c in channels])
    for bid in [c.id for c in channels]:
        for d in res[bid]["dots"]:
            d["baselines"]["maze_2"] = 200.0
            d["dark_floors"]["maze_2"] = 190.0      # blind here only
    res["_mazes"] = [MAZE, "maze_2"]
    _, warnings = sweep.validate(None, res, channels)
    assert any("maze_2" in w and "can never fire" in w for w in warnings)
    assert not any(f"{MAZE}" in w and "can never fire" in w for w in warnings)


# ---------------------------------------------------------------------------
# The write — a failed sweep must never cost an outage
# ---------------------------------------------------------------------------

def test_write_updates_dots_and_keeps_a_backup(tmp_path, channels):
    src = Path(loader.CONFIG_DIR) / "beams.json"
    work = tmp_path / "beams.json"
    work.write_text(src.read_text())

    res = _result([c.id for c in channels], cam="cam_2")
    backup = sweep.write_beams(work, res, channels, None, {"cam_2": (1920, 1080)})

    out = json.loads(work.read_text())
    entry = next(b for b in out["beams"] if b["id"] == channels[0].id)
    assert len(entry["dots"]) == 5
    assert entry["dots"][0]["camera"] == "cam_2"
    assert entry["camera"] == "cam_2"
    assert (entry["capture_w"], entry["capture_h"]) == (1920, 1080)
    assert "roi" not in entry, "legacy single roi should be replaced by dots"
    assert backup.exists(), "no backup written"
    assert json.loads(backup.read_text()) == json.loads(src.read_text())


def test_write_is_atomic_and_leaves_no_temp(tmp_path, channels):
    src = Path(loader.CONFIG_DIR) / "beams.json"
    work = tmp_path / "beams.json"
    work.write_text(src.read_text())
    sweep.write_beams(work, _result([c.id for c in channels]), channels, None, {})
    assert not (tmp_path / "beams.json.new").exists()
    json.loads(work.read_text())      # parses, so the rename was atomic


def test_the_written_file_still_loads(tmp_path, channels, monkeypatch):
    """
    A malformed beams.json makes config/loader.py raise and the service refuses
    to start. Whatever the sweep writes must survive a real load.
    """
    src_dir = Path(loader.CONFIG_DIR)
    for name in ("beams.json", "hardware.yaml", "game.yaml", "mazes.yaml"):
        (tmp_path / name).write_text((src_dir / name).read_text())

    sweep.write_beams(tmp_path / "beams.json",
                      _result([c.id for c in channels]), channels, None,
                      {"cam_1": (1920, 1080)})

    monkeypatch.setattr(loader, "CONFIG_DIR", tmp_path)
    cfg = loader.load_all()
    beam = next(b for b in cfg.beams.beams if b.id == channels[0].id)
    assert len(beam.dots) == 5
    assert beam.dots[0].camera == "cam_1"
    assert beam.dots[0].baseline_for(MAZE) == 200.0
    assert beam.dots[0].dark_floor_for(MAZE) == 10.0
    # Flat fallback filled in from the brightest maze, for a preset-less read.
    assert beam.dots[0].baseline == 200.0


def test_zero_baseline_does_not_divide_by_zero(channels):
    """
    A dot found in the label pass but dark in the measure pass has baseline 0.
    The old code computed dark_floor/baseline anyway and reported a ratio of
    1.00 for 0/0, alongside a duplicate "no baseline" warning for the same dot.
    """
    res = _result([c.id for c in channels], baseline=0.0, floor=0.0)
    _, warnings = sweep.validate(None, res, channels)
    assert not any("1.00" in w for w in warnings), "reported a ratio for 0/0"
    assert any("read 0" in w for w in warnings)


def test_warnings_are_one_per_channel_not_one_per_dot(channels):
    """Five bad dots on a channel is one operator-actionable fact, not five."""
    res = _result([c.id for c in channels], baseline=0.0, floor=0.0)
    per_channel = [w for w in sweep.validate(None, res, channels)[1]
                   if "read 0" in w]
    assert len(per_channel) == len(channels), \
        f"expected 1 warning per channel, got {len(per_channel)}"


# ---------------------------------------------------------------------------
# Ambient light — the failure that produced a populated, useless calibration
# ---------------------------------------------------------------------------

def test_a_lit_room_produces_ghosts_not_dots():
    """
    Why the ambient check exists. Ceiling texture under house lights survives
    the top-hat and gets recorded as dots, but reads 0 through the red-isolated
    sampler the game uses — so the calibration looks populated and detects
    nothing.
    """
    from vision.detect import sample_circle
    lit_room = np.full((240, 320, 3), 90, np.uint8)
    cv2.rectangle(lit_room, (40, 40), (56, 56), (150, 150, 150), -1)   # a fitting
    cv2.circle(lit_room, (200, 120), 5, (140, 140, 140), -1)           # texture

    ghosts = sweep.find_dots(lit_room)
    assert ghosts, "fixture should produce ghost blobs"
    for (cx, cy, r) in ghosts:
        assert sample_circle(lit_room, cx, cy, r) < 20, \
            "a grey ghost must read near zero through the red-isolated sampler"


def test_ambient_threshold_rejects_a_noisy_scene():
    noisy = np.zeros((240, 320, 3), np.uint8)
    rng = np.random.default_rng(3)
    for _ in range(40):
        x, y = int(rng.integers(10, 310)), int(rng.integers(10, 230))
        cv2.circle(noisy, (x, y), 3, (120, 120, 120), -1)
    assert len(sweep.find_dots(noisy)) > sweep._MAX_AMBIENT_BLOBS


def test_a_dark_scene_passes_the_ambient_threshold():
    assert len(sweep.find_dots(np.zeros((240, 320, 3), np.uint8))) <= sweep._MAX_AMBIENT_BLOBS


# ---------------------------------------------------------------------------
# Web control — the sweep waits to be started, and can be stopped
# ---------------------------------------------------------------------------

def test_a_run_must_be_requested_before_it_starts():
    ui = sweep._UI()
    assert ui.take_start() is None, "ran without being asked to"
    assert ui.request_start({"channels": "", "mazes": "maze_1", "no_write": True})
    assert ui.take_start()["mazes"] == "maze_1"


def test_a_second_start_is_refused_while_busy():
    """Two concurrent sweeps would fight over the relays."""
    ui = sweep._UI()
    ui.request_start({"channels": "", "mazes": "maze_1", "no_write": True})
    ui.take_start()
    assert ui.request_start({"channels": "", "mazes": "maze_2", "no_write": True}) is False
    ui.finished()
    assert ui.request_start({"channels": "", "mazes": "maze_2", "no_write": True}) is True


def test_abort_raises_inside_the_sweep():
    """
    Cooperative: the sweep calls check_abort() between channels so it unwinds
    through its own finally and leaves the maze dark.
    """
    ui = sweep._UI()
    ui.check_abort()                       # no-op when not aborting
    ui.request_abort()
    with pytest.raises(sweep.SweepAborted):
        ui.check_abort()


def test_finishing_clears_the_abort_flag():
    ui = sweep._UI()
    ui.request_start({"channels": "", "mazes": "maze_1", "no_write": True})
    ui.take_start()
    ui.request_abort()
    ui.finished()
    assert ui.aborting is False, "a stale abort would kill the next run instantly"


def test_channel_selection_prefers_an_explicit_list(channels):
    cfg = loader.load_all()
    args = type("A", (), {"all_channels": False})()
    picked = sweep._select_channels(cfg, args, "11,12", "maze_1")
    assert sorted(b.id for b in picked) == ["11", "12"]


def test_channel_selection_falls_back_to_the_chosen_mazes():
    cfg = loader.load_all()
    args = type("A", (), {"all_channels": False})()
    picked = sweep._select_channels(cfg, args, "", "maze_1")
    assert {b.id for b in picked} == set(cfg.watchlists["maze_1"])
    assert len(picked) < len(cfg.beams.beams), "should skip channels maze_1 does not light"
