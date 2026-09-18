"""
vision/detect.py — dot detection with hysteresis, rate limiting, and flap detection.

Inputs:  decoded frames from CameraStream, BeamsConfig, on_break/on_clear callbacks
Outputs: BreakConfirmed events via on_break; BeamCleared via on_clear
Invariant: suppresses all events when a camera is stalled; never emits a break in
           the first arm_grace_ms after arming; a burst larger than
           max_simultaneous_breaks is suppressed, because a body blocks a handful
           of dots while a relay failure or preset change kills dozens.

Detection is per DOT, not per relay channel. Calibration lights a whole maze and
records what the cameras can see; nothing establishes which relay drives which
dot, and the game does not need it — a dot going dark means a beam was broken.
Each dot keeps a stable id (cam_3:d17) so a bust can name what broke, a dot can
be masked on its own, and evidence can crop the right region.
"""

from __future__ import annotations

import collections
import logging
import time
from typing import Callable

import numpy as np

from config.loader import BeamsConfig, Dot

log = logging.getLogger(__name__)

# How many top-percentile pixels to use for the ROI sample
_TOP_FRACTION = 0.20


def sample_circle(frame: np.ndarray, cx: int, cy: int, r: int) -> float:
    """
    Sample mean of the top 20% brightest pixels in the circular ROI on the
    red-isolated image: R - (G+B)/2, clipped to [0, 255].

    Module level on purpose: the calibration tool measures baselines with this
    exact function, so stored baselines and runtime samples are always in the
    same units. If the two diverged, every ratio in the game would be silently
    wrong.
    """
    h, w = frame.shape[:2]

    x0 = max(0, cx - r)
    y0 = max(0, cy - r)
    x1 = min(w, cx + r + 1)
    y1 = min(h, cy + r + 1)

    patch = frame[y0:y1, x0:x1]
    if patch.size == 0:
        return 0.0

    # Red isolation: R - (G+B)/2
    b_ch = patch[:, :, 0].astype(np.float32)
    g_ch = patch[:, :, 1].astype(np.float32)
    r_ch = patch[:, :, 2].astype(np.float32)
    isolated = np.clip(r_ch - (g_ch + b_ch) / 2.0, 0, 255)

    ys, xs = np.ogrid[y0:y1, x0:x1]
    mask = ((xs - cx) ** 2 + (ys - cy) ** 2) <= r ** 2
    pixels = isolated[mask]
    if pixels.size == 0:
        return 0.0

    k = max(1, int(len(pixels) * _TOP_FRACTION))
    top_k = np.partition(pixels, -k)[-k:]
    return float(np.mean(top_k))


class _DotState:
    """Hysteresis for one dot. Chatter is filtered here, before counting."""

    def __init__(self, dot: Dot, camera: str, n: int) -> None:
        self.dot = dot
        self.camera = camera
        self.n = n
        # Owned, not read through to config: the BaselineManager's rolling EMA
        # and a flash capture both rewrite this, and neither should mutate the
        # loaded config dataclass.
        self.baseline: float = dot.baseline
        self.dark_consecutive: int = 0
        self.lit_consecutive: int = 0
        self.is_dark: bool = False
        self.last_ratio: float = 1.0
        self.flap_times: collections.deque[int] = collections.deque()
        self.auto_masked: bool = False


class DotDetector:
    """
    Turns decoded frames into break and clear events.

    Per frame it samples the dots this camera watches for the maze currently
    lit, applies per-dot hysteresis, then decides on the COUNT of dark dots:

        0            nothing
        1..max       a body is in the beams -> break
        > max        not a person. A relay that did not fire, a preset change
                     the detector has not been told about, or a camera glitch.
                     Suppressed and reported as a fault instead.

    The count replaces the old per-channel rule ("all 5 dots of this channel are
    dark, so it is hardware"), which needed a dot-to-channel mapping that
    calibration no longer produces.
    """

    def __init__(
        self,
        beams_config: BeamsConfig,
        on_break: Callable,
        on_clear: Callable,
        metrics_emit: Callable,
        on_fault: Callable | None = None,
        on_sample: Callable | None = None,
    ) -> None:
        self._cfg = beams_config
        self._on_break = on_break       # (dot_id: str, ratio: float, ts_ns: int) → None
        self._on_clear = on_clear       # (dot_id: str, ts_ns: int) → None
        self._on_fault = on_fault       # (reason: str, ts_ns: int) → None
        self._on_sample = on_sample     # (dot_id: str, value: float) → None
        self._metrics_emit = metrics_emit

        self._n = beams_config.detection.consecutive_frames
        self._max_burst = beams_config.detection.max_simultaneous_breaks
        self._min_burst = getattr(beams_config.detection,
                                  "min_simultaneous_breaks", 1)
        self._min_baseline = getattr(beams_config.detection,
                                     "min_baseline", 40.0)

        # camera_id -> {dot_id: _DotState}, for the maze currently lit.
        self._states: dict[str, dict[str, _DotState]] = {}
        self._maze: str | None = None
        self._settle_until_ns: int = 0
        # camera -> (w, h) the ROIs were measured at; see _frame_matches_capture
        self._capture_size: dict[str, tuple[int, int]] = {}
        self._size_warned: set[str] = set()

        self._armed: bool = False
        self._run_id: str | None = None
        self._arm_time_ns: int | None = None
        self._arm_grace_ms: int | None = None

        self._recent_break_times: collections.deque[int] = collections.deque()
        self._stalled: bool = False
        # Flap history and auto-masks live HERE, not on _DotState.
        #
        # set_maze rebuilds every _DotState from config, so both used to be
        # thrown away on every preset change — twice per run at the
        # checkpoints, and ~2.5 times a SECOND during the attract show. The
        # rule needs 5 breaks in 60 s, and the counter was cleared long before
        # the fifth ever arrived, so the one automatic defence against a dot
        # that keeps breaking on its own could never fire. Worse, each of those
        # 5 breaks is a ruined run, so the defence has to survive between them
        # or it is not a defence.
        self._flap_times: dict[str, collections.deque] = {}
        self._auto_masked: set[str] = set()
        # Cameras whose frames no longer match their capture resolution. Their
        # dots are not sampled, so is_dark is frozen — see _decide.
        self._excluded_cameras: set[str] = set()
        self._fault_reported: bool = False
        # Dots already announced as broken, so one body standing in a beam is
        # one event rather than six a second.
        self._reported_dark: set[str] = set()
        # Presets already reported as having no capture, so the attract show
        # cannot flood the journal.
        self._no_capture_warned: set[str] = set()

    # ------------------------------------------------------------------
    # Which maze is lit
    # ------------------------------------------------------------------

    def set_maze(self, preset: str | None, settle_ms: int = 250) -> None:
        """
        Swap to the dot set captured while this maze was lit.

        Called on every preset change. This is what stops a maze change reading
        as a mass beam break: the dots that vanish belong to the shape being
        left, they are not in the new set, so nothing asks about them.

        An unknown or uncalibrated preset watches nothing rather than guessing.
        Silence is the safe failure — a missed break is recoverable, a phantom
        bust in front of a queue is not.
        """
        prev_maze = self._maze
        self._maze = preset
        self._states = {}
        self._capture_size = {}
        self._size_warned = set()
        self._fault_reported = False
        self._reported_dark = set()
        rois = self._cfg.mazes.get(preset) if preset else None
        if rois is None:
            # Once per preset, not once per call. _apply_maze runs on EVERY
            # show step, and the attract show alternates all_on/blackout every
            # 400-500 ms — so this logged ~2.5 WARNING lines a second, about
            # 216k a day, all day. journald's rate limiter then started
            # dropping OTHER messages, which meant the relay-failure and
            # vision-stall lines you actually need were the ones discarded.
            if preset and preset not in self._no_capture_warned:
                self._no_capture_warned.add(preset)
                log.warning("no ROI capture for preset '%s' — watching nothing "
                            "(logged once per preset)", preset)
            return

        for cam_id, cap in rois.cameras.items():
            states = {}
            for d in cap.dots:
                if d.masked:
                    continue
                st = _DotState(d, cam_id, self._n)
                # Carry the flap history and any auto-mask across the rebuild.
                st.flap_times = self._flap_times.setdefault(
                    d.id, collections.deque())
                st.auto_masked = d.id in self._auto_masked
                states[d.id] = st
            self._states[cam_id] = states
            if cap.w and cap.h:
                self._capture_size[cam_id] = (cap.w, cap.h)
        # Newly lit dots are still coming on. Give the relays and the camera a
        # moment before believing a dark reading.
        #
        # Only when the SHAPE actually changed. Re-applying the preset that is
        # already lit — a show step that repeats itself, a redundant write —
        # used to restart the window, so a sequence of same-preset applies
        # faster than settle_ms held the detector permanently blind with
        # nothing in any log to say so.
        if preset != prev_maze:
            self._settle_until_ns = time.monotonic_ns() + settle_ms * 1_000_000
        log.info("watching maze '%s': %d dots across %d camera(s)",
                 preset, self.watching, len(self._states))

    @property
    def watching(self) -> int:
        return sum(len(v) for v in self._states.values())

    # ------------------------------------------------------------------
    # Arm / disarm
    # ------------------------------------------------------------------

    def arm(self, run_id: str, grace_ms: int) -> None:
        """Arm detection. Breaks are suppressed for grace_ms after this call."""
        self._armed = True
        self._run_id = run_id
        self._arm_time_ns = time.monotonic_ns()
        # The caller's grace wins. It used to be logged and then dropped, with
        # the config value read instead, so passing a grace had no effect.
        self._arm_grace_ms = grace_ms
        log.info("DotDetector armed for run='%s' grace_ms=%d (%d dots)",
                 run_id, grace_ms, self.watching)
        # Not just the per-dot state: the report latch too. It happened to be
        # empty because _decide clears it while unarmed — but that only runs if
        # a frame actually reached _decide, which a stall or an empty state map
        # prevents. Correctness should not depend on a frame having arrived.
        self._reported_dark.clear()
        for cam in self._states.values():
            for st in cam.values():
                st.dark_consecutive = st.lit_consecutive = 0
                st.is_dark = False

    def disarm(self) -> None:
        self._armed = False
        self._run_id = None
        self._arm_time_ns = None
        log.info("DotDetector disarmed")

    @property
    def is_armed(self) -> bool:
        return self._armed

    # ------------------------------------------------------------------
    # Stall suppression (invariant 5)
    # ------------------------------------------------------------------

    def set_stalled(self, stalled: bool) -> None:
        self._stalled = stalled
        if stalled:
            log.warning("DotDetector: stall suppression active — no breaks will fire")

    # ------------------------------------------------------------------
    # Per-frame processing
    # ------------------------------------------------------------------

    def process_frame(
        self, frame: np.ndarray, timestamp_ns: int, camera_id: str | None = None
    ) -> None:
        """
        Sample this camera's dots for the lit maze, then decide what happened.

        camera_id routes the frame to the dots this camera actually sees. ROIs
        are pixels in one camera's view, so sampling against another camera's
        frame reads coordinates that mean nothing there.
        """
        if self._stalled or not self._states:
            return
        if timestamp_ns < self._settle_until_ns:
            return

        for cid in ([camera_id] if camera_id is not None else list(self._states)):
            if not self._frame_matches_capture(cid, frame):
                continue
            for st in (self._states.get(cid) or {}).values():
                if not self._sampled(st):
                    continue   # masked, never calibrated, or below min_baseline
                value = sample_circle(frame, st.dot.cx, st.dot.cy, st.dot.r)
                st.last_ratio = value / st.baseline
                self._update_dot(st)
                # Feed the rolling baseline only between runs. The manager drops
                # samples while frozen anyway; skipping the call keeps the hot
                # path clear during the one minute that matters.
                if self._on_sample is not None and not self._armed:
                    self._on_sample(st.dot.id, value)

        self._decide(timestamp_ns)

    def _frame_matches_capture(self, cid: str, frame: np.ndarray) -> bool:
        """
        Refuse to sample a frame that is not the size the ROIs were measured at.

        ROIs are frame pixels. A substream resolution change silently
        invalidates every one of them, and sample_circle clamps out-of-range
        coordinates and happily returns a number — so the failure is not a
        crash, it is a detector that is confidently wrong. Watching nothing is
        the safe answer (invariant 5).
        """
        want = self._capture_size.get(cid)
        if want is None:
            return True
        h, w = frame.shape[:2]
        if (w, h) == want:
            self._excluded_cameras.discard(cid)
            return True
        self._excluded_cameras.add(cid)
        # is_dark is frozen at whatever it was when sampling stopped, so reset
        # it — otherwise a dot that happened to be dark at that instant counts
        # toward max_simultaneous_breaks forever, and can be REPORTED as a
        # break by a camera that has not produced a valid frame in hours. Same
        # bug already fixed for masked dots; this path was not covered.
        for st in (self._states.get(cid) or {}).values():
            st.is_dark = False
            st.dark_consecutive = st.lit_consecutive = 0
        if cid not in self._size_warned:
            self._size_warned.add(cid)
            log.error("%s: frame is %dx%d but its ROIs were captured at %dx%d — "
                      "every ROI is invalid. Not sampling this camera. Recapture.",
                      cid, w, h, want[0], want[1])
            self._metrics_emit("vision.resolution_mismatch", 1.0, {"camera": cid})
            if self._on_fault:
                self._on_fault(f"{cid}: resolution changed since calibration",
                               time.monotonic_ns())
        return False

    def _update_dot(self, st: _DotState) -> None:
        """Per-dot hysteresis, so one noisy frame cannot end a run."""
        ratio = st.last_ratio
        if not st.is_dark:
            if ratio < self._break_ratio:
                st.dark_consecutive += 1
                st.lit_consecutive = 0
                if st.dark_consecutive >= st.n:
                    st.is_dark = True
                    st.dark_consecutive = 0
            else:
                st.dark_consecutive = 0
        else:
            if ratio > self._clear_ratio:
                st.lit_consecutive += 1
                st.dark_consecutive = 0
                if st.lit_consecutive >= st.n:
                    st.is_dark = False
                    st.lit_consecutive = 0
            else:
                st.lit_consecutive = 0

    @property
    def _break_ratio(self) -> float:
        return self._cfg.detection.break_ratio

    @property
    def _clear_ratio(self) -> float:
        return self._cfg.detection.clear_ratio

    @property
    def in_fault(self) -> bool:
        """
        True while ANY detector-level condition is currently suppressing.

        It used to report the mass-dark latch alone, so _expire_fault called
        the detector healthy 30 s after a RESOLUTION MISMATCH and restored auto
        detection — while _frame_matches_capture was still rejecting every
        frame from that camera. A camera whose stream reverted to its old
        resolution went permanently unsampled with the game claiming to watch
        it.
        """
        return self._fault_reported or bool(self._excluded_cameras)

    def _decide(self, timestamp_ns: int) -> None:
        """Act on the COUNT of dark dots, not on which ones."""
        # Only while ARMED. Unarmed, no break can be emitted anyway
        # (_can_emit_break refuses), so the only thing this could still do is
        # raise the mass-dark fault — and between runs "most dots are dark" is
        # the INTENDED state, not a hardware failure.
        #
        # This stopped every run dead. ARM lights `arm_box` — three channels,
        # so the player is boxed in rather than standing in the dark — while
        # the detector stays pointed at maze_1's 137 dots, because preflight
        # needs a calibrated dot count to check. ~130 of those dots are
        # therefore legitimately dark, the rule called it "31 dots dark at
        # once", vision dropped to manual and the FSM went ARM -> FAULT. Five
        # test runs in a row never reached the count-in.
        #
        # The mismatch between "what is watched" and "what is lit" is
        # deliberate here; what was wrong was judging it.
        if not self._armed:
            self._reported_dark.clear()
            if self._fault_reported:
                self._fault_reported = False
            return

        # See _reported_dark below: a break is announced once per dot, not once
        # per frame, or a player standing in a beam produces ~6 events/second.
        # auto_masked dots are NOT sampled any more (process_frame skips them),
        # so is_dark is frozen at whatever it was when they were masked — True.
        # Counting them here meant a masked dot stayed dark forever, kept
        # counting toward max_simultaneous_breaks, and eventually tipped the
        # detector into a permanent mass-dark fault. stats() already excludes
        # them; this did not.
        dark = [st for cid, cam in self._states.items()
                for st in cam.values()
                if st.is_dark
                and cid not in self._excluded_cameras
                and self._sampled(st)]

        if not dark:
            self._reported_dark.clear()
            if self._fault_reported:
                log.info("DotDetector: dots recovered")
                self._fault_reported = False
            return
        # Drop dots that have cleared, so the same dot can legitimately break
        # again later in the run.
        still_dark = {st.dot.id for st in dark}
        cleared = self._reported_dark - still_dark
        self._reported_dark &= still_dark
        # A dot that re-lit is the single best signal separating a body from a
        # flap — a body holds a dot dark for hundreds of ms, noise is back on
        # the next frame. The detector computed this set and threw it away:
        # on_clear is declared, documented in the module header, and was never
        # once called, so only vision/fake.py ever emitted it.
        for dot_id in cleared:
            try:
                self._on_clear(dot_id, timestamp_ns)
            except Exception:
                log.exception("on_clear raised for %s", dot_id)

        # A body blocks a handful. Dozens at once is the maze changing, a relay
        # not firing, or a camera glitch — never a player. Calling that a break
        # would end a run for a hardware fault.
        if len(dark) > self._max_burst:
            if not self._fault_reported:
                self._fault_reported = True
                log.error(
                    "DotDetector: %d dots dark at once (limit %d) — not a player. "
                    "Relay, preset change or camera fault. Suppressing.",
                    len(dark), self._max_burst,
                )
                self._metrics_emit("vision.mass_dark", float(len(dark)),
                                   {"maze": self._maze or ""})
                if self._on_fault:
                    self._on_fault(f"{len(dark)} dots dark at once", timestamp_ns)
            return

        # ...and too FEW is not a person either — WHEN THIS IS ENABLED.
        #
        # The shipped value is 1, which makes the test below a no-op, and that
        # is deliberate: above 1 it requires dots dark SIMULTANEOUSLY, which
        # structurally misses a swipe (an arm darkens one dot, clears it, then
        # the next). This paragraph used to argue the opposite — the
        # pre-2026-09 position, when a noise floor of ~55 phantom dots per
        # camera made a lone dark dot meaningless. Recalibration took that
        # floor to 0, and min_baseline now removes what was left, so the
        # defence described here is carried by not watching noise in the first
        # place — which costs no latency, unlike requiring a second dot.
        #
        # Counted per camera, not globally: two unrelated single-dot flickers
        # on two cameras are two glitches, not one body.
        # Announce a given dot once, not once per frame — but choose the
        # cluster from the dots NOT yet announced.
        #
        # The report-once guard used to sit AFTER this selection and `return`
        # outright, with no fall-through. So while one dot stayed dark, no
        # other dot on ANY camera could be reported: a player who parked one
        # deep dot walked the rest of the maze unwatched, and a chronically
        # dark dot shadowed every real break behind it indefinitely. The latch
        # is still load-bearing (see below); it just has to filter the
        # candidates rather than veto the verdict.
        candidates = [st for st in dark if st.dot.id not in self._reported_dark]
        if not candidates:
            return

        per_cam: dict[str, list] = {}
        for st in candidates:
            per_cam.setdefault(st.camera, []).append(st)
        # Ties go to the camera holding the DEEPEST dot, not to whichever
        # camera happens to come first in beams.json. Config file ordering must
        # never decide which camera a bust is attributed to.
        best_cam, cluster = max(
            per_cam.items(),
            key=lambda kv: (len(kv[1]), -min(st.last_ratio for st in kv[1])))
        if len(cluster) < self._min_burst:
            return

        if not self._can_emit_break(timestamp_ns):
            return

        # Report the darkest dot of the cluster — the one a body is most
        # squarely blocking — so the bust names a dot on the camera that
        # actually saw it.
        worst = min(cluster, key=lambda st: st.last_ratio)

        # Announce a given dot ONCE, not once per frame.
        #
        # Nothing latched this, so a player standing in a beam produced a fresh
        # BreakConfirmed roughly six times a second for as long as they stood
        # there (the global rate limit was the only cap). In assisted mode —
        # the shipped default — the break does not disarm detection, and every
        # event re-ran StopStopwatch, which cancels and recreates the assisted
        # decision timer. The documented 60 s auto-ABORT could therefore never
        # fire: the game sat in a RUN state with a frozen clock and an
        # in_progress row until a human intervened.
        self._reported_dark.add(worst.dot.id)
        self._confirm_break(worst, timestamp_ns)

    def _can_emit_break(self, timestamp_ns: int) -> bool:
        """Grace period and global rate limit."""
        if not self._armed:
            return False
        if self._arm_time_ns is not None:
            grace_ms = self._arm_grace_ms
            if grace_ms is None:
                grace_ms = self._cfg.detection.arm_grace_ms
            if (timestamp_ns - self._arm_time_ns) / 1_000_000 < grace_ms:
                return False

        cutoff_ns = timestamp_ns - 1_000_000_000
        while self._recent_break_times and self._recent_break_times[0] < cutoff_ns:
            self._recent_break_times.popleft()
        if len(self._recent_break_times) >= self._cfg.detection.global_break_rate_limit:
            self._metrics_emit("vision.break_rate_suppressed", 1.0, {})
            return False
        return True

    def _confirm_break(self, st: _DotState, timestamp_ns: int) -> None:
        self._recent_break_times.append(timestamp_ns)
        log.info("DotDetector: BREAK %s ratio=%.3f run=%s",
                 st.dot.id, st.last_ratio, self._run_id)
        self._metrics_emit("break.detected", st.last_ratio,
                           {"dot_id": st.dot.id, "run_id": self._run_id or ""})
        self._on_break(st.dot.id, st.last_ratio, timestamp_ns)
        self._check_flap(st, timestamp_ns)

    def _check_flap(self, st: _DotState, timestamp_ns: int) -> None:
        """A dot that keeps breaking on its own is faulty, not blocked."""
        window_ns = self._cfg.detection.flap_window_s * 1_000_000_000
        st.flap_times.append(timestamp_ns)
        cutoff = timestamp_ns - window_ns
        while st.flap_times and st.flap_times[0] < cutoff:
            st.flap_times.popleft()
        if len(st.flap_times) >= self._cfg.detection.flap_count_threshold:
            st.auto_masked = True
            self._auto_masked.add(st.dot.id)
            log.warning("DotDetector: auto-masking %s — %d breaks in %d s",
                        st.dot.id, len(st.flap_times),
                        self._cfg.detection.flap_window_s)
            self._metrics_emit("beam.masked", 1.0, {"dot_id": st.dot.id})

    # ------------------------------------------------------------------
    # Masking and telemetry
    # ------------------------------------------------------------------

    def set_baseline(self, dot_id: str, value: float) -> bool:
        """Override one dot's baseline. Returns False if it is not being watched."""
        for cam in self._states.values():
            st = cam.get(dot_id)
            if st is not None:
                st.baseline = value
                return True
        return False

    def apply_baselines(self, values: dict[str, float]) -> int:
        """
        Seed every watched dot from a baseline map, ignoring absent entries.

        Called right after set_maze() so a drifted baseline learned in ATTRACT
        survives the maze change, rather than snapping back to what calibration
        measured days ago.
        """
        applied = 0
        for cam in self._states.values():
            for dot_id, st in cam.items():
                v = values.get(dot_id)
                if v is not None and v > 0:
                    st.baseline = v
                    applied += 1
        return applied

    def mask_dot(self, dot_id: str, masked: bool = True) -> bool:
        """Mask one dot. Returns False if it is not in the lit maze's set."""
        for cam in self._states.values():
            st = cam.get(dot_id)
            if st is not None:
                st.auto_masked = masked
                return True
        return False

    def _sampled(self, st: "_DotState") -> bool:
        """
        A dot this detector will actually look at on the next frame.

        The min_baseline floor is the important half. ratio = value/baseline,
        so a dot whose lit baseline is a handful of counts is dividing noise by
        noise: at baseline 0.68 a drop of 0.34 of one count ends a run. Those
        dots are not beams — find_dots enrols on GRAYSCALE contrast while
        sample_circle measures R-(G+B)/2, and a bright colourless blob (corner
        glare, an edge fitting) passes the first and scores ~0 on the second.
        Not watching them is invariant 5: when the measurement cannot mean
        anything, say nothing.
        """
        return (not st.auto_masked
                and st.baseline > 0
                and st.baseline >= self._min_baseline)

    def stats(self) -> dict:
        """
        Live counts for the GM console and the admin hardware page.

        `watched` counts dots that are actually SAMPLED. It used to count
        states, which meant an uncalibrated capture (every baseline 0) reported
        a full healthy watch count while process_frame skipped every one of
        them — a green console in front of a maze detecting nothing. `blind` is
        reported separately so that case is visible rather than invisible.
        """
        cams = {}
        for cid, states in self._states.items():
            cams[cid] = {
                "watched": sum(1 for st in states.values() if self._sampled(st)),
                "blind": sum(1 for st in states.values()
                             if not st.auto_masked and not self._sampled(st)),
                "dark": sum(1 for st in states.values()
                            if st.is_dark and self._sampled(st)),
                "masked": sum(1 for st in states.values() if st.auto_masked),
            }
        return {
            "maze": self._maze,
            "total": sum(c["watched"] for c in cams.values()),
            "blind": sum(c["blind"] for c in cams.values()),
            "cameras": cams,
        }

    def clear_masks(self) -> int:
        """
        Unmask every auto-masked dot. Returns how many were cleared.

        The admin unmask-all route called this behind a hasattr guard and it did
        not exist on either backend, so it silently skipped and returned ok.
        """
        n = 0
        for cam in self._states.values():
            for st in cam.values():
                if st.auto_masked:
                    st.auto_masked = False
                    st.flap_times.clear()
                    n += 1
        self._auto_masked.clear()
        for times in self._flap_times.values():
            times.clear()
        if n:
            log.info("DotDetector: cleared %d auto-mask(s)", n)
        return n
