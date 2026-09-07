#!/bin/bash
# ScanMania Kiosk — launches X + Chromium on connected displays
# HDMI-1: in-container display (/display/in)
# HDMI-2: outdoor display (/display/out)

export DISPLAY=:0
SERVER=http://localhost:8000

# Start X server
startx &
sleep 3

# Disable screen blanking and cursor
xset s off -dpms
unclutter -idle 0.5 -root &

# Detect connected outputs
OUTPUTS=$(xrandr | grep ' connected' | awk '{print $1}')
echo "Connected outputs: $OUTPUTS"

# Count outputs
NUM=$(echo "$OUTPUTS" | wc -l)

if [ "$NUM" -ge 2 ]; then
    # Two displays: first=in-container, second=outdoor
    OUT1=$(echo "$OUTPUTS" | head -1)
    OUT2=$(echo "$OUTPUTS" | tail -1)
    
    # Get resolution of each
    RES1=$(xrandr | grep "$OUT1" -A1 | tail -1 | awk '{print $1}')
    RES2=$(xrandr | grep "$OUT2" -A1 | tail -1 | awk '{print $1}')
    W1=$(echo $RES1 | cut -dx -f1)
    
    # Place displays side by side
    xrandr --output $OUT1 --auto --pos 0x0 --output $OUT2 --auto --right-of $OUT1
    sleep 1
    
    # In-container display on first output
    chromium --kiosk --noerrdialogs --disable-translate --no-first-run         --disable-infobars --disable-session-crashed-bubble         --window-position=0,0 --app="$SERVER/display/in" &
    
    # Outdoor display on second output
    chromium --kiosk --noerrdialogs --disable-translate --no-first-run         --disable-infobars --disable-session-crashed-bubble         --user-data-dir=/tmp/chromium-display-out         --window-position=$W1,0 --app="$SERVER/display/out" &
else
    # Single display: show in-container by default
    OUT1=$(echo "$OUTPUTS" | head -1)
    xrandr --output $OUT1 --auto
    sleep 1
    
    chromium --kiosk --noerrdialogs --disable-translate --no-first-run         --disable-infobars --disable-session-crashed-bubble         --app="$SERVER/display/in" &
fi

wait
