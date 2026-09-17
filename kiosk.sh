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
  # The box is offline at a venue, and Chromium still spends its startup trying
  # to register for Google push. That is 60 ERROR lines per boot in the kiosk
  # journal (DEPRECATED_ENDPOINT / QUOTA_EXCEEDED / "Registration URL fetching
  # failed"), which is noise that buries real display faults — and pointless
  # outbound chatter from a machine on a show LAN. Nothing here is a display
  # setting; the pages do not change.
  --disable-background-networking --disable-component-update --disable-sync
  --disable-domain-reliability --disable-breakpad --metrics-recording-only
  --autoplay-policy=no-user-gesture-required
)

Xorg :0 -nolisten tcp vt7 &
XPID=$!
trap 'kill $XPID 2>/dev/null' EXIT

export DISPLAY=:0

# WAIT for X to accept connections instead of guessing.
#
# This was `sleep 2`, and the guess was wrong: Xorg on this box needs about
# five seconds to reach "Connected outputs", so every xset below ran against a
# server that was not listening yet and failed — silently, because the errors
# went to /dev/null. Screen blanking therefore kept X's DEFAULT 600 s timeout,
# and ten minutes into a show both panels went black while Chromium carried on
# running behind them, title counter still ticking. That is the kiosk
# "stopping" after a while.
for _i in $(seq 1 60); do
    xset q >/dev/null 2>&1 && break
    sleep 0.5
done
if ! xset q >/dev/null 2>&1; then
    echo "X did not accept connections within 30 s — exiting so systemd retries" >&2
    exit 1
fi

# Never swallow these again: a failure here is invisible until a panel goes
# dark mid-show, which is the worst possible time to find out.
blanking_off() {
    xset s off      || echo "WARN: xset s off failed" >&2
    xset -dpms      || echo "WARN: xset -dpms failed" >&2
    xset s noblank  || echo "WARN: xset s noblank failed" >&2
}
blanking_off
if xset q | grep -q "DPMS is Enabled"; then
    echo "WARN: DPMS is still enabled — the panels may blank" >&2
else
    echo "screen blanking and DPMS disabled"
fi

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
# Liveness is judged by the PAGE, not the process.
#
# pgrep -f "scanmania-kiosk-$name" matched the --user-data-dir flag, which
# Chromium repeats on the command line of every helper it spawns. So when a
# RENDERER died — the OOM killer's usual victim, and what produces an
# "Aw, Snap!" page — the browser process survived, pgrep found it, and the
# supervisor decided the window was healthy. The panel sat frozen indefinitely
# with the unit reporting active and nothing in the journal.
#
# Each display now stamps a counter into its window title on every broadcast it
# processes. A stopped counter means the page is dead however it died: crashed
# renderer, wedged JS, or a websocket that never came back.
declare -A LAST_BEAT=()
declare -A STALL_COUNT=()

# How many consecutive checks with no title change before we relaunch. The loop
# sleeps at least 2 s, the server broadcasts at 10 Hz, so three misses is ~6 s
# of genuine silence — long enough not to trip on a slow frame.
_BEAT_MISSES=3

page_title() {   # page_title <in|out>
  local want
  case "$1" in
    in)  want="In-container" ;;
    out) want="Public" ;;
    *)   return 1 ;;
  esac
  local id
  id=$(xdotool search --name "$want" 2>/dev/null | head -1) || return 1
  [ -n "$id" ] || return 1
  xdotool getwindowname "$id" 2>/dev/null
}

page_alive() {   # page_alive <in|out>
  local name=$1 title
  title=$(page_title "$name") || return 1
  [ -n "$title" ] || return 1
  if [ "$title" = "${LAST_BEAT[$name]:-}" ]; then
      STALL_COUNT[$name]=$(( ${STALL_COUNT[$name]:-0} + 1 ))
  else
      LAST_BEAT[$name]="$title"
      STALL_COUNT[$name]=0
  fi
  [ "${STALL_COUNT[$name]:-0}" -lt "$_BEAT_MISSES" ]
}

declare -A RESTARTS=()
RELAUNCH_BACKOFF_S=2
_CHECK_INTERVAL_S=5
# Relaunches allowed inside _RELAUNCH_WINDOW_S before we give up. A CUMULATIVE
# cap was wrong for a box that runs for two months: a display that needs one
# relaunch a day would trip a lifetime cap of 50 halfway through the tour and
# take the unit down for good. What we actually want to catch is a window that
# cannot stay up RIGHT NOW.
_RELAUNCH_WINDOW_S=600
_RELAUNCH_MAX_IN_WINDOW=10
declare -a _RELAUNCH_TIMES=()

while true; do
    # POLL. This used to be `wait -n`, which blocks until a background job
    # TERMINATES — so the whole health check below only ran after a Chromium
    # process died, and page_alive (the title heartbeat) never got to run in
    # the one case it was written for: a renderer that hangs while the browser
    # process stays alive. The display froze, the process looked fine, and
    # nothing ever relaunched it. That is the "kiosk stops working after a
    # while".
    #
    # page_alive covers the crash case too — a dead window has no title — so
    # nothing is lost by not waiting on the job.
    sleep "$_CHECK_INTERVAL_S"
    # Reap any job that did exit, without blocking, so a crashed Chromium does
    # not sit as a zombie for the life of the unit.
    jobs -rp >/dev/null 2>&1 || true
    # Re-assert blanking-off. Costs nothing, and means a panel cannot go dark
    # mid-show because something re-enabled DPMS behind us.
    xset s off >/dev/null 2>&1 || true
    xset -dpms >/dev/null 2>&1 || true
    for name in in out; do
        [ "$name" = "in" ]  && [ -z "${MODE_IN:-}" ]  && continue
        [ "$name" = "out" ] && [ -z "${MODE_OUT:-}" ] && continue
        if page_alive "$name"; then
            RELAUNCH_BACKOFF_S=2        # it is up AND updating; forget the backoff
            continue
        else
            RESTARTS[$name]=$(( ${RESTARTS[$name]:-0} + 1 ))
            echo "$(date -Is) $name window dead or frozen — relaunch #${RESTARTS[$name]}"
            LAST_BEAT[$name]=""
            STALL_COUNT[$name]=0
            pkill -f "scanmania-kiosk-$name" 2>/dev/null || true
            sleep 1
            # Rate, not lifetime total: keep only the relaunches inside the
            # window, then judge on how many are left.
            _now=$(date +%s)
            _kept=()
            for _t in "${_RELAUNCH_TIMES[@]:-}"; do
                [ -n "$_t" ] || continue
                [ $(( _now - _t )) -lt "$_RELAUNCH_WINDOW_S" ] && _kept+=("$_t")
            done
            _kept+=("$_now")
            _RELAUNCH_TIMES=("${_kept[@]}")
            if [ "${#_RELAUNCH_TIMES[@]}" -gt "$_RELAUNCH_MAX_IN_WINDOW" ]; then
                echo "Giving up: ${#_RELAUNCH_TIMES[@]} relaunches in the last"
                echo "$_RELAUNCH_WINDOW_S s — the window is not staying up. Exiting so"
                echo "systemd records a FAILURE instead of reporting a healthy"
                echo "unit with black panels."
                exit 1
            fi
            if [ "$name" = "in" ]; then
                launch in "$MODE_IN" 0 "$SERVER/display/in"
            else
                launch out "$MODE_OUT" "$W_IN" "$SERVER/display/out"
            fi
            # Grow the gap only when a relaunch actually happened, so a launch
            # that can never succeed is not retried in a hot spin.
            sleep "$RELAUNCH_BACKOFF_S"
            if [ "$RELAUNCH_BACKOFF_S" -lt 30 ]; then
                RELAUNCH_BACKOFF_S=$(( RELAUNCH_BACKOFF_S * 2 ))
            fi
        fi
    done
done
