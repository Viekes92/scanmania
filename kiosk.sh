#!/bin/bash
# ScanMania Kiosk — X + Chromium fullscreen
SERVER=http://localhost:8000

# Start X on vt7
Xorg :0 -nolisten tcp vt7 &
sleep 2

export DISPLAY=:0

# Disable blanking and cursor
xset s off -dpms 2>/dev/null
unclutter -idle 0.5 -root &

# Detect outputs
OUTPUTS=$(xrandr | grep ' connected' | awk '{print $1}')
NUM=$(echo "$OUTPUTS" | wc -w)
echo "Connected: $OUTPUTS ($NUM)"

if [ "$NUM" -ge 2 ]; then
    OUT1=$(echo $OUTPUTS | cut -d' ' -f1)
    OUT2=$(echo $OUTPUTS | cut -d' ' -f2)
    W1=$(xrandr | grep "$OUT1" -A1 | tail -1 | awk '{print $1}' | cut -dx -f1)
    xrandr --output $OUT1 --auto --pos 0x0 --output $OUT2 --auto --right-of $OUT1
    sleep 1
    chromium --no-sandbox --kiosk --noerrdialogs --disable-translate --no-first-run         --disable-infobars --window-position=0,0 --app="$SERVER/display/in" &
    chromium --no-sandbox --kiosk --noerrdialogs --disable-translate --no-first-run         --disable-infobars --window-position=$W1,0 --app="$SERVER/display/out" &
else
    xrandr --output $(echo $OUTPUTS | cut -d' ' -f1) --auto
    sleep 1
    chromium --no-sandbox --kiosk --noerrdialogs --disable-translate --no-first-run         --disable-infobars --app="$SERVER/display/in" &
fi

wait
