"""
tests/test_audio.py — the soundtrack: cue mapping, and that it can never bite.

Inputs:  the shipped audio config, the fake player, a real AudioPlayer with no
         mixer behind it
Outputs: assertions that the right thing plays at the right moment, and that
         every failure mode is silence rather than an exception
Invariant under test: audio is decoration. Nothing in here may be able to end
         a run, so every fault path is checked for silence, not for raising.
"""

from __future__ import annotations

import time

import pytest

import config.loader as loader
from audio.cues import AudioCuePlayer
from audio.fake import FakeAudioPlayer
from audio.player import AudioPlayer

RUN = ["ATTRACT", "REGISTERED", "ARM", "COUNTDOWN",
       "RUN_SEG_1", "RUN_SEG_2", "RUN_SEG_3", "FINISHED", "RESULT"]


@pytest.fixture
def audio_cfg():
    return loader.load_game().audio


@pytest.fixture
def player():
    p = FakeAudioPlayer()
    p.start()
    return p


# ---------------------------------------------------------------------------
# What plays when
# ---------------------------------------------------------------------------

def test_a_clean_run_sounds_right(player, audio_cfg):
    cues = AudioCuePlayer(player, audio_cfg)
    for state in RUN:
        cues.set_state(state)

    # Deliberately asserts the SHIPPED soundtrack, so a careless edit to
    # game.yaml is caught. The 3-2-1 is no longer a cue — it is baked into
    # game.mp3 and synced with count_in.audio_lead_ms — and there is no
    # victory one-shot any more — end.mp3 closes every game. One sting PER
    # sector, so the player hears which checkpoint they just took.
    assert player.cues_played == [
        "sector2.mp3", "sector3.mp3"
    ], "wrong one-shots, or wrong order, through a clean run"


def test_the_run_track_survives_both_checkpoints(player, audio_cfg):
    """The whole point of holding the bed across states.

    Re-naming the same file at RUN_SEG_2 and RUN_SEG_3 must not restart it —
    a track that jumps back to bar one every time someone crosses a checkpoint
    is worse than no music.
    """
    # Its OWN config, not the shipped one: this is a property of the player,
    # and pinning it to whatever game.yaml currently names turns routine show
    # tuning into a red build. (It did — renaming the run track broke this.)
    cfg = {"music": {"COUNTDOWN": "run.mp3", "RUN_SEG_1": "run.mp3",
                     "RUN_SEG_2": "run.mp3", "RUN_SEG_3": "run.mp3"},
           "cues": {}}
    cues = AudioCuePlayer(player, cfg)
    for state in RUN:
        cues.set_state(state)

    assert player.music_starts.count("run.mp3") == 1, "run track restarted"
    assert player.restarts_avoided >= 2


def test_the_shipped_config_carries_one_track_across_the_whole_run(audio_cfg):
    """The run states are deliberately UNLISTED so the count-in bed plays on.

    game.mp3 opens with the spoken 3-2-1 and then becomes the run track, so
    naming it again at RUN_SEG_* would be at best a no-op and at worst a
    restart back to the voice-over.
    """
    music = audio_cfg.music if hasattr(audio_cfg, "music") else audio_cfg["music"]
    assert music.get("COUNTDOWN"), "nothing starts the count-in bed"
    for seg in ("RUN_SEG_1", "RUN_SEG_2", "RUN_SEG_3"):
        assert seg not in music, (
            f"{seg} names a track; an unlisted state is what keeps the "
            f"count-in bed running unbroken into the run")


def test_a_state_with_no_music_entry_keeps_the_bed(player):
    """Silence has to be asked for by name, or every unlisted state would
    punch a hole in the soundtrack."""
    cues = AudioCuePlayer(player, {"music": {"ATTRACT": "ambient.wav"},
                                   "cues": {}})
    cues.set_state("ATTRACT")
    cues.set_state("SOME_STATE_NOBODY_CONFIGURED")
    assert player.music == "ambient.wav"


def test_silence_is_a_real_instruction(player):
    cues = AudioCuePlayer(player, {"music": {"A": "bed.wav", "B": "silence"}})
    cues.set_state("A")
    assert player.music == "bed.wav"
    cues.set_state("B")
    assert player.music is None


def test_re_entering_a_state_does_not_re_fire_its_cue(player, audio_cfg):
    cues = AudioCuePlayer(player, audio_cfg)
    cues.set_state("RUN_SEG_2")
    cues.set_state("RUN_SEG_2")
    assert player.cues_played == ["sector2.mp3"]


def test_config_keys_are_case_insensitive(player):
    cues = AudioCuePlayer(player, {"music": {"attract": "bed.wav"}})
    cues.set_state("ATTRACT")
    assert player.music == "bed.wav"


def test_mute_stops_the_bed_and_restores_it(player, audio_cfg):
    cfg = {"music": {"ATTRACT": "bed.mp3", "RUN_SEG_1": "run.mp3"}, "cues": {}}
    cues = AudioCuePlayer(player, cfg)
    cues.set_state("ATTRACT")
    cues.set_muted(True)
    assert player.music is None
    cues.set_state("RUN_SEG_1")
    assert player.music is None, "muted audio still started a track"
    cues.set_muted(False)
    assert player.music == "run.mp3", "unmuting did not restore the bed"


# ---------------------------------------------------------------------------
# Every failure mode is silence, never an exception
# ---------------------------------------------------------------------------

def test_a_broken_player_cannot_end_a_run(audio_cfg):
    class _Exploding:
        def play_music(self, *a, **k): raise RuntimeError("sound card on fire")
        def play_cue(self, *a, **k): raise RuntimeError("sound card on fire")
        def stop_all(self, *a, **k): raise RuntimeError("sound card on fire")
        def status(self): raise RuntimeError("sound card on fire")

    cues = AudioCuePlayer(_Exploding(), audio_cfg)
    for state in RUN:
        cues.set_state(state)          # must not raise
    cues.stop()
    assert cues.status()["muted"] is False


def test_no_mixer_means_silence_not_a_crash(tmp_path):
    """A box with no sound card still runs a full day of games."""
    p = AudioPlayer(sounds_dir=tmp_path, enabled=True)
    p._available = False               # as if start() had failed
    p.play_music("anything.wav")
    p.play_cue("anything.wav")
    p.stop_music()
    p.stop_all()
    p.stop()
    assert p.available is False


def test_disabled_in_config_never_touches_the_device(tmp_path):
    p = AudioPlayer(sounds_dir=tmp_path, enabled=False)
    assert p.start() is False
    assert p.status()["error"] == "disabled in config"


def test_a_missing_file_is_logged_once_and_silent(tmp_path):
    p = AudioPlayer(sounds_dir=tmp_path)
    p._available = True                # pretend the mixer is up
    p._mixer = object()                # any use of it would raise
    assert p._resolve("nope.wav") is None
    assert p._resolve("nope.wav") is None
    assert p.status()["missing"] == ["nope.wav"]


def test_a_filename_cannot_escape_the_sounds_directory(tmp_path):
    """The names come from a hand-edited config file."""
    (tmp_path / "sounds").mkdir()
    secret = tmp_path / "secret.wav"
    secret.write_bytes(b"RIFF")
    p = AudioPlayer(sounds_dir=tmp_path / "sounds")
    assert p._resolve("../secret.wav") is None
    assert p._resolve("/etc/passwd") is None


# ---------------------------------------------------------------------------
# The shipped config and the shipped files agree
# ---------------------------------------------------------------------------

def test_every_sound_the_config_names_exists(audio_cfg):
    """A cue pointing at a file nobody shipped is silent at a venue, and the
    first anyone hears of it is the moment it does not play.

    Audio is not carried in git — it is scp'd to the box — so a fresh clone has
    none and there is nothing to check. Skipping is right there: asserting
    would fail every clean checkout and CI run, which teaches people to ignore
    this test. Once ANY audio is present the check is real again, which is the
    state the box is actually in.
    """
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent / audio_cfg.sounds_dir
    present = [p for p in root.glob("*") if p.suffix.lower() in
               (".wav", ".mp3", ".ogg", ".flac")] if root.is_dir() else []
    if not present:
        pytest.skip("no audio present (fresh checkout) — "
                    "run tools/gen_placeholder_sounds.py or scp the real files")
    missing = [n for n in audio_cfg.filenames() if not (root / n).is_file()]
    assert not missing, f"config names sounds that are not in {root}: {missing}"


def test_cues_and_beds_use_a_format_the_box_can_decode(audio_cfg):
    """Different jobs, different formats.

    A cue is decoded into RAM at startup and must fire the instant a checkpoint
    goes by. The bed streams and runs for minutes, so forcing .wav there would
    mean 5 GB for an 8-hour ambient track.

    .wav is still the SAFE choice for a cue, and this test used to require it.
    It no longer does: the shipped stings are .mp3, and SDL_mixer was verified
    to load each of them as a fully-decoded in-memory Sound — which is the
    property the rule actually protects, since a Sound is resident either way.
    The residual risk is a box whose SDL build lacks the mp3 decoder, where the
    cue would be silent; that surfaces as a startup log line, not as a failed
    run, because audio can never affect the game path.
    """
    playable = (".wav", ".mp3", ".ogg")
    bad_cues = [n for n in audio_cfg.cue_files()
                if not n.lower().endswith(playable)]
    assert not bad_cues, f"cue format needs a decoder that may not exist: {bad_cues}"

    bad_music = [n for n in audio_cfg.music_files()
                 if not n.lower().endswith(playable)]
    assert not bad_music, f"bed format needs a decoder that may not exist: {bad_music}"


def test_the_bed_is_not_shipped_as_wav(audio_cfg):
    """A long .wav bed is the mistake this whole split exists to prevent."""
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent / audio_cfg.sounds_dir
    for name in audio_cfg.music_files():
        f = root / name
        if f.is_file() and f.suffix.lower() == ".wav":
            assert f.stat().st_size < 20_000_000, (
                f"{name} is a {f.stat().st_size/1e6:.0f} MB wav — "
                f"encode the bed as .mp3 or .ogg")


# ---------------------------------------------------------------------------
# Audit round 2 — regressions
# ---------------------------------------------------------------------------

def test_unmuting_restores_the_bed_without_refiring_the_cue(player, audio_cfg):
    """Cues fire once, on entering a state.

    set_muted(False) used to replay the whole state, so unmuting while the box
    sat in BUSTED fired defeat.wav with nobody running, and unmuting mid-run
    fired a spurious checkpoint sting.
    """
    cues = AudioCuePlayer(player, audio_cfg)
    cues.set_state("BUSTED")
    fired_before = list(player.cues_played)
    cues.set_muted(True)
    cues.set_muted(False)
    assert player.cues_played == fired_before, "unmuting re-fired the one-shot"


def test_a_lost_device_stops_the_box_claiming_it_is_playing(tmp_path):
    """The admin card is the surface the docs point operators at.

    _available was only written in start()/stop() and playback errors were
    swallowed, so a DAC unplugged mid-tour left the box silent all day while
    the API kept answering available:true with a track name.
    """
    (tmp_path / "bed.wav").write_bytes(b"RIFF")
    p = AudioPlayer(sounds_dir=tmp_path)
    p._available = True
    p._current_music = "bed.wav"

    class _Dead:
        class music:
            @staticmethod
            def load(*a): raise OSError("No such device")
            @staticmethod
            def set_volume(*a): pass
            @staticmethod
            def play(*a): pass
            @staticmethod
            def get_busy(): return False
    p._mixer = _Dead

    p.play_music("bed.wav")
    assert p.available is False, "device loss did not mark the player unavailable"
    st = p.status()
    assert st["available"] is False and st["music"] is None
    assert "device lost" in (st["error"] or "")


def test_a_file_that_will_not_decode_is_not_retried_every_frame(tmp_path):
    """play_cue consulted _sounds but never _missing, so a bad file was
    re-stat'd and re-decoded on the game path at every checkpoint."""
    p = AudioPlayer(sounds_dir=tmp_path)
    p._available = True
    p._mixer = object()
    p._missing.add("broken.wav")
    p.play_cue("broken.wav")          # must not touch the mixer at all


# ---------------------------------------------------------------------------
# One-shot beds: a sting plays once, then the bed comes back
# ---------------------------------------------------------------------------

def test_a_one_shot_bed_hands_back_to_its_follow_on():
    """
    end.mp3 is an eight-second sting. Looping it left the fanfare repeating
    under the score for the rest of the result, so it plays once and ambient
    returns — without waiting for the FSM to reach RESULT.
    """
    from audio.player import AudioPlayer
    p = AudioPlayer(sounds_dir="sounds")
    played: list[tuple] = []
    busy = {"v": True}
    p._is_music_busy = lambda: busy["v"]
    p.play_music = lambda f, fade=400, loop=True, follow=None: played.append((f, loop))

    p._follow_when_done(p._music_gen, "ambient.mp3", 400)
    time.sleep(0.35)
    assert played == [], "handed back while the sting was still playing"

    busy["v"] = False
    time.sleep(0.45)
    assert played == [("ambient.mp3", True)], "the bed never came back"


def test_a_superseded_one_shot_does_not_stamp_on_the_new_bed():
    """
    The safety property of the generation counter.

    If the GM force-resets during the outcome sting, ATTRACT's bed starts and
    the sting's pending follow-on must NOT fire a second later and yank the
    soundtrack back to whatever it thought was next.
    """
    from audio.player import AudioPlayer
    p = AudioPlayer(sounds_dir="sounds")
    played: list[tuple] = []
    p._is_music_busy = lambda: False          # sting already finished
    p.play_music = lambda f, fade=400, loop=True, follow=None: played.append((f, loop))

    stale_gen = p._music_gen
    p._music_gen += 1                         # something else took the bed
    p._follow_when_done(stale_gen, "ambient.mp3", 400)
    time.sleep(0.35)
    assert played == [], "a superseded sting overwrote the current bed"
