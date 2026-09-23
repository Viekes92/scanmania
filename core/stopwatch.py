"""
core/stopwatch.py — monotonic elapsed-time stopwatch for the ScanMania server.

Input:  explicit start/stop/reset calls from runner.py, plus time penalties.
Output: elapsed_ms (int), is_running (bool), and a WebSocket clock message dict.
Invariant: uses time.monotonic_ns() only — never datetime.now() or wall clock.
           The server is the sole authority over elapsed time; browsers interpolate.
Invariant: a penalty is a constant added to the measured interval, never a
           manipulation of the clock. raw_elapsed_ms() is always what the
           monotonic clock actually measured, so the two numbers can be
           recorded separately and a penalty can be revoked exactly.
"""

from __future__ import annotations

import time


class Stopwatch:
    """
    Simple monotonic stopwatch.

    States
    ------
    idle (not yet started):  elapsed_ms() == 0, is_running == False
    running:                 elapsed_ms() returns live elapsed, is_running == True
    stopped:                 elapsed_ms() returns final frozen value, is_running == False
    """

    def __init__(self) -> None:
        self._started_at_ns: int | None = None   # monotonic_ns at last start()
        self._stopped_elapsed_ns: int | None = None  # frozen elapsed when stopped
        # Accumulated time penalties for this run, in nanoseconds.
        #
        # Held here rather than added at scoring time so that BOTH displays and
        # the GM console show a penalty the instant it lands — they render
        # elapsed_ms from the broadcast and interpolate between frames, so a
        # penalty applied anywhere else would be invisible until the run ended.
        # raw_elapsed_ms() still reports what the clock measured, so the run
        # record can carry the honest time and the penalty separately.
        self._penalty_ns: int = 0

    # ------------------------------------------------------------------
    # Control
    # ------------------------------------------------------------------

    def start(self) -> None:
        """
        Start (or restart) the stopwatch.

        If the stopwatch was previously stopped, calling start() again begins a
        fresh measurement (it does NOT resume from the stopped elapsed). Runner.py
        handles the assisted-mode "resume from halted elapsed" by adjusting
        started_at_ns manually if needed — this class stays simple.
        """
        self._started_at_ns = time.monotonic_ns()
        self._stopped_elapsed_ns = None
        # A fresh measurement is a fresh run. resume() deliberately does NOT do
        # this: it is the assisted-veto path, mid-run, and the penalties already
        # earned still stand.
        self._penalty_ns = 0

    def resume(self) -> None:
        """
        Resume from the stopped elapsed value.

        Used by assisted-mode veto: the stopwatch was halted, the GM vetoed
        the break, and the clock resumes from where it was frozen.
        If not stopped, this is a no-op.
        """
        if self._stopped_elapsed_ns is None:
            return  # not stopped, nothing to resume
        # Set started_at so that current elapsed = frozen elapsed + time since resume
        self._started_at_ns = time.monotonic_ns() - self._stopped_elapsed_ns
        self._stopped_elapsed_ns = None

    def stop(self) -> None:
        """
        Stop the stopwatch and freeze the elapsed value.

        Idempotent: calling stop() when already stopped has no effect.
        Calling stop() before start() records 0 ns elapsed.
        """
        if self._stopped_elapsed_ns is not None:
            return  # already stopped
        now_ns = time.monotonic_ns()
        if self._started_at_ns is None:
            self._stopped_elapsed_ns = 0
        else:
            self._stopped_elapsed_ns = now_ns - self._started_at_ns
        self._started_at_ns = None

    def reset(self) -> None:
        """
        Reset to idle state. elapsed_ms() returns 0, is_running is False.
        """
        self._started_at_ns = None
        self._stopped_elapsed_ns = None
        self._penalty_ns = 0

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def elapsed_ms(self) -> int:
        """
        Return the player's time in whole milliseconds, penalties INCLUDED.

        This is the number shown on both displays, ranked on the leaderboard
        and written to the run row, because it is the time the player actually
        achieved under the rules.

        - Idle (never started): 0
        - Running: live elapsed since start(), plus penalties so far
        - Stopped: frozen elapsed at stop() time, plus penalties
        """
        return self.raw_elapsed_ms() + self.penalty_ms

    def raw_elapsed_ms(self) -> int:
        """
        What the monotonic clock measured, with no penalties applied.

        Kept separate so a result can say "42.1s + 10.0s penalty = 52.1s"
        rather than presenting one number nobody can check.
        """
        if self._stopped_elapsed_ns is not None:
            return self._stopped_elapsed_ns // 1_000_000
        if self._started_at_ns is not None:
            return (time.monotonic_ns() - self._started_at_ns) // 1_000_000
        return 0

    # ------------------------------------------------------------------
    # Time penalties
    # ------------------------------------------------------------------

    def add_penalty_ms(self, ms: int) -> int:
        """
        Add a time penalty. Returns the new penalty total in ms.

        Applies whether the stopwatch is running or stopped: a penalty
        adjudicated after the player hit the button is still their penalty.
        """
        self._penalty_ns += max(0, int(ms)) * 1_000_000
        return self.penalty_ms

    def revoke_penalty_ms(self, ms: int) -> int:
        """
        Take a penalty back — the GM vetoed it. Returns the new total.

        Clamped at zero so a double-veto, or a veto of a penalty that was never
        applied, can never hand a player a negative time.
        """
        self._penalty_ns = max(0, self._penalty_ns - max(0, int(ms)) * 1_000_000)
        return self.penalty_ms

    @property
    def penalty_ms(self) -> int:
        """Total penalty applied to the current run, in whole milliseconds."""
        return self._penalty_ns // 1_000_000

    @property
    def is_running(self) -> bool:
        """True only while the stopwatch has been started and not yet stopped."""
        return self._started_at_ns is not None and self._stopped_elapsed_ns is None

    @property
    def started_at_ns(self) -> int | None:
        """
        The monotonic_ns value at the moment start() was called, or None if the
        stopwatch is idle or stopped. Used by the WebSocket clock message so that
        browsers can interpolate elapsed time locally between server broadcasts.
        """
        return self._started_at_ns


# ---------------------------------------------------------------------------
# WebSocket clock message
# ---------------------------------------------------------------------------

def server_clock_message(stopwatch: Stopwatch) -> dict:
    """
    Build the dict broadcast over WebSocket at ~10 Hz for browser interpolation.

    The browser uses started_at_mono_ns and server_mono_now_ns to compute a
    local time-of-flight estimate and interpolates at 60 fps until the next
    server message. On each received message it hard-corrects to elapsed_ms.

    Keys
    ----
    state:               "idle" | "running" | "stopped"
    started_at_mono_ns:  int or None — the monotonic_ns of start(); None if idle/stopped
    server_mono_now_ns:  int — current server monotonic_ns at message build time
    elapsed_ms:          int — authoritative elapsed at message build time
    """
    now_ns = time.monotonic_ns()

    if stopwatch.is_running:
        state = "running"
    elif stopwatch._stopped_elapsed_ns is not None:
        state = "stopped"
    else:
        state = "idle"

    return {
        "state": state,
        "started_at_mono_ns": stopwatch.started_at_ns,
        "server_mono_now_ns": now_ns,
        "elapsed_ms": stopwatch.elapsed_ms(),
    }
