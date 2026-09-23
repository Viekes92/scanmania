#!/usr/bin/env python3
"""
tools/gen_placeholder_sounds.py — write stand-in audio into sounds/.

Inputs:  the audio block of config/game.yaml (which names every file)
Outputs: sounds/<whatever the config asks for> — generated tones, not music
Invariant: never overwrites an existing file without --force. The real
           soundtrack lives under these same names and is NOT in git, so a
           careless regeneration here would destroy the only copy on the box.
Invariant: driven by the config, never by a hardcoded list. The two drifted
           once already — the config moved the bed to ambient.mp3 while this
           still wrote ambient.wav, so a fresh checkout generated placeholders
           that did not satisfy its own config.

The real audio is gitignored (see sounds/README.md), so a fresh checkout has
none. These exist so the wiring can be heard and tested without shipping large
binaries. Replace them.
"""

from __future__ import annotations

import argparse
import math
import shutil
import struct
import subprocess
import sys
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

RATE = 44100

# Matched on the filename stem, so renaming ambient.wav -> bed.mp3 in the config
# still gets something bed-like. (seconds, [(freq, start_frac, end_frac), ...])
SHAPES: dict[str, tuple[float, list[tuple[float, float, float]]]] = {
    "ambient":   (8.0, [(110, 0.0, 1.0), (165, 0.0, 1.0), (220, 0.3, 0.9)]),
    "game":      (8.0, [(147, 0.0, 1.0), (220, 0.0, 1.0), (294, 0.5, 1.0)]),
    "countdown": (3.0, [(660, 0.00, 0.12), (660, 0.33, 0.45),
                        (660, 0.66, 0.78), (990, 0.90, 1.00)]),
    "sector2":    (0.35, [(880, 0.0, 0.5), (1320, 0.0, 0.25)]),
}
_BED_DEFAULT = SHAPES["ambient"]
_CUE_DEFAULT = SHAPES["sector"]


def shape_for(name: str, is_bed: bool):
    stem = Path(name).stem.lower()
    for key, spec in SHAPES.items():
        if key in stem:
            return spec
    return _BED_DEFAULT if is_bed else _CUE_DEFAULT


def render(seconds: float, parts: list[tuple[float, float, float]]) -> bytes:
    n = int(RATE * seconds)
    samples = [0.0] * n
    for freq, t0, t1 in parts:
        a, b = int(n * t0), int(n * t1)
        span = max(1, b - a)
        for i in range(a, min(b, n)):
            # Fade each part in and out, or the joins click.
            env = min(1.0, (i - a) / (span * 0.15 + 1), (b - i) / (span * 0.35 + 1))
            samples[i] += math.sin(2 * math.pi * freq * (i / RATE)) * env

    peak = max((abs(s) for s in samples), default=1.0) or 1.0
    frames = bytearray()
    for s in samples:
        v = int(max(-1.0, min(1.0, s / peak)) * 32767 * 0.8)
        frames += struct.pack("<hh", v, v)          # stereo, same both sides
    return bytes(frames)


def write_wav(path: Path, pcm: bytes) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(pcm)


def write_encoded(path: Path, pcm: bytes) -> bool:
    """Encode via ffmpeg for a compressed extension. False if it cannot."""
    if not shutil.which("ffmpeg"):
        return False
    proc = subprocess.run(
        ["ffmpeg", "-v", "error", "-y",
         "-f", "s16le", "-ar", str(RATE), "-ac", "2", "-i", "pipe:0",
         str(path)],
        input=pcm, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    if proc.returncode != 0:
        print(f"  ffmpeg failed for {path.name}: "
              f"{proc.stderr.decode().strip()[:120]}")
        return False
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description="Generate placeholder sounds.")
    ap.add_argument("--dir", default=None,
                    help="Override the sounds directory (default: from config).")
    ap.add_argument("--force", action="store_true",
                    help="Overwrite existing files. DESTROYS real audio — the "
                         "soundtrack is not in git, so there may be no copy.")
    args = ap.parse_args()

    from config.loader import load_game
    acfg = load_game().audio

    out = Path(args.dir) if args.dir else (
        Path(__file__).resolve().parent.parent / acfg.sounds_dir)
    out.mkdir(parents=True, exist_ok=True)

    beds = set(acfg.music_files())
    wanted = sorted(beds | set(acfg.cue_files()))
    if not wanted:
        print("config names no sounds — nothing to do")
        return 0

    written = skipped = failed = 0
    for name in wanted:
        path = out / name
        if path.exists() and not args.force:
            print(f"  skip  {name} (already there)")
            skipped += 1
            continue
        seconds, parts = shape_for(name, name in beds)
        pcm = render(seconds, parts)
        if path.suffix.lower() == ".wav":
            write_wav(path, pcm)
        elif not write_encoded(path, pcm):
            print(f"  FAIL  {name} — needs ffmpeg to encode {path.suffix}")
            failed += 1
            continue
        print(f"  wrote {name}  ({seconds:.2f}s)")
        written += 1

    print(f"\n{written} written, {skipped} left alone, {failed} failed -> {out}/")
    if failed:
        print("Install ffmpeg, or point the config at .wav names instead.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
