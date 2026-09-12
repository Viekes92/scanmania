"""
tests/conftest.py — shared pytest fixtures for the ScanMania test suite.

Inputs:  ./config/ (real config files for the `config` fixture)
Outputs: typed fixture objects (AppConfig, Database, FakeIOBackend, etc.)
Invariant: the `db` fixture always uses in-memory SQLite (":memory:") so
           tests never touch the production database.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import AsyncGenerator

import pytest
import pytest_asyncio

# ---------------------------------------------------------------------------
# Config fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def config():
    """
    Real AppConfig loaded from ./config/ (the actual project config files).
    Use this when the test exercises behaviour that depends on real thresholds,
    preset names, or board topology.

    Session-scoped: config loading is expensive and the files don't change
    between tests.
    """
    from config.loader import load_all
    return load_all()


@pytest.fixture
def fake_config():
    """
    Minimal AppConfig with one relay board, one camera, and two beams.
    Use this for unit tests that just need *some* config without caring about
    the actual hardware topology.

    Function-scoped so each test gets a fresh instance (config dataclasses
    are mutable in some fields like beams[].masked).
    """
    from config.loader import (
        AppConfig, HardwareConfig, RelayBoard, CameraConfig, NetworkConfig,
        BeamsConfig, BeamConfig, BeamROI, DetectionConfig,
        GameConfig, CountInConfig, CountInPulse, LeaderboardConfig,
        MazesConfig, PresetConfig, ShowConfig, ShowStep,
    )

    board = RelayBoard(
        id="SM-NODE-1",
        ip="10.0.0.10",
        port=502,
        channels=16,
        timeout_ms=200,
    )
    camera = CameraConfig(
        id="cam_a",
        url="rtsp://10.0.0.30:554/substream",
        reference_frame="ref/cam_a.png",
    )
    beams = [
        BeamConfig(
            id="s01",
            relay_channel=1,
            board_id="SM-NODE-1",
            camera="cam_a",
            roi=BeamROI(cx=100, cy=150, r=9),
            baseline=210.0,
            break_ratio=0.40,
            clear_ratio=0.65,
            masked=False,
            row=1,
            segment=1,
            strip_type="H-left",
        ),
        BeamConfig(
            id="s02",
            relay_channel=2,
            board_id="SM-NODE-1",
            camera="cam_a",
            roi=BeamROI(cx=200, cy=150, r=9),
            baseline=205.0,
            break_ratio=0.40,
            clear_ratio=0.65,
            masked=False,
            row=1,
            segment=1,
            strip_type="V-left",
        ),
    ]
    detection = DetectionConfig(
        consecutive_frames=3,
        arm_grace_ms=150,
        stall_threshold_ms=300,
        global_break_rate_limit=6,
        flap_count_threshold=5,
        flap_window_s=60,
    )
    count_in = CountInConfig(
        preset="maze_1",
        ready_blink_ms=150,
        baseline_pulse_index=0,
        pulses=[
            CountInPulse(on_ms=280, off_ms=320),
            CountInPulse(on_ms=200, off_ms=250),
            CountInPulse(on_ms=150, off_ms=190),
        ],
        solid_at_end=True,
    )
    game = GameConfig(
        mode="hard_cutoff",
        max_run_ms=180000,
        result_display_ms=5000,   # shorter for tests
        arm_timeout_ms=10000,
        detection_mode="auto",
        show="main_game",
        count_in=count_in,
        arm_grace_ms=150,
        leaderboard=LeaderboardConfig(scope="daily", show_busted=False, max_entries=20),
    )
    presets = {
        "blackout":  PresetConfig(channels=[]),
        "all_on":    PresetConfig(channels="*"),
        "maze_1": PresetConfig(channels=[1, 2, 3, 8, 9, 14]),
        "maze_2": PresetConfig(channels=[1, 2, 5, 6, 11, 12]),
        "maze_3": PresetConfig(channels=[3, 4, 7, 9, 10, 13]),
    }
    shows = {
        "attract": ShowConfig(steps=[ShowStep("all_on", 2000), ShowStep("blackout", 300)], loop=True),
        "bust": ShowConfig(steps=[ShowStep("all_on", 150), ShowStep("blackout", 150)], loop=True),
        "clean": ShowConfig(steps=[ShowStep("all_on", 400), ShowStep("blackout", 100)], loop=True),
        "main_game": ShowConfig(steps=[], segments=["maze_1", "maze_2", "maze_3"]),
    }
    return AppConfig(
        hardware=HardwareConfig(
            relay_boards=[board],
            cameras=[camera],
            network=NetworkConfig(subnet="10.0.0.0/24", nuc_ip="10.0.0.1", web_port=8000),
            hazer=None,
        ),
        beams=BeamsConfig(beams=beams, detection=detection),
        game=game,
        mazes=MazesConfig(presets=presets, shows=shows),
    )


# ---------------------------------------------------------------------------
# Database fixture
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def db():
    """
    Initialised in-memory SQLite Database.
    Always ':memory:' — never touches the production DB.
    Torn down automatically after each test.
    """
    from persist.db import Database
    database = Database(":memory:")
    await database.init()
    yield database
    await database.close()


# ---------------------------------------------------------------------------
# Fake backend fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def fake_io(fake_config):
    """
    FakeIOBackend using the minimal fake_config topology.
    All relay writes go to in-memory state; inspectable via fake_io.boards.
    """
    from iobackend.fake import FakeIOBackend
    return FakeIOBackend(fake_config.hardware)


@pytest.fixture
def fake_inputs():
    """
    FakeInputs with no-op callbacks.
    Tests drive it with fake_inputs.trigger_input(input_id, state).
    NOTE: no test currently does — see docs/testing.md. The fakes are handed
    to the runner as stubs while tests push events straight into the runner.
    """
    from inputs.fake import FakeInputs
    return FakeInputs()


@pytest.fixture
def fake_vision(fake_config):
    """
    FakeVision with no-op callbacks.
    Tests drive it with fake_vision.trigger_break(beam_id, ratio) and
    trigger_clear(beam_id). NOTE: no test currently does — see docs/testing.md.
    """
    from vision.fake import FakeVision
    return FakeVision(fake_config.beams)


# ---------------------------------------------------------------------------
# FSM context factory fixture
# ---------------------------------------------------------------------------

@pytest.fixture
def fsm_context():
    """
    Fresh FSMContext with default values.
    Convenience for FSM unit tests that don't need the full runner.
    """
    from core.fsm import FSMContext
    return FSMContext()


# ---------------------------------------------------------------------------
# Runner fixture — shared by test_runner.py and test_vision_service.py
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def runner(fake_config, fake_io, fake_inputs, fake_vision, db):
    """
    Fresh GameRunner wired to all fake backends and an in-memory DB.
    No hub is attached (WebSocket broadcast is a no-op).
    """
    from core.runner import GameRunner
    r = GameRunner(
        config=fake_config,
        io_backend=fake_io,
        inputs_backend=fake_inputs,
        vision_backend=fake_vision,
        db=db,
    )
    yield r
    # Cancel any lingering timer tasks so the event loop is clean afterward.
    for attr in ("_arm_timeout_task", "_result_timeout_task",
                 "_max_run_task", "_count_in_task", "_show_task", "_self_test_task"):
        r._cancel_task(attr)
