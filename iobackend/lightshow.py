"""
iobackend/lightshow.py — drives the room lights from the FSM state.

Inputs:  light cues from mazes.yaml, the current FSM state, the DMX controller
Outputs: set_light() calls on the DMX owner; nothing talks to Art-Net directly
Invariant: RUN states are always fully dark, whatever the cue file says and
           whatever the GM has set. The detector samples raw brightness inside
           each dot's ROI with no background subtraction, so ambient light
           raises the reading and a genuinely broken beam can still read above
           break_ratio — a MISSED break, where a player runs clean through a
           beam they broke. That fails in the direction nobody sees.
Invariant: the entrance light is never touched. The DMX layer refuses to dim it.

A cue is a list of steps; each step sets levels and holds. Loop for ambient
states, one-shot for moments:

    attract:
      loop: true
      steps:
        - {left: 12, right: 0,  ms: 1400}
        - {left: 0,  right: 12, ms: 1400}
    finished:
      steps:
        - {left: 255, right: 255, ms: 90, fade: false}
        - {left: 0,   right: 0,   ms: 90, fade: false}
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

log = logging.getLogger(__name__)

# States where the container must be dark for detection to be trustworthy.
# Not configurable on purpose: this is a correctness property, not a look.
_DARK_STATES = frozenset({
    "COUNTDOWN", "RUN_SEG_1", "RUN_SEG_2", "RUN_SEG_3",
})


class LightCuePlayer:
    """
    Plays one cue at a time, swapping when the FSM state changes.

    Holds no DMX socket of its own — every level goes through the controller
    that owns the universe, because an Art-Net frame carries all 512 channels
    and a second sender would zero the first one's work twice a second.
    """

    def __init__(self, dmx: Any, cues: dict[str, Any] | None = None) -> None:
        self._dmx = dmx
        self._cues = cues or {}
        self._state: str | None = None
        self._task: asyncio.Task | None = None
        # "Work lights": the GM's solid-on override for loading and unloading.
        # Suspends cues, but never survives into a RUN state.
        # Tri-state, not a boolean.
        #   True  -> GM forced them on  (loading, unloading)
        #   False -> GM forced them OFF (which MASTER could not do before: the
        #            master cue simply overrode it, so there was no way to
        #            stand in a dark container and look at the lasers)
        #   None  -> no override; the cue table decides
        self._work_lights = True

    # ------------------------------------------------------------------
    # Control
    # ------------------------------------------------------------------

    @property
    def work_lights(self) -> bool:
        return self._work_lights

    def set_work_lights(self, on: bool) -> None:
        self._work_lights = on
        log.info("work lights %s", "ON" if on else "OFF")
        self._restart()

    def set_state(self, state: str) -> None:
        """Called on every FSM transition. Cheap when the state has not changed."""
        if state == self._state:
            return
        self._state = state
        self._restart()

    def stop(self) -> None:
        self._cancel()

    def reset(self) -> None:
        """
        Forget the current state so the next set_state() re-applies its cue.

        set_state() is a no-op when the state has not changed, which is what
        keeps it cheap on every transition. After something external has taken
        the lights away — a power-down that was then released — the FSM state
        is usually still the same string, so nothing would re-light without
        this.
        """
        self._cancel()
        self._state = None

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _cancel(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
        self._task = None

    def _restart(self) -> None:
        self._cancel()
        state = self._state or ""

        if state in _DARK_STATES:
            # Dark wins over every cue and over the GM's work lights.
            self._all_off(fade=False)
            return

        if self._work_lights is True:
            self._all_on()
            return
        if self._work_lights is False and state == "MASTER":
            # MASTER is the one state where "off" has to mean off. Its cue is
            # full working light — correct for walking the container, useless
            # when the GM wants to stand inside and look at the lasers — and
            # the cue would otherwise just override the switch.
            #
            # Everywhere else, OFF still means "hand the room back to the
            # show", which is what the cue table is for and what the rest of
            # the states are tested against.
            self._all_off()
            # The entrance follows the house lights here, rather than staying
            # lit on its always_on default: in MASTER the GM is deliberately
            # making the container dark to look at the lasers, and the entrance
            # is the brightest leak in the box. Every other path leaves it
            # alone, and blackout() still restores it if the process dies.
            try:
                self._dmx.set_light("entrance", 0, fade=True, allow_always_on=True)
            except Exception as exc:
                log.warning("could not dim the entrance: %s", exc)
            return

        cue = self._cues.get(state.lower()) or self._cues.get(state)
        if not cue or not cue.get("steps"):
            self._all_off()
            return
        # Check for a loop BEFORE building the coroutine. Constructing it and
        # then failing to schedule leaves a "coroutine was never awaited"
        # warning and a dangling object.
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            # No loop (unit tests, or called before startup). Apply the cue's
            # first step so the levels are at least sane, and do not animate.
            self._apply(cue["steps"][0])
            return
        self._task = asyncio.create_task(
            self._play(cue, state), name=f"lightcue_{state}")

    def _all_off(self, fade: bool = True) -> None:
        if hasattr(self._dmx, "set_maze_lights"):
            self._dmx.set_maze_lights(False, fade=fade)

    def _all_on(self) -> None:
        if hasattr(self._dmx, "set_maze_lights"):
            self._dmx.set_maze_lights(True)

    def _apply(self, step: dict) -> None:
        fade = bool(step.get("fade", True))
        for name, level in step.items():
            if name in ("ms", "fade"):
                continue
            # allow_always_on: a cue is the only thing permitted to take the
            # entrance dark, because only the cue table knows whether anyone is
            # walking in or out right now.
            self._dmx.set_light(name, int(level), fade=fade, allow_always_on=True)

    async def _play(self, cue: dict, state: str) -> None:
        steps = cue["steps"]
        loop = bool(cue.get("loop", False))
        try:
            while True:
                for step in steps:
                    self._apply(step)
                    await asyncio.sleep(max(0, int(step.get("ms", 300))) / 1000.0)
                if not loop:
                    break
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # A broken cue must never take the game down with it.
            log.error("light cue %r failed: %s", state, exc)
