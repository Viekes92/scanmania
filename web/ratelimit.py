"""
web/ratelimit.py — a small in-memory rate limiter for the public endpoints.

Inputs:  a bucket name and a client key (source IP)
Outputs: True if the call is allowed, False if it should be refused
Invariant: memory is bounded. Entries expire, and the table is swept, so a
           scanner hitting a thousand source addresses cannot grow it forever.

There is no external dependency here on purpose: the box runs on a venue LAN
with no internet guarantee, and one dict is enough for the two endpoints that
are reachable without a password.
"""

from __future__ import annotations

import time
from collections import deque

# bucket -> key -> timestamps (monotonic seconds)
_HITS: dict[str, dict[str, deque[float]]] = {}
_LAST_SWEEP = 0.0
_SWEEP_EVERY_S = 300.0
# Hard cap on distinct keys per bucket, so the limiter itself cannot be the
# memory leak. Past this, the bucket refuses rather than grows.
_MAX_KEYS = 4096


def _sweep(now: float, window_s: float) -> None:
    global _LAST_SWEEP
    if now - _LAST_SWEEP < _SWEEP_EVERY_S:
        return
    _LAST_SWEEP = now
    for bucket in list(_HITS):
        keys = _HITS[bucket]
        for key in list(keys):
            hits = keys[key]
            while hits and hits[0] < now - window_s:
                hits.popleft()
            if not hits:
                del keys[key]
        if not keys:
            del _HITS[bucket]


def allow(bucket: str, key: str, limit: int, window_s: float) -> bool:
    """Record a hit and report whether it is within `limit` per `window_s`."""
    now = time.monotonic()
    _sweep(now, window_s)
    keys = _HITS.setdefault(bucket, {})
    if key not in keys and len(keys) >= _MAX_KEYS:
        return False
    hits = keys.setdefault(key, deque())
    while hits and hits[0] < now - window_s:
        hits.popleft()
    if len(hits) >= limit:
        return False
    hits.append(now)
    return True


def retry_after(bucket: str, key: str, window_s: float) -> int:
    """Seconds until the oldest hit in the window expires. For Retry-After."""
    hits = _HITS.get(bucket, {}).get(key)
    if not hits:
        return 1
    return max(1, int(window_s - (time.monotonic() - hits[0])) + 1)


def reset() -> None:
    """Drop all state. Tests only."""
    _HITS.clear()
