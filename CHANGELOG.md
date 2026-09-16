# Changelog

All notable changes to the ScanMania system are documented here.
Format: [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

---

## [Unreleased]

### Fixed

- **The house lights stayed bright after a run — the decoder, not the value.**
  Art-Net capture off the NIC showed the NUC transmitting `left=3 right=3` for
  the full 29.5 s of FINISHED and RESULT while the container was plainly not at
  3. Value, controller, packets and deployed config were all correct and all
  verified; the D5 DMX5125 decoders simply did not follow.

  They handle steady values perfectly — 0 is off, 1 and 3 are visibly
  different, slow fades land exactly where asked. What they do not handle is a
  90 ms hard-edged burst followed by a single frame at a very low value: they
  stay sitting bright. The settle was a hard cut (`fade: false`) straight out
  of the last flash beat, so dropping that gives the decoder a slew it can
  follow instead of an edge it cannot. Same destination, ~800 ms to get there;
  the flash itself stays hard, because that half always worked.

  Worth remembering when tuning any cue on this rig: the usable range on these
  fixtures is roughly **0-6**, not 0-255 — 1 reads as "very dim" and anything
  past ~10 is effectively full.

### Changed

- **The outcome flash is five beats, room and maze together** (was three).
  10 whole-board relay writes per run now, on top of the count-in ramp — the
  comment in `mazes.yaml` says so, because every beat switches 45 channels at
  once and that is the thing to check before adding more.


- **A cold boot no longer latches FAULT while the router is still coming up.**
  The self-test retried six times five seconds apart — about 25 s, which was
  enough for the PoE switch it was written for but not for the router, which
  takes roughly two minutes from cold. The NUC is ready long before it, so
  powering the rack on gave up ~95 s early and latched FAULT, and FAULT can
  only be left with FORCE RESET on the iPad.

  The window is a deadline now rather than an attempt count, and the boot one
  defaults to 180 s (`game.self_test_boot_timeout_s`, 0-600). Coming back from
  MASTER keeps a short 25 s window, because there the hardware was working a
  minute ago and a miss is a real fault rather than a cold start. While it
  waits it logs how long is left, so an operator can see it waiting for the
  network instead of wondering whether it has hung.


- **The GM work-light override was sticky, and defaulted to ON.** This is what
  "the house lights stay on after the stop button and are very very bright"
  actually was — and why it was so hard to pin on the stop button: the 255
  arrived at RESET, about thirty seconds after the press, once the outcome
  window had run out.

  Nothing ever cleared the override, and it beat the cue table in every state
  except the ones forced dark and (since the last fix) the outcome. So a GM who
  switched the lights on to walk somebody in got a flat 255 from RESET onward,
  held through attract and every run after it. Defaulting to `True` meant the
  same thing on a fresh boot with nobody having touched the switch at all: no
  attract breathing, no outcome settle, just full work light in every state
  that was not forced dark.

  Two changes. It now defaults to `None` — the cue table decides — and nothing
  is lost by that, because the box boots into MASTER and the `master` cue is
  already 255/255/255, so the container is lit for the walk-round either way.
  And signing a player in releases the override, because that is the moment the
  show takes over. The GM can switch it straight back on.


- **Signing a player in while they already stand on the plate now arms at
  once.** Inputs are edge-triggered, which is right for a game driven by people
  stepping on things — but a plate that is *already* held down when the GM
  finishes typing the name never sends another `PlateHigh`, so the box sat in
  REGISTERED and the player had to step off and back on with a queue watching.

  Entering REGISTERED now asks the inputs backend for the plate's current
  level instead of waiting for an edge, and enqueues a `PlateHigh` if it is
  already down. `input_level()` is new on all three backends (Opta/Modbus reads
  its existing poll cache; the Pico link and the fake now keep one). The FSM is
  untouched and still pure — it sees an ordinary event and applies its usual
  guards. Best-effort by design: a backend that cannot answer, or a link that
  is down, falls back to the edge exactly as before.


- **The end of a run now flashes, then holds the verdict.** Stop pressed → the
  room and the maze flash together → the verdict shape stays lit over a very
  dim room until RESET swaps in the attract show.

  Two things were wrong. The maze never flashed at all: `clean` and `bust` went
  straight to a solid preset. And they were `loop: true`, which combined with
  the fact that nothing between the outcome and RESET stops a show (FINISHED
  emits only `BroadcastState`) meant the shape held through FINISHED *and*
  RESULT — `result_display_ms` twice over — while the house cue had already
  taken the room to a hard 0. A lit maze over a black room read as a fault
  rather than a result.

  Both shows now flash the verdict three times on the same cadence as the
  house-light cue and end solid. Neither loops: a show that ends leaves its
  last preset applied and the reconciler holds it there, so the final step *is*
  the hold — no long `hold_ms`, and no relay moving while it waits.

  The room settles at **3/255** rather than 0. The old reasoning — that a
  half-lit room is the one condition in which you cannot read the laser
  curtains — still holds, and 3 is not half-lit: it is enough to see the walls
  and the way out while the verdict stays the brightest thing in the container.
  `result` matches that level exactly, because FINISHED and RESULT are two
  states but one continuous moment to anyone watching, and a mismatch drops the
  room a step partway through for no visible reason.

- **Signing a player in while they already stand on the plate now arms at
  once.** Inputs are edge-triggered, which is right for a game driven by people
  stepping on things — but a plate that is *already* held down when the GM
  finishes typing the name never sends another `PlateHigh`, so the box sat in
  REGISTERED and the player had to step off and back on with a queue watching.

  Entering REGISTERED now asks the inputs backend for the plate's current
  level instead of waiting for an edge, and enqueues a `PlateHigh` if it is
  already down. `input_level()` is new on all three backends (Opta/Modbus reads
  its existing poll cache; the Pico link and the fake now keep one). The FSM is
  untouched and still pure — it sees an ordinary event and applies its usual
  guards. Best-effort by design: a backend that cannot answer, or a link that
  is down, falls back to the edge exactly as before.


- **The maze stayed lit for 30 s after the stop button.** The house-light flash
  was right — the thing still on afterwards was the lasers. `clean` and `bust`
  were `loop: true` over a single solid preset, and nothing between the outcome
  and RESET stops a show (FINISHED emits only `BroadcastState`), so the verdict
  shape held through FINISHED *and* RESULT — `result_display_ms` twice over,
  30 s of a fully lit container with nobody in it. From the floor that reads as
  the flash dropping into permanent light.

  Both now hold the verdict for 2.5 s and end on `blackout`. The shape is the
  verdict and only has to be read once. Tune the read time in `mazes.yaml` or
  Admin → Shows.


- **The attract sparkle never ran.** `sparkle_off` was in `mazes.yaml` and the
  show player understood it, but `ShowStep` had no such field — so the loader
  dropped it and the runner read 0 off the dataclass. The attract show was four
  identical `all_on` steps: a dead, fully-lit grid. The key is parsed and
  bounded now (0-45, one per segment).

  It was being deleted from the far end as well: the admin show editor rebuilds
  every step as `preset` + `hold_ms`, so one save of any show silently stripped
  the sparkle out of the file. The key now round-trips through `ShowStepBody`
  and the editor, and is written back only when non-zero so ordinary shows keep
  a two-key step.

- **The house lights came on at the end of every run and stayed on.** The GM's
  work-light override is not cleared by a run, and it beat the cue table
  everywhere except the states forced dark — so at FINISHED the room went to a
  flat 255, held it through RESULT and back into ATTRACT, and went dark again
  only when the next run forced it. The outcome cue never played.

  The outcome states (FINISHED/BUSTED/RESULT) now belong to the cue table even
  with the switch on: the flash plays and the room snaps dark so the lasers
  read while the player looks up at the score. The work lights come back at
  ATTRACT — the player is walking out and the next group is loading in, which
  is the moment they are for. ABORTED and FAULT deliberately still hand the
  room to the GM: somebody is coming out of a dark container early.

- **Calibration ran with the entrance light on.** It is the brightest fixture
  in the box and points straight down the container, and nothing else can
  switch it off — `always_on` makes the DMX layer refuse. So its reflections
  counted against the ambient gate, which is the check that refuses a capture
  when the cameras can see anything with every laser off. Recalibration now
  takes the room and the entrance dark for the capture and restores both in a
  `finally`, on every path out including a refusal or an exception.

### Changed

- **Default haze 35 -> 50** (`hardware.yaml`), tuned in the container.

- **Chromium background networking off in `kiosk.sh`.** The box is offline at a
  venue and Chromium still spent its startup trying to register for Google
  push, at 60 ERROR lines per boot in the kiosk journal — noise that buries
  real display faults, and pointless outbound chatter from a show LAN. No
  display setting changed.

### Added

- **A 300 ms pause before the maze changes shape at a checkpoint**
  (`game.checkpoint_shape_delay_ms`, 0-1000, 0 restores the old snap). The
  player is stepping over the checkpoint as it fires, and a shape that changes
  under their feet is both startling and unfair.

  `ApplyPreset` gained a `defer` flag — a flag, not a duration, so `core/fsm.py`
  stays pure and reads no config and no clock (invariant 1); the runner owns the
  timing. It is scheduled rather than awaited, because the event drain is
  single-consumer and sleeping in it would stall the stop button and every beam
  event for the duration. It is cancelled with the other timers on power-down,
  so a pending shape change cannot land after a run has ended.

  The detector and the coils move **together** after the wait, and that pairing
  is the whole correctness argument: `_apply_maze` points the detector at the
  dots captured for a preset, so moving it early while the coils moved late
  would leave it watching dots that are not lit yet — they read dark, and the
  player is busted for a shape that has not appeared. The old shape stays lit
  and watched during the wait, which is right: the player is still in the
  container and those beams are still real.

### Changed

- **Default haze 1 -> 35** (`hardware.yaml`, `hazer.default_haze`), measured in
  the container. The old comment asserted 1-2 was the only usable range on the
  theory that more would wash the dots out; 35 is what actually makes the beams
  read. The duty cycle still governs the dose — this is the amount, not the
  duration — and the note now says to come down from here before touching
  detection thresholds.


- **Recalibration from the admin panel** (Calibration → Check / Save). After the
  container is moved, the dots drift — and they do not drift together: a dot is
  where a *beam lands*, so a settled mount or a flexed ceiling moves each one by
  its own amount. There is no offset to apply, so this genuinely re-finds every
  ROI from the live cameras, maze by maze, and re-measures each baseline with
  the same `sample_circle()` the runtime compares against.

  It re-detects geometry only: the per-camera thr/tophat/min_area were tuned by
  hand against this container's lighting and are carried forward untouched. A
  move changes where the dots are, not what a dot looks like — tuning is still
  `tools/capture.py` with the game stopped.

  It runs *inside* the game process, which is why it needs nothing stopped: the
  game already holds the RTSP streams and the relay boards, which is exactly
  what a capture needs and exactly why the standalone tool demands exclusivity.

  Guarded, because it can replace a working calibration: MASTER MODE only, an
  ambient check that refuses if the cameras see anything with every laser off
  (house lights on, or a door open onto a sunlit yard), a dry run by default,
  a `beams.json` backup before writing, and a refusal to save a result that
  loses more than a quarter of the dots — far more likely to be someone
  standing in the maze than a real change of that size.

  `find_dots`/`stages` moved from `tools/capture.py` to `vision/dots.py` so
  there is ONE implementation. `tools/` is not a package, so nothing in the
  running game could import it, and a second copy would have drifted from the
  one the calibration was made with.

### Added

- **Sound.** A looping music bed plus one-shot cues, both keyed by FSM state in
  `audio:` in `game.yaml`, playing `.wav` files from `sounds/`. Swap a sound by
  dropping a file in under the same name, or by pointing the config at a new
  one; `Admin → Hardware → Audio` lists what loaded, what is missing, and gives
  each file a Play button.

  The bed is **held across states that do not name one**, which is what carries
  one track through `RUN_SEG_1 → 2 → 3` rather than restarting it at every
  checkpoint — naming the same file again is a no-op. `silence` is how a state
  asks for quiet. Cues fire once on entering a state: count-in, a hit at each
  checkpoint, victory, defeat.

  Decoration, and built so it can never be anything else: a missing file, an
  absent `pygame`, a mixer that will not start, a sound card that is not there
  — every one degrades to silence and a log line rather than raising onto the
  game path. Cue sounds are decoded at startup because the game path may only
  call `play()`, and a filename cannot resolve outside `sounds/`. `.wav` only,
  so no decoder can be missing at a venue.

  **Audio is gitignored**, not carried in the repo: a single ambient bed is
  14 MB and the repo is cloned and pulled far more often than the music
  changes. `tools/push_sounds.sh` copies it to the box (rsync where available)
  and `deploy.sh` leaves it alone — git does not touch ignored files, so the
  audio on the box survives every deploy. A fresh checkout therefore has no
  sound and runs silent; `tools/gen_placeholder_sounds.py` writes stand-in
  tones, and the config-vs-files test skips rather than failing, since a test
  that fails on every clean checkout is one people learn to ignore.

  Formats split by job: **cues stay `.wav`** (decoded into RAM at startup, must
  fire the instant a checkpoint goes by, and too short for compression to be
  worth anything), while **the bed may be `.mp3` or `.ogg`** because it streams
  and runs for minutes — an 8-hour ambient track is 5 GB as `.wav` and 14 MB as
  a 10-minute `.mp3` loop. The bed loops forever, so length only ever buys
  variety. Bed tracks are opened once at startup (`verify_music`) so a codec the
  box cannot decode shows up in the boot log instead of as silence mid-show.

  Audible where you actually work: **not** part of `--fake-all` (every other
  fake stands in for hardware a laptop lacks; a sound card it has), and on by
  default in `tools/fake_run.py`, which grew `--pace` because a run that
  completes in 300 ms collapses the whole soundtrack into one noise and exits
  before the victory cue finishes. `--fake-audio` and `--silent` opt out.

- **End-of-day shutdown** (admin portal → Dashboard → End of day, or
  `tools/shutdown.py`). Saves first, darkens second, because a shutdown must
  never be what loses a day's runs: a run still in progress is settled as
  `aborted`, the fire-and-forget event rows still in flight are drained, and
  the database is snapshotted and closed — all before a single light drops.
  Then lasers, then haze and the maze lights, and **the entrance last**, so the
  operator can see their way out while the box goes down. Optionally halts the
  NUC afterwards: cutting mains under a running filesystem is how the box comes
  back with a corrupt database, and a DB snapshot does not protect the
  filesystem it was written to.

  Only once the container is dark does it stop the kiosk, stop the game and
  halt the NUC — scheduled in a detached transient unit, because stopping the
  game kills whatever issued the command, and kiosk-first because the kiosk's
  `Wants=` drags the game back up otherwise. Stopping the service is what
  closes the database cleanly, so the sequence does not close it itself.

  `DmxController.power_down()` is the one sanctioned override of the
  `always_on` entrance guard, reachable only from an explicit shutdown request.
  `blackout()` — what a dying process calls — still leaves the entrance lit.

  Every step reports its own outcome. A sequence with a failed step halts
  nothing and says so: a coil that did not answer may still be energised, and a
  halted box cannot be asked about it. A box that stays up — halt unticked, or
  a halt that could not be scheduled — keeps its database open and releases the
  light latch, so FORCE RESET brings it back rather than ssh being the only way
  out of a dark container.

- **Both displays rebuilt to the brand design.** A shared
  `web/static/shared/brand.css` carries the Proxima Nova faces and the tokens;
  colours are sampled from the supplied artwork rather than eyeballed (red
  `#EC1C24`, blue `#005AA9`). Fonts load from disk, not a CDN — a venue with no
  internet must still render correctly. The laser lines are the supplied
  artwork rather than drawn, so the screens match the printed collateral.

- **`/display/out` is portrait, 9:16** — people photograph it with a phone, and
  a portrait frame is what they can post. Logo, clock, camera stage, then the
  leaderboard and sponsor lockup pinned along the bottom, because that strip is
  what ends up in the photo and must not move when a name is longer. The panel
  is mounted rotated and `kiosk.sh` now tells X
  (`SCANMANIA_ROTATE_OUT`, default `left`), swapping width and height for the
  Chromium window — there is no window manager, so the window is positioned from
  explicit pixels and an unrotated size would have put the sponsor lockup
  off-screen.

- **The clock is two-tone** on both screens, as in the artwork: minutes in blue,
  seconds in red, so the part that is actually moving is the part that reads
  from across a container.


### Added

- **`iobackend/hazer.py` is now `iobackend/dmx.py`.** The name stopped being
  accurate when the room lights joined it. `HazerController` is `DmxController`,
  with an alias kept so an out-of-tree import does not break.

- **Room lights on DMX**, on the hazer's existing Art-Net universe: ch3 left,
  ch4 right, ch5 entrance (a 3-channel decoder addressed at 3, so its own
  ch1/ch2/ch3 land there). 1-channel white, so they dim and fade. They live in
  the DMX module rather than one of their own because **one object must own the
  universe** — every Art-Net frame carries all 512 channels, so a second sender
  would zero the lights twice a second.

- **`tools/dmxpatch.py`** prints the patch sheet, generated from config so it
  cannot drift from what the software actually sends.

- **Haze is duty-cycled** — 60 s on, 240 s off (20% of continuous). Tested
  manually in the container: 1% (DMX 2-3) was too much, so the working range is
  DMX 1-2 — barely any room to tune the level. The duty cycle is the main dose
  control, and the interval is the dial: if there is still too much
  haze, lengthen `haze_interval_s` rather than shortening the burst. The burst
  must stay long because a hazer has a heater — a 2 s command is spent warming
  up and produces essentially nothing. The blower runs throughout so settled
  haze stays distributed, and the GM switch wins mid-burst.

- **Light cues driven by FSM state** (`light_cues` in `mazes.yaml`): a slow dim
  alternating pulse in ATTRACT, a step up on sign-in, a flash on a clean finish,
  one hard snap on a bust, a throb in FAULT. Steps set levels and hold; `loop`
  repeats, `fade: false` snaps — which is what makes a flash read as a flash.

- **The GM switch is a work-light override**, not a level: solid on for loading
  and unloading, suspending the cue; off hands the lights back to the show.

### Fixed — round-2 audit (10 reviewers)

Five Critical, the rest High and Medium. The two that mattered most were both
"the box does not work", not edge cases.

- **No run could start on real hardware.** ARM never pointed vision at the
  maze, so preflight read whichever preset the attract show last applied and
  faulted every time. Confirmed in the NUC journal; invisible in tests because
  `vision/fake.py` reports a healthy dot count regardless.
- **One network blip killed vision until restart.** `wait_for` cancels the
  future, not the thread, so a parked reader stayed parked; a repeating outage
  drained the shared pool in ~2 minutes and healthy cameras then only queued.
  Each camera owns a retirable single-worker executor now.
- **The shutdown lied and then re-lit the maze.** `apply_all_off` reports a
  failed write by returning False, which was read as success; and three of
  seven timers were cancelled, so a surviving one walked the FSM to RESET and
  replayed the attract show ~30 s after "container is dark".
- **Any box behind origin could not deploy.** `deploy.sh` pushed local config
  before pulling, so the push was always a non-fast-forward.
- **MASTER MODE left the lasers on and the room dark** — no blackout in the
  transition, and no `master` light cue, in the one mode that means a human is
  walking inside.
- Run records: FORCE RESET mid-run left the row `in_progress` and later stamped
  it with a false reason; aborted runs were recorded as busted on a dot nobody
  adjudicated.
- Detection: a break re-fired ~6×/s and starved the assisted safety abort;
  auto-masked dots counted dark forever and tipped the detector into a
  permanent fault; a dead camera was invisible to the stall gate; the settle
  window was shorter than a reconcile cycle, so a failed relay write busted the
  player.
- Security: every gated admin route was an unthrottled password oracle;
  request bodies were read before auth with no size cap; the CSRF allowlist was
  derived from the attacker-controlled `Host` header; raising a light mid-run
  could wash out the dots and force a bust.
- Data: `snapshot_interval_min: 0` also disabled the PII purge; the corrupt-DB
  runbook left the WAL behind and re-corrupted the restore; the leaderboard
  never refreshed at the operating-day rollover, so the street display showed
  yesterday's board all morning.

Full detail in the commit messages for `a408f03`, `c19edf2`, `d0b0306`,
`49741dd` and the follow-ups.

### Fixed

- **A failed boot probe left the box unable to leave MASTER.** `boot_to_master`
  means "this is the boot", not "the boot succeeded", but only the self-test
  pass path consumed it. A boot whose probe failed — the PoE switch coming up
  after the NUC, which is the entire reason the self test retries — left the
  flag set for the rest of the session. Every later "exit master mode" then
  re-ran the probe, passed, saw the flag and returned to MASTER: the GM's EXIT
  button looked dead. FORCE RESET still escaped, but only if someone knew to
  try it. The fail path consumes the flag too.

- **The snapshot directory was open-coded in three places** and had already
  drifted into a path that failed on a developer Mac for no useful reason. One
  `persist.backup.snapshot_dir()`.

- **`deploy.sh` rewrote its own source mid-run.** Bash reads a script lazily and
  seeks by byte offset, so the `git pull` in step 4 left the interpreter
  pointing into the middle of the new file — a deploy could run the old half of
  the script after pulling the new one, with nothing in the output to say so.
  It now re-execs from a private copy in `/tmp` before touching git.

- **The fault list was never empty at idle.** "detection is blind" fired
  whenever no maze was armed — which is MASTER and ATTRACT, i.e. most of the
  day, and now every boot, since the box boots into MASTER. The detector
  watching nothing with no maze lit is the design, not a fault. It is reported
  only when a maze is actually lit, where zero dots is genuinely run-ending.

- **Every deploy printed a camera fault.** The health check read
  `/api/admin/status` the instant uvicorn answered, and eight RTSP cameras have
  not connected by then. It now waits up to 45 s for transient faults to clear
  before believing them. A fault printed on every deploy is one nobody reads.

- **The kiosk never read its own overrides.** `kiosk.sh` documents
  `SCANMANIA_OUT_IN` / `SCANMANIA_OUT_OUT` / `SCANMANIA_ROTATE_OUT` as settings
  in `/etc/default/scanmania`, but `scanmania-kiosk.service` had no
  `EnvironmentFile`, so which page landed on which panel was hard-coded in
  practice and editing the documented file changed nothing.

- **A missing panel stole the other panel's role.** An unconnected `OUT_IN`
  fell back to "the first connected output" — which is the screen `OUT_OUT`
  had already resolved to. With one panel cabled, asking for the outdoor page
  on it still launched the in-container page. Each role now resolves
  independently, and positional assignment only applies when neither
  configured output is connected.

- **The kiosk served a cached frontend after a deploy.** The screens are
  single-file HTML with no build step, so no filename carries a content hash.
  Chromium kept the previous build in disk cache across restarts: after a deploy
  the NUC ran new code while the panels showed the old design, which reads as a
  deploy that silently did nothing rather than as a caching problem. Markup and
  `brand.css` are now served `no-store`; fonts and artwork stay cacheable, being
  3.4 MB that never changes.

- **The attract pulse did not run after a force reset.** RESET is transient: the
  runner sets `self.state = ATTRACT` directly and executes its own side effects
  rather than going through `dispatch()`. The light-cue hook lived only in
  `dispatch`, so the player's last known state stayed RESET — a state with no
  cue — and fell through to all-off. Nothing looked broken: the FSM was in
  ATTRACT, the WebSocket said ATTRACT, and the room was simply dark. The hook is
  now a single `_cue_lights()` helper called from every path that changes the
  state, including that hop.

- **Fades were jumpy because short moves did not fade at all.** The per-tick
  step was derived from full 0-255 travel and applied whatever the distance, so
  the attract pulse — twelve levels, 14 to 2 — completed in a single tick. Fades
  are now interpolated against elapsed time, so every move takes `fade_ms`
  regardless of distance: that pulse went from one 12-level jump to 52 frames of
  1-level steps. Levels are carried as floats and rounded rather than truncated
  (truncation biased every ramp a level low, reading as a stall then a jump),
  and the tick rate now sits above the node's 35 Hz output so each frame it
  sends carries a fresh value. A full 0-255 fade in 800 ms is still inherently
  stepped — 8-bit DMX cannot fit 255 levels into ~28 frames — so lengthen
  `fade_ms` if a long throw needs to be smooth.

- **COUNTDOWN and the RUN states are forced dark in code**, over any cue and over
  the GM override. This is correctness, not a look: the detector samples raw
  brightness inside each dot's ROI with no background subtraction, so ambient
  light raises the reading and a genuinely broken beam can still read above
  `break_ratio` — a MISSED break, where a player runs clean through a beam they
  broke. Measured: each camera sees 12-31 ambient blobs with the house lights on.
  A forgotten tap can no longer invalidate a run.

- **The entrance light is never switched off by software**, including on
  shutdown. An Art-Net node holds the last frame it received, so that frame is
  the state the container is left in when the process exits — and a dark box
  with people in it and no lit way out is worth hard-coding against.

### Fixed — from the 10-agent longevity audit

Findings ranked by how they would fail over a 2-month tour. Everything below was
verified against the code, not assumed.

**Critical**

- **A camera stall crash-looped the whole process.** `VisionStalled` was used in
  `core/runner.py` and never imported, so any frame gap over 300 ms raised
  `NameError`, killed the vision listener, and took the process down —
  restarting mid-run, over and over, for a flapping camera. Invariant 5's
  "drop to manual, never bust" path had never executed. The test suite stayed
  green because it dispatches `VisionStalled` straight into the FSM, bypassing
  the listener.
- **The unit file still forced `--fake-vision`,** with a comment claiming
  `VisionService` did not exist. It does. Production would have run fake vision
  with eight cameras wired up and nothing reading them.
- **One wedged WebSocket client froze the entire game.** `broadcast()` awaited
  each client serially with no timeout, and only an *exception* evicted anyone —
  a sleeping iPad filled its TCP window and blocked the single event drain for
  minutes. The stop button, beam breaks and GM BUST all queued unprocessed, and
  because `Stopwatch.stop()` reads the clock when it executes, a 28-second run
  was persisted with the stalled elapsed. Now: one serialisation, concurrent
  sends, 250 ms per-client deadline, drop what cannot keep up.
- **Lasers and the hazer stayed ON when the service stopped.** `apply_all_off`
  was only ever called inside the count-in. Coils latch and the boards are
  separately powered, so `systemctl stop` — which the recalibration runbook
  tells the operator to run — left the maze lit in an unattended container.
  `GameRunner.blackout()` now runs on every exit path, bounded so an unreachable
  board cannot hold shutdown, and the hazer sends a zeroed DMX frame.
- **Eight camera threads exactly filled the default executor.** Every RTSP read
  used `run_in_executor(None, …)`, the same pool as every Modbus write and the
  20 Hz Opta poll that carries the stop button — `min(32, cpu+4)` = 8 workers on
  a 4-core NUC. Modbus timed out while merely *queued* and marked boards
  DEGRADED, so camera traffic could take the maze and the stop button down with
  no network fault at all. Cameras now have their own pool, with open and read
  deadlines so a frozen stream reconnects instead of costing a worker forever.
- **Five fast restarts left the unit permanently dead.** `StartLimitIntervalSec=0`
  sat in `[Service]`, where systemd silently ignores that spelling, so the
  defaults (5 starts / 10 s) applied and the "never give up" comment said the
  opposite of what the box did.

**High**

- **The rolling EMA corrupted baselines during ATTRACT.** Shows drive presets
  straight through the resolver, bypassing `_apply_maze`, so the detector kept
  watching the last *applied* maze while the attract show flashed blackout —
  and every dark frame was folded into that maze's baselines. Editing a show's
  `hold_ms` silently retuned live detection sensitivity.
- **A beam-breaking run could top the leaderboard with a faster time.** In
  assisted mode, a player reaching the stop button before the GM decided was
  recorded `clean` — and `Stopwatch.stop()` being idempotent meant the saved
  time was the *halt* time, not the finish time. The pending break now wins.
- **An undecided assisted break wedged the game** in a RUN state with the
  stopwatch frozen and every timer cancelled. There is now an
  `assisted_timeout_ms` deadline (default 60 s). On timeout the run **aborts**:
  auto-veto re-arms detection on a player still standing in the beam and loops
  forever, and auto-bust convicts someone nobody looked at. Abort is the honest
  outcome — the run does not count and the player runs again.
- **`max_run_ms` restarted from zero on every veto**, so three false positives
  bought a player nine minutes. It is now an absolute deadline anchored at GO.
- **The box booted into terminal FAULT if the relay boards were slower than the
  NUC.** A single failed self-test latched FAULT, which only FORCE RESET
  escapes. It now retries six times over 30 s.
- **`POST /api/gm/master-mode` was an unauthenticated kill switch** with zero
  consumers — one curl from the venue wifi stopped a run and cleared `run_id`
  with no `SaveRun`. Removed, along with `/api/gm/mask-beam` (a stub that only
  logged). `MasterModeEngage` from a RUN state now saves the run as aborted.
- **The GM stopwatch kept running after the WebSocket dropped.** Both displays
  freeze theirs, commented *"A frozen clock is honest. A running one is a lie."*
  The GM console did not.
- **Masking a beam was a no-op through all three paths.** `clear_masks()` was
  called behind a `hasattr` guard and existed on neither backend — returning
  `{"ok": true, "unmasked": 45}` while changing nothing. Masking now works on
  dot ids against the maze captures, which is the only masked flag detection
  reads, and reports when a dot is not in the maze currently lit.
- **Detection could watch zero dots while the GM showed healthy counts.**
  `stats()` counted states, not *sampled* states, so an uncalibrated capture
  reported a full watch count while `process_frame` skipped every dot. It now
  counts what is actually sampled and reports `blind` separately.
- **Detection thresholds came from `beams[0]`** — one entry of the 45-channel
  wiring record that invariant 3 says is not read at runtime. Moved into
  `detection.break_ratio` / `clear_ratio`.
- **Preflight could not fail.** `PreflightFail` was never emitted from anywhere,
  so a run would arm with unreachable boards, stalled cameras and zero
  calibrated dots. It now checks board health, camera liveness and whether the
  lit maze has dots.
- **`deploy.sh` could wedge the box permanently.** It committed local config
  drift *then* pushed; a rejected push left the commit behind and every later
  deploy failed `--ff-only`. It now verifies a fast-forward first, undoes its
  own commit on failure, backs up with `sqlite3 .backup` instead of `cp` of a
  live WAL database, and rolls the checkout back if `pip install` fails.
- **Modbus `wait_for` orphaned the executor thread**, leaving two threads on one
  socket — and RTU has no transaction id, so a crossed response CRCs clean and
  is accepted as the wrong transaction. Timeouts now burn the socket.
- **A DEGRADED board never dropped its socket**, so a blackholed flow stayed
  dead for the kernel retransmit timeout — about 15 minutes.
- **The Opta never validated the Modbus transaction id.** One timeout left the
  button reader permanently one poll behind, compounding with each subsequent
  timeout: late stop-button registration and phantom false starts, with nothing
  in the logs. `is_connected` now means a read succeeded, not that TCP
  handshook.
- **"Download snapshot" deleted 34 of 48 backups** — a hardcoded `14` against
  the loop's `snapshot_keep`, on the same directory with the same glob.
- **Every metric was discarded.** `metrics.configure()` had zero callers, so
  `relay.mismatch`, `vision.stall`, `vision.mass_dark` and `break.detected` had
  never recorded a value. They are now structured log lines (`METRIC name=…`).
- **`vision.run()` and `inputs.run()` were unsupervised orphan tasks**, and
  `VisionService.run()` *returned* (no exception) when no cameras were
  configured — a silently dead pipeline behind a healthy console.

**Medium**

- The reconciler compared a stale `desired` snapshot against a fresh read,
  producing a false mismatch on every preset change — a warning and a redundant
  full-board write several times a second during a show.
- `reload_config()` never touched vision, so pasting in a fresh calibration
  returned `ok` and changed nothing until a restart. The admin UI now also shows
  the `reload_note` it had been discarding ("deferred — FSM is in RUN_SEG_2").
- `_apply_maze` ran *after* the coil write, so the settle window did not cover
  the transition.
- The outdoor leaderboard went blank after every restart: the cache started `[]`
  and was only refreshed on save, and `if (msg.leaderboard)` is true for `[]`.
- "Daily" meant UTC midnight — the public board wiped at 02:00 local, and 01:00
  after the 25 Oct 2026 DST change, inside the tour. It is now a configurable
  local operating day (`SCANMANIA_DAY_START_HOUR`, default 09:00), bounded on
  both sides so a session recorded with a wrong clock cannot pin junk to it
  forever. `leaderboard.scope` is finally honoured, and one row per player.
- Config range comments are enforced: `max_simultaneous_breaks: 0` used to load
  clean and suppress every break forever.
- A schema downgrade is refused rather than run against an unknown schema, and a
  `PRAGMA quick_check` runs at open.
- The snapshot loop prunes *before* writing (it could never recover from a full
  disk) and snapshots immediately on start rather than after the first hour.
- `/api/signin` and `/api/admin/login` are rate limited; failed logins are
  logged. Display names are normalised, with bidi/zero-width controls stripped
  and combining runs capped — those went straight onto the public display.
- The DB no longer silently relocates to a gitignored, never-backed-up file when
  `/var/lib/scanmania` is missing.
- A resolution mismatch now stops that camera being sampled instead of clamping
  ROIs to the frame edge and returning confident nonsense.
- Credentials are redacted from `config_audit`, which was copying whole config
  files — including camera RTSP passwords — into all 48 rolling snapshots.

**Low**

- Evidence uses one ring buffer per camera (one shared ring held ~50 ms of
  interleaved frames, so crops came from the wrong camera) and filenames are
  stamped with wall-clock UTC (they used monotonic time, which restarts near
  zero each boot, so day 2 overwrote day 1's proof).
- `faults()` no longer flags the configured `assisted` mode as a fault — it
  cried wolf on every deploy — and now reports dead cameras, uncalibrated mazes,
  dots without baselines and failed DB writes, none of which it could see.
- All four frontends have a stale-data watchdog: a half-open socket or a dead
  broadcaster used to freeze every screen silently.
- The outdoor display no longer rebuilds its leaderboard DOM 10×/second, and no
  longer fires a failing connection every 15 s to a port nothing listens on.
- `vision/mjpeg.py` removed: never fed (`push_frame` had no caller), depended on
  an undeclared `aiohttp`, leaked a task per client per 5 s, and keyed a
  never-evicted dict on a client-supplied path.
- `requirements.txt` is pinned. ADR 0005 claimed it already was.
- `kiosk.sh` supervises each window (`wait` blocked on *all* jobs, so one dead
  panel was never noticed); the kiosk unit and journald limits are now in git.

### Added

- **End-of-day CSV export.** `EXPORT DAY` on the GM console downloads every run
  of the operating day: run id, player id, first name, surname, email, DOB,
  gender, local start time, time of day, elapsed (ms and mm:ss.mmm), busted
  flag, outcome, segment reached, detection mode, busting dot, and void status
  with reason. It is the one GM action that asks for the admin password —
  everything else on that console is deliberately passwordless, and this carries
  personal data. `?day=YYYY-MM-DD` exports a past day.
- **The flight recorder is wired.** `insert_event` had zero callers, so every
  disputed bust opened a run detail with an empty timeline. FSM transitions are
  now recorded per run, fire-and-forget so the game path never waits on the DB.
- **Invariant 7's record-keeping half.** The run row is written pessimistically
  at GO, so a crash or power cut mid-run leaves a row instead of nothing. Runs
  left `in_progress` are settled as aborted at the next boot — the run is still
  never *resumed*.
- **Retention that runs.** Events rotate at 30 days (the docstring had claimed
  this for months while nothing called it), player contact details are purged
  after 90 days, and `config_audit` is capped.
- **`docs/deployment.md`** — two ADRs referenced it; it did not exist. Bare
  Debian to a playable box, the stop order, DB recovery and rollback.


### Fixed

- **BUST, VOID and FORCE RESET were unreachable from the GM console.** All three
  are tap-to-confirm: first tap shows "TAP AGAIN", second tap fires. The reset
  that clears a pending confirmation was meant to run on a state change, but it
  ran inside `applyState()`, which is called on **every** WebSocket frame — about
  10 per second. A pending confirmation was therefore cleared within 100 ms, and
  the operator's second tap only restarted the cycle. The request never left the
  browser, which is why a FORCE RESET out of MASTER appeared to do nothing: the
  FSM had handled it correctly every time it was actually asked. Guarded on
  `prevState`.

### Changed

- **Schema v4 forgets email and gender.** Sign-in stopped collecting them, but
  rows written earlier still carried them in `extra_json` — and in every hourly
  snapshot taken since. Data we have decided not to hold should not survive in
  the file just because it was written before the decision. The migration runs
  once at startup, after `deploy.sh` has taken its pre-migration backup, so
  nobody has to remember a manual purge on a touring box.

  Rewritten in Python rather than with `json_remove()`: JSON1 is near-universal
  but this has to run unattended in a shipping container, and a migration that
  fails there fails at boot. It only touches rows that actually carry the
  fields, and leaves unparseable `extra_json` alone rather than discarding a
  row's other data trying to clean it.

- **Sign-in collects only what identifies a player: first name, surname and
  date of birth.** Email and gender are no longer asked for, stored or
  exported. A player's identity is their name and DOB; everything the game
  actually needs — run time, outcome, segment reached, which dot broke — hangs
  off the run row, not off them. Personal data you do not hold is data you
  cannot leak, cannot mishandle at a venue, and never have to purge.

  Surname is now **required** (it was optional), validated on the server and
  checked in the console first so the GM is told before the round trip, with a
  player standing in front of them. A stale iPad that still posts `email` or
  `gender` is not rejected — the fields are ignored and never stored — so an
  un-refreshed console keeps working.

  Older rows may still carry the dropped fields in `extra_json`. The day export
  deliberately does not read them, so the CSV cannot re-spread data we have
  stopped asking for.

### Changed

- **The stop button on Opta I4 is now treated as normally closed.** The
  inversion happens in the sketch, immediately after the pin read, so the
  debounced state — and with it the Modbus bit, the Opta's own LEDs, its web
  page and its serial log — all keep meaning "1 = pressed" whatever a given
  button is wired as. Nothing on the NUC side changed; `("stop", 1)` still maps
  to `StopPressed`. Per-input contact type lives in `IN_NC[]`.

  Normally closed is the right choice for a stop button: a cut wire, a pulled
  connector or a dead contact reads as OPEN, which after the inversion reads as
  PRESSED, so the run ends. The failure that matters is the other one — a stop
  button that cannot end a run — and NC makes it impossible.

  The cost is that a broken stop circuit now ends every run the instant it
  starts, which from the floor looks like the game is simply broken. Preflight
  names it: arming with the stop input already reading pressed fails with a
  message that points at the wiring rather than at the player.

### Changed

- **The GM console and admin portal now carry the brand.** Proxima Nova and the
  wordmark from the displays, and the accent moved to the sampled brand blue
  and red, so the tools read as the same product as the screens.

  Identity, not layout: both stay dense, dark and big-targeted, because they
  are read at arm's length in a lit container rather than photographed from the
  street. Two things deliberately stay monospace — the stopwatch, whose digits
  would jitter ten times a second in a proportional face, and the player
  nickname, which an operator reads back character by character against what
  was typed at sign-in.

  The brand colours are chrome, not text: `#005AA9` is 2.8:1 and `#EC1C24` is
  4.4:1 on these near-black surfaces, both under AA for body text. Each gained
  a lighter same-hue text tint (`--accent-text` 5.6:1, `--red-text` 5.9:1) and
  the 18 text usages were moved onto them. A bust indicator that is harder to
  read is not a trade worth making for a closer swatch.

### Changed

- **`tools/capture.py` shows every stage of the detection pipeline**, per camera:
  raw / signal / mask / overlay. Looking only at the final overlay tells you a
  camera found nothing but not why, and the causes want opposite corrections — a
  dark signal is exposure, an empty mask is the threshold, and rings in the mask
  mean the top-hat kernel is smaller than a dot. Rejected blobs are drawn too, in
  amber below `min_area` and blue above `max_area`, so a bound that is cutting
  real dots is visible rather than inferred. Each camera also reports its signal
  peak and median dot area, which is the number that says whether the kernel is
  in the right range.

- **`tools/capture.py --no-relays`**, and an unreachable relay board no longer
  aborts the tool. Looking at cameras and tuning parameters is useful on its own.

- **Removed `tools/pick_rois.py` and `tools/dot_calib.py`.** Both are superseded
  by `tools/capture.py`: `pick_rois` clicked dots by hand to print stanzas that
  had to be pasted in, and `dot_calib`'s stage panels are now in the capture page
  next to the sliders that write the file.

- **`tools/cam_probe.py` defaults to the cameras in `hardware.yaml`.** Typing
  eight RTSP URLs to look at the eight cameras already declared in config is
  busywork that invites a typo you then debug as a network fault.

- **Eight ceiling cameras, named by position.** `cam_1`-`cam_4` on `.201`-`.204`
  became `SM-CAM-11`-`14` on `.211`-`.214` (left side) and `SM-CAM-21`-`24` on
  `.221`-`.224` (right side), matching the `SM-NODE-*` house style. Full ceiling
  coverage means every dot is close to some camera, which is what makes
  per-camera tuning tractable — and it removes the blind spots four cameras left.
  The four original cameras are in the new set; their MAC-derived paths prove it.
  Substream resolution is 1024x576 on all eight, declared per camera in
  `hardware.yaml` so `tools/capture.py` can catch a camera that drifted.

- **`find_dots` defaults err large on both bounds**, because the two failure
  directions are not symmetric. A top-hat kernel that is too *large* only weakens
  background subtraction; one that is too *small* leaves the dot as a ring that
  fragments into arcs — which is why one camera found 5 dots and another 11
  looking at the same five lasers. Both artefacts are now pinned by tests.
  Worth knowing: `max_area` does **not** reject a ceiling light fitting. The
  top-hat hollows out anything bigger than the kernel, so a fitting arrives as
  four small *corner* blobs that are dot-sized by area. `min_area` rejects those,
  and the real defence is the ambient guard.

- **`beams.json` no longer declares cameras.** Its top-level `cameras` block
  duplicated `hardware.yaml`, still held the old `.201`-`.204` addresses, and
  was read by nothing. Two places declaring camera URLs with one of them wrong
  is how a stream silently points at nothing. The per-channel `camera`, `roi`
  and `baseline` fields went with it — leftovers from when a channel owned its
  dots, and a dead camera name repeated 45 times after a rename.

- **Camera ids are validated where they matter.** `load_all()` checked the 45
  wiring entries' `camera` field, which nothing reads; it now checks the maze
  captures' camera ids, which route live frames to ROIs. A stale one there means
  dots sampled against a frame they do not belong to.

- **`tools/camshow.py` reads `hardware.yaml`** instead of carrying its own copy
  of the camera table. Takes `SM-CAM-13`, `13`, `left`, `right` or `all`, tiles
  8 in a 4x2 grid, and `--probe` now reports a camera answering at someone
  else's address as a stale config rather than silently correcting it — the IPs
  are static.

- **Calibration is per maze, not per relay channel.** `tools/sweep.py` lit one
  channel at a time to learn which 5 dots belonged to it — two passes, 45 relay
  switches, and a fault rule that depended on the mapping being right. The
  mapping bought nothing: ending a run needs to know that *a* beam broke, not
  which relay drives it. `tools/capture.py` replaces it. Light a maze, tune each
  camera until its count looks right, save what it saw. See
  [ADR 0009](docs/adr/0009-per-maze-dot-capture.md).

- **Detection parameters are per camera.** One top-hat kernel cannot serve
  cameras at different distances: the kernel must be larger than a dot and
  smaller than the dot spacing, and both scale with distance. A real sweep found
  5 dots on one camera and 11 on another looking at the same five lasers — the
  near camera's dots were bigger than the kernel, so each became a ring that
  fragmented into arcs. Params are now stored alongside the dots they produced,
  so a capture is reproducible.

- **The hardware-fault rule is a count.** It was "all 5 dots of this channel are
  dark, so it is a relay, not a player". With no channel mapping there is
  nothing to key that on. Now: 1 to `max_simultaneous_breaks` dark dots is a
  player, more than that is suppressed and reported as a fault. Coarser, but it
  rests on nothing that can be miscalibrated.

- **The GM beam strip is a per-camera summary.** It was 45 pills, one per relay
  channel. Detection watches ~175 individual dots now, and 175 pills is not
  something anyone reads on an iPad mid-run. Each camera shows watched / dark /
  masked, with a total line above. Long-press masking moved to `/admin/beams`,
  where you can see the dot on the frame.

- **`/admin/beams` leads with the calibration state** — per maze, per camera,
  dot counts, mean and minimum baseline, capture resolution and the params that
  produced them. The 45 channel entries are still listed, now labelled as the
  relay wiring reference that nothing reads at runtime.

- **A bust names a dot, not a channel** (`cam_3:d17`). Which segment broke is no
  longer knowable, accepted deliberately: the operator needs to know a beam
  broke and roughly where to look, and the camera id answers that.

### Fixed

- **The rolling EMA baseline was dead code.** `BaselineManager.update_ema()` and
  `save_to_config()` had zero callers, so the haze-drift correction that
  CLAUDE.md documents as deliberate behaviour had never run. The detector now
  feeds samples between runs, and only between runs — during a run the manager
  is frozen, so adapting to a broken beam remains impossible.

- **Evidence filenames no longer carry a colon.** Dot ids are `cam_3:d17`.
  Legal in a POSIX filename and a trap in a URL, on SMB, and on Windows.

### Added


- **The four real cameras are wired in.** `config/hardware.yaml` had a single `cam_a`
  pointing at a stale IP with the wrong password and a path served by a different
  camera. Replaced with `cam_1`-`cam_4` on `.201`-`.204`, keyed by their MAC-derived
  paths. `beams.json`'s `cameras` block still held `rtsp://172.16.0.30:554/substream`.

- **`CameraStream` no longer depends on how OpenCV was built.** `cv2.VideoCapture` can
  only open RTSP when the wheel bundles FFmpeg, and that differs per machine: the Mac
  wheel reports `FFMPEG: NO` and fails in 0.0 s with `isOpened()` False — which looks
  exactly like a network fault. It now falls back to an ffmpeg subprocess, the approach
  `dot_calib.py` and `camshow.py` already proved. Either backend works, and neither
  machine needs to care which it gets.

### Fixed

- **Nothing ever started the vision loop.** `GameRunner.run()` started
  `inputs.run()` but never `vision.run()`, so the backend was constructed, handed to the
  runner and left idle — `_vision_listener` waited on a queue nobody filled. No camera
  was opened in the real service at all.

- **`camera_stats()` did not match its caller.** `web/routes_admin.py` calls
  `camera_stats(cam_id)` and reads `stall_count`; the implementation took no argument and
  returned `stalled`. The admin Hardware page showed `fps: null` for every camera. Now
  serves both shapes and tracks a cumulative stall count, so a feed that flaps but
  happens to be up right now is still visible as a problem.

### Removed

- **Cloud sync, entirely.** See [ADR 0008](docs/adr/0008-remove-cloud-sync.md). It was
  built and never connected to an endpoint — `OutboxWorker` only started when
  `SCANMANIA_SYNC_URL` was set, and it never was. Gone: `persist/outbox.py`, the
  `outbox` table (schema v3 drops it), the `QueueSync` side effect and its 8 FSM
  emission sites, `httpx`, the six `/api/admin/outbox/*` routes, the Cloud Sync
  admin tab, the `scanmania-sync` log unit, and `SCANMANIA_SYNC_URL` /
  `SCANMANIA_SYNC_TOKEN`.

  `persist/sync.py` is renamed `persist/backup.py`. It never contained any cloud
  code — only snapshots and CSV export — and the filename was the single biggest
  remaining suggestion that sync still existed.

  Invariant 6 is rewritten, not deleted: "gameplay never awaits the network" still
  binds Modbus, Art-Net, RTSP and WebSocket.

  **Migration note:** existing databases lose their outbox rows on first start.
  Verified on a real v2 database — schema goes to v3, the table is dropped, runs and
  players are untouched. Back up before deploying; a dropped table does not come back
  with a git revert.

### Fixed

- **The reconciler re-lit the maze during count-in and throughout ARM.** `_run_count_in_ramp()`
  and `_handle_ready_blink()` called `board.write_coils([False] * 16)` directly, bypassing
  `iobackend/presets.py` and leaving the resolver's `_desired` state lit. `ReconcileLoop` saw the
  drift and re-asserted within 500 ms. Measured: lasers re-lit 354 ms into a 420 ms dark gap, about
  2.6 times per count-in, and — because the ARM handler emits no `ApplyPreset` — the full maze
  stayed lit for the entire ARM state, up to `arm_timeout_ms` (3 minutes), while the player stood
  on the start plate. Latent since the initial commit; activated by `8a8ea83` turning the
  reconciler on. Both sites now use a new `PresetResolver.apply_all_off()`, which updates
  `_desired` and does not depend on the user-editable `blackout` preset.

- **The attract show ran straight through REGISTERED, ARM and the count-in ramp.** Nothing
  cancelled `_show_task` until `ApplyPreset("maze_1")` at `RampComplete`, so the show kept writing
  coils every 300–500 ms while the ready blink and flash ramp were trying to drive them. It also
  partially masked the bug above. New `StopShow` side effect, emitted on ATTRACT → REGISTERED.

- **A subsystem crash left the game dead but the process alive.** `GameRunner.run()` used
  `asyncio.gather()` with no `return_exceptions`, so the first failure re-raised and skipped the
  sibling-cancellation loop, orphaning the other tasks. The clock kept broadcasting and both
  displays looked healthy while the start plate did nothing, with no log line until shutdown —
  and because the process never exited, `Restart=always` never fired. Now uses
  `asyncio.wait(FIRST_EXCEPTION)`, logs the dead task at CRITICAL, cancels the rest and re-raises;
  `__main__` waits on the service tasks alongside the shutdown signal and exits non-zero.

- **The GM console's VOID button was silently inert.** `GmVoid` emitted `SaveRun(outcome="voided")`,
  which ran a plain `INSERT` against a primary key that already existed. The resulting
  `IntegrityError` was logged to syslog and swallowed, the HTTP response was still 200, and the
  broadcast state flipped to "voided" while SQLite still said "clean" — so the run kept its
  leaderboard slot and the GM's reason was discarded. New `VoidRun` side effect routes to the
  existing, idempotent `db.void_run()`, which preserves `pre_void_outcome` for unvoid. `GmVoid` is
  now restricted to post-run states, closing the reverse failure where voiding mid-run wrote the
  row first and made the real end-of-run save collide. `insert_run()` is an upsert as defence in
  depth, and will not revert a void.

- **Saving config mid-run blacked out the maze.** The reload guard in `web/routes_admin.py`
  compared against `"RUN"` and `"HALTED"`, neither of which is a state, so it failed open through
  ARM and all three `RUN_SEG_*` states. `reload_config()` then built a fresh `PresetResolver` whose
  desired state is all-off and the reconciler drove every laser dark within 500 ms, while the admin
  UI reported success. Now uses `core.events.RUN_STATES`, which already existed.

- **A relay board that failed every transaction reported healthy.** `connect()` cleared
  `_consecutive_failures`, so a board that drops the connection each request reconnected
  constantly and never reached `DEGRADED`. Measured: 40 of 40 writes failed with `status="OK"` and
  an empty fault list. Since the game path ignores `apply_preset()`'s return value, the maze
  silently stopped changing with nothing on the admin page. Only a completed transaction clears the
  counter now, `error_count` is rendered in the admin board table (the API already returned it),
  non-timeout `OSError` closes the socket deliberately rather than relying on an `AttributeError`
  escaping a narrow `except`, and sockets set `SO_KEEPALIVE`.

- **Both displays kept a stopwatch running after the WebSocket dropped.** `swRunning` was never
  cleared on close, so the ticker extrapolated off `performance.now()` indefinitely — the outdoor
  display showed the crowd a run that never ended, past `max_run_ms`, with nobody on stage. The
  only indicator was a badge measured at 1.24:1, and 1.05:1 over a live camera frame. Both now
  freeze the clock on close and show `NO SIGNAL — CLOCK STOPPED` at 7.9:1.

- **The FAULT screen was blank.** `#waitingText` was 1.23:1 on the in-container display, so a
  faulted machine showed the player nothing at all. FAULT now has its own message and treatment.

- **GM endpoints accepted cross-origin POSTs.** The app had no middleware, so any page in a browser
  that could route to the NUC — including the GM's own Wi-Fi iPad — could bust or force-reset a run
  via a simple form POST. An Origin guard now rejects foreign origins on state-changing methods;
  a missing Origin still passes, so `curl` and `tools/` are unaffected.

- **`python -m scanmania` without a fake flag crashed after migrating the database.**
  `vision.camera` had no `VisionService`, so the documented "production" invocation died mid-boot
  and never bound port 8000. `vision/service.py` now provides that class — see **Added**. The
  invocation works; the remaining prerequisite is calibrating `config/beams.json`, which is still
  uncalibrated (45 entries, every ROI `0,0`, all on `cam_a`).

- **`DotDetector.arm(run_id, grace_ms)` ignored its own argument.** The value was
  logged and then dropped; `_can_emit_break` read `detection.arm_grace_ms` from
  config instead, so passing a grace had no effect at all. The caller's value now
  wins, falling back to config when omitted.

- **`process_frame` sampled every beam against every camera's frame.** ROIs are
  pixels in one camera's view, so with several cameras each beam was measured
  three times against coordinates that mean nothing. It now takes `camera_id` and
  skips beams belonging to other cameras. Latent until now because all 45 channels
  are assigned to `cam_a`.

- **`test_pause_stops_drain` could not fail.** It re-implemented the pause check inside the test
  body, so `_drain()` was never called and the final assertion compared a number to itself. The
  pause guard moved into `_drain()` where the work happens, and the test asserts the row's
  `attempts` counter — queue depth alone is not a discriminator, because the push fails either way
  without an endpoint. Verified by deleting the guard and watching the test go red.

### Added

- **A maze shape change no longer reads as a mass beam break.** `vision/detect.py`
  never referenced `camera` or `segment` — it evaluated every beam on every frame.
  At `Cp1Pressed` the relays switch, a whole shape's dots vanish at once, and the
  first `BreakConfirmed` busted the player. `global_break_rate_limit` did not help:
  it suppresses the 7th break onward, and the 1st already ended the run.

  Detection now follows a **watch-list** that moves with the maze. `load_all()`
  joins `mazes.yaml` (which channels a preset lights) with `beams.json` (which
  channel each entry is) into `AppConfig.watchlists`, and `_handle_apply_preset`
  swaps it on every preset change. A dot that goes dark because its relay opened
  is not in the list, so nothing looks at it. No new config to author — both
  halves already existed.

  Channels lit in **both** the old and new shape keep their hysteresis state, so a
  real break during the switch is still caught. At the stated ~80% shape overlap
  that is roughly 77% of the maze watched without interruption. Only newly-lit
  channels are paused, for `preset_settle_ms` (default 250), because those lasers
  are physically still coming on.

- **`beams.json` can describe all five dots of a relay channel.** One channel drives
  5 colinear lasers; the schema had a single `roi` and could only record one of
  them, so calibration would have covered 45 dots and ignored 180. New `dots: [...]`
  array with per-dot baseline and mask. The old single-`roi` form still loads as a
  one-dot list, so the file can be migrated channel by channel.

- **A dead relay channel is a fault, not a bust.** If every dot on a channel that is
  commanded ON goes dark at once, that is the relay failing, the PSU dropping or the
  view being occluded — a body blocks one or two dots in a colinear array, never all
  five. Reported via a new `on_fault` callback and the `vision.channel_dark` metric.
  Averaging would have missed the opposite case too: one dark dot in five averages
  to 0.8 and never crosses `break_ratio`, so a single blocked laser went undetected.

- **`vision/service.py` — the object that was missing.** `CameraStream`,
  `DotDetector`, `BaselineManager`, `EvidenceCapture` and `MjpegServer` all existed
  and were individually plausible, but nothing constructed or connected them.
  `__main__.py` imported a `VisionService` that was never written, and
  `core/runner.py`'s docstring called it `CameraManager` — the two call sites did
  not even agree on the name. `VisionService` mirrors `vision/fake.py`'s interface
  exactly, so the runner cannot tell which one it holds.
- **Invariant 5 is now reachable.** It had two independent breaks. `VisionStalled`
  was emitted only by `test_fsm.py`, and `runner._vision_listener` understood
  `"break"` tuples only, so a stall would have been discarded even if something
  had emitted one. Stall detection also moved out of the frame-read loop into a
  watchdog task: `CameraStream` only ever checks the gap when a frame *arrives*,
  so the two genuine no-frame cases — a blocking read that never returns, and a
  failed read that goes down the reconnect path — both bypassed it, and the
  reconnect path cleared the flag on the way through.
- `PresetResolver.apply_all_off()` — config-independent all-off that keeps `_desired` in sync.
- `StopShow` and `VoidRun` side effects.
- `deploy/scanmania.service` — the unit existed only on the NUC, so `Restart=always` was documented
  in prose that nothing enforced. `deploy/README.md` records that `scanmania-kiosk.service` and
  `tools/kiosk.sh` are still NUC-only.
- Rolling DB snapshots now actually run. `rolling_snapshot_loop()` had no callers, so an unclean
  shutdown lost every run back to the last manual snapshot. New `snapshot_interval_min` and
  `snapshot_keep` keys in `config/game.yaml`.
- `docs/testing.md` — what the suite does and does not cover, and how to write a test that can fail.
- Regression tests for the reconciler/all-off interaction, the attract-show stop, and the void path.

### Changed

- **The GM action row no longer moves under the operator's thumb.** Seven buttons were
  `flex: 1 1 0` and `applyState` hid the inapplicable ones with `display: none`, so the
  survivors resized *and relocated* on every transition. FORCE RESET — the most
  destructive control in the app — took a third to half the bar in every state at the
  same 72px height as BUST. Worse, COUNTDOWN → RUN happens automatically with no tap,
  so the leftmost third silently changed meaning from ABORT to BUST while the GM was
  reaching for it.

  Now a 6-column grid. Every button owns its column for the whole session and dims
  when it does not apply, so ABORT (col 4) and BUST (col 5) can never swap. FORCE RESET
  moved out to a small fixed corner control, still tap-to-confirm and still clearing the
  44px touch minimum.

  Banners moved *below* the action row. Above it, every appearing or disappearing banner
  shoved the buttons vertically — the manual-mode banner moved them ~39px at the exact
  moment the GM needs BUST.

  Disabled opacity went 0.2 → 0.28: at 0.2 the dimmed set was barely readable, and the
  point of keeping them on screen is that the GM can see what is not available yet.

- Display type scale: every `clamp()` on both HDMI screens saturated below 1500 px while the panels
  are 2560 px, so both rendered at roughly half their intended size. Caps raised — the in-container
  stopwatch from 260 px to 520 px, the countdown digit from 120 px to 560 px inside its 720 px ring.
- Text colours on `/gm` and both displays now use the palette `admin/index.html` already
  established and documented (`#5b8fd6` at 6.15:1, `#6c8cae` at 5.82:1), replacing `#264f9a` at
  2.59:1. Chrome — ring strokes, glows, gradients — is unchanged.
- `core/fsm.py` returns the state-transition metric as an `EmitMetric` effect instead of calling
  `metrics.emit()` inline. That call was harmless only because no sink is configured; wiring one up
  would have made the pure FSM perform I/O on every transition. **Move any sink wiring in after
  this change, not before.**
- `web/static/shared/test.py` moved to `tools/click_relays.py`. It was served unauthenticated from
  the web root, disclosing board IPs, port 4196 and a working relay driver.
- CLAUDE.md corrected: `io/` is `iobackend/`, `tools/replay.py` does not exist, `docs/metrics.md`
  does not exist, the FSM signature takes a context argument, `pick_rois.py` prints rather than
  writes, invariant 7's record-keeping half is documented as unimplemented, and the test-coverage
  claim now matches reality.

- **Editing the show that is currently playing did nothing until a restart.** `_run_show()` holds a
  reference to the step list it was handed, so `GameRunner.reload_config()` swapping `self.config`
  left the old sequence looping. Saving `attract` from the admin panel reported a successful reload
  while the maze kept running the pre-edit steps. The runner now tracks the playing show's name and
  re-arms it against the reloaded config. `reload_config()` is `async` as a result — its only
  caller, `_reload_config()` in `web/routes_admin.py`, now awaits it.

- **The invariant-4 safety backstop was never running.** `ReconcileLoop` was fully implemented
  but never instantiated, so nothing detected or corrected relay coils that drifted from the
  desired state. `GameRunner.run()` now starts it as the `reconcile` task. Its constructor takes a
  callable returning the resolver instead of the resolver itself, so a config reload doesn't strand
  it on the pre-edit desired state. Per-board `mismatch_count` added to `GET /api/admin/hardware`.
  Covered by `tests/test_reconcile.py`.

### Added

- `tools/deploy.sh` — one-command update for the NUC. Refuses to run with uncommitted work outside
  `config/`, commits and pushes live config drift (the admin panel writes into the checkout), backs
  up the database before the automatic schema migration, pulls, reinstalls dependencies only if
  `requirements.txt` changed, restarts both services and health-checks the API.

### Changed

- Show step `hold_ms` ceiling raised from 10 s to 60 s in `ShowStepBody` and in the two admin show
  editor inputs. The old limit rejected legitimate slow attract sequences with a 422; the loader and
  show player never had a cap.

### Fixed — count-in and GM health LEDs

- **The count-in raced 8→7→6 instead of counting seconds.** `display_in` rendered the *pulse
  index* (`countdown_total - countdown_step`), and the pulse ramp is deliberately accelerating —
  eight pulses in 2.13 s. The runner now broadcasts `countdown_remaining_ms` (a real deadline,
  computed server-side per invariant 2) and the display shows `ceil(remaining / 1000)`. The
  ramp in `config/game.yaml` was retimed to exactly 3000 ms so the digits land on 3, 2, 1 at
  one-second intervals; the accelerating relay clatter is preserved, just with a slower opening.
  The GO deadline is anchored in `_handle_start_count_in` rather than inside the ramp task,
  because `BroadcastState` runs first and would otherwise emit one frame with no deadline set.

- **The GM console's SM-NODE-DMX LED was amber forever.** It was set to "unknown" at init and
  never updated — there was no DMX health anywhere in the WebSocket message. `HazerController`
  now tracks whether its Art-Net keepalive is sending without a socket error, exposes it as
  `link_ok`, and the GM console polls `/api/gm/hazer` every 5 s to drive the LED. Art-Net is
  fire-and-forget UDP, so green means "we are transmitting", not "the node acknowledged" —
  noted in the code so nobody reads more into it later.

### Fixed — kiosk displays

Neither HDMI panel had ever shown the right thing. Three independent causes in `kiosk.sh`:

- **Only one browser window ever existed.** Both Chromium invocations shared the default profile
  directory, so the second one handed its URL to the first and exited (`Opening in existing
  browser session` in the journal). `/display/in` was never on screen. Each window now gets its
  own `--user-data-dir`.
- **Nothing was fullscreen.** There is no window manager on the box, so `--kiosk` — which asks
  the WM for fullscreen — did nothing and Chromium sized its own window (1265x1420 on a
  2560x1440 panel). Both windows are now positioned and sized explicitly from what xrandr
  reports, and `--app` hides the chrome that `--kiosk` used to.
- **A Chromium infobar covered the top of both panels.** `--disable-infobars` no longer
  suppresses the "unsupported command-line flag: --no-sandbox" warning; `--test-type` does.

Also in `kiosk.sh`: mode selection prefers the highest resolution that runs at ≥ 50 Hz rather
than taking `--auto`, because the outdoor panel's native 3840x2160 is a 30 Hz mode and a
stopwatch at 30 Hz reads as stuttering. Output-to-page mapping is overridable via
`SCANMANIA_OUT_IN` / `SCANMANIA_OUT_OUT` since which panel is which is cabling, not logic.
Chromium runs under `dbus-run-session` to stop it flooding the journal with dbus errors.

### Admin panel — CSS / UX / accessibility pass

All in `web/static/admin/index.html`; no behaviour changes to any API.

- **Contrast.** `--accent` (#264f9a) is 2.3:1 on the panel surfaces and was used for body text.
  It is now chrome-only (borders, glows); a new `--accent-text` (#5b8fd6, 5.5:1) covers the nine
  text uses and the login overlay. `--muted` went #5a7899 → #6c8cae, clearing AA for the 10–12 px
  labels it is used on.
- **Focus is visible again.** `outline: none` removed from the config editor, the shared
  `input, select` rule and the login field; a `:focus-visible` ring added globally plus explicit
  overrides for controls that set their own outline. Mouse users see no change.
- **Tabs are a real ARIA tablist.** `role="tablist"` / `tab` / `tabpanel` with `aria-controls`,
  `aria-labelledby` and `aria-selected`, a roving `tabindex` so Tab enters the strip once, and
  ArrowLeft / ArrowRight / Home / End to move between tabs.
- **The beam grid and the maze-editor grid are keyboard-operable.** Both were `<div>`s with a
  click handler and no way in from the keyboard. They now carry `role="button"`, `tabindex="0"`,
  `aria-pressed` and an accessible name, activate on Enter and Space, and the maze editor restores
  focus to the toggled strip after its re-render.
- **Labels.** `aria-label` on the 17 inputs, selects and textareas that had only a placeholder or
  nothing at all; the confirm modal is now `role="dialog" aria-modal="true"` wired to its title
  and body.
- **Responsive.** Both maze diagrams changed from a fixed `width` to `max-width`, and breakpoints
  added at 700 px and for coarse pointers. A skip link and `<header>` element round it out, and
  `prefers-reduced-motion: reduce` disables the transitions and the state-pill pulse.

### Admin panel audit — backend and frontend

#### Fixed (correctness / data loss)

- **Logs tab was completely unreachable.** `/api/admin/logs/{unit}` was registered before
  `/api/admin/logs/stream`; Starlette matches in registration order, so the path parameter
  swallowed `stream`. The stream route is now registered first, with a comment pinning the order.

- **Admin edited the wrong config directory in production.** `web/server.py` hardcoded
  `<repo>/config` while `__main__.py` resolves `/etc/scanmania/config`. Every admin config write
  on the NUC targeted files the process never loaded, silently breaking invariant 3. The resolved
  directory is now threaded from `__main__.py` through `ScanManiaApp` into `register_routes`.

- **Unvoid guessed the original outcome.** Voiding overwrote `runs.outcome`, so unvoiding had to
  invent a replacement and could resurrect a busted run onto the leaderboard. Schema v2 adds
  `runs.pre_void_outcome`; unvoid now restores the real value and returns 409 when it is unknown.
  Voiding with an empty reason is rejected with 422.

- **Config writes could brick the next boot.** A saved file is now validated by running the real
  loader over a temp directory containing the full candidate config set before anything is written.
  Writes are serialised under a lock, written atomically with `fsync` on both file and directory,
  and the audit row records the true before/after content.

- **Master mode wrote coils directly**, bypassing `PresetResolver` and leaving `_desired` stale
  (invariant 4). `apply_channels()` and `apply_direct()` in `iobackend/presets.py` now own these
  writes and keep desired state in sync, so the reconciler cannot fight an admin write.

- **The admin API could reset a live player's stopwatch** (invariant 2). All master-mode
  operations are now gated behind `_require_master()` and raise `MasterModeRequired`.

- **Master-mode failures were invisible.** Routes awaited nothing and always returned `ok: true`.
  They now await the runner and translate failures into 404 / 409 / 422 / 502.

#### Fixed (security)

- **Removed the default admin password** that was committed in the repo. Auth now fails closed on
  an unset `SCANMANIA_ADMIN_PASSWORD` (503). To avoid locking out an operator, `__main__.py`
  generates a per-boot password and logs it at WARNING, recoverable via `journalctl`.

- **Gated every unauthenticated admin read** that exposed operational data, hardware topology,
  config audit history (including full file contents), or sync error text. `/api/admin/runs`,
  `/beams` and `/leaderboard` stay open by design — the GM console and outdoor display consume
  them and have no password.

- **Downloads and the log stream are authenticated.** `window.location` and `EventSource` cannot
  send headers, so they now use short-lived, single-use download tokens issued over the
  header-authenticated channel.

- **CSV formula injection** — exported fields beginning with `=`, `+`, `-`, `@`, tab or CR are
  prefixed with a quote.

- **Non-ASCII password header returned 500** instead of failing auth, because Starlette decodes
  headers as latin-1 and `hmac.compare_digest` rejects non-ASCII `str`.

- **Dev triggers are restricted to `--fake-all`** and return 404 otherwise;
  `GET /api/admin/dev/available` lets the UI hide the controls.

- **Journal unit names are validated against an allowlist**, and request bodies are typed and
  bounded via Pydantic models (channel counts, show steps, name patterns, string lengths).

#### Fixed (responsiveness)

- **The event loop no longer blocks.** `subprocess.run` for journalctl became
  `asyncio.create_subprocess_exec` with a timeout, and config file reads/writes moved to
  `asyncio.to_thread`.

- **Run search and pagination moved server-side** (`list_runs(q=...)`, `count_runs`), instead of
  fetching every run and filtering in the browser.

#### Added

- `GET /api/admin/shows`, real uptime and a `faults` list on `/api/admin/status`, per-board Modbus
  error counts and reconcile mismatch counters, and `GameRunner.reload_config()` so config saves
  apply without a restart (deferred, with a note, while a run is in progress).

- `/api/gm/hazer` (unauthenticated, matching the rest of `/api/gm/*`) so the passwordless GM
  console keeps working now that `/api/admin/hazer` requires auth.

#### Changed

- Admin frontend: fixed the maze editor saving the wrong preset, the show editor's play loop,
  403 handling with forced re-login, HTML escaping, delegated run-row listeners, a capped log
  buffer, updates gated to the visible tab, and in-page modals replacing `confirm`/`prompt`.

---

## Phase 2 — Runner wired, hardware configured, 72 tests green

### Added

- **Runner side effects wired** — `core/runner.py` fully wired: FSM side effects executed against
  real or fake backends (io, inputs, vision). Countdown pulse loop driven from `monotonic_ns` with
  absolute scheduling (no accumulated sleep drift). Timeout tasks (`max_run_ms`, `arm_timeout_ms`)
  scheduled and cancelled correctly on state transitions.

- **`python -m scanmania` entry point** — `__main__.py` wires argparse (`--fake-all`, `--fake-io`,
  `--fake-vision`, `--fake-inputs`), constructs backends, and launches all async tasks via a single
  `asyncio.run()`. SIGINT/SIGTERM trigger a graceful shutdown: snapshot written, outbox drained,
  services stopped in reverse order.

- **OutboxWorker env-var driven** — `SCANMANIA_CLOUD_URL` and `SCANMANIA_CLOUD_TOKEN` read from
  environment (sourced from `/etc/scanmania/secrets.env` in production). Worker skips cloud sync
  if `SCANMANIA_CLOUD_URL` is unset, accumulating outbox rows silently until configured. No
  credentials in the repo.

- **Admin status with live runner state** — `GET /api/admin/status` returns current FSM state,
  active player, detection mode, beam health summary, outbox depth, and last sync timestamp.
  Admin dashboard auto-refreshes every 2 s via fetch().

- **23 audit issues fixed** — issues found during Phase 1 review: monotonic clock not propagated
  correctly into WebSocket broadcast, FSM `VOID` transition missing from `RESULT` state, outbox
  worker not woken on insert, assisted-mode veto not crediting elapsed time back, count-in pulse
  timing drifting on slow systems, arm-grace window off by one frame, several missing event
  emissions, and incorrect beam-state reset on `FORCE_RESET`.

- **Runner tests (72 tests green)** — `tests/test_runner.py`: every FSM transition exercised via
  fake backends end-to-end. Includes false starts, out-of-order checkpoint events, break during
  each segment, `max_run_ms` abort, service restart mid-run (→ `ABORTED` → `RESET`), detection
  mode switches mid-run, `VOID` after `FINISHED`, `VOID` after `BUSTED`, and outbox idempotency
  across simulated retries.

### Changed

- **Sign-in redesign — full contact fields** — `players` table and sign-in form updated with:
  `first_name`, `surname`, `email`, `dob` (date of birth), `gender`. The display name shown on
  the GM console and leaderboard is derived as `first_name + surname[0]` (e.g. "Sarah K.").
  All fields validated server-side. Email is stored hashed for GDPR-friendliness; raw email
  retained in `extra_json` under a separate consent flag.

- **GM page inline sign-in** — `/gm` now embeds a compact sign-in panel (collapsible) so the
  gamemaster can register a walk-up player without handing the iPad to a steward. Full `/signin`
  iPad flow remains the primary path for attended queues.

- **Display welcome screens** — `/display/in` and `/display/out` show a welcome/idle screen in
  `ATTRACT` state: animated logo, "STEP UP TO PLAY", and live leaderboard. These replace the
  previous blank attract state on both displays.

### Hardware configuration

- **45 laser strips, 9 rows × 5 columns** — `config/beams.json` authored for the full geometry:
  45 beams, IDs `b01`–`b45`, arranged in a 9-row × 5-column grid. Each beam assigned to a relay
  channel and a camera ROI.

- **3 relay boards: SM-NODE-1, SM-NODE-2, SM-NODE-3** — `config/hardware.yaml` defines:
  - `SM-NODE-1`: 16 channels (beams b01–b16), static IP `10.0.0.11`
  - `SM-NODE-2`: 16 channels (beams b17–b32), static IP `10.0.0.12`
  - `SM-NODE-3`: 13 channels (beams b33–b45), static IP `10.0.0.13`

- **Modbus RTU over TCP, port 4196** — Waveshare boards configured in transparent/passthrough
  mode. The wire protocol is Modbus RTU (not standard Modbus TCP with MBAP header). `io/modbus.py`
  sends raw RTU frames over a TCP socket to port 4196. See ADR 0001 for the full explanation.

- **Admin grid visualisation with masking** — `/admin` Beams tab renders a 9×5 grid of beam pills
  showing live ratio, state (intact/broken/masked/offline), and threshold bars. Clicking a cell
  toggles the `masked` flag. The grid matches the physical layout so on-site diagnosis is
  immediate.

- **Real Modbus backend wired** — `io/modbus.py` production path active: RTU-over-TCP raw socket
  per board, 200 ms connect timeout, 3 consecutive failures → `DEGRADED` metric + GM console
  amber. Reconciliation loop (500 ms) reads actual coil state via function 0x01 and re-asserts
  if it differs from desired state.

- **RTSP camera URL configured** — `config/hardware.yaml` camera entry updated with the real
  substream URL (`rtsp://10.0.0.30:554/substream`). Resolution locked to 1280×720 @ 25 fps.
  Exposure, gain, white balance, and focus locked via ONVIF commands on first connect.

---

## Phase 1 — Skeleton and docs, zero hardware required

### Added

- `CLAUDE.md` — repo orientation, invariants, conventions, definition of done
- `plan.md` — full system plan (§1–§16), locked decisions, build phases
- `docs/glossary.md` — beam, dot, cluster, channel, preset, segment, run, bust, void, mask,
  gamemaster, master mode
- `docs/architecture.md` — system topology, service map, data flow diagram
- `docs/adr/0001-modbus-tcp-not-artnet.md` — why Modbus TCP (RTU-over-TCP) replaced Art-Net
- `docs/adr/0002-ceiling-dot-detection.md` — why ceiling dots, not beam-line sampling
- `docs/adr/0003-hard-cutoff-scoring.md` — a break ends the run immediately
- `docs/adr/0004-pico-over-usb-not-raspberry-pi.md` — Pico for physical inputs
- `docs/adr/0005-systemd-not-docker.md` — native Python + systemd units
- `docs/adr/0006-local-first-cloud-backup.md` — SQLite source of truth, outbox sync
- `docs/adr/0007-detection-modes-and-master-mode.md` — AUTO/ASSISTED/MANUAL + master mode

- `config/hardware.yaml` — relay board IPs, camera URLs, channel map
- `config/mazes.yaml` — preset channel lists, show definitions
- `config/beams.json` — dot ROIs, thresholds, masks (source of truth)
- `config/game.yaml` — timings, detection mode, count-in pulse list
- `config/metrics.yaml` — metric sink configuration
- `config/loader.py` — typed dataclass loader with cross-config validation

- `core/events.py` — full event and side-effect type definitions; the wire contract
- `core/fsm.py` — pure state machine (no I/O); all transitions, all detection modes
- `core/stopwatch.py` — monotonic elapsed timer using `time.monotonic_ns()`
- `core/scoring.py` — rank computation, leaderboard queries
- `core/metrics.py` — `emit()` façade, pluggable sinks, naming convention
- `core/runner.py` — skeleton wiring FSM to I/O backends (Phase 1 stub)

- `io/modbus.py` — async Modbus TCP master, one connection per board, 200 ms timeout
- `io/presets.py` — preset → per-board coil arrays, `apply_preset()`, master-mode toggle
- `io/reconcile.py` — desired-vs-actual reconciliation loop at 500 ms
- `io/fake.py` — in-memory relay board simulator (MANDATORY for laptop dev)

- `inputs/pico_link.py` — serial protocol handler, clock offset estimation, auto-reconnect
- `inputs/fake.py` — no-op Pico link fake (MANDATORY)
- `inputs/firmware/main.py` — MicroPython firmware for the Raspberry Pi Pico

- `vision/camera.py` — RTSP decode, stall detection (300 ms gap), drift check
- `vision/detect.py` — dot ROI sampling, hysteresis, cooldowns, global rate limit
- `vision/baseline.py` — rolling EMA in ATTRACT, flash-capture at count-in pulse 0
- `vision/evidence.py` — JPEG crop thumbnails saved per beam hit
- `vision/mjpeg.py` — downscaled MJPEG re-stream at http://localhost:8081/cam_a.mjpg
- `vision/fake.py` — replay from recorded video / no-op fake (MANDATORY)

- `persist/db.py` — SQLite schema + migrations, WAL mode, all query helpers
- `persist/outbox.py` — continuously running drain worker, exponential backoff, never drops rows
- `persist/sync.py` — cloud POST with Idempotency-Key, backoff, snapshot/export utilities

- `web/server.py` — FastAPI app, WebSocket hub broadcasting at 10 Hz, static file serving
- `web/routes_signin.py` — POST /api/signin → PlayerRegistered event
- `web/routes_gm.py` — POST /api/gm/* → all gamemaster actions
- `web/routes_admin.py` — GET/POST /api/admin/* → all admin portal endpoints

- `web/static/signin/index.html` — player sign-in: contact fields (first name, surname, email,
  DOB, gender), LET'S GO button, success overlay, wait screen for game-in-progress states,
  WebSocket state mirroring
- `web/static/gm/index.html` — gamemaster console: top bar (state/stopwatch/player/detection
  badge), primary action buttons (COUNT IN/BUST/ABORT/VOID/FORCE RESET), detection mode
  segmented control, live beam strip (tap to mask), health strip, assisted-mode confirm/veto
  overlay, inline sign-in panel, recent runs list; 60 fps stopwatch interpolation
- `web/static/admin/index.html` — admin portal: Dashboard, Hardware, Beams (9×5 grid),
  Presets, Config, Runs, Leaderboard, Cloud Sync, Logs, Master Mode tabs; dark/light theme toggle
- `web/static/display_in/index.html` — in-container display (HDMI 1): accelerating countdown
  ring, enormous MM:SS.mmm stopwatch through haze, BUSTED/CLEAN! full-screen states, ATTRACT
  welcome screen; 60 fps stopwatch interpolation
- `web/static/display_out/index.html` — outdoor display (HDMI 2): MJPEG background (graceful
  fallback), stopwatch overlay, leaderboard when idle, BUSTED/FINISHED overlays; 60 fps
  stopwatch interpolation

- `tools/fake_run.py` — CLI script driving clean/busted/aborted/voided runs via fake backends;
  prints final run record as JSON
- `tools/ramp.py` — generate count-in pulse list from (total_ms, pulses, ratio, duty); outputs
  YAML block + ASCII timeline + warnings for short pulses
- `tools/pick_rois.py` — OpenCV GUI: click ceiling dots → JSON stanzas for beams.json;
  auto-sequences IDs, right-click undo, Q to finish

- `requirements.txt` — all dependencies with minimum version pins
- `__main__.py` — entry point: argparse, --fake-all/io/vision/inputs, asyncio task
  orchestration, SIGINT/SIGTERM graceful shutdown, snapshot on exit
- `tests/conftest.py` — pytest fixtures: config, fake_config, db (in-memory SQLite),
  fake_io, fake_inputs, fake_vision, fsm_context

### Phase 1 gate

- `pytest tests/ -v` green
- `python tools/fake_run.py --scenario clean` prints a valid run record
- `python tools/fake_run.py --scenario busted` prints outcome=busted
- `python tools/fake_run.py --scenario aborted` prints outcome=aborted
- `python tools/fake_run.py --scenario voided` prints outcome=voided
- `python -m scanmania --fake-all` starts without error; frontends live at http://localhost:8000
- A fresh reader can run it from `CLAUDE.md` alone (no prior context)
