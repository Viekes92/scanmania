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


def _result(beam_ids, n_dots=5, baseline=200.0, floor=10.0, cam="cam_1"):
    res = {bid: {"dots": [{"cx": 10 + i * 20, "cy": 40, "r": 5, "camera": cam,
                           "baseline": baseline, "dark_floor": floor}
                          for i in range(n_dots)]} for bid in beam_ids}
    res["_all_on_counts"] = {cam: n_dots * len(beam_ids)}
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
    res["_all_on_counts"] = {"cam_1": 0}
    errors, _ = sweep.validate(None, res, channels)
    assert any("no dots detected at all_on" in e for e in errors)


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
    assert beam.dots[0].dark_floor == 10.0


def test_zero_baseline_does_not_divide_by_zero(channels):
    """
    A dot found in the label pass but dark in the measure pass has baseline 0.
    The old code computed dark_floor/baseline anyway and reported a ratio of
    1.00 for 0/0, alongside a duplicate "no baseline" warning for the same dot.
    """
    res = _result([c.id for c in channels], baseline=0.0, floor=0.0)
    _, warnings = sweep.validate(None, res, channels)
    assert not any("1.00" in w for w in warnings), "reported a ratio for 0/0"
    assert any("read 0 at all_on" in w for w in warnings)


def test_warnings_are_one_per_channel_not_one_per_dot(channels):
    """Five bad dots on a channel is one operator-actionable fact, not five."""
    res = _result([c.id for c in channels], baseline=0.0, floor=0.0)
    per_channel = [w for w in sweep.validate(None, res, channels)[1]
                   if "read 0 at all_on" in w]
    assert len(per_channel) == len(channels), \
        f"expected 1 warning per channel, got {len(per_channel)}"
