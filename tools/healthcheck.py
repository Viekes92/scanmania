#!/usr/bin/env python3
"""
tools/healthcheck.py — what the box thinks is wrong, without curl.

Inputs:  /etc/default/scanmania (admin password), the running game on :8000
Outputs: state, uptime, active faults; exit 1 if any fault is active
Invariant: read-only. It asks the box questions and changes nothing.

curl is not installed on the NUC and is not in the bare-metal apt list, so the
documented verification steps could not actually be run by an operator with no
laptop. This is that step.

    python3 tools/healthcheck.py
    python3 tools/healthcheck.py --url http://172.16.0.10:8000
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


def admin_password(path: str = ENV_FILE) -> str | None:
    """Read the password without printing it. None if unreadable."""
    try:
        with open(path) as fh:
            for line in fh:
                m = re.match(r"\s*(?:export\s+)?SCANMANIA_ADMIN_PASSWORD=(.*)", line)
                if m:
                    return m.group(1).strip().strip('"').strip("'")
    except OSError:
        return None
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description="Report the box's health.")
    ap.add_argument("--url", default="http://localhost:8000")
    args = ap.parse_args()

    pw = admin_password()
    headers = {}
    if pw:
        headers["X-Admin-Password"] = hashlib.sha256(pw.encode()).hexdigest()

    try:
        doc = json.load(urllib.request.urlopen(
            urllib.request.Request(args.url + "/api/admin/status", headers=headers),
            timeout=10))
    except urllib.error.HTTPError as exc:
        print(f"  api   HTTP {exc.code} — "
              + ("password not readable from " + ENV_FILE if not pw
                 else "auth rejected"), file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"  api   UNREACHABLE ({exc})", file=sys.stderr)
        return 2

    print(f"  state   {doc.get('state')}")
    if doc.get("uptime_s") is not None:
        print(f"  uptime  {int(doc['uptime_s']) // 60} min")

    faults = doc.get("faults") or []
    if not faults:
        print("  faults  none")
        return 0
    for f in faults:
        print(f"  FAULT   {f.get('subsystem')}: {f.get('message')}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
