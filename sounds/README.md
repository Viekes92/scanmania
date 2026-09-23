# sounds/

Every sound the container makes. Drop `.wav` files in here; nothing else reads
this directory.

## Swapping a sound

Two ways, both live-reloadable from the admin portal (Config → game.yaml → Save):

1. **Keep the name.** Overwrite `end.mp3` with a different `end.mp3`.
   Nothing in config changes.
2. **Change the name.** Put `fanfare_v2.wav` here and point `audio.cues.FINISHED`
   at it in `config/game.yaml`.

A filename that does not resolve is a log line and silence — never a crash.
Audio is decoration, and it must never be able to end someone's run. Check what
actually loaded at **Admin → Hardware → Audio**, which lists both the loaded
cues and the missing ones.

## What plays when

Set in `config/game.yaml` under `audio:`, keyed by FSM state.

| file | role | fires |
|---|---|---|
| `ambient.mp3` | the idle bed, looping | ATTRACT, REGISTERED, ARM, RESULT |
| `game.wav` | the run track, looping | RUN_SEG_1, and keeps playing through 2 and 3 |
| `countdown.wav` | one-shot | entering COUNTDOWN |
| `sector2.mp3` | one-shot | entering RUN_SEG_2 (checkpoint 1 reached) |
| `sector3.mp3` | one-shot | entering RUN_SEG_3 (checkpoint 2 reached) |
| `break.mp3` | one-shot | a beam break — the TIME PENALTY sting. Not keyed by state: a penalty does not change state, the run carries on. |
| `end.mp3` | bed, plays once | FINISHED and BUSTED. There is no victory/defeat pair any more — end.mp3 closes every game. |

The music bed is **held across states that do not name one**. That is what
keeps a single track running for a whole run instead of restarting at every
checkpoint. Ask for quiet explicitly with `silence`.

Naming the same file for `RUN_SEG_1/2/3`, as the shipped config does, is a
no-op on entry 2 and 3 — the track is already playing, so it is left alone.

## Format — different jobs, different formats

**Cues: `.wav`.** They are decoded into RAM at startup and must fire the instant
someone crosses a checkpoint. They are short, so compression saves nothing worth
having. Keep them pre-trimmed: leading silence in `sector2.mp3` or `break.mp3` reads as lag, and the
penalty sting in particular has to land on the moment it happened.

**The bed: `.mp3` or `.ogg`.** It streams rather than loading, and it runs for
minutes. Forcing `.wav` here is how you end up with a 5 GB file — an 8-hour
ambient track as `.wav` is six times larger than the same thing as `.mp3`:

| 8-hour ambient | size |
|---|---|
| `.wav` 44.1 kHz stereo | 5.1 GB |
| `.mp3` 192 kbps | ~700 MB |
| 10-minute seamless loop, `.mp3` 192 kbps | **14 MB** |

The bed **loops forever**, so length buys you variety and nothing else. Nobody
is in the container for more than a few minutes. Cut a loop rather than shipping
hours of audio.

`.ogg` loops truly gaplessly; `.mp3` carries a few milliseconds of encoder
padding at the wrap. Under a crossfade that is inaudible, which is why
`ambient.mp3` is fine. On a hard loop point, prefer `.ogg`.

44.1 kHz stereo is what the mixer runs at; other rates work but are resampled
on load.

## Making a seamless loop

Cut `D + X` seconds, then wrap the tail back onto the head with an `X`-second
crossfade so the end flows into the start:

```bash
D=600; X=6            # 10-minute loop, 6-second crossfade
ffmpeg -ss 1800 -t $((D+X)) -i source.mp3 -ar 44100 -ac 2 loop_src.wav
ffmpeg -i loop_src.wav -filter_complex "
  [0:a]atrim=$X:$D,asetpts=PTS-STARTPTS[body];
  [0:a]atrim=$D:$((D+X)),asetpts=PTS-STARTPTS[tail];
  [0:a]atrim=0:$X,asetpts=PTS-STARTPTS[head];
  [tail][head]acrossfade=d=$X:c1=tri:c2=tri[xf];
  [body][xf]concat=n=2:v=0:a=1[out]" \
  -map "[out]" -c:a libmp3lame -b:a 192k ambient.mp3
```

Start well past the beginning (`-ss 1800` above) so you miss any intro or
fade-in. That is exactly how the shipped `ambient.mp3` was made.

## Volume

`audio.music_volume` and `audio.cue_volume` in `game.yaml`, both `0.0`–`1.0`.
Set the bed well under the cues: the run track is meant to sit behind a
checkpoint hit, not fight it.

## Audio is not in git

Everything in here except this README is **gitignored**. A single ambient bed is
14 MB, and the repo is cloned and pulled far more often than the music changes.
So a fresh checkout has no audio at all, and that is expected — the audio tests
skip rather than fail, and the game runs silent.

**To get sound on a fresh checkout:**

```bash
python3 tools/gen_placeholder_sounds.py   # generated tones, enough to test wiring
```

**To get the real soundtrack onto the box:**

```bash
./tools/push_sounds.sh                    # -> root@172.16.0.10:/opt/scanmania/sounds
ssh root@172.16.0.10 'systemctl restart scanmania'
```

Sounds are loaded once at startup, so the restart is what picks up a change.
`DRY=1 ./tools/push_sounds.sh` shows what would be sent without sending it.

`deploy.sh` does **not** carry audio. It pulls, and git leaves ignored files
alone, so what you push here survives every later deploy untouched. It also
means the box is the only copy of the real files — keep them somewhere else
too.
