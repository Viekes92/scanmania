# Grand prize mode

Three changes to the shipping game, each independently switchable, for a final
where the times must stay secret until the prize is awarded. Merged to `main`;
originally the `grand-prize` branch.

Everything here is **opt-in**, and the defaults in git are OFF — these are
settings for a day, not a release. A box with nothing set behaves exactly as
normal: times on screen, runs dark, the start plate the only way into ARM.
Set them on the box via **Admin → Config**, which validates, audits and live-
reloads, and which you undo from the same screen.

---

## 1. The screens show no times

`config/game.yaml`:

```yaml
display:
  hide_times: true      # false = normal play
```

Sealed, the two public screens lose:

| | `/display/in` | `/display/out` |
|---|---|---|
| running clock | gone | gone |
| segment `n / 3` | **scaled up** — it is the only thing left on screen | — |
| result time | gone | gone |
| rank | gone | gone |
| leaderboard | — | **whole block gone**, LAST RUN with it |
| penalty | reads `PENALTY` | reads `PENALTY` / `3 PENALTIES` |

What is left is the verdict — `CLEAN RUN` / `BUSTED` — and the player's name.
`/display/in`'s RESULT scene swaps its "YOUR TIME" half for the verdict and the
name, so it carries straight on from FINISHED rather than going blank.

The leaderboard goes **entirely**, not just its time column: a ranked board
with the times blanked still publishes the order, and the order is what the
next player in the queue wants to know.

### What is NOT hidden

- **The stopwatch still runs.** `core/stopwatch.py` is untouched, invariant 2
  holds, and the run row records both `elapsed_ms` and `raw_elapsed_ms`.
- **The GM console shows everything** — live clock, penalty strip, the lot.
- **The admin portal shows everything** — the runs table, the leaderboard, the
  CSV export. That is how you settle the prize.
- **The API still serves it.** `/api/admin/runs`, `/beams` and `/leaderboard`
  are unauthenticated on purpose (the passwordless GM console and the outdoor
  display fetch them), so anyone on the venue network can still read the times
  out of `/api/admin/leaderboard`. The seal is on the *screens*. If that
  matters for your final, keep the network closed.

### How the seal reaches the screens

`hide_times` rides on every WebSocket state broadcast, so flipping it in the
admin config editor takes effect on both screens without a kiosk restart.

Both pages ship **sealed** — `<body class="hide-times">` is in the markup and
the JS only ever takes the class off, on a message that says `hide_times` is
false. So:

- a screen that comes up mid-final shows no time rather than a stale one;
- a dropped WebSocket fails sealed, which is the direction that cannot spoil a
  final;
- unsealing is a class change with nothing to back-fill — the clock has been
  running and the leaderboard rendering into hidden elements the whole time.

---

## 2. A little light in the container, for the camera

`config/hardware.yaml`:

```yaml
hazer:
  lights:
    run_level: 2        # 0 = a run is dark, as normal
```

Holds the left and right house fixtures at that level through COUNTDOWN and all
three RUN segments — the four states the lightshow otherwise forces to a hard
0. The **entrance stays dark** whatever this is set to.

### Setting it

**From the GM console: the `Run light − 2 +` pill in the Health strip.** Each
tap applies immediately — including mid-run, which is the point — and saves to
`hardware.yaml` so it survives a reboot. The file keeps all its comments; the
endpoint rewrites one line, it does not round-trip the YAML.

**Tune it by eye, against the camera. The number means nothing on its own** —
the D5 DMX5125 decoders here are effectively full past about 10, so the whole
useful band is **1-6**. Start at 2, look at the picture, go up by ones. The
console stops at 16 and so does the server.

`Admin → Config` still works if you would rather type it; `reload_config()`
re-applies the level to the state you are standing in either way.

Check the boot log to see what actually loaded:

```
Run fill light: level 2 through COUNTDOWN and the RUN segments
```

### The trade

> **With `run_level` above 0, camera detection is not trustworthy.**

`vision/detect.py` samples raw brightness inside each dot's ROI with no
background subtraction. Ambient light raises every reading, so a genuinely
broken beam can read above `break_ratio` and the break is **missed** — the
failure direction nobody sees. Run the final on **manual** or **assisted**
detection with a gamemaster calling penalties, and do not read a clean
auto-detected run as evidence of anything while the room is lit.

Full reasoning, and what the change deliberately does *not* give up, in
[ADR 0012](adr/0012-fill-light-during-a-run.md).

---

## 3. Manual inputs on the GM console

A **`Manual inputs`** strip under the action row: **PLATE · CP 1 · CP 2 ·
STOP**. Each sends exactly the event the Opta would send for that channel, so
a whole run can be driven by hand when the input module is not reporting.

| button | event | state it works in |
|---|---|---|
| PLATE | `PlateHigh` | REGISTERED → ARM |
| CP 1 | `Cp1Pressed` | RUN_SEG_1 → RUN_SEG_2 |
| CP 2 | `Cp2Pressed` | RUN_SEG_2 → RUN_SEG_3 |
| STOP | `StopPressed` | RUN_SEG_3 → FINISHED |

**All four, not just the plate.** With the Opta down, a manual PLATE gets a
player into ARM and COUNT IN starts them running — and then nothing can *end*
the run. No checkpoint, no stop button, so the only ways out are ABORT, which
records no time, and the four-minute backstop. The plate alone makes the game
startable, not playable.

- **The live one is highlighted.** With dead inputs this strip is the input
  panel, and which one comes next has to read at a glance in a dark container.
- **Each is live in one state only.** The FSM accepts each event in exactly
  one state, so a mis-tap — or a stale iPad, or anything else on the network
  POSTing `/api/gm/input/...` — is a no-op. The greyed-out button is the
  courtesy; the state machine is the guarantee.
- **No tap-to-confirm.** They are stand-ins for a sensor, not decisions, and
  the point is that they are as fast as stepping on the plate.
- **The label turns red** and reads `OPTA NOT REPORTING` when the input module
  is actually down, so you are told why nothing is happening rather than
  having to work it out.

Driving a run by hand is recorded: each tap emits `inputs.manual`, tagged with
the input and the state it fired in.

### When the Opta stops answering

Symptom: nothing responds — plate, checkpoints and stop button all dead at
once. Check it before blaming the game:

```bash
ping 172.16.0.100                 # device alive?
journalctl -u scanmania | grep ModbusInputs | tail
```

`Connection refused` with ping working means the device is up but its Modbus
server is not listening — a sketch that died, not a network fault. **Power-
cycle the Opta**, not the NUC. The game reconnects on its own within 3 s and
logs `ModbusInputs connected to 172.16.0.100:502`.

Until it comes back, run the game from the manual strip above.

---

## 4. `/scoreboard` — the organiser's view

Names and times, ranked, live. Open it on a laptop or a phone.

**This is the one page that deliberately ignores `hide_times.`** That is the
whole point: the two public screens are sealed so nobody in the queue knows
what there is to beat, and somebody still has to read the times to award the
prize. The GM console is busy with a run and the admin portal needs a password
and shows forty other things; this is just the answer.

- One row per **player**, not per run — their best clean run.
- Busted and aborted runs never appear: a time nobody completed is not a score.
- Day-scoped server-side, so the 09:00 rollover is handled.
- Live over the same WebSocket as the displays, seeded from
  `/api/admin/leaderboard` so a reload lands on the answer immediately.
- While the seal is on it shows a red reminder that the public screens are
  hiding times, so nobody mistakes this page for one of them.

**It is unauthenticated**, like `/gm` and like the `/api/admin/leaderboard` it
reads — anyone on the venue network can open it. Keep it off any screen the
public can see during a sealed final, and do not link it from the display
pages.

---

## Running the final

1. Deploy the branch. `git -C /opt/scanmania status` first — the NUC's
   `config/mazes.yaml` is usually locally modified from live show tuning.
2. Confirm the three settings loaded, from the boot log:
   - `Run fill light: level N through COUNTDOWN and the RUN segments`
   - Admin → Config → game.yaml shows `display.hide_times: true`
3. Put detection on **MANUAL** or **ASSISTED** from the GM console. The room is
   lit; the cameras are not to be believed.
4. Walk the container out of MASTER with FORCE RESET, as always.
5. Check both screens before the queue arrives: no clock, no leaderboard.
6. Dry run, and tune `Run light` from the console while it is running — you
   are looking at the camera, not at the number.
7. Settle the prize from **Admin → Runs**, or the day's CSV export.

### Going back to normal play

Set `display.hide_times: false` and `run_level: 0` from the admin config
editor. Both take effect live — the screens unseal on the next broadcast and
the next run is dark again. No redeploy, and nothing to merge or revert: git
already holds the off values, so this is only ever undoing box config.
