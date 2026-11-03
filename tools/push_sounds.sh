#!/usr/bin/env bash
#
# tools/push_sounds.sh — copy the soundtrack to the box.
#
# Inputs:  sounds/*.wav|mp3|ogg|flac here; NUC host (default root@172.16.0.10)
# Outputs: the same files in /opt/scanmania/sounds/ on the box
# Invariant: additive. Never deletes on the far side — a file the box has and
#            this checkout does not is left alone, because the operator may
#            have put it there deliberately and losing it means silence at a
#            venue with no copy to restore from.
#
# Audio is gitignored (a single bed is 14 MB and the repo is pulled far more
# often than the music changes), so `deploy.sh` does not carry it. git leaves
# ignored files alone, so what this copies survives every later deploy.
#
# Usage:
#   ./tools/push_sounds.sh                  # copy to the default NUC
#   ./tools/push_sounds.sh root@10.0.0.5    # somewhere else
#   DRY=1 ./tools/push_sounds.sh            # show what would be sent
#   YES=1 ./tools/push_sounds.sh            # skip the overwrite confirmation

set -euo pipefail

HOST="${1:-root@172.16.0.10}"
REMOTE_DIR="${SCANMANIA_REMOTE_DIR:-/opt/scanmania/sounds}"
LOCAL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../sounds" && pwd)"

shopt -s nullglob
FILES=("$LOCAL_DIR"/*.wav "$LOCAL_DIR"/*.mp3 "$LOCAL_DIR"/*.ogg "$LOCAL_DIR"/*.flac)
shopt -u nullglob

if [ ${#FILES[@]} -eq 0 ]; then
    echo "No audio in $LOCAL_DIR."
    echo "Put the real files there, or run: python3 tools/gen_placeholder_sounds.py"
    exit 1
fi

printf '\033[1m== Sending %d file(s) to %s:%s\033[0m\n' "${#FILES[@]}" "$HOST" "$REMOTE_DIR"
for f in "${FILES[@]}"; do
    printf '  %-24s %s\n' "$(basename "$f")" "$(du -h "$f" | cut -f1)"
done

if [ -n "${DRY:-}" ]; then
    echo; echo "DRY set — nothing sent."
    exit 0
fi

ssh "$HOST" "mkdir -p '$REMOTE_DIR'"

# Back up whatever is already there before overwriting it.
#
# Audio is gitignored, so THE BOX IS THE ONLY COPY. A second operator on a
# fresh clone runs gen_placeholder_sounds.py to have something to test with,
# then runs this to "make sure the box has the audio" — and replaces the real
# soundtrack with sine tones. The generator guards that exact hazard with
# --force; this did not guard it at all.
BACKUP="$REMOTE_DIR/.replaced-$(date +%Y%m%dT%H%M%S)"
# find, not `ls a b c`: ls exits NON-ZERO if ANY one of the globs matches
# nothing, so with no .ogg on the box — which is every box — this test failed
# and the whole block below was skipped in silence. The backup never ran and
# neither did the "type replace" confirmation, which is the guard the comment
# above describes. .flac is included here too; it is in FILES at the top of
# the script but was missing from both globs here.
AUDIO_GLOB=( -name '*.wav' -o -name '*.mp3' -o -name '*.ogg' -o -name '*.flac' )
if ssh "$HOST" "find '$REMOTE_DIR' -maxdepth 1 -type f \\( ${AUDIO_GLOB[*]} \\) | grep -q ."; then
    echo
    echo "The box already has audio. It will be overwritten where names match."
    ssh "$HOST" "ls -la '$REMOTE_DIR' | tail -n +2"
    if [ -z "${YES:-}" ]; then
        printf '\nType "replace" to continue: '
        read -r reply
        [ "$reply" = "replace" ] || { echo "Nothing sent."; exit 1; }
    fi
    ssh "$HOST" "mkdir -p '$BACKUP' && find '$REMOTE_DIR' -maxdepth 1 -type f \\( ${AUDIO_GLOB[*]} \\) -exec cp -p {} '$BACKUP'/ \\;"
    # Prove it rather than announcing it: a backup that silently copied
    # nothing is worse than no backup, because it stops you looking.
    kept=$(ssh "$HOST" "find '$BACKUP' -type f | wc -l")
    [ "$kept" -gt 0 ] || { echo "ABORT: backup to $BACKUP copied nothing."; exit 1; }
    echo "Previous audio kept at $BACKUP ($kept file(s))"
fi

# rsync if the box has it (only sends what changed — a 14 MB bed does not need
# resending because a 60 KB cue did); scp otherwise.
if ssh "$HOST" 'command -v rsync >/dev/null 2>&1'; then
    rsync -av --progress "${FILES[@]}" "$HOST:$REMOTE_DIR/"
else
    echo "(no rsync on the box — falling back to scp)"
    scp "${FILES[@]}" "$HOST:$REMOTE_DIR/"
fi

echo
printf '\033[1m== On the box\033[0m\n'
ssh "$HOST" "ls -la '$REMOTE_DIR' | tail -n +2"

cat <<'EOF'

The running game only loads sounds at startup, so restart it to pick these up:
  ssh HOST 'systemctl restart scanmania'
Then check Admin -> Hardware -> Audio: it lists what loaded and what is missing.
EOF
