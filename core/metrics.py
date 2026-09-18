"""
core/metrics.py — thin metrics emission facade for ScanMania.

Input:  name (str), value (float), tags (dict) via emit().
Output: delegates to a registered sink callable; no-op if none registered.
Invariant: never raises; never does I/O itself; sink is registered at startup
           by runner.py. All metric name constants are defined here.
"""

from __future__ import annotations

import logging
from typing import Callable

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Metric name constants (§9.2)
# Convention: <domain>.<thing>.<verb|state>, snake_case.
# Do not add metric names inline in other modules — extend this list here.
# ---------------------------------------------------------------------------

RUN_STARTED            = "run.started"
RUN_COMPLETED          = "run.completed"
RUN_VOIDED             = "run.voided"
BREAK_DETECTED         = "break.detected"
BREAK_MANUAL           = "break.manual"
# A break that arrived after the run had already been decided. The break path
# is the SLOWER of the two (camera + decode + two queues, against a 20 Hz
# button poll), so a beam genuinely broken just before the stop press can lose
# the race and the run is recorded clean. It used to vanish at DEBUG with no
# metric at all, which made the size of the problem unmeasurable.
BREAK_AFTER_VERDICT    = "break.after_verdict"
# Milliseconds the stopwatch sat frozen while a GM adjudicated an assisted-mode
# break that they then vetoed. The player keeps running during that window and
# the clock does not, so the time is a gift. Measured, not yet corrected — see
# the note in _handle_start_stopwatch.
ASSISTED_HALT_MS       = "run.assisted_halt_ms"
DETECTION_MODE_CHANGED = "detection.mode_changed"
BEAM_MASKED            = "beam.masked"
VISION_RECOVERED       = "vision.recovered"
VISION_STALL           = "vision.stall"
VISION_FPS             = "vision.fps"
RELAY_MISMATCH         = "relay.mismatch"
RELAY_TIMEOUT          = "relay.timeout"
PICO_LINK_DOWN         = "pico.link_down"
PREFLIGHT_FAILED       = "preflight.failed"
COUNTIN_ABORTED        = "countin.aborted"
STATE_TRANSITION       = "state.transition"

# ---------------------------------------------------------------------------
# Sink registry
# ---------------------------------------------------------------------------

_sink: Callable[[str, float, dict], None] | None = None


def configure(sink: Callable[[str, float, dict], None]) -> None:
    """
    Register the metrics sink. Call this once at startup in runner.py.

    The sink is a callable that accepts (name: str, value: float, tags: dict).
    Multiple sinks can be composed by the caller into a single callable before
    passing here. Calling configure() again replaces the previous sink.

    Example sinks defined in §9.1: sqlite (always on), prometheus_textfile,
    cloud.
    """
    global _sink
    _sink = sink


def emit(name: str, value: float = 1.0, tags: dict | None = None) -> None:
    """
    Emit a metric to the registered sink.

    Parameters
    ----------
    name:  metric name constant from this module (e.g. metrics.RUN_COMPLETED)
    value: numeric value — interpretation is metric-specific (e.g. elapsed_ms,
           fps, queue depth, or a simple count of 1.0)
    tags:  arbitrary key/value pairs for slicing in downstream analysis

    This function is safe to call from anywhere: no I/O, never raises, and is
    a no-op when no sink has been configured (e.g. during FSM unit tests).
    """
    if _sink is None:
        return
    try:
        _sink(name, value, tags or {})
    except Exception:
        # Metrics must never crash the caller. Log and swallow.
        log.exception("metrics sink raised on emit(%r, %r)", name, value)
