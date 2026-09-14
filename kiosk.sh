#!/bin/bash
# ScanMania kiosk — one bare X server, one Chromium window per HDMI output.
#
# There is no window manager on this box, so nothing honours a fullscreen
# request: --kiosk quietly did nothing and Chromium picked its own window size.
# Every window is therefore positioned and sized explicitly from what xrandr
# reports, and --app hides the browser chrome that --kiosk used to.
#
# Which page lands on which panel is cabling, not logic — override with
# SCANMANIA_OUT_IN / SCANMANIA_OUT_OUT in /etc/default/scanmania.
set -u

SERVER=${SCANMANIA_SERVER:-http://localhost:8000}
OUT_IN=${SCANMANIA_OUT_IN:-HDMI-1}     # in-container stopwatch
OUT_OUT=${SCANMANIA_OUT_OUT:-HDMI-2}   # outdoor feed + leaderboard
MIN_HZ=${SCANMANIA_MIN_HZ:-50}         # a stopwatch at 30 Hz reads as stuttering

CHROME_FLAGS=(
  # --test-type is what suppresses the yellow "unsupported command-line flag:
  # --no-sandbox" infobar; --disable-infobars stopped covering that one and it
  # was eating ~45 px off the top of both panels.
  --no-sandbox --test-type
  --noerrdialogs --disable-infobars --disable-translate
  --no-first-run --no-default-browser-check --disable-pinch
  --disable-session-crashed-bubble --disable-features=TranslateUI
  --autoplay-policy=no-user-gesture-required
)

Xorg :0 -nolisten tcp vt7 &
XPID=$!
trap 'kill $XPID 2>/dev/null' EXIT
sleep 2

export DISPLAY=:0
xset s off -dpms 2>/dev/null
xset s noblank 2>/dev/null
unclutter -idle 0.5 -root &

# Highest-resolution mode on $1 that runs at >= MIN_HZ. xrandr lists modes
# largest first, so the first match is the best one. Falls back to the
# preferred mode if the panel offers nothing fast enough.
pick_mode() {
  xrandr --query | awk -v out="$1" -v minhz="$MIN_HZ" '
    $1 == out && $2 == "connected" { on = 1; next }
    on && $1 ~ /^[0-9]+x[0-9]+$/ {
      res = $1
      if (pref == "") pref = res
      for (i = 2; i <= NF; i++) {
        hz = $i; gsub(/[*+]/, "", hz)
        if (hz + 0 >= minhz) { found = res; exit }
      }
      next
    }
    on { exit }
    END { print (found != "" ? found : pref) }
  '
}

mapfile -t CONNECTED < <(xrandr --query | awk '$2 == "connected" { print $1 }')
echo "Connected outputs: ${CONNECTED[*]}"

has_output() {
  local o
  for o in "${CONNECTED[@]}"; do [ "$o" = "$1" ] && return 0; done
  return 1
}

# Fall back to whatever is actually plugged in if the configured names are not.
has_output "$OUT_IN"  || OUT_IN=${CONNECTED[0]:-}
has_output "$OUT_OUT" || OUT_OUT=${CONNECTED[1]:-}
[ -n "$OUT_IN" ] || { echo "No connected output, giving up."; exit 1; }

MODE_IN=$(pick_mode "$OUT_IN")
xrandr --output "$OUT_IN" --mode "$MODE_IN" --pos 0x0 --primary
W_IN=${MODE_IN%x*}

if [ -n "$OUT_OUT" ] && [ "$OUT_OUT" != "$OUT_IN" ]; then
  MODE_OUT=$(pick_mode "$OUT_OUT")
  xrandr --output "$OUT_OUT" --mode "$MODE_OUT" --pos "${W_IN}x0"
fi
sleep 1
xrandr --query | grep ' connected'

launch() {   # launch <name> <WxH> <x-offset> <url>
  local name=$1 mode=$2 xoff=$3 url=$4
  local w=${mode%x*} h=${mode#*x}
  echo "  $name: ${w}x${h} at ${xoff},0 -> $url"
  # Its own profile directory, or the second Chromium just hands the URL to the
  # first ("Opening in existing browser session") and exits, leaving one window.
  dbus-run-session -- chromium "${CHROME_FLAGS[@]}" \
    --user-data-dir="/var/tmp/scanmania-kiosk-$name" \
    --window-position="${xoff},0" --window-size="${w},${h}" \
    --app="$url" &
}

echo "Launching:"
launch in "$MODE_IN" 0 "$SERVER/display/in"
[ -n "${MODE_OUT:-}" ] && launch out "$MODE_OUT" "$W_IN" "$SERVER/display/out"

# Supervise each window individually.
#
# A bare `wait` blocks until EVERY job exits, so a Chromium that got OOM-killed
# on day 40 left one panel black permanently while the script sat waiting on the
# other — and systemd never restarted anything, because nothing had exited.
# `wait -n` returns on the FIRST exit, so a dead window is noticed and relaunched.
declare -A RESTARTS=()
while true; do
    wait -n || true
    sleep 2
    for name in in out; do
        [ "$name" = "out" ] && [ -z "${MODE_OUT:-}" ] && continue
        if ! pgrep -f "scanmania-kiosk-$name" >/dev/null 2>&1; then
            RESTARTS[$name]=$(( ${RESTARTS[$name]:-0} + 1 ))
            echo "$(date -Is) $name window gone — relaunch #${RESTARTS[$name]}"
            if [ "$name" = "in" ]; then
                launch in "$MODE_IN" 0 "$SERVER/display/in"
            else
                launch out "$MODE_OUT" "$W_IN" "$SERVER/display/out"
            fi
        fi
    done
done
