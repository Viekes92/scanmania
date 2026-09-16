"""
inputs/fake.py — fake Pico input backend for development without hardware.

Inputs:  programmatic trigger calls from fake_run.py or test code
Outputs: same on_event / on_link_change callbacks as PicoLink; LED commands logged
Invariant: MANDATORY. Allows the full game to run on a laptop with no Pico attached.
           Implements the same interface as PicoLink so runner.py accepts either.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Callable

log = logging.getLogger(__name__)

# Valid LED modes, mirroring PicoLink
LED_MODES = frozenset({"off", "on", "pulse", "flash"})


class FakeInputs:
    """
    Drop-in replacement for PicoLink that runs entirely in process.

    Events are injected via trigger_input(). LED commands are logged but not
    acted on. is_connected is always True (the link never goes down unless
    set_connected(False) is called explicitly in tests).

    Events are available via the async events() generator (used by runner.py)
    and optionally forwarded to on_event / on_link_change callbacks.
    """

    def __init__(
        self,
        on_event: Callable | None = None,
        on_link_change: Callable | None = None,
    ) -> None:
        self._on_event = on_event          # (input_id: str, state: int, host_ns: int) → None
        self._on_link_change = on_link_change
        self._connected: bool = True
        self._last_heartbeat_ns: int | None = time.monotonic_ns()
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=256)
        # Mirrors the real backends' level cache, so a test can put the plate
        # down and have it STAY down.
        self._levels: dict[str, int] = {}

    # ------------------------------------------------------------------
    # Main loop — no-op; events are injected programmatically
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """
        No-op loop — keeps the coroutine alive so runner.py can await it
        alongside other service tasks. Events are injected via trigger_input().
        """
        log.info("FakeInputs: running (no hardware; events injected via trigger_input)")
        while True:
            await asyncio.sleep(3600)

    # ------------------------------------------------------------------
    # Test / fake_run.py injection API
    # ------------------------------------------------------------------

    async def trigger_input(self, input_id: str, state: int) -> None:
        """
        Simulate a button press (state=1) or release (state=0).

        Puts the event into the internal queue (consumed by events()) and
        optionally dispatches the on_event callback.
        """
        if not self._connected:
            log.debug("FakeInputs: trigger_input ignored — not connected")
            return
        host_ns = time.monotonic_ns()
        self._levels[input_id] = int(state)
        log.debug("FakeInputs: trigger_input %s=%d", input_id, state)
        self._queue.put_nowait((input_id, state, host_ns))
        if self._on_event:
            self._on_event(input_id, state, host_ns)

    def input_level(self, input_id: str) -> int | None:
        """Current level, or None if this input has never been triggered."""
        if not self._connected:
            return None
        return self._levels.get(input_id)

    def set_connected(self, value: bool) -> None:
        """Explicitly change link state — for connection-failure tests."""
        if self._connected != value:
            self._connected = value
            log.info("FakeInputs: link %s", "UP" if value else "DOWN")
            if self._on_link_change:
                self._on_link_change(value)

    async def events(self):
        """Async generator yielding (input_id, state, host_ns) tuples."""
        while True:
            yield await self._queue.get()

    # ------------------------------------------------------------------
    # Commands to Pico — logged, not acted on
    # ------------------------------------------------------------------

    async def send_led(self, led_id: str, mode: str) -> None:
        if mode not in LED_MODES:
            raise ValueError(f"Invalid LED mode '{mode}'")
        log.info("FakeInputs: LED %s %s", led_id, mode)

    async def send_ping(self) -> None:
        log.debug("FakeInputs: PING")

    async def send_reset(self) -> None:
        log.info("FakeInputs: RESET")

    def pico_ms_to_host_ns(self, pico_ms: int) -> int:
        """Fake clock conversion — just returns current host time."""
        return time.monotonic_ns()

    # ------------------------------------------------------------------
    # Properties — mirror PicoLink interface exactly
    # ------------------------------------------------------------------

    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def last_heartbeat_ns(self) -> int | None:
        # Fake: pretend heartbeat just happened
        return time.monotonic_ns()
