"""
tests/test_stopwatch.py — unit tests for core/stopwatch.py.

Verifies monotonic timing, state transitions, and the WebSocket clock message.
No mocks needed — the stopwatch uses time.monotonic_ns() which is always available.
"""

from __future__ import annotations

import time

import pytest

from core.stopwatch import Stopwatch, server_clock_message


class TestStopwatchInitialState:
    def test_elapsed_ms_is_zero_before_start(self):
        sw = Stopwatch()
        assert sw.elapsed_ms() == 0

    def test_is_running_false_before_start(self):
        sw = Stopwatch()
        assert sw.is_running is False

    def test_started_at_ns_is_none_before_start(self):
        sw = Stopwatch()
        assert sw.started_at_ns is None


class TestStopwatchStart:
    def test_is_running_after_start(self):
        sw = Stopwatch()
        sw.start()
        assert sw.is_running is True

    def test_started_at_ns_is_set_after_start(self):
        sw = Stopwatch()
        sw.start()
        assert sw.started_at_ns is not None
        assert isinstance(sw.started_at_ns, int)

    def test_elapsed_ms_positive_after_start(self):
        sw = Stopwatch()
        sw.start()
        time.sleep(0.01)  # 10 ms
        assert sw.elapsed_ms() > 0

    def test_elapsed_ms_increases_while_running(self):
        sw = Stopwatch()
        sw.start()
        e1 = sw.elapsed_ms()
        time.sleep(0.01)
        e2 = sw.elapsed_ms()
        assert e2 >= e1

    def test_elapsed_ms_is_in_milliseconds(self):
        sw = Stopwatch()
        sw.start()
        time.sleep(0.1)  # 100 ms
        sw.stop()
        # Should be roughly 100 ms; allow wide margin for slow CI environments.
        assert 50 <= sw.elapsed_ms() <= 500

    def test_restart_resets_elapsed(self):
        sw = Stopwatch()
        sw.start()
        time.sleep(0.05)
        sw.stop()
        frozen = sw.elapsed_ms()
        sw.start()  # fresh start
        time.sleep(0.001)
        # After restart, elapsed_ms is the new run, not the old one.
        assert sw.elapsed_ms() < frozen


class TestStopwatchStop:
    def test_is_running_false_after_stop(self):
        sw = Stopwatch()
        sw.start()
        sw.stop()
        assert sw.is_running is False

    def test_elapsed_frozen_after_stop(self):
        sw = Stopwatch()
        sw.start()
        time.sleep(0.05)
        sw.stop()
        e1 = sw.elapsed_ms()
        time.sleep(0.05)
        e2 = sw.elapsed_ms()
        assert e1 == e2

    def test_started_at_ns_none_after_stop(self):
        sw = Stopwatch()
        sw.start()
        sw.stop()
        assert sw.started_at_ns is None

    def test_stop_before_start_gives_zero(self):
        sw = Stopwatch()
        sw.stop()
        assert sw.elapsed_ms() == 0

    def test_stop_idempotent(self):
        sw = Stopwatch()
        sw.start()
        time.sleep(0.02)
        sw.stop()
        e1 = sw.elapsed_ms()
        sw.stop()  # second stop should be no-op
        e2 = sw.elapsed_ms()
        assert e1 == e2

    def test_is_running_false_double_stop(self):
        sw = Stopwatch()
        sw.start()
        sw.stop()
        sw.stop()
        assert sw.is_running is False


class TestStopwatchReset:
    def test_reset_from_running(self):
        sw = Stopwatch()
        sw.start()
        time.sleep(0.01)
        sw.reset()
        assert sw.elapsed_ms() == 0
        assert sw.is_running is False
        assert sw.started_at_ns is None

    def test_reset_from_stopped(self):
        sw = Stopwatch()
        sw.start()
        sw.stop()
        sw.reset()
        assert sw.elapsed_ms() == 0
        assert sw.is_running is False

    def test_reset_from_idle(self):
        sw = Stopwatch()
        sw.reset()
        assert sw.elapsed_ms() == 0

    def test_can_start_after_reset(self):
        sw = Stopwatch()
        sw.start()
        sw.stop()
        sw.reset()
        sw.start()
        assert sw.is_running is True


class TestStopwatchAccuracy:
    def test_elapsed_ms_returns_int(self):
        sw = Stopwatch()
        assert isinstance(sw.elapsed_ms(), int)

    def test_elapsed_ms_returns_int_while_running(self):
        sw = Stopwatch()
        sw.start()
        assert isinstance(sw.elapsed_ms(), int)

    def test_monotonic_ns_used_not_wall_clock(self):
        """Verify the stopwatch uses time.monotonic_ns by checking started_at_ns
        is in the same ballpark as time.monotonic_ns()."""
        before = time.monotonic_ns()
        sw = Stopwatch()
        sw.start()
        after = time.monotonic_ns()
        assert before <= sw.started_at_ns <= after


class TestServerClockMessage:
    def test_idle_state(self):
        sw = Stopwatch()
        msg = server_clock_message(sw)
        assert msg["state"] == "idle"
        assert msg["started_at_mono_ns"] is None
        assert msg["elapsed_ms"] == 0
        assert isinstance(msg["server_mono_now_ns"], int)

    def test_running_state(self):
        sw = Stopwatch()
        sw.start()
        msg = server_clock_message(sw)
        assert msg["state"] == "running"
        assert msg["started_at_mono_ns"] is not None
        assert msg["elapsed_ms"] >= 0

    def test_stopped_state(self):
        sw = Stopwatch()
        sw.start()
        time.sleep(0.01)
        sw.stop()
        msg = server_clock_message(sw)
        assert msg["state"] == "stopped"
        assert msg["elapsed_ms"] > 0
        # started_at_mono_ns is None once stopped
        assert msg["started_at_mono_ns"] is None

    def test_server_mono_now_ns_is_current(self):
        sw = Stopwatch()
        before = time.monotonic_ns()
        msg = server_clock_message(sw)
        after = time.monotonic_ns()
        assert before <= msg["server_mono_now_ns"] <= after

    def test_message_keys_present(self):
        sw = Stopwatch()
        msg = server_clock_message(sw)
        for key in ["state", "started_at_mono_ns", "server_mono_now_ns", "elapsed_ms"]:
            assert key in msg

    def test_elapsed_ms_matches_stopwatch(self):
        sw = Stopwatch()
        sw.start()
        time.sleep(0.05)
        sw.stop()
        msg = server_clock_message(sw)
        assert msg["elapsed_ms"] == sw.elapsed_ms()
