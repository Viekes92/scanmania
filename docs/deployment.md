# Deployment

The box is a NUC at `172.16.0.10`, repo at `/opt/scanmania`, one systemd unit
for the game and one for the kiosk displays. Two ADRs referenced this document
before it existed; this is it.

## Before any deploy

**Check the NUC's git status first.** `config/mazes.yaml` and `config/beams.json`
are routinely modified live on the box — show tuning and calibration write to
them — and they are tracked in git. `tools/deploy.sh` commits and pushes that
drift before pulling, and refuses to continue if the box has diverged.

```bash
ssh root@172.16.0.10 'cd /opt/scanmania && git status --short && git log --oneline -3'
```

**Back up the database.** `deploy.sh` does this with `sqlite3 .backup` (never
`cp` — the DB is in WAL mode and the service is still running), keeps the last
10, and rolls the checkout back if anything after the pull fails.

## Deploying

```bash
ssh root@172.16.0.10 'cd /opt/scanmania && ./tools/deploy.sh'
```

The script: fetches and verifies a fast-forward is possible → commits and pushes
live config drift → backs up the DB → pulls → installs dependencies (rolling
back on failure) → restarts both units → probes the HTTP API.

## Bare Debian 12 to a playable box

```bash
# 1. System packages
apt-get update
apt-get install -y python3 python3-venv git sqlite3 chromium xserver-xorg xinit x11-xserver-utils xdotool

# 2. Repo and venv
git clone <origin> /opt/scanmania
cd /opt/scanmania
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt     # pinned; see the file header

# 3. State directories
mkdir -p /var/lib/scanmania /var/backups/scanmania
chmod 700 /var/backups/scanmania               # snapshots contain player PII

# 4. Environment — the admin password lives here, 0600
cat > /etc/default/scanmania <<'ENVEOF'
SCANMANIA_ADMIN_PASSWORD=<the password>
# Operating day rolls over at this local hour. The leaderboard and the
# end-of-day export both use it, so a session past midnight stays on one day.
SCANMANIA_DAY_START_HOUR=9
# The outdoor panel is mounted PORTRAIT — /display/out is designed 9:16 so the
# screen photographs well on a phone. "left" or "right" depending on which way
# it was physically turned; get it wrong and the picture is upside down.
SCANMANIA_ROTATE_OUT=left
# Which page lands on which panel. This is cabling, not logic — swap these two
# rather than moving plugs. An output named here that is not connected drops
# only its own page; the other panel keeps the page it was assigned.
SCANMANIA_OUT_IN=HDMI-2
SCANMANIA_OUT_OUT=HDMI-1
ENVEOF
chmod 600 /etc/default/scanmania

# 5. Units and journald limits
cp deploy/scanmania.service deploy/scanmania-kiosk.service /etc/systemd/system/
cp -r deploy/journald.conf.d /etc/systemd/
systemctl daemon-reload
systemctl restart systemd-journald
systemctl enable --now scanmania scanmania-kiosk

# 6. Verify
systemctl is-active scanmania scanmania-kiosk
curl -s localhost:8000/api/admin/status | head -c 200
```

Then calibrate — see `plan.md` §19 and ADR 0009. Nothing detects anything until
`config/beams.json` has a `mazes` block.

## Audio

The soundtrack lives in `/opt/scanmania/sounds/`, mapped to FSM states by
`audio:` in `config/game.yaml`. Swap a sound by overwriting the file and keeping
the name, or by pointing the config at a new name. See `sounds/README.md`.

**Audio is gitignored, so `deploy.sh` does not carry it.** Push it separately:

```bash
./tools/push_sounds.sh          # from the laptop; rsync if the box has it
ssh root@172.16.0.10 'systemctl restart scanmania'
```

That is a feature, not a gap: git leaves ignored files alone, so the audio on
the box survives every deploy untouched. It also means **the box is the only
copy** — keep the masters somewhere else as well.

Cues (`.wav`) are decoded into RAM at startup; bed tracks (`.mp3`/`.ogg`) are
opened once at startup to prove they decode. Both failures show in the boot log
and in the admin panel rather than as silence mid-show.

Audio is decoration and never fatal: no sound card, no `pygame`, a missing file
-- each is silence and a log line, and the box still runs a full day of games.
Check what actually loaded at **Admin -> Hardware -> Audio**, which lists the
loaded cues, the missing ones, and gives each file a Play button so you can
prove a sound reaches the speakers without waiting for a run to reach that
state.

To pin the output (so a plugged-in HDMI cannot steal the sound mid-tour), list
the devices and put the name in `audio.device`:

```bash
aplay -L | grep -v '^ '        # device names ALSA knows
```

`pygame` is pinned in `requirements.txt` and its wheels bundle SDL2, so no
extra apt packages are normally needed. If pip has to build it from source,
install `libsdl2-dev libsdl2-mixer-dev` first -- or set `audio.enabled: false`
and run the box silent until it can be sorted.

## Shutting down for the day

**Admin portal -> Dashboard -> End of day -> Shut down.** This is how to close
the box, not `systemctl stop`. From a shell on the NUC:

```bash
python3 tools/shutdown.py                # go dark, then halt the NUC
python3 tools/shutdown.py --no-poweroff  # go dark, leave the NUC running
```

It saves first and darkens second, in that order, because a shutdown must never
be the thing that loses a day's runs:

1. settles a run still in progress, recorded as `aborted` -- truthful, never
   resumable (invariant 7)
2. drains the fire-and-forget event rows still in flight (invariant 6 means
   there usually are some)
3. snapshots the database
4. lasers off
5. haze and the maze lights off
6. **the entrance light last**, so the operator can still see their way out
7. then, and only then: stop the kiosk, stop the game, halt the NUC

Step 7 is scheduled in a detached transient unit (`systemd-run --no-block`),
because stopping the game kills whatever process issued the command. **Kiosk
first** -- `scanmania-kiosk` has `Wants=scanmania.service`, so stopping the game
on its own gets it dragged back up within five seconds.

Stopping the service is also what closes the database: `__main__` blacks out,
snapshots and closes on its way down. The shutdown sequence deliberately does
**not** close it itself -- see "Dark but still running" below.

Wait for the NUC's power light to go out, then pull the breaker. Cutting mains
under a running filesystem is how the box comes back with a corrupt database,
and the DB snapshot does not protect the filesystem it was written to.

Relay coils latch and an Art-Net node holds its last frame, so the container
stays dark once power is cut. That cuts both ways: **a shutdown that reports a
failed step has left something energised.** Nothing is halted in that case --
the box deliberately stays up so you can see what failed. Walk the container
before cutting power.

The entrance light is `always_on` and the DMX layer refuses to dim it on every
other path, including when the process dies. This is the one sanctioned
exception, and it exists because the operator is standing at the breaker.

### Dark but still running

Unticking the halt (or `--no-poweroff`) darkens the container and leaves the
box up. That state has to stay usable, so the sequence does not close the
database and does not latch the lights off: **FORCE RESET on the GM console
brings it back**. Haze stays off until the GM turns it back on.

The same applies if a halt is requested and cannot be scheduled -- the box
releases the lights rather than sitting dark, running, and recoverable only
over ssh.

### Powering back on

Just the NUC. `scanmania` and `scanmania-kiosk` are enabled, so mains-on is the
whole procedure: the box boots into MASTER with the house lights up, and the GM
walks the container and presses FORCE RESET to reach game mode.


## Stopping the game

**Kiosk first, or it drags the game back up within five seconds** via its
`Wants=scanmania.service`:

```bash
systemctl stop scanmania-kiosk scanmania
```

Verify with `systemctl is-active scanmania-kiosk scanmania` (both `inactive`)
and `ss -ltnp | grep :8000` showing the port free.

Stopping the service drives every coil off and zeroes the hazer before exiting.
Relay coils latch and the boards are separately powered, so this matters: a
process that simply dies would otherwise leave the maze lit in an empty
container.

## Recovering a corrupt database

The game logs `DATABASE INTEGRITY CHECK FAILED` at startup and keeps running.

```bash
systemctl stop scanmania-kiosk scanmania
mv /var/lib/scanmania/scanmania.db /var/lib/scanmania/scanmania.db.corrupt
cp /var/backups/scanmania/$(ls -t /var/backups/scanmania | head -1) \
   /var/lib/scanmania/scanmania.db
systemctl start scanmania scanmania-kiosk
```

Snapshots are hourly, keep 48. Losing one is losing at most an hour of runs;
the show going dark for the night is worse, so prefer starting on a clean DB
over debugging in front of a queue.

## Rolling back

The schema refuses to start when the DB is newer than the binary, so a rollback
past a migration needs its matching snapshot restored too.

```bash
cd /opt/scanmania
git log --oneline -10
git reset --hard <sha>
.venv/bin/pip install -r requirements.txt
systemctl restart scanmania scanmania-kiosk
```

## The screens show the wrong thing

```bash
# Which page is on which panel, and what X thinks is connected:
journalctl -u scanmania-kiosk -n 40 --no-pager -o cat | grep -E 'Connected|Launching|-> http'
DISPLAY=:0 xrandr --query | grep ' connected'
```

Swap the panels by editing `SCANMANIA_OUT_IN` / `SCANMANIA_OUT_OUT` in
`/etc/default/scanmania` and `systemctl restart scanmania-kiosk`. No code change
and no re-cabling.

The markup and `brand.css` are served `no-store`, so a restarted kiosk always
picks up a redeployed frontend. Fonts and artwork are still cached on purpose.
If a screen looks stale anyway, the profile cache is disposable:

```bash
systemctl stop scanmania-kiosk
rm -rf /var/tmp/scanmania-kiosk-*
systemctl start scanmania-kiosk
```

There is no window manager, so nothing looks at a window's requested size —
geometry comes from `xrandr` and explicit pixels. A rotated output swaps width
and height; get that wrong and the page hangs off the edge of the panel.

## What to check when something is wrong

```bash
journalctl -u scanmania -n 200 --no-pager          # one unit, not six
journalctl -u scanmania -g 'METRIC|FAULT|ERROR'    # metrics are log lines
curl -s localhost:8000/api/admin/status            # faults list
python3 tools/camshow.py --probe                   # which camera answers where
```

The admin dashboard's fault list reports relay boards, the Opta, cameras not
delivering frames, uncalibrated mazes, dots with no baseline, and failed DB
writes. An empty list means those are all healthy.
