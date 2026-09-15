"""
audio/player.py — owns the sound device: one music bed plus one-shot cues.

Inputs:  audio files under the sounds directory, named by config. The bed
         streams, so a long track should be .mp3 (8 hours of .wav is 5 GB);
         cues are decoded into RAM at startup, so they should be short .wav.
Outputs: audio on the system device; nothing else in the system makes sound
Invariant: audio NEVER breaks the game. A missing file, a dead device, a
           mixer that will not initialise — every one of them degrades to
           silence and a log line. Nothing here raises into the game path.
Invariant: re-requesting the music bed that is already playing is a no-op, so
           a track survives RUN_SEG_1 -> 2 -> 3 instead of restarting at every
           checkpoint.
Invariant: cue sounds are decoded once at startup. The game path only ever
           calls Sound.play(), which returns immediately — a checkpoint sting
           must not wait on a disk read.

The bed streams (pygame.mixer.music); cues are held in RAM as Sound objects on
their own channels, so a sting layers over the music instead of cutting it.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# Enough channels that a checkpoint sting, a countdown beep and a result
# fanfare can overlap without cutting each other off.
_CUE_CHANNELS = 8


class AudioPlayer:
    """Wraps pygame.mixer. Silent and harmless when it cannot initialise."""

    def __init__(
        self,
        sounds_dir: str | Path,
        music_volume: float = 0.6,
        cue_volume: float = 0.9,
        device: str = "",
        enabled: bool = True,
    ) -> None:
        self._dir = Path(sounds_dir)
        self._music_volume = _clamp(music_volume)
        self._cue_volume = _clamp(cue_volume)
        self._device = device or ""
        self._enabled = bool(enabled)

        self._mixer: Any = None
        self._available = False
        self._error: str | None = None
        self._current_music: str | None = None
        self._sounds: dict[str, Any] = {}
        self._missing: set[str] = set()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self, preload: list[str] | None = None) -> bool:
        """
        Initialise the mixer and decode the cue sounds. Never raises.

        Returns True if audio is live. False is a normal outcome on a box with
        no sound card, and the rest of the class no-ops from then on.
        """
        if not self._enabled:
            self._error = "disabled in config"
            log.info("Audio: disabled in config")
            return False
        try:
            # pygame greets stdout on import ("Hello from the pygame
            # community"), which lands in the journal on every boot.
            os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")
            import pygame                    # noqa: PLC0415 — optional dependency
        except Exception as exc:
            self._error = f"pygame not installed ({exc})"
            log.warning("Audio: %s — running silent", self._error)
            return False

        try:
            # mixer only. pygame.init() would bring up video, which this box
            # does not have and does not need in order to make a noise.
            kwargs = {"frequency": 44100, "size": -16, "channels": 2, "buffer": 512}
            if self._device:
                kwargs["devicename"] = self._device
            pygame.mixer.init(**kwargs)
            pygame.mixer.set_num_channels(_CUE_CHANNELS)
        except Exception as exc:
            self._error = f"mixer init failed ({exc})"
            log.warning("Audio: %s — running silent", self._error)
            return False

        self._mixer = pygame.mixer
        self._available = True
        self._error = None
        self._mixer.music.set_volume(self._music_volume)
        log.info("Audio: mixer up (device=%s, sounds=%s)",
                 self._device or "system default", self._dir)

        for name in preload or []:
            self._load_cue(name)
        return True

    def verify_music(self, names: list[str]) -> list[str]:
        """
        Prove every bed track can actually be opened, at startup.

        Cues are decoded during start(), so a broken one is known immediately.
        The bed is not: it streams, and mixer.music.load() only runs the first
        time that state is reached. A codec the bundled SDL_mixer cannot handle
        would therefore surface as silence mid-show rather than as a line in
        the boot log. Nothing is playing yet at startup, so loading each one is
        free.

        Returns the names that failed.
        """
        if not self._available:
            return []
        bad: list[str] = []
        for name in names:
            path = self._resolve(name)
            if path is None:
                bad.append(name)
                continue
            try:
                self._mixer.music.load(str(path))
            except Exception as exc:
                log.warning("Audio: %s will not decode (%s) — that bed is silent",
                            name, exc)
                self._missing.add(name)
                bad.append(name)
        try:
            self._mixer.music.unload()
        except Exception:
            pass                      # older SDL_mixer has no unload; harmless
        return bad

    def stop(self) -> None:
        """Stop everything and release the device. Safe to call twice."""
        if not self._available:
            return
        try:
            self._mixer.music.stop()
            self._mixer.stop()
            self._mixer.quit()
        except Exception as exc:
            log.warning("Audio: stopping the mixer failed: %s", exc)
        self._available = False
        self._current_music = None
        self._sounds.clear()

    # ------------------------------------------------------------------
    # Playback
    # ------------------------------------------------------------------

    def play_music(self, filename: str | None, fade_ms: int = 400) -> None:
        """
        Start (or swap) the looping bed. `None` or "silence" stops it.

        Asking for the track already playing does nothing — that is what keeps
        one song running across a whole run instead of restarting it at every
        checkpoint.
        """
        if not self._available:
            return
        if not filename or filename.lower() in ("silence", "none", "off"):
            self.stop_music(fade_ms)
            return
        if filename == self._current_music and self._is_music_busy():
            return

        path = self._resolve(filename)
        if path is None:
            return
        try:
            self._mixer.music.load(str(path))
            self._mixer.music.set_volume(self._music_volume)
            self._mixer.music.play(loops=-1, fade_ms=max(0, fade_ms))
            self._current_music = filename
            log.info("Audio: music -> %s", filename)
        except Exception as exc:
            self._device_lost(f"playing {filename!r}: {exc}")
            self._current_music = None

    def stop_music(self, fade_ms: int = 400) -> None:
        if not self._available or self._current_music is None:
            return
        try:
            if fade_ms > 0:
                self._mixer.music.fadeout(fade_ms)
            else:
                self._mixer.music.stop()
        except Exception as exc:
            log.warning("Audio: could not stop music: %s", exc)
        self._current_music = None

    def play_cue(self, filename: str | None) -> None:
        """Fire a one-shot over the top of the bed. Cheap; safe on the game path."""
        if not self._available or not filename:
            return
        if filename in self._missing:
            return          # already known bad; do not re-stat and re-decode
                            # it on the game path at every checkpoint
        sound = self._sounds.get(filename) or self._load_cue(filename)
        if sound is None:
            return
        try:
            sound.play()
        except Exception as exc:
            self._device_lost(f"playing cue {filename!r}: {exc}")

    def stop_all(self, fade_ms: int = 0) -> None:
        """Everything quiet, bed included. Used by the shutdown sequence."""
        if not self._available:
            return
        self.stop_music(fade_ms)
        try:
            self._mixer.stop()
        except Exception as exc:
            log.warning("Audio: could not stop cues: %s", exc)

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    def _device_lost(self, why: str) -> None:
        """
        Mark the device gone so the box stops claiming it is playing.

        _available and _error were only ever written in start()/stop(), and
        playback errors were swallowed into a log line — so unplugging the DAC,
        or a plugged-in HDMI stealing the default sink, left the box silent for
        the rest of the day while /api/admin/audio kept answering
        {"available": true, "music": "ambient.mp3"} and the admin card the docs
        point operators at showed it happily playing. Silent when it should be
        loud, on the one surface meant to tell you.
        """
        if self._available:
            log.error("Audio: device lost while %s — running silent", why)
        self._available = False
        self._error = f"device lost while {why}"
        self._current_music = None

    @property
    def available(self) -> bool:
        return self._available

    def status(self) -> dict:
        return {
            "available": self._available,
            "error": self._error,
            "device": self._device or "system default",
            "sounds_dir": str(self._dir),
            "music": self._current_music,
            "loaded_cues": sorted(self._sounds),
            "missing": sorted(self._missing),
            "music_volume": self._music_volume,
            "cue_volume": self._cue_volume,
        }

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _is_music_busy(self) -> bool:
        try:
            return bool(self._mixer.music.get_busy())
        except Exception:
            return False

    def _resolve(self, filename: str) -> Path | None:
        """
        Locate a sound file, refusing anything outside the sounds directory.

        The names come from a config file an operator edits by hand, so a typo
        should be a log line and silence, not a traceback on the game path —
        and "../../etc/something" should not resolve at all.
        """
        path = (self._dir / filename).resolve()
        try:
            path.relative_to(self._dir.resolve())
        except ValueError:
            log.error("Audio: %r is outside %s — ignoring", filename, self._dir)
            self._missing.add(filename)
            return None
        if path.is_file():
            # An operator who drops the file in mid-day should stop being told
            # it is missing.
            self._missing.discard(filename)
        if not path.is_file():
            if filename not in self._missing:
                self._missing.add(filename)
                log.warning("Audio: %s not found in %s — that cue is silent",
                            filename, self._dir)
            return None
        return path

    def _load_cue(self, filename: str) -> Any:
        if not self._available:
            return None
        path = self._resolve(filename)
        if path is None:
            return None
        try:
            sound = self._mixer.Sound(str(path))
            sound.set_volume(self._cue_volume)
            self._sounds[filename] = sound
            return sound
        except Exception as exc:
            log.warning("Audio: could not load %s: %s", filename, exc)
            self._missing.add(filename)
            return None


def _clamp(v: float) -> float:
    return max(0.0, min(1.0, float(v)))
