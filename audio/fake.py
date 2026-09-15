"""
audio/fake.py — a silent AudioPlayer that records what it was asked to play.

Inputs:  the same calls as audio/player.AudioPlayer
Outputs: nothing audible; a call log for tests and for --fake-audio
Invariant: identical surface to the real player, so swapping it in cannot
           change control flow. Mandatory, like every other fake in this repo.
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)


class FakeAudioPlayer:
    """Records calls instead of making noise."""

    def __init__(self, sounds_dir: str = "sounds", **kwargs) -> None:
        self._dir = sounds_dir
        self.calls: list[tuple] = []
        self.music: str | None = None
        self.music_starts: list[str | None] = []
        self.restarts_avoided: int = 0
        self.cues_played: list[str] = []

    def start(self, preload: list[str] | None = None) -> bool:
        self.calls.append(("start", tuple(preload or ())))
        log.info("Audio: FAKE player (silent), would preload %d cue(s)",
                 len(preload or []))
        return True

    def verify_music(self, names: list[str]) -> list[str]:
        self.calls.append(("verify_music", tuple(names)))
        return []

    def stop(self) -> None:
        self.calls.append(("stop",))

    def play_music(self, filename: str | None, fade_ms: int = 400) -> None:
        self.calls.append(("play_music", filename, fade_ms))
        want = None if not filename or filename.lower() in (
            "silence", "none", "off") else filename
        # Model the real player's no-op on a repeat, or this fake would let a
        # test assert a restart that production does not do — which is exactly
        # the behaviour that carries one track across all three run segments.
        if want == self.music:
            self.restarts_avoided += 1
            return
        self.music = want
        self.music_starts.append(want)
        log.info("Audio[fake]: music -> %s", want or "silence")

    def stop_music(self, fade_ms: int = 400) -> None:
        self.calls.append(("stop_music", fade_ms))
        self.music = None

    def play_cue(self, filename: str | None) -> None:
        self.calls.append(("play_cue", filename))
        if filename:
            self.cues_played.append(filename)
            log.info("Audio[fake]: cue -> %s", filename)

    def stop_all(self, fade_ms: int = 0) -> None:
        self.calls.append(("stop_all", fade_ms))
        self.music = None

    @property
    def available(self) -> bool:
        return True

    def status(self) -> dict:
        return {"available": True, "fake": True, "music": self.music,
                "sounds_dir": self._dir, "cues_played": list(self.cues_played)}
