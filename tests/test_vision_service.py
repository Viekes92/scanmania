"""
tests/test_vision_service.py — invariant 5: vision suppresses events when unsure.

Inputs:  a VisionService built from the real config, driven by hand
Outputs: assertions that a stall suppresses the detector, reaches the runner,
         and drops detection to manual
Invariant: a missed break is recoverable. A phantom bust in front of a queue is
           not. Every path here must fail safe toward silence.

No cameras are opened. run() is never called; the callbacks and the watchdog are
driven directly, which is what makes this runnable on a laptop.
"""

from __future__ import annotations

import asyncio

import pytest

from core.events import VisionStalled, DetectionMode
from vision.service import VisionService


@pytest.fixture
def service(fake_config):
    return VisionService(fake_config, metrics_emit=lambda *a, **k: None)


def test_starts_unstalled(service):
    assert service.is_stalled() is False
    assert service._detector._stalled is False


def test_camera_stall_suppresses_the_detector(service):
    service._on_camera_stall("cam_a")

    assert service.is_stalled() is True
    # The detector is what actually refuses to emit breaks.
    assert service._detector._stalled is True


def test_stall_is_published_as_an_event(service):
    service._on_camera_stall("cam_a")

    kind, stalled, cameras = service._queue.get_nowait()
    assert (kind, stalled) == ("stall", True)
    assert cameras == ["cam_a"]


def test_stall_event_is_edge_triggered(service):
    """A stalled camera must not flood the queue every watchdog tick."""
    service._on_camera_stall("cam_a")
    service._queue.get_nowait()

    service._on_camera_stall("cam_a")
    assert service._queue.empty()


def test_recovery_clears_suppression_and_publishes(service):
    service._on_camera_stall("cam_a")
    service._queue.get_nowait()

    service._clear_stall("cam_a")

    assert service.is_stalled() is False
    assert service._detector._stalled is False
    kind, stalled, cameras = service._queue.get_nowait()
    assert (kind, stalled, cameras) == ("stall", False, [])


def test_any_stalled_camera_suppresses_everything(service):
    """
    With several cameras, one bad feed must suppress the whole detector.

    The detector cannot attribute a beam to a camera, so a partial view is not
    safe to act on.
    """
    service._stalled_cameras.add("cam_b")
    service._sync_detector_stall()
    assert service._detector._stalled is True

    service._stalled_cameras.discard("cam_b")
    service._sync_detector_stall()
    assert service._detector._stalled is False


@pytest.mark.asyncio
async def test_watchdog_catches_a_camera_that_stops_delivering(service, monkeypatch):
    """
    The failure CameraStream cannot see.

    Its own gap check runs on frame arrival, so a camera that freezes with the
    TCP connection open never triggers it. The watchdog runs outside the frame
    path for exactly this case.
    """
    import time

    stream = service._streams["cam_a"]
    stale_ns = time.monotonic_ns() - (service._stall_threshold_ms + 500) * 1_000_000
    monkeypatch.setattr(type(stream), "last_frame_ns", property(lambda self: stale_ns))

    task = asyncio.create_task(service._watchdog())
    await asyncio.sleep(0.25)
    task.cancel()

    assert service.is_stalled() is True, "watchdog missed a frozen camera"


@pytest.mark.asyncio
async def test_watchdog_ignores_a_camera_that_never_started(service):
    """Before the first frame there is no gap to measure — that is startup."""
    assert service._streams["cam_a"].last_frame_ns is None

    task = asyncio.create_task(service._watchdog())
    await asyncio.sleep(0.25)
    task.cancel()

    assert service.is_stalled() is False


@pytest.mark.asyncio
async def test_stall_reaches_the_fsm_and_drops_to_manual(runner):
    """
    The other half of invariant 5.

    runner._vision_listener understood "break" only, so a stall tuple was
    silently discarded even once something emitted one. Both halves have to work
    for the invariant to hold.
    """
    assert runner.context.detection_mode != DetectionMode.manual

    await runner.dispatch(VisionStalled())

    assert runner.context.detection_mode == DetectionMode.manual


# ---------------------------------------------------------------------------
# Audit round 2 — the camera reader pool
# ---------------------------------------------------------------------------

def test_a_lost_reader_thread_cannot_starve_the_other_cameras():
    """asyncio.wait_for cancels the future, not the thread.

    A worker parked in a blocking cap.read() against a silent camera stays
    parked. With one shared pool, a repeating failure loop — a PoE or VLAN
    outage — burned every worker in about two minutes, after which the HEALTHY
    cameras' reads only queued, timed out, and queued more. Vision died and
    never recovered without a restart.
    """
    from vision.camera import _CameraExecutor

    cams = [_CameraExecutor(f"SM-CAM-1{i}") for i in range(4)]
    pools_before = [c.pool for c in cams]

    # One camera loses its thread over and over, as in a real outage.
    for _ in range(20):
        cams[0].retire()

    assert cams[0].retired == 20
    assert cams[0].pool is not pools_before[0], "no fresh worker after retiring"
    for c, before in zip(cams[1:], pools_before[1:]):
        assert c.pool is before, "a neighbour's executor was disturbed"
        assert c.retired == 0
    for c in cams:
        c.pool.shutdown(wait=False)


def test_retiring_releases_the_capture_on_the_abandoned_worker():
    """Releasing from the loop thread while a read is parked is not
    thread-safe, so the release is queued behind it on the same worker."""
    import time
    from vision.camera import _CameraExecutor

    released = []

    class _Cap:
        def release(self):
            released.append(True)

    ex = _CameraExecutor("SM-CAM-11")
    ex.retire(_Cap())
    for _ in range(50):
        if released:
            break
        time.sleep(0.01)
    assert released, "the capture was never released by the retiring worker"
    ex.pool.shutdown(wait=False)


def _fake_state(cam: str, idx: int, ratio: float):
    """A real _DotState, so the test cannot drift from the class it exercises."""
    from vision.detect import _DotState
    from config.loader import Dot
    st = _DotState(Dot(id=f"{cam}:d{idx}", cx=idx, cy=0, r=4,
                       baseline=10.0, masked=False), cam, 3)
    st.is_dark = True
    st.last_ratio = ratio
    return st


def test_one_lonely_dot_does_not_raise_the_gm_dialog():
    """
    A body crossing a curtain blocks several of its lasers at once. A single
    dot going dark is haze drifting, a marginal r=4 dot, or sensor noise — and
    in assisted mode every one of those put the CONFIRM/VETO dialog in front of
    the GM mid-run.
    """
    from vision.detect import DotDetector
    import config.loader as loader

    cfg = loader.load_beams()
    cfg.detection.min_simultaneous_breaks = 3
    fired: list = []
    d = DotDetector(cfg, on_break=lambda *a, **k: fired.append(a),
                    on_clear=lambda *a, **k: None,
                    metrics_emit=lambda *a, **k: None)
    d._armed = True
    d._arm_time_ns = None

    def _dark(cam: str, n: int):
        d._states = {cam: {f"d{i}": _fake_state(cam, i, 0.1 - i * 0.01)
                           for i in range(n)}}
        d._reported_dark = set()
        d._decide(0)

    _dark("SM-CAM-11", 1)
    assert fired == [], "a single dark dot still raised a break"
    _dark("SM-CAM-11", 2)
    assert fired == [], "two dark dots still raised a break"
    _dark("SM-CAM-11", 3)
    assert fired, "three dots on one camera is a body and must report"


def test_two_cameras_each_flickering_once_is_not_a_body():
    """Counted per camera: two unrelated single-dot glitches are two glitches."""
    from vision.detect import DotDetector
    import config.loader as loader

    cfg = loader.load_beams()
    cfg.detection.min_simultaneous_breaks = 3
    fired: list = []
    d = DotDetector(cfg, on_break=lambda *a, **k: fired.append(a),
                    on_clear=lambda *a, **k: None,
                    metrics_emit=lambda *a, **k: None)
    d._armed = True
    d._arm_time_ns = None

    d._states = {}
    for cam in ("SM-CAM-11", "SM-CAM-12", "SM-CAM-13"):
        d._states[cam] = {"d0": _fake_state(cam, 0, 0.1)}
    d._reported_dark = set()
    d._decide(0)
    assert fired == [], "three dots spread over three cameras reported as a body"
