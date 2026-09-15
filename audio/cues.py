"""
audio/cues.py — maps the FSM state to what the container should sound like.

Inputs:  the audio config (music bed per state, one-shot per state), FSM states
Outputs: play_music/play_cue calls on the player; holds no device of its own
Invariant: a state with no music entry KEEPS whatever is playing. Silence has
           to be asked for by name, or every unlisted state would punch a hole
           in the soundtrack.
Invariant: cues fire on ENTERING a state, once. Re-entering the same state does
           not re-fire, which is what stops a checkpoint sting repeating if the
           FSM re-broadcasts.
Invariant: never raises into the game path. A broken cue is silence.

The shape mirrors light_cues in mazes.yaml on purpose — same idea, same file,
same mental model for whoever is editing it at a venue.
"""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)


class AudioCuePlayer:
    """Drives one AudioPlayer from FSM state changes."""

    def __init__(self, player: Any, config: Any = None) -> None:
        self._player = player
        cfg = config or {}
        # Config keys are FSM state names. Accept either case so an operator
        # writing "attract" gets the same result as "ATTRACT".
        self._music = _upper_keys(_get(cfg, "music", {}))
        self._cues = _upper_keys(_get(cfg, "cues", {}))
        self._fade_ms = int(_get(cfg, "fade_ms", 400) or 0)
        self._state: str | None = None
        self._muted = False

    # ------------------------------------------------------------------
    # Control
    # ------------------------------------------------------------------

    @property
    def muted(self) -> bool:
        return self._muted

    def set_muted(self, muted: bool) -> None:
        """GM kill switch. Stops the bed now; cues stay silent while set."""
        self._muted = bool(muted)
        log.info("Audio: %s", "MUTED" if self._muted else "unmuted")
        if self._muted:
            self._safe(self._player.stop_all, self._fade_ms)
        else:
            # Put the BED back — not the one-shot. set_state replays both, so
            # unmuting while the box sat in BUSTED fired defeat.wav with nobody
            # running, and unmuting mid-run fired a checkpoint sting. Cues are
            # meant to fire once, on entering a state.
            key = (self._state or "").upper()
            if key in self._music:
                self._safe(self._player.play_music, self._music[key], self._fade_ms)

    def set_state(self, state: str) -> None:
        """Called on every FSM transition. Cheap when the state has not changed."""
        if state == self._state:
            return
        self._state = state
        if self._muted:
            return

        key = (state or "").upper()

        # Music first, so a state that swaps the bed and fires a sting does not
        # have the sting clipped by the crossfade starting after it.
        if key in self._music:
            self._safe(self._player.play_music, self._music[key], self._fade_ms)

        cue = self._cues.get(key)
        if cue:
            self._safe(self._player.play_cue, cue)

    def reset(self) -> None:
        """Forget the current state so the next set_state() re-applies it."""
        self._state = None

    def stop(self) -> None:
        self._state = None
        self._safe(self._player.stop_all, 0)

    def status(self) -> dict:
        st = {}
        try:
            st = self._player.status()
        except Exception:
            pass
        st["muted"] = self._muted
        st["state"] = self._state
        return st

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    @staticmethod
    def _safe(fn, *args) -> None:
        """A broken sound must never take a run down with it."""
        try:
            fn(*args)
        except Exception as exc:
            log.error("audio cue failed: %s", exc)


def _get(cfg: Any, name: str, default: Any) -> Any:
    if isinstance(cfg, dict):
        return cfg.get(name, default)
    return getattr(cfg, name, default)


def _upper_keys(d: Any) -> dict[str, str]:
    if not isinstance(d, dict):
        return {}
    return {str(k).upper(): str(v) for k, v in d.items() if v is not None}
