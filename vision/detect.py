"""
vision/detect.py — dot detection with hysteresis, rate limiting, and flap detection.

Inputs:  decoded frames from CameraStream, BeamsConfig, on_break/on_clear callbacks
Outputs: BreakConfirmed events via on_break; BeamCleared via on_clear
Invariant: suppresses all events when camera is stalled; never emits a break in the
           first arm_grace_ms after arming; global rate limit (>6 breaks/s) suppresses
           haze-puff bursts. Per-beam flap detector auto-masks after 5 breaks in 60 s.
"""

from __future__ import annotations

import collections
import logging
import time
from typing import Callable

import cv2
import numpy as np

from config.loader import BeamConfig, BeamsConfig

log = logging.getLogger(__name__)

# How many top-percentile pixels to use for the ROI sample
_TOP_FRACTION = 0.20


class _BeamState:
    """Per-beam tracking state for the hysteresis FSM."""

    def __init__(self, beam: BeamConfig, consecutive_frames_needed: int) -> None:
        self.beam = beam
        self.n = consecutive_frames_needed
        # "broken" hysteresis: count consecutive frames below break_ratio
        self.break_consecutive: int = 0
        # "clear" hysteresis: count consecutive frames above clear_ratio
        self.clear_consecutive: int = 0
        # Current logical state: True = broken, False = clear
        self.is_broken: bool = False
        # Flap tracking: timestamps (monotonic_ns) of recent break confirmations
        self.flap_times: collections.deque[int] = collections.deque()
        # Auto-masked by flap detector (separate from config masked flag)
        self.auto_masked: bool = False


class DotDetector:
    """
    Processes decoded camera frames to detect beam breaks via dot disappearance.

    Uses hysteresis (N consecutive frames above/below threshold) to avoid chatter.
    Rate-limits global break events and tracks per-beam flap behaviour.
    """

    def __init__(
        self,
        beams_config: BeamsConfig,
        on_break: Callable,
        on_clear: Callable,
        metrics_emit: Callable,
        on_fault: Callable | None = None,
    ) -> None:
        self._cfg = beams_config
        self._on_break = on_break       # (beam_id: str, ratio: float, ts_ns: int) → None
        self._on_clear = on_clear       # (beam_id: str, ts_ns: int) → None
        self._on_fault = on_fault       # (beam_id: str, ts_ns: int) → None
        self._faulted: set[str] = set()  # channels already reported dark
        self._metrics_emit = metrics_emit

        n = beams_config.detection.consecutive_frames
        self._states: dict[str, _BeamState] = {
            b.id: _BeamState(b, n) for b in beams_config.beams
        }

        self._armed: bool = False
        self._run_id: str | None = None
        self._arm_time_ns: int | None = None
        self._arm_grace_ms: int | None = None

        # Global rate limit: timestamps of recent break events
        self._recent_break_times: collections.deque[int] = collections.deque()

        # Stall suppression
        self._stalled: bool = False

        # Watch-list: the channel ids lit by the current maze shape. None
        # means 'watch everything' (single-shape setups and the fakes).
        # A dot that goes dark because its relay opened is not in this set,
        # so nothing looks at it — that is how a shape change stops looking
        # like a mass beam break.
        self._watching: set[str] | None = None
        # Channels that just lit up; they are still coming on, so ignore
        # them until this monotonic_ns deadline passes.
        self._settling: dict[str, int] = {}

        # External baseline values (injected from BaselineManager)
        self._baselines: dict[str, float] = {
            b.id: b.baseline for b in beams_config.beams
        }

    # ------------------------------------------------------------------
    # Arm / disarm
    # ------------------------------------------------------------------

    def arm(self, run_id: str, grace_ms: int) -> None:
        """Arm detection. Break events are suppressed for grace_ms after this call."""
        self._armed = True
        self._run_id = run_id
        self._arm_time_ns = time.monotonic_ns()
        # The caller's grace wins. This used to be logged and then dropped,
        # with _can_emit_break silently reading the config value instead, so
        # passing a grace_ms had no effect whatsoever.
        self._arm_grace_ms = grace_ms
        log.info("DotDetector armed for run='%s' grace_ms=%d", run_id, grace_ms)
        # Reset all hysteresis counters at arm time
        for st in self._states.values():
            st.break_consecutive = 0
            st.clear_consecutive = 0
            st.is_broken = False

    def disarm(self) -> None:
        self._armed = False
        self._run_id = None
        self._arm_time_ns = None
        log.info("DotDetector disarmed")

    # ------------------------------------------------------------------
    # Stall notification (called by CameraStream via callback chain)
    # ------------------------------------------------------------------

    def set_stalled(self, stalled: bool) -> None:
        self._stalled = stalled
        if stalled:
            log.warning("DotDetector: stall suppression active — no break events will fire")

    # ------------------------------------------------------------------
    # Baseline injection
    # ------------------------------------------------------------------

    def set_baseline(self, beam_id: str, value: float) -> None:
        self._baselines[beam_id] = value

    # ------------------------------------------------------------------
    # Watch-list — swapped whenever the maze shape changes
    # ------------------------------------------------------------------

    def set_watchlist(self, channel_ids, settle_ms: int = 250) -> None:
        """
        Replace the set of channels detection looks at.

        Call this on every preset change. Channels that leave the list are
        dropped immediately: their dots go dark because the relay opened, and a
        question nobody asks cannot produce a wrong answer.

        Channels that were already lit keep their hysteresis state untouched, so
        a player breaking one of them during the switch is still caught. At ~80%
        shape overlap that is most of the maze.

        Channels that are newly lit get settle_ms before they can report
        anything — they are physically still coming on.

        Pass None to watch everything.
        """
        if channel_ids is None:
            self._watching = None
            self._settling.clear()
            return

        new = set(channel_ids)
        previous = self._watching if self._watching is not None else set(self._states)
        now_ns = time.monotonic_ns()
        deadline = now_ns + settle_ms * 1_000_000

        for cid in new - previous:
            self._settling[cid] = deadline
            st = self._states.get(cid)
            if st is not None:
                st.break_consecutive = 0
                st.clear_consecutive = 0
                st.is_broken = False

        for cid in previous - new:
            self._settling.pop(cid, None)

        self._watching = new
        log.info(
            "DotDetector watch-list: %d channels (%d continuing, %d settling)",
            len(new), len(new & previous), len(new - previous),
        )

    def _is_watched(self, beam_id: str, timestamp_ns: int) -> bool:
        if self._watching is not None and beam_id not in self._watching:
            return False
        deadline = self._settling.get(beam_id)
        if deadline is not None:
            if timestamp_ns < deadline:
                return False
            del self._settling[beam_id]
        return True

    # ------------------------------------------------------------------
    # Per-frame processing
    # ------------------------------------------------------------------

    def process_frame(
        self, frame: np.ndarray, timestamp_ns: int, camera_id: str | None = None
    ) -> None:
        """
        Process one decoded frame. Applies detection logic to every non-masked beam.

        camera_id routes the frame to the beams that this camera actually sees.
        ROIs are frame pixels belonging to one camera's view, so sampling a beam
        against a different camera's frame reads meaningless coordinates. With
        several cameras and no routing, every beam was sampled against every
        frame. Pass None only when there is a single camera (or a fake).

        Suppresses all events when stalled. Suppresses break events during grace period
        and when the global rate limit is exceeded.
        """
        if self._stalled:
            return

        for beam_id, st in self._states.items():
            beam = st.beam
            # Only beams this camera can see.
            if camera_id is not None and beam.camera != camera_id:
                continue
            # Only channels the current maze shape has lit, and not still settling.
            if not self._is_watched(beam_id, timestamp_ns):
                continue
            # Skip masked beams (both config-level and auto-masked by flap detector)
            if beam.masked or st.auto_masked:
                continue

            signal = self._channel_signal(frame, beam)
            if signal is None:
                # No dot on this channel has a baseline yet — nothing to compare.
                continue
            dark, total, worst = signal

            # Every dot on a channel that is commanded ON went dark at once. A
            # body blocks part of a colinear array, never all of it. This is the
            # relay not firing, the PSU dropping, or the view being occluded.
            # Reporting it as a break would end a run for a hardware fault.
            if total >= 2 and dark == total:
                self._report_channel_fault(beam, timestamp_ns)
                continue

            # Any dark dot means the beam is broken. Do not average: one dark dot
            # out of five averages to 0.8 and would never cross break_ratio.
            broken = dark > 0
            self._update_group_hysteresis(st, beam, broken, worst, timestamp_ns)

    def _report_channel_fault(self, beam: BeamConfig, timestamp_ns: int) -> None:
        """All dots dark on a lit channel — a fault for the GM, never a bust."""
        if getattr(self, "_faulted", None) is None:
            self._faulted = set()
        if beam.id in self._faulted:
            return
        self._faulted.add(beam.id)
        log.error(
            "DotDetector: channel '%s' is commanded ON but all %d dots are dark "
            "— relay, PSU or occlusion. Not reporting a break.",
            beam.id, len(beam.dots),
        )
        self._metrics_emit("vision.channel_dark", value=1, tags={"beam_id": beam.id})
        if self._on_fault is not None:
            self._on_fault(beam.id, timestamp_ns)

    def _update_group_hysteresis(
        self, st: _BeamState, beam: BeamConfig, broken: bool,
        worst_ratio: float, timestamp_ns: int
    ) -> None:
        """Hysteresis over the channel's dot group rather than a single ratio."""
        self._faulted = getattr(self, "_faulted", set())
        self._faulted.discard(beam.id)

        if not st.is_broken:
            if broken:
                st.break_consecutive += 1
                st.clear_consecutive = 0
                if st.break_consecutive >= st.n and self._can_emit_break(timestamp_ns):
                    st.is_broken = True
                    st.break_consecutive = 0
                    self._confirm_break(st, beam, worst_ratio, timestamp_ns)
            else:
                st.break_consecutive = 0
        else:
            if not broken and worst_ratio > beam.clear_ratio:
                st.clear_consecutive += 1
                st.break_consecutive = 0
                if st.clear_consecutive >= st.n:
                    st.is_broken = False
                    st.clear_consecutive = 0
                    log.info("DotDetector: beam '%s' CLEARED ratio=%.3f", beam.id, worst_ratio)
                    self._on_clear(beam.id, timestamp_ns)
                    self._metrics_emit("break.cleared", value=1, tags={"beam_id": beam.id})
            else:
                st.clear_consecutive = 0

    def _compute_ratio(self, frame: np.ndarray, beam: BeamConfig) -> float | None:
        """Sample the ROI and return brightness ratio against baseline. None if baseline=0."""
        baseline = self._baselines.get(beam.id, beam.baseline)
        if baseline <= 0:
            return None
        sample = self._sample_roi(frame, beam)
        return sample / baseline

    def _channel_signal(self, frame, beam: BeamConfig) -> tuple[int, int, float] | None:
        """
        Sample every dot on this relay channel.

        Returns (dark_count, total_dots, worst_ratio), or None when no dot has a
        usable baseline yet.

        One relay drives 5 colinear dots. A body blocks one or two of them; it
        cannot plausibly extinguish all five at once. So the count discriminates
        a player from a hardware fault, which a single averaged ratio cannot —
        average one dark dot across five and the mean is 0.8, nowhere near
        break_ratio, and the break is missed entirely.
        """
        dark = 0
        total = 0
        worst = 1.0
        for dot in beam.dots:
            if dot.masked:
                continue
            baseline = dot.baseline or self._baselines.get(beam.id, beam.baseline)
            if baseline <= 0:
                continue
            total += 1
            ratio = self._sample_dot(frame, dot) / baseline
            worst = min(worst, ratio)
            if ratio < beam.break_ratio:
                dark += 1
        if total == 0:
            return None
        return dark, total, worst

    def _update_hysteresis(
        self, st: _BeamState, beam: BeamConfig, ratio: float, timestamp_ns: int
    ) -> None:
        """Apply hysteresis FSM: count consecutive frames and fire callbacks."""
        if not st.is_broken:
            # Looking for a break
            if ratio < beam.break_ratio:
                st.break_consecutive += 1
                st.clear_consecutive = 0
                if st.break_consecutive >= st.n:
                    if self._can_emit_break(timestamp_ns):
                        st.is_broken = True
                        st.break_consecutive = 0
                        self._confirm_break(st, beam, ratio, timestamp_ns)
            else:
                st.break_consecutive = 0
        else:
            # Looking for a clear
            if ratio > beam.clear_ratio:
                st.clear_consecutive += 1
                st.break_consecutive = 0
                if st.clear_consecutive >= st.n:
                    st.is_broken = False
                    st.clear_consecutive = 0
                    log.info("DotDetector: beam '%s' CLEARED ratio=%.3f", beam.id, ratio)
                    self._on_clear(beam.id, timestamp_ns)
                    self._metrics_emit("break.cleared", value=1, tags={"beam_id": beam.id})
            else:
                st.clear_consecutive = 0

    def _can_emit_break(self, timestamp_ns: int) -> bool:
        """Check grace period and global rate limit. Returns False if suppressed."""
        # Grace period: ignore breaks immediately after arming
        if self._armed and self._arm_time_ns is not None:
            grace_ms = getattr(self, "_arm_grace_ms", None)
            if grace_ms is None:
                grace_ms = self._cfg.detection.arm_grace_ms
            elapsed_ms = (timestamp_ns - self._arm_time_ns) / 1_000_000
            if elapsed_ms < grace_ms:
                log.debug("DotDetector: break suppressed — grace period (%.0f ms)", elapsed_ms)
                return False

        # Must be armed to emit break events
        if not self._armed:
            return False

        # Global rate limit: prune old events
        cutoff_ns = timestamp_ns - 1_000_000_000  # 1 second ago
        while self._recent_break_times and self._recent_break_times[0] < cutoff_ns:
            self._recent_break_times.popleft()

        limit = self._cfg.detection.global_break_rate_limit
        if len(self._recent_break_times) >= limit:
            log.warning(
                "DotDetector: global rate limit hit (%d breaks/s) — suppressing burst",
                limit,
            )
            self._metrics_emit("vision.break_rate_suppressed", value=1, tags={})
            return False

        return True

    def _confirm_break(
        self, st: _BeamState, beam: BeamConfig, ratio: float, timestamp_ns: int
    ) -> None:
        """Fire break callback, update rate-limit buffer, check flap detector."""
        log.info(
            "DotDetector: BREAK confirmed beam='%s' ratio=%.3f run='%s'",
            beam.id, ratio, self._run_id,
        )
        self._recent_break_times.append(timestamp_ns)
        self._metrics_emit(
            "break.detected",
            value=ratio,
            tags={"beam_id": beam.id, "run_id": self._run_id or ""},
        )
        self._on_break(beam.id, ratio, timestamp_ns)

        # Flap detection (counts breaks while idle — but track always for the window)
        now_ns = timestamp_ns
        window_ns = self._cfg.detection.flap_window_s * 1_000_000_000
        st.flap_times.append(now_ns)
        cutoff = now_ns - window_ns
        while st.flap_times and st.flap_times[0] < cutoff:
            st.flap_times.popleft()

        threshold = self._cfg.detection.flap_count_threshold
        if len(st.flap_times) >= threshold and not self._armed:
            log.warning(
                "DotDetector: beam '%s' flapping (%d times in %d s) — auto-masking",
                beam.id, len(st.flap_times), self._cfg.detection.flap_window_s,
            )
            st.auto_masked = True
            self._metrics_emit("beam.masked", value=1, tags={"beam_id": beam.id, "reason": "flap"})

    # ------------------------------------------------------------------
    # ROI sampling
    # ------------------------------------------------------------------

    def _sample_dot(self, frame: np.ndarray, dot) -> float:
        """Sample one dot's ROI. Same maths as _sample_roi, per dot."""
        return self._sample_circle(frame, dot.cx, dot.cy, dot.r)

    def _sample_roi(self, frame: np.ndarray, beam: BeamConfig) -> float:
        """Legacy single-ROI sample, kept for the admin overlay and baselines."""
        return self._sample_circle(frame, beam.roi.cx, beam.roi.cy, beam.roi.r)

    def _sample_circle(self, frame: np.ndarray, cx: int, cy: int, r: int) -> float:
        """
        Sample mean of the top 20% brightest pixels in the circular ROI on the
        red-isolated image: R - (G+B)/2, clipped to [0, 255].
        """
        h, w = frame.shape[:2]

        # Bounding box of the circle, clamped to frame
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

        # Circular mask
        ys, xs = np.ogrid[y0:y1, x0:x1]
        mask = ((xs - cx) ** 2 + (ys - cy) ** 2) <= r ** 2
        pixels = isolated[mask]
        if pixels.size == 0:
            return 0.0

        # Top 20% brightest pixels
        k = max(1, int(len(pixels) * _TOP_FRACTION))
        top_k = np.partition(pixels, -k)[-k:]
        return float(np.mean(top_k))

    def update_baseline_from_frame(self, frame: np.ndarray, beam_id: str) -> float:
        """
        Capture a fresh baseline for one beam from the current frame.
        Returns the sampled brightness value.
        """
        st = self._states.get(beam_id)
        if st is None:
            raise KeyError(f"Unknown beam_id: '{beam_id}'")
        value = self._sample_roi(frame, st.beam)
        self._baselines[beam_id] = value
        log.info("DotDetector: baseline updated beam='%s' value=%.1f", beam_id, value)
        return value

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def is_armed(self) -> bool:
        return self._armed
