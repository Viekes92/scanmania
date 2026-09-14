"""
tests/test_capture.py — the calibration tool, without cameras or relays.

Inputs:  synthetic frames with known dots; tools/capture.py's pure functions
Outputs: assertions on dot finding, per-camera params, baseline units, the
         ambient guard, validation, and the atomic write
Invariant: a capture must never overwrite a good beams.json with something the
           loader cannot read, and baselines must be measured in the exact units
           the game samples in.
"""

from __future__ import annotations

import json
from pathlib import Path

import cv2
import numpy as np
import pytest

import config.loader as loader
import tools.capture as capture
from vision.detect import sample_circle

_W, _H = 320, 240


def _frame(dots, radius=6, value=255):
    f = np.zeros((_H, _W, 3), dtype=np.uint8)
    for (x, y) in dots:
        cv2.circle(f, (x, y), radius, (value, value, value), -1)
    return f


def _truth(n=6, y=120):
    return [(25 + i * 45, y) for i in range(n)]


# ---------------------------------------------------------------------------
# Dot finding
# ---------------------------------------------------------------------------

def test_finds_every_dot_on_a_clean_frame():
    truth = _truth()
    found = capture.find_dots(_frame(truth))
    assert len(found) == len(truth)
    for (tx, ty) in truth:
        assert any(abs(cx - tx) <= 2 and abs(cy - ty) <= 2 for cx, cy, _ in found)


def test_a_black_frame_finds_nothing():
    assert capture.find_dots(np.zeros((_H, _W, 3), np.uint8)) == []


def test_min_area_rejects_sensor_noise():
    """min_area 2 at 1080p catches every speck. That was the first sweep's bug."""
    rng = np.random.default_rng(7)
    noisy = rng.integers(0, 60, (_H, _W, 3), dtype=np.uint8)
    assert len(capture.find_dots(noisy, {"min_area": 1})) > \
           len(capture.find_dots(noisy, {"min_area": 30}))


def test_the_tophat_kernel_is_forced_odd():
    """An even kernel makes getStructuringElement produce an off-centre element."""
    capture.find_dots(_frame(_truth()), {"tophat": 16})    # must not raise


def test_a_light_fitting_survives_as_corner_blobs():
    """
    The top-hat removes the interior of anything larger than its kernel, so a
    bright rectangle comes through as four small CORNER blobs — dot-sized by
    area, and nothing downstream can tell them from dots. min_area is what
    rejects them; the real defence is the ambient guard.
    """
    f = _frame(_truth(3))
    cv2.rectangle(f, (200, 20), (300, 90), (255, 255, 255), -1)
    assert len(capture.find_dots(f)) == 3, "defaults must reject the corners"
    # Drop min_area and the corners walk straight in.
    assert len(capture.find_dots(f, {"min_area": 2})) == 7


def test_a_dot_larger_than_the_kernel_becomes_a_ring():
    """
    tophat = img - opening(img) keeps only what the kernel could not contain, so
    a dot bigger than the kernel hollows out and fragments. This is why one
    camera found 5 dots and another 11 looking at the same five lasers, and why
    the kernel default errs large.
    """
    big = _frame(_truth(3), radius=14)          # 29 px across, area ~615
    roomy = {"max_area": 2000}                  # so max_area is not the variable
    assert len(capture.find_dots(big, {**roomy, "tophat": 9})) < 3
    assert len(capture.find_dots(big, {**roomy, "tophat": 41})) == 3


# ---------------------------------------------------------------------------
# Per-camera params — the reason this tool exists
# ---------------------------------------------------------------------------

def test_each_camera_keeps_its_own_params():
    ui = capture._UI()
    ui.register_camera("SM-CAM-11")
    ui.register_camera("SM-CAM-22")
    ui.set_params("SM-CAM-11", {"tophat": 31})
    assert ui.params_for("SM-CAM-11")["tophat"] == 31
    assert ui.params_for("SM-CAM-22")["tophat"] == capture.DEFAULT_PARAMS["tophat"]


def test_a_bad_param_value_never_replaces_a_good_one():
    ui = capture._UI()
    ui.register_camera("SM-CAM-11")
    ui.set_params("SM-CAM-11", {"thr": "banana", "tophat": 0, "min_area": -4})
    p = ui.params_for("SM-CAM-11")
    assert p == capture.DEFAULT_PARAMS


def test_captures_use_the_camera_that_took_the_frame():
    frames = {"SM-CAM-11": _frame(_truth(4)), "SM-CAM-22": _frame(_truth(2, y=60))}
    caps = capture.capture_from_frames(
        frames, {"SM-CAM-11": dict(capture.DEFAULT_PARAMS),
                 "SM-CAM-22": dict(capture.DEFAULT_PARAMS)})
    assert len(caps["SM-CAM-11"].dots) == 4
    assert len(caps["SM-CAM-22"].dots) == 2
    assert all(d.id.startswith("SM-CAM-11:") for d in caps["SM-CAM-11"].dots)


def test_a_capture_records_the_frame_size():
    """ROIs are frame pixels. A resolution change silently invalidates them."""
    caps = capture.capture_from_frames({"SM-CAM-11": _frame(_truth(3))}, {})
    assert (caps["SM-CAM-11"].w, caps["SM-CAM-11"].h) == (_W, _H)


def test_the_params_that_produced_the_dots_are_stored():
    p = {**capture.DEFAULT_PARAMS, "thr": 41}
    caps = capture.capture_from_frames({"SM-CAM-11": _frame(_truth(3))}, {"SM-CAM-11": p})
    assert caps["SM-CAM-11"].params["thr"] == 41


# ---------------------------------------------------------------------------
# Baselines
# ---------------------------------------------------------------------------

def test_baselines_are_measured_in_the_games_units():
    """
    Same function, or stored baselines and live samples are in different units
    and every ratio in the game is silently wrong.
    """
    f = np.zeros((_H, _W, 3), dtype=np.uint8)
    for (x, y) in _truth(3):
        cv2.circle(f, (x, y), 6, (0, 0, 255), -1)      # red, as real dots are
    caps = capture.capture_from_frames({"SM-CAM-11": f}, {})
    for d in caps["SM-CAM-11"].dots:
        assert d.baseline == pytest.approx(sample_circle(f, d.cx, d.cy, d.r), abs=0.01)


def test_a_red_dot_reads_high_and_a_white_one_does_not():
    """Red isolation is what rejects the container's green/yellow reflections."""
    red = np.zeros((_H, _W, 3), np.uint8)
    cv2.circle(red, (100, 100), 6, (0, 0, 255), -1)
    white = np.zeros((_H, _W, 3), np.uint8)
    cv2.circle(white, (100, 100), 6, (255, 255, 255), -1)
    assert sample_circle(red, 100, 100, 6) > 200
    assert sample_circle(white, 100, 100, 6) < 10


# ---------------------------------------------------------------------------
# The ambient guard
# ---------------------------------------------------------------------------

def test_a_lit_room_trips_the_ambient_guard():
    rng = np.random.default_rng(3)
    lit = rng.integers(70, 190, (_H, _W, 3), dtype=np.uint8)
    assert len(capture.find_dots(lit)) > capture._MAX_AMBIENT_BLOBS


def test_a_dark_room_passes_the_ambient_guard():
    assert len(capture.find_dots(np.zeros((_H, _W, 3), np.uint8))) \
        <= capture._MAX_AMBIENT_BLOBS


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def _cap(cam="SM-CAM-11", n=5, baseline=180.0, w=_W, h=_H):
    return loader.CameraCapture(
        camera=cam, w=w, h=h, params=dict(capture.DEFAULT_PARAMS),
        dots=[loader.Dot(id=f"{cam}:d{i}", cx=10 + i * 20, cy=50, r=6,
                         baseline=baseline) for i in range(n)])


def test_nothing_captured_is_an_error():
    errs, _ = capture.validate({})
    assert errs


def test_a_zero_baseline_dot_is_an_error():
    """It can never register a break — it would look permanently broken."""
    errs, _ = capture.validate({"maze_1": {"SM-CAM-11": _cap(baseline=0.0)}})
    assert any("zero baseline" in e for e in errs)


def test_a_camera_that_found_nothing_is_a_warning_not_an_error():
    """Blind spots are expected; that is why there are eight cameras."""
    errs, warns = capture.validate({
        "maze_1": {"SM-CAM-11": _cap(), "SM-CAM-22": _cap("SM-CAM-22", n=0)}})
    assert not errs
    assert any("found nothing" in w for w in warns)


def test_dim_dots_are_warned_about():
    _, warns = capture.validate({"maze_1": {"SM-CAM-11": _cap(baseline=8.0)}})
    assert any("dim" in w for w in warns)


def test_a_resolution_change_mid_calibration_is_an_error():
    errs, _ = capture.validate({
        "maze_1": {"SM-CAM-11": _cap()},
        "maze_2": {"SM-CAM-11": _cap(w=1920, h=1080)},
    })
    assert any("resolution changed" in e for e in errs)


def test_a_clean_capture_validates():
    errs, warns = capture.validate({
        "maze_1": {"SM-CAM-11": _cap(), "SM-CAM-22": _cap("SM-CAM-22")},
        "maze_2": {"SM-CAM-11": _cap(n=3)},
    })
    assert errs == [] and warns == []


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------

@pytest.fixture
def beams_file(tmp_path):
    src = Path(loader.CONFIG_DIR) / "beams.json"
    dst = tmp_path / "beams.json"
    dst.write_text(src.read_text())
    return dst


def test_the_write_round_trips_through_the_loader(beams_file):
    capture.write_beams(beams_file, {"maze_1": {"SM-CAM-11": _cap()}})
    cfg = loader.load_beams(beams_file)
    assert cfg.mazes["maze_1"].total == 5
    assert cfg.mazes["maze_1"].dots_for("SM-CAM-11")[0].baseline == 180.0


def test_the_45_channel_entries_survive(beams_file):
    """They are no longer used for detection, but they are the wiring record."""
    before = len(loader.load_beams(beams_file).beams)
    capture.write_beams(beams_file, {"maze_1": {"SM-CAM-11": _cap()}})
    assert len(loader.load_beams(beams_file).beams) == before


def test_a_backup_is_left_behind(beams_file):
    original = beams_file.read_text()
    capture.write_beams(beams_file, {"maze_1": {"SM-CAM-11": _cap()}})
    backups = list(beams_file.parent.glob("beams.json.*.bak"))
    assert len(backups) == 1
    assert backups[0].read_text() == original


def test_recapturing_one_maze_leaves_the_others_alone(beams_file):
    capture.write_beams(beams_file, {"maze_1": {"SM-CAM-11": _cap()},
                                     "maze_2": {"SM-CAM-11": _cap(n=3)}})
    capture.write_beams(beams_file, {"maze_1": {"SM-CAM-11": _cap(n=9)}})
    cfg = loader.load_beams(beams_file)
    assert cfg.mazes["maze_1"].total == 9
    assert cfg.mazes["maze_2"].total == 3


def test_the_capture_timestamp_is_recorded(beams_file):
    capture.write_beams(beams_file, {"maze_1": {"SM-CAM-11": _cap()}})
    data = json.loads(beams_file.read_text())
    assert data["mazes"]["maze_1"]["captured_at"]
