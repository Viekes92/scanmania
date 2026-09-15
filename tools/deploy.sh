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

# Re-exec from a private copy before touching git.
#
# Step 4 pulls, which rewrites THIS file while bash is still reading it. Bash
# reads a script lazily and seeks by byte offset, so a pull that changes the
# file's length leaves the interpreter mid-line in the new bytes: it silently
# runs the wrong half of the script. That is how a deploy ran the OLD health
# check after pulling the new one — the update appeared not to take effect,
# with nothing in the output to say why.
if [ -z "${SCANMANIA_DEPLOY_SELF:-}" ]; then
    _self="$(mktemp /tmp/scanmania-deploy.XXXXXX)"
    cat "${BASH_SOURCE[0]}" > "$_self"
    # The copy lives in /tmp, so the checkout can no longer be derived from
    # $0 — hand the original path down.
    SCANMANIA_DEPLOY_ORIG="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)" \
    SCANMANIA_DEPLOY_SELF="$_self" exec bash "$_self" "$@"
fi
trap 'rm -f "$SCANMANIA_DEPLOY_SELF"' EXIT

REPO="$(cd "${SCANMANIA_DEPLOY_ORIG}/.." && pwd)"
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
# Tracked modifications only. --porcelain also lists UNTRACKED files, so any
# stray file outside config/ blocked every deploy — including tools/laser.py,
# the operator's own reference file that CLAUDE.md says never to commit, and
# any xwd dump or scratch script left on the box. The escape was to commit the
# file (breaking that rule) or delete it. An untracked file cannot be clobbered
# by a fast-forward pull, so it is not a reason to refuse.
dirty_other="$(git status --porcelain --untracked-files=no -- . ':(exclude)config' | head -20)"
[ -n "$dirty_other" ] && {
    printf '%s\n' "$dirty_other"
    die "uncommitted changes outside config/ — commit or stash them first"
}

# Check we can actually fast-forward BEFORE committing anything locally.
# This used to commit local config first and then push. When the push was
# rejected — bad venue wifi, or any upstream commit landing between deploys —
# it died with the local commit already made, so every later deploy failed
# --ff-only and the box was un-deployable until someone rebased by hand.
say "Fetching"
git fetch -q origin main || die "could not reach origin — check the network"
remote="$(git rev-parse origin/main)"
local_head="$(git rev-parse HEAD)"
if [ "$local_head" != "$remote" ] && ! git merge-base --is-ancestor "$local_head" "$remote"; then
    die "this box has diverged from origin/main — resolve by hand before deploying"
fi

# Commit local config, then REBASE ONTO ORIGIN BEFORE PUSHING.
#
# This used to commit and push straight away, before the pull further down.
# Whenever the box was behind origin — which is the whole reason anyone runs
# this script — the push was a non-fast-forward and was rejected, and the
# script died blaming the network. Retrying never helped: any box carrying show
# tuning became un-deployable until someone rebased it by hand at the venue.
#
# Pulling first is what makes the push a fast-forward. --rebase, not merge, so
# the box never writes a merge commit nobody will review.
CONFIG_COMMITTED=0
if ! git diff --quiet -- config || [ -n "$(git ls-files --others --exclude-standard config)" ]; then
    echo "Live config changed on this box — committing:"
    git --no-pager diff --stat -- config
    git add config
    git commit -q -m "config: live tuning from $(hostname)"
    CONFIG_COMMITTED=1

    if [ "$local_head" != "$remote" ]; then
        echo "Box is behind origin — rebasing the config commit on top before pushing."
        if ! git rebase -q origin/main; then
            git rebase --abort 2>/dev/null || true
            git reset -q --soft "$local_head"
            die "config conflicts with origin/main — resolve by hand before deploying"
        fi
    fi

    if ! git push -q origin HEAD:main; then
        # Leave the box exactly as it was found rather than diverged.
        git reset -q --hard "$local_head"
        die "could not push local config; nothing was changed — retry when the network is back"
    fi
    echo "Pushed."
else
    echo "Clean."
fi

# --- 2. Backup ---------------------------------------------------------------
# Migrations run automatically at startup and are not reversible.
say "Backing up the database"
if [ -f "$DB" ]; then
    backup="$DB.bak-$(date +%Y%m%dT%H%M%S)"
    # sqlite3 .backup, not cp. The service is still running and the DB is in WAL
    # mode, so cp misses everything committed since the last checkpoint and can
    # tear a page mid-write — and this is the artefact you restore from when an
    # irreversible migration goes wrong.
    if command -v sqlite3 >/dev/null 2>&1; then
        sqlite3 "$DB" ".backup '$backup'" || die "database backup failed"
    else
        "$VENV/bin/python" - "$DB" "$backup" <<'PYBACKUP' || die "database backup failed"
import sqlite3, sys
src, dst = sys.argv[1], sys.argv[2]
with sqlite3.connect(src) as s, sqlite3.connect(dst) as d:
    s.backup(d)
PYBACKUP
    fi
    echo "$backup"
    # Keep the last 10. Nothing pruned these, so they accumulated on the same
    # partition as the live database, forever.
    ls -1t "$DB".bak-* 2>/dev/null | tail -n +11 | xargs -r rm -f
else
    echo "No database at $DB yet — skipping."
fi

# --- 3. Pull -----------------------------------------------------------------
say "Pulling"
before="$(git rev-parse HEAD)"
git pull -q --ff-only origin main
after="$(git rev-parse HEAD)"

if [ "$before" = "$after" ]; then
    echo "Already up to date."
else
    git --no-pager log --oneline "$before..$after"
fi

# --- 4. Dependencies ---------------------------------------------------------
if [ "$before" != "$after" ] && ! git diff --quiet "$before" "$after" -- requirements.txt; then
    say "requirements.txt changed — installing"
    if ! "$VENV/bin/pip" install -q -r requirements.txt; then
        # Roll the checkout back. Otherwise new code sits on disk with old deps,
        # the running process keeps working all evening, and the failure only
        # appears at the next power cycle — twelve hours and one venue away
        # from its cause.
        git reset -q --hard "$before"
        die "pip install failed — rolled back to $before, nothing restarted"
    fi
    # Prove the process can still import before we restart it.
    "$VENV/bin/python" -c "import scanmania" 2>/dev/null || \
        "$VENV/bin/python" -c "import fastapi, cv2, numpy" || {
            git reset -q --hard "$before"
            die "dependencies are broken after install — rolled back to $before"
        }
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
import hashlib, json, os, sys, time, urllib.request

URL = "http://127.0.0.1:8000/api/admin/status"
pw = os.environ.get("SCANMANIA_ADMIN_PASSWORD", "")
hdr = {"X-Admin-Password": hashlib.sha256(pw.encode()).hexdigest()} if pw else {}
try:
    d = json.load(urllib.request.urlopen(
        urllib.request.Request(URL, headers=hdr), timeout=10))
except Exception as exc:
    print(f"  api                          UNREACHABLE ({exc})")
    sys.exit(1)

print(f"  {'api':<28} ok — state={d.get('state')}")


def faults_of(doc):
    return [(f.get("subsystem"), f.get("message")) for f in (doc.get("faults") or [])]


# Eight RTSP cameras do not finish connecting in the time it takes uvicorn to
# answer, so a status read taken the instant the service comes back ALWAYS
# reported cameras missing. A fault printed by every deploy is a fault nobody
# reads, so give the transient ones time to clear before believing them.
pending = faults_of(d)
deadline = time.monotonic() + 45
while pending and time.monotonic() < deadline:
    time.sleep(3)
    try:
        d = json.load(urllib.request.urlopen(
            urllib.request.Request(URL, headers=hdr), timeout=10))
    except Exception:
        break
    pending = faults_of(d)

for sub, msg in pending:
    print(f"    FAULT {sub}: {msg}")
PY

if [ "$failed" -ne 0 ]; then
    printf '\n\033[31mDeploy finished with problems — check: journalctl -u scanmania -n 50\033[0m\n'
    exit 1
fi
printf '\n\033[32mDeploy OK.\033[0m\n'
