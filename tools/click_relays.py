#!/usr/bin/env python3
"""Click every relay on all three Waveshare boards, as one continuous walk.

All sockets are opened up front so there is no connect delay mid-run.
Modbus RTU over TCP, port 4196 (transparent mode).
Run: python3 click_relays.py
"""

import socket
import time

BOARDS = ["172.16.0.101", "172.16.0.102", "172.16.0.103"]
PORT = 4196
CHANNELS = 16
ON_TIME = 0.05   # how long each relay stays on
GAP = 0.05       # pause between relays


def crc16(data):
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc.to_bytes(2, "little")


def send(sock, body):
    """body = address + function + data, CRC gets added here."""
    sock.send(body + crc16(body))
    try:
        sock.recv(64)          # swallow the echo so we stay in sync
    except (socket.timeout, OSError):
        pass


def relay(sock, channel, state):
    """channel 0-15, state True/False"""
    value = b"\xff\x00" if state else b"\x00\x00"
    send(sock, b"\x01\x05" + channel.to_bytes(2, "big") + value)


def all_relays(sock, state):
    value = b"\xff\x00" if state else b"\x00\x00"
    send(sock, b"\x01\x05\x00\xff" + value)


# open everything first
socks = {}
for ip in BOARDS:
    s = socket.socket()
    s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    s.settimeout(0.3)
    try:
        s.connect((ip, PORT))
        socks[ip] = s
        print(f"connected {ip}")
    except OSError as e:
        print(f"can't connect {ip}: {e}")

if not socks:
    raise SystemExit("no boards reachable")

for s in socks.values():
    all_relays(s, False)
time.sleep(0.2)

# one continuous walk across every board
try:
    for ip, s in socks.items():
        for ch in range(CHANNELS):
            print(f"  {ip} relay {ch + 1:2d}", flush=True)
            relay(s, ch, True)
            time.sleep(ON_TIME)
            relay(s, ch, False)
            time.sleep(GAP)
finally:
    for s in socks.values():
        s.close()

print("done")