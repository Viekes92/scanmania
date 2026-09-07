#!/usr/bin/env bash
#
# tools/deploy.sh — update a running ScanMania box from git.
#
# Inputs:  none (operates on the checkout it lives in)
# Outputs: updated checkout, restarted services, health summary on stdout
# Invariant: never destroys local work. config/ is written by the admin panel, so
#            live tuning is committed and pushed rather than discarded; any other
#            uncommitted file aborts the deploy for a human to look at.
#
# Usage:  ssh root@172.16.0.10 '/opt/scanmania/tools/deploy.sh'

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="$REPO/.venv"
DB="/var/lib/scanmania/scanmania.db"
SERVICES=(scanmania.service scanmania-kiosk.service)

cd "$REPO"

say() { printf '\n\033[1m== %s\033[0m\n' "$*"; }
die() { printf '\n\033[31mABORT: %s\033[0m\n' "$*" >&2; exit 1; }

# --- 1. Local work -----------------------------------------------------------
# The admin panel writes into config/, so that directory legitimately drifts on
# the box. Preserve it by committing; anything else means someone edited code
# here and a pull would clobber it.
say "Checking for local changes"
dirty_other="$(git status --porcelain -- . ':(exclude)config' | head -20)"
[ -n "$dirty_other" ] && {
    printf '%s\n' "$dirty_other"
    die "uncommitted changes outside config/ — commit or stash them first"
}

if ! git diff --quiet -- config || [ -n "$(git ls-files --others --exclude-standard config)" ]; then
    echo "Live config changed on this box — committing before pull:"
    git --no-pager diff --stat -- config
    git add config
    git commit -q -m "config: live tuning from $(hostname)"
    git push -q origin HEAD:main || die "could not push local config; resolve by hand"
    echo "Pushed."
else
    echo "Clean."
fi

# --- 2. Backup ---------------------------------------------------------------
# Migrations run automatically at startup and are not reversible.
say "Backing up the database"
if [ -f "$DB" ]; then
    backup="$DB.bak-$(date +%Y%m%dT%H%M%S)"
    cp -a "$DB" "$backup"
    echo "$backup"
else
    echo "No database at $DB yet — skipping."
fi

# --- 3. Pull -----------------------------------------------------------------
say "Pulling"
before="$(git rev-parse HEAD)"
git pull --ff-only origin main
after="$(git rev-parse HEAD)"

if [ "$before" = "$after" ]; then
    echo "Already up to date."
else
    git --no-pager log --oneline "$before..$after"
fi

# --- 4. Dependencies ---------------------------------------------------------
if [ "$before" != "$after" ] && ! git diff --quiet "$before" "$after" -- requirements.txt; then
    say "requirements.txt changed — installing"
    "$VENV/bin/pip" install -q -r requirements.txt
else
    echo "requirements.txt unchanged."
fi

# --- 5. Restart --------------------------------------------------------------
# The kiosk restarts too: the browser caches the single-file frontends.
say "Restarting services"
systemctl restart "${SERVICES[@]}"
sleep 6

# --- 6. Health ---------------------------------------------------------------
say "Health"
# /api/admin/status is password-gated; the service's own env file has the password.
# shellcheck disable=SC1091
[ -r /etc/default/scanmania ] && . /etc/default/scanmania && export SCANMANIA_ADMIN_PASSWORD
failed=0
for unit in "${SERVICES[@]}"; do
    state="$(systemctl is-active "$unit" || true)"
    printf '  %-28s %s\n' "$unit" "$state"
    [ "$state" = "active" ] || failed=1
done

"$VENV/bin/python" - <<'PY' || failed=1
import json, os, sys, urllib.request

pw = os.environ.get("SCANMANIA_ADMIN_PASSWORD", "")
try:
    import hashlib
    hdr = {"X-Admin-Password": hashlib.sha256(pw.encode()).hexdigest()} if pw else {}
    req = urllib.request.Request("http://127.0.0.1:8000/api/admin/status", headers=hdr)
    d = json.load(urllib.request.urlopen(req, timeout=10))
except Exception as exc:
    print(f"  api                          UNREACHABLE ({exc})")
    sys.exit(1)

print(f"  {'api':<28} ok — state={d.get('state')}")
for f in d.get("faults") or []:
    print(f"    FAULT {f.get('subsystem')}: {f.get('message')}")
PY

if [ "$failed" -ne 0 ]; then
    printf '\n\033[31mDeploy finished with problems — check: journalctl -u scanmania -n 50\033[0m\n'
    exit 1
fi
printf '\n\033[32mDeploy OK.\033[0m\n'
