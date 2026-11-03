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

**By eye, against the camera. The number means nothing on its own** — the D5
DMX5125 decoders here are effectively full past about 10, so the whole useful
band is **1-6**. Start at 2, look at the picture, go up by ones.

You do not need a restart: save `hardware.yaml` from **Admin → Config**, and
`runner.reload_config()` re-applies the level to the state you are standing in.
Values above 16 are clamped and logged.

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

## 3. PLATE on the GM console

A **PLATE** button, second in the action row, between NEW PLAYER and COUNT IN.
It sends the `PlateHigh` the Opta would send when somebody stands on the start
plate, which is the `REGISTERED → ARM` transition.

It is the only transition in the game that waits on a sensor rather than on the
console, so a plate that is not reporting used to strand a signed-in player
with no way forward but FORCE RESET and a re-sign-in — a minute of the queue's
time, per player.

- Live in **REGISTERED only**. The FSM accepts `PlateHigh` in that state and
  nowhere else, so a mis-tap — or a stale iPad, or anything else on the network
  POSTing `/api/gm/plate` — cannot start a run, skip a count-in or touch a run
  in progress. The greyed-out button is the courtesy; the state machine is the
  guarantee.
- **No tap-to-confirm.** The worst it can do is arm a player who has not
  stepped on the plate yet, and CANCEL undoes that.
- Blue, not green, because it sits next to COUNT IN and the two are tapped
  seconds apart.

The action row is seven fixed columns now instead of six, with the label type
scaled down inside the row. Every button still keeps its column in every state
— a control that moves between two broadcasts moves under the operator's thumb.

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
6. Set `run_level` by eye against the actual camera, during a dry run.
7. Settle the prize from **Admin → Runs**, or the day's CSV export.

### Going back to normal play

Set `display.hide_times: false` and `run_level: 0` from the admin config
editor. Both take effect live — the screens unseal on the next broadcast and
the next run is dark again. No redeploy, and nothing to merge or revert: git
already holds the off values, so this is only ever undoing box config.
