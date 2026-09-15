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
