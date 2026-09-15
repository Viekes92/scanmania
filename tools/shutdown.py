#!/usr/bin/env python3
"""
tools/shutdown.py — end-of-day shutdown from the command line.

Inputs:  /etc/default/scanmania (admin password), the running game on :8000
Outputs: a step-by-step report on stdout; exit 0 only if every step succeeded
Invariant: saves before it darkens — a shutdown must never be what loses runs.
Invariant: does nothing by itself. It calls /api/admin/shutdown, which owns the
           sequence; this file is only a way to reach it without a browser.

The admin portal (Dashboard → End of day) is the normal way to do this. This
exists for a box you are already ssh'd into, and for the case where the kiosk
is the only screen in the container.

    python3 tools/shutdown.py              # go dark, then halt the NUC
    python3 tools/shutdown.py --no-poweroff  # go dark, leave the NUC running
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import urllib.error
import urllib.request

ENV_FILE = "/etc/default/scanmania"
URL = "http://localhost:8000/api/admin/shutdown"


def admin_password(path: str = ENV_FILE) -> str:
    """Read SCANMANIA_ADMIN_PASSWORD without printing it."""
    try:
        with open(path) as fh:
            for line in fh:
                m = re.match(r"\s*(?:export\s+)?SCANMANIA_ADMIN_PASSWORD=(.*)", line)
                if m:
                    return m.group(1).strip().strip('"').strip("'")
    except OSError as exc:
        sys.exit(f"cannot read {path}: {exc}")
    sys.exit(f"SCANMANIA_ADMIN_PASSWORD not found in {path}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--no-poweroff", action="store_true",
                    help="darken the container but leave the NUC running")
    ap.add_argument("--url", default=URL)
    args = ap.parse_args()

    poweroff = not args.no_poweroff
    body = json.dumps({"confirm": "SHUTDOWN", "poweroff": poweroff}).encode()
    req = urllib.request.Request(
        args.url, data=body, method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Admin-Password": hashlib.sha256(admin_password().encode()).hexdigest(),
        },
    )
    try:
        # Generous: the sequence snapshots the database before it answers.
        result = json.load(urllib.request.urlopen(req, timeout=120))
    except urllib.error.HTTPError as exc:
        sys.exit(f"shutdown refused: HTTP {exc.code} {exc.read().decode()[:200]}")
    except Exception as exc:
        sys.exit(f"shutdown failed: {exc}")

    for step in result.get("steps", []):
        mark = "OK  " if step.get("ok") else "FAIL"
        detail = step.get("detail") or ""
        print(f"  {mark} {step.get('step', ''):<28} {detail}")

    if result.get("ok"):
        if poweroff:
            print("\nContainer is dark. The NUC is halting — wait for its power "
                  "light to go out, then cut the breaker.")
        else:
            print("\nContainer is dark. The NUC is still running.")
        return 0

    # Never tell someone it is safe to cut power when a step failed: a coil that
    # did not answer is a coil that may still be energised.
    print("\nFinished WITH PROBLEMS — walk the container before cutting power.",
          file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
