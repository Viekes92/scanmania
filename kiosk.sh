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

# The outdoor panel is mounted portrait: /display/out is designed 9:16 because
# people photograph it with a phone, and a portrait frame is what they can post.
# X has to be told, or the page renders letterboxed inside a landscape desktop.
#
# "left" or "right" depending on which way the panel was turned. Get it wrong
# and the picture is upside down, which is obvious the moment you look — set
# SCANMANIA_ROTATE_OUT=right in /etc/default/scanmania to flip it. "normal"
# disables rotation entirely.
ROTATE_OUT=${SCANMANIA_ROTATE_OUT:-left}

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
  # The LARGEST mode that runs at >= MIN_HZ, compared by pixel area.
  #
  # This used to take the first qualifying line and stop, assuming xrandr lists
  # modes highest-resolution first. It does not: it lists the PREFERRED mode
  # first, and a panel whose preferred mode is not its best puts a small one at
  # the top. HDMI-2 advertises 1280x720 (preferred) before 1920x1080, so the
  # in-container display ran at 720p on a panel that can do 1080p, and nothing
  # said so — it just looked soft.
  xrandr --query | awk -v out="$1" -v minhz="$MIN_HZ" '
    $1 == out && $2 == "connected" { on = 1; next }
    on && $1 ~ /^[0-9]+x[0-9]+$/ {
      res = $1
      if (pref == "") pref = res
      split(res, wh, "x")
      area = wh[1] * wh[2]
      for (i = 2; i <= NF; i++) {
        hz = $i; gsub(/[*+]/, "", hz)
        if (hz + 0 >= minhz) {
          if (area > best_area) { best_area = area; found = res }
          break
        }
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

# Resolve each role against what is actually plugged in. A configured output
# that is missing drops ITS OWN role — it must not fall through to "the first
# connected output", because that steals the other role's panel: with only one
# screen cabled, asking for the outdoor page on it still produced the
# in-container page, and the override looked like it did nothing.
has_output "$OUT_IN"  || OUT_IN=""
has_output "$OUT_OUT" || OUT_OUT=""

if [ -z "$OUT_IN" ] && [ -z "$OUT_OUT" ]; then
  # Neither configured name is connected: the cabling moved wholesale, so use
  # whatever is there in xrandr order rather than showing nothing.
  OUT_IN=${CONNECTED[0]:-}
  OUT_OUT=${CONNECTED[1]:-}
fi
[ -n "$OUT_IN$OUT_OUT" ] || { echo "No connected output, giving up."; exit 1; }

# X lays the outputs out side by side; the outdoor window starts where the
# in-container one ends. With no in-container panel that offset is 0.
W_IN=0
if [ -n "$OUT_IN" ]; then
  MODE_IN=$(pick_mode "$OUT_IN")
  xrandr --output "$OUT_IN" --mode "$MODE_IN" --pos 0x0 --primary
  W_IN=${MODE_IN%x*}
fi

if [ -n "$OUT_OUT" ] && [ "$OUT_OUT" != "$OUT_IN" ]; then
  MODE_OUT=$(pick_mode "$OUT_OUT")
  xrandr --output "$OUT_OUT" --mode "$MODE_OUT" --pos "${W_IN}x0" \
         --rotate "$ROTATE_OUT"
  # Something has to be primary, or Chromium guesses at a screen.
  [ -n "$OUT_IN" ] || xrandr --output "$OUT_OUT" --primary
  # A rotated output swaps width and height, and Chromium is positioned from
  # explicit pixel values because there is no window manager to ask. Using the
  # unrotated mode here put a 2560-wide window on a 1440-wide panel: the right
  # third of the page, including the sponsor lockup, sat off-screen.
  if [ "$ROTATE_OUT" = "left" ] || [ "$ROTATE_OUT" = "right" ]; then
    MODE_OUT="${MODE_OUT#*x}x${MODE_OUT%x*}"
  fi
  echo "  $OUT_OUT rotated $ROTATE_OUT -> ${MODE_OUT}"
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
[ -n "${MODE_IN:-}" ]  && launch in  "$MODE_IN"  0       "$SERVER/display/in"
[ -n "${MODE_OUT:-}" ] && launch out "$MODE_OUT" "$W_IN" "$SERVER/display/out"

# Supervise each window individually.
#
# A bare `wait` blocks until EVERY job exits, so a Chromium that got OOM-killed
# on day 40 left one panel black permanently while the script sat waiting on the
# other — and systemd never restarted anything, because nothing had exited.
# `wait -n` returns on the FIRST exit, so a dead window is noticed and relaunched.
declare -A RESTARTS=()
RELAUNCH_BACKOFF_S=2
_TOTAL_RELAUNCHES=0
_RELAUNCH_CAP=50
while true; do
    wait -n || true
    # Back off. A launch that can never succeed — dbus-run-session missing,
    # chromium gone — relaunched every 2 s forever while systemd reported the
    # unit active and both panels stayed black. Growing the gap turns a hot
    # spin into something a human can read in the journal, and the cap means we
    # stop pretending it is going to work.
    sleep "$RELAUNCH_BACKOFF_S"
    if [ "$RELAUNCH_BACKOFF_S" -lt 30 ]; then
        RELAUNCH_BACKOFF_S=$(( RELAUNCH_BACKOFF_S * 2 ))
    fi
    for name in in out; do
        [ "$name" = "in" ]  && [ -z "${MODE_IN:-}" ]  && continue
        [ "$name" = "out" ] && [ -z "${MODE_OUT:-}" ] && continue
        if pgrep -f "scanmania-kiosk-$name" >/dev/null 2>&1; then
            RELAUNCH_BACKOFF_S=2        # it is up; forget the backoff
        else
            RESTARTS[$name]=$(( ${RESTARTS[$name]:-0} + 1 ))
            _TOTAL_RELAUNCHES=$(( _TOTAL_RELAUNCHES + 1 ))
            echo "$(date -Is) $name window gone — relaunch #${RESTARTS[$name]}"
            if [ "$_TOTAL_RELAUNCHES" -gt "$_RELAUNCH_CAP" ]; then
                echo "Giving up after $_TOTAL_RELAUNCHES relaunches — the window"
                echo "is not staying up. Exiting so systemd records a FAILURE"
                echo "instead of reporting a healthy unit with black panels."
                exit 1
            fi
            if [ "$name" = "in" ]; then
                launch in "$MODE_IN" 0 "$SERVER/display/in"
            else
                launch out "$MODE_OUT" "$W_IN" "$SERVER/display/out"
            fi
        fi
    done
done
