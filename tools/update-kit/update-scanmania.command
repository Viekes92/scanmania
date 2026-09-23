#!/bin/bash
#
# Update ScanMania on the container's NUC, from a MacBook on the same network.
#
# Double-click this file in Finder. It asks for the NUC password ONCE, shows
# you what is about to change, and then runs the box's own deploy script.
#
# Inputs:  the sounds/ folder next to this file (optional), the NUC password
# Outputs: an updated, restarted box, and a summary on screen
# Invariant: every destructive step lives on the NUC in tools/deploy.sh, which
#            backs the database up first and rolls the checkout back if the
#            update fails. This script decides nothing on its own — it
#            authenticates once, copies sound files, and calls that.
#
# Written for stock macOS: bash 3.2, no sshpass, no timeout, no extra installs.

set -u

HOST="${SCANMANIA_HOST:-172.16.0.10}"
USER_="${SCANMANIA_USER:-root}"
TARGET="${USER_}@${HOST}"
REMOTE="/opt/scanmania"

HERE="$(cd "$(dirname "$0")" && pwd)"
SOUNDS_DIR="$HERE/sounds"

# One socket for every connection below, so the password is typed ONCE instead
# of once per ssh and scp. ControlPersist keeps it open between steps.
# Kept in /tmp and short: a unix socket path longer than ~104 characters fails,
# and a home directory with a long name is enough to hit that.
CM="/tmp/.smupd-$$"
SSH_OPTS="-o ControlMaster=auto -o ControlPath=$CM -o ControlPersist=180 -o ConnectTimeout=8"

bold()  { printf '\n\033[1m== %s\033[0m\n' "$*"; }
info()  { printf '   %s\n' "$*"; }
warn()  { printf '\033[33m   %s\033[0m\n' "$*"; }
ok()    { printf '\033[32m   %s\033[0m\n' "$*"; }
fail()  { printf '\n\033[31mSTOPPED: %s\033[0m\n' "$*" >&2; finish 1; }

finish() {
    # Always drop the shared connection, or it lingers for ControlPersist
    # seconds holding an authenticated session to the box.
    ssh -O exit -o ControlPath="$CM" "$TARGET" 2>/dev/null
    rm -f "$CM" 2>/dev/null
    printf '\n'
    if [ "${1:-0}" -eq 0 ]; then
        printf '\033[32mDone. You can close this window.\033[0m\n'
    else
        printf '\033[31mNothing further was changed. You can close this window.\033[0m\n'
    fi
    # Keep the Terminal window up when launched by double-click, or the result
    # vanishes the instant the script ends and nobody learns anything.
    printf '\nPress return to close. '
    read -r _dummy
    exit "${1:-0}"
}
trap 'finish 1' INT TERM

clear
cat <<'BANNER'
  ____                 __  __             _
 / ___|  ___ __ _ _ __ |  \/  | __ _ _ __ (_) __ _
 \___ \ / __/ _` | '_ \| |\/| |/ _` | '_ \| |/ _` |
  ___) | (_| (_| | | | | |  | | (_| | | | | | (_| |
 |____/ \___\__,_|_| |_|_|  |_|\__,_|_| |_|_|\__,_|

  SOFTWARE UPDATE
BANNER
info "Target: $TARGET"

# ---------------------------------------------------------------------------
# 1. Is the box even there?
# ---------------------------------------------------------------------------
bold "Looking for the container"
# -G is macOS netcat's connect timeout. Checking port 22 rather than pinging:
# a box that answers ping but not ssh is the more common and more confusing
# failure, and this tells the two apart.
if ! nc -z -G 5 "$HOST" 22 2>/dev/null; then
    warn "No answer from $HOST on port 22."
    warn ""
    warn "Check, in this order:"
    warn "  1. Is this Mac on the container's network (not a phone hotspot)?"
    warn "  2. Is the NUC powered on? Give it two minutes after switch-on —"
    warn "     the network switch is slower to boot than the computer."
    warn "  3. Is the network switch powered?"
    fail "cannot reach the box"
fi
ok "Found it."

# ---------------------------------------------------------------------------
# 2. Authenticate once
# ---------------------------------------------------------------------------
bold "Signing in"
info "Enter the NUC password when asked. You will only be asked once."
printf '\n'
if ! ssh $SSH_OPTS "$TARGET" true; then
    fail "could not sign in — wrong password, or the box refused the connection"
fi
ok "Signed in."

# ---------------------------------------------------------------------------
# 3. What is on the box right now?
#
# The deploy doc's first rule: look at the box's git status BEFORE changing
# anything. config/mazes.yaml and config/beams.json are written live by the
# admin panel — show tuning and calibration — so the box legitimately differs
# from git, and deploy.sh commits that drift rather than discarding it. If
# anything ELSE is modified, a human needs to look before we pull.
# ---------------------------------------------------------------------------
bold "Checking the box"
CURRENT="$(ssh $SSH_OPTS "$TARGET" "cd $REMOTE && git log --oneline -1" 2>/dev/null)"
[ -n "$CURRENT" ] || fail "no ScanMania checkout at $REMOTE on the box"
info "Currently running: $CURRENT"

DIRTY="$(ssh $SSH_OPTS "$TARGET" \
    "cd $REMOTE && git status --porcelain --untracked-files=no -- . ':(exclude)config'" 2>/dev/null)"
if [ -n "$DIRTY" ]; then
    warn "The box has local changes outside config/:"
    printf '%s\n' "$DIRTY" | sed 's/^/     /'
    warn ""
    warn "That is unusual and the update script on the box will refuse to run."
    warn "Send this screen to whoever maintains the software."
    fail "the box has uncommitted work"
fi
ok "Nothing unexpected has been edited on the box."

# ---------------------------------------------------------------------------
# 4. Sound files (optional, and before the deploy so a restart picks them up)
#
# Additive on purpose: a file the box has and this folder does not is left
# alone. The operator may have put it there deliberately, and losing it means
# silence at a venue with no copy to restore from.
# ---------------------------------------------------------------------------
bold "Sound files"
SOUND_COUNT=0
if [ -d "$SOUNDS_DIR" ]; then
    for f in "$SOUNDS_DIR"/*.mp3 "$SOUNDS_DIR"/*.wav "$SOUNDS_DIR"/*.ogg "$SOUNDS_DIR"/*.flac; do
        [ -e "$f" ] || continue
        SOUND_COUNT=$((SOUND_COUNT + 1))
    done
fi

if [ "$SOUND_COUNT" -eq 0 ]; then
    info "None to send — the sounds folder is empty, so the box keeps what it has."
else
    info "Sending $SOUND_COUNT file(s):"
    for f in "$SOUNDS_DIR"/*.mp3 "$SOUNDS_DIR"/*.wav "$SOUNDS_DIR"/*.ogg "$SOUNDS_DIR"/*.flac; do
        [ -e "$f" ] || continue
        printf '     %-22s %s\n' "$(basename "$f")" "$(du -h "$f" | cut -f1)"
    done
    ssh $SSH_OPTS "$TARGET" "mkdir -p $REMOTE/sounds" \
        || fail "could not create the sounds folder on the box"
    for f in "$SOUNDS_DIR"/*.mp3 "$SOUNDS_DIR"/*.wav "$SOUNDS_DIR"/*.ogg "$SOUNDS_DIR"/*.flac; do
        [ -e "$f" ] || continue
        scp -q $SSH_OPTS "$f" "$TARGET:$REMOTE/sounds/" \
            || fail "could not copy $(basename "$f") — the box may be out of disk"
    done
    ok "Sound files copied."
fi

# ---------------------------------------------------------------------------
# 5. The update itself
#
# Everything risky happens inside the box's own tools/deploy.sh: it backs up
# the database with sqlite3 .backup (the service is running and the DB is in
# WAL mode, so a plain copy would be corrupt), pulls, reinstalls dependencies
# if they changed, proves the tree still imports, syncs the systemd unit files,
# restarts the game and the kiosk, and rolls everything back if any of that
# fails. Database schema migrations run when the service starts.
# ---------------------------------------------------------------------------
bold "Updating"
info "This takes a minute or two. Leave the window open."
printf '\n'
if ! ssh $SSH_OPTS -t "$TARGET" "cd $REMOTE && ./tools/deploy.sh"; then
    warn ""
    warn "The update did not finish cleanly."
    warn "The box rolls itself back when that happens, so it should still be"
    warn "running the version it was running before."
    fail "deploy failed on the box"
fi

# ---------------------------------------------------------------------------
# 6. Say what actually changed
# ---------------------------------------------------------------------------
bold "Result"
NEW="$(ssh $SSH_OPTS "$TARGET" "cd $REMOTE && git log --oneline -1" 2>/dev/null)"
if [ "$NEW" = "$CURRENT" ]; then
    ok "Already up to date — nothing to pull."
else
    info "Was: $CURRENT"
    ok   "Now: $NEW"
fi

# Schema migrations are logged by the service as it starts. Surfacing it means
# "migrate if needed" is something the person running this can SEE happened,
# rather than something they have to trust.
MIG="$(ssh $SSH_OPTS "$TARGET" \
    "journalctl -u scanmania --since '5 min ago' --no-pager 2>/dev/null | grep -i 'schema migrated' | tail -3" 2>/dev/null)"
if [ -n "$MIG" ]; then
    printf '\n'
    ok "Database upgraded:"
    printf '%s\n' "$MIG" | sed 's/^/     /'
fi

RUNNING="$(ssh $SSH_OPTS "$TARGET" "systemctl is-active scanmania" 2>/dev/null)"
printf '\n'
if [ "$RUNNING" = "active" ]; then
    ok "The game is running."
else
    warn "The game service reports: $RUNNING"
    warn "Send this screen to whoever maintains the software."
    finish 1
fi

finish 0
