"""
core/stopwatch.py — monotonic elapsed-time stopwatch for the ScanMania server.

Input:  explicit start/stop/reset calls from runner.py.
Output: elapsed_ms (int), is_running (bool), and a WebSocket clock message dict.
Invariant: uses time.monotonic_ns() only — never datetime.now() or wall clock.
           The server is the sole authority over elapsed time; browsers interpolate.
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

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def elapsed_ms(self) -> int:
        """
        Return elapsed time in whole milliseconds.

        - Idle (never started): 0
        - Running: current live elapsed since last start()
        - Stopped: frozen elapsed at stop() time
        """
        if self._stopped_elapsed_ns is not None:
            return self._stopped_elapsed_ns // 1_000_000
        if self._started_at_ns is not None:
            return (time.monotonic_ns() - self._started_at_ns) // 1_000_000
        return 0

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
