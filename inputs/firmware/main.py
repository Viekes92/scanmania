"""MicroPython firmware for the Raspberry Pi Pico. See docs/protocols/pico-serial.md."""

# ---------------------------------------------------------------------------
# ScanMania Pico firmware — v1.0.0
#
# Inputs:  dry-contact buttons on GP0-GP7 (pull-up, active LOW)
# Outputs: LED control on GP16-GP17; ASCII protocol over USB CDC at 115200
# Protocol: EV / HB / BOOT messages to host; LED / PING / RESET from host
# Invariant: hardware WDT with 5 s timeout. Debounce: 30 ms, checked twice.
#            Never block in the main loop for more than a few ms.
# ---------------------------------------------------------------------------

import sys
import time

import machine

# ---------------------------------------------------------------------------
# Firmware version
# ---------------------------------------------------------------------------
FIRMWARE_VERSION = "1.0.0"

# ---------------------------------------------------------------------------
# Pin assignments
# ---------------------------------------------------------------------------
INPUT_PINS = {
    "start_plate": 0,
    "stop":        1,
    "cp1":         2,
    "cp2":         3,
    "spare1":      4,
    "spare2":      5,
    "spare3":      6,
    "spare4":      7,
}
INPUT_ORDER = ["start_plate", "stop", "cp1", "cp2", "spare1", "spare2", "spare3", "spare4"]

LED_PINS = {
    "start_led": 16,
    "stop_led":  17,
}

# ---------------------------------------------------------------------------
# Timing constants
# ---------------------------------------------------------------------------
DEBOUNCE_MS       = 30    # check twice with this gap to confirm a state change
HEARTBEAT_MS      = 250   # send HB every 250 ms
PULSE_PERIOD_MS   = 500   # LED pulse toggle period
FLASH_PERIOD_MS   = 100   # LED flash toggle period

# ---------------------------------------------------------------------------
# Initialise hardware
# ---------------------------------------------------------------------------

# Input pins — pull-up; button press pulls to GND → reads 0
inputs: dict[str, machine.Pin] = {}
for name, gp in INPUT_PINS.items():
    inputs[name] = machine.Pin(gp, machine.Pin.IN, machine.Pin.PULL_UP)

# LED pins — output, active HIGH
leds: dict[str, machine.Pin] = {}
for name, gp in LED_PINS.items():
    leds[name] = machine.Pin(gp, machine.Pin.OUT, value=0)

# Hardware watchdog — 5 s timeout
wdt = machine.WDT(timeout=5000)

# ---------------------------------------------------------------------------
# State tracking
# ---------------------------------------------------------------------------

# Debounced logical state: True = button pressed (active LOW inverted)
input_state: dict[str, bool] = {name: False for name in INPUT_ORDER}
# Raw reading used by debouncer
input_raw: dict[str, int] = {name: inputs[name].value() for name in INPUT_ORDER}
# Pending debounce: name → (pending_state, first_seen_ms)
input_pending: dict[str, tuple[bool, int]] = {}

# LED modes: name → "off" | "on" | "pulse" | "flash"
led_mode: dict[str, str] = {name: "off" for name in LED_PINS}
# LED toggle state for pulse/flash
led_toggle_state: dict[str, bool] = {name: False for name in LED_PINS}
led_next_toggle_ms: dict[str, int] = {name: 0 for name in LED_PINS}

# Sequence counter — increments on every outgoing message
seq: int = 0

# Heartbeat
last_hb_ms: int = 0

# ---------------------------------------------------------------------------
# Serial output — USB CDC
# ---------------------------------------------------------------------------

def emit(msg: str) -> None:
    """Write a raw line to USB CDC stdout. Does NOT touch seq (use for BOOT)."""
    sys.stdout.write(msg + "\r\n")


def send(msg_template: str, *args) -> None:
    """
    Increment seq and write a sequenced message to USB CDC stdout.

    msg_template is a format string whose FIRST positional argument is the
    sequence number; remaining *args follow in order.

    Example: send("EV {} {} {} {}", pico_ms, input_id, state)
    → writes "EV <seq> <pico_ms> <input_id> <state>"
    """
    global seq
    seq += 1
    sys.stdout.write(msg_template.format(seq, *args) + "\r\n")


def input_bitmask() -> int:
    """Build bitmask from current debounced input states. Bit 0 = INPUT_ORDER[0]."""
    mask = 0
    for i, name in enumerate(INPUT_ORDER):
        if input_state[name]:
            mask |= (1 << i)
    return mask


# ---------------------------------------------------------------------------
# Boot announcement
# ---------------------------------------------------------------------------
emit("BOOT " + FIRMWARE_VERSION)


# ---------------------------------------------------------------------------
# Command parser — host → Pico
# ---------------------------------------------------------------------------

_input_buf: str = ""


def poll_commands() -> None:
    """
    Non-blocking read of pending bytes from stdin.
    Parse complete lines as commands: LED, PING, RESET.
    """
    global _input_buf
    while True:
        try:
            ch = sys.stdin.read(1)
        except Exception:
            break
        if ch is None or ch == "":
            break
        _input_buf += ch
        if "\n" in _input_buf:
            line, _input_buf = _input_buf.split("\n", 1)
            line = line.strip("\r").strip()
            if line:
                handle_command(line)


def handle_command(line: str) -> None:
    parts = line.split()
    if not parts:
        return
    cmd = parts[0]

    if cmd == "LED" and len(parts) == 3:
        led_id = parts[1]
        mode = parts[2]
        if led_id in led_mode and mode in ("off", "on", "pulse", "flash"):
            led_mode[led_id] = mode
            led_toggle_state[led_id] = False
            led_next_toggle_ms[led_id] = time.ticks_ms()

    elif cmd == "PING":
        send("HB {} {} {}", time.ticks_ms(), input_bitmask())

    elif cmd == "RESET":
        machine.reset()


# ---------------------------------------------------------------------------
# LED update
# ---------------------------------------------------------------------------

def update_leds() -> None:
    """Drive LED outputs according to their current mode."""
    now = time.ticks_ms()
    for name, pin in leds.items():
        mode = led_mode[name]
        if mode == "off":
            pin.value(0)
        elif mode == "on":
            pin.value(1)
        elif mode == "pulse":
            if time.ticks_diff(now, led_next_toggle_ms[name]) >= 0:
                led_toggle_state[name] = not led_toggle_state[name]
                pin.value(1 if led_toggle_state[name] else 0)
                led_next_toggle_ms[name] = time.ticks_add(now, PULSE_PERIOD_MS)
        elif mode == "flash":
            if time.ticks_diff(now, led_next_toggle_ms[name]) >= 0:
                led_toggle_state[name] = not led_toggle_state[name]
                pin.value(1 if led_toggle_state[name] else 0)
                led_next_toggle_ms[name] = time.ticks_add(now, FLASH_PERIOD_MS)


# ---------------------------------------------------------------------------
# Debounce
# ---------------------------------------------------------------------------

def poll_inputs() -> None:
    """
    Read all input pins. Apply 30 ms debounce: a state change is confirmed only
    if the new reading holds for DEBOUNCE_MS ms (read twice with that gap).
    Emits EV messages on confirmed transitions.
    """
    now = time.ticks_ms()
    for name in INPUT_ORDER:
        raw = inputs[name].value()
        pressed = (raw == 0)  # active LOW: 0 means pressed

        if pressed != input_state[name]:
            # State appears to have changed — start or confirm debounce
            if name not in input_pending:
                input_pending[name] = (pressed, now)
            else:
                pending_state, first_ms = input_pending[name]
                if pending_state == pressed:
                    # Same pending state — check if debounce window elapsed
                    if time.ticks_diff(now, first_ms) >= DEBOUNCE_MS:
                        # Confirmed
                        input_state[name] = pressed
                        del input_pending[name]
                        state_int = 1 if pressed else 0
                        send("EV {} {} {} {}", time.ticks_ms(), name, state_int)
                else:
                    # Pending state changed (bounced again) — restart timer
                    input_pending[name] = (pressed, now)
        else:
            # Current reading matches stable state — clear any pending debounce
            if name in input_pending:
                del input_pending[name]


# ---------------------------------------------------------------------------
# Heartbeat
# ---------------------------------------------------------------------------

def poll_heartbeat() -> None:
    """Send HB every HEARTBEAT_MS ms with current ticks_ms and input bitmask."""
    global last_hb_ms
    now = time.ticks_ms()
    if time.ticks_diff(now, last_hb_ms) >= HEARTBEAT_MS:
        send("HB {} {} {}", now, input_bitmask())
        last_hb_ms = now


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

while True:
    wdt.feed()
    poll_commands()
    poll_inputs()
    poll_heartbeat()
    update_leds()
    # Brief yield — keeps loop tight enough for 30 ms debounce accuracy
    time.sleep_ms(1)
