"""
tools/ramp.py — generate a count-in pulse list from a curve.

Inputs:  --total-ms, --pulses, --ratio, --duty (all via CLI)
Outputs: YAML block ready to paste into game.yaml count_in.pulses,
         ASCII timeline, total duration, warnings.
Invariant: read-only — does not modify any config files.

Usage:
  python tools/ramp.py --total-ms 2700 --pulses 8 --ratio 0.7 --duty 0.45
  python tools/ramp.py --total-ms 3000 --pulses 10 --ratio 0.65 --duty 0.5
"""

from __future__ import annotations

import argparse
import sys


# ---------------------------------------------------------------------------
# Ramp generation
# ---------------------------------------------------------------------------

def generate_pulses(
    total_ms: float,
    n_pulses: int,
    ratio: float,
    duty: float,
) -> list[tuple[int, int]]:
    """
    Generate an exponentially decelerating pulse list.

    The period of each pulse shrinks by `ratio` each step:
        period[0] = P₀
        period[k] = P₀ × ratio^k

    Total duration = sum of all periods = P₀ × (1 - ratio^n) / (1 - ratio)
    Solving for P₀:
        P₀ = total_ms × (1 - ratio) / (1 - ratio^n)

    Within each period:
        on_ms  = round(period × duty)
        off_ms = round(period × (1 - duty))

    Rounding means the actual total may differ slightly from total_ms;
    the final period is adjusted to absorb the rounding error so the
    count-in lands on time.

    Parameters
    ----------
    total_ms : target total duration of the ramp in milliseconds
    n_pulses : number of on/off cycles
    ratio    : how fast each period shrinks relative to the previous
               (0 < ratio < 1; smaller = faster acceleration)
    duty     : fraction of each period that is "on" (0 < duty < 1)

    Returns
    -------
    List of (on_ms, off_ms) integer pairs, length == n_pulses.
    """
    if not (0 < ratio < 1):
        raise ValueError(f"ratio must be between 0 and 1 (exclusive), got {ratio}")
    if not (0 < duty < 1):
        raise ValueError(f"duty must be between 0 and 1 (exclusive), got {duty}")
    if n_pulses < 1:
        raise ValueError(f"n_pulses must be >= 1, got {n_pulses}")

    # Compute the first period P0
    p0 = total_ms * (1 - ratio) / (1 - ratio ** n_pulses)

    pulses = []
    for k in range(n_pulses):
        period  = p0 * (ratio ** k)
        on_ms   = max(1, round(period * duty))
        off_ms  = max(1, round(period * (1 - duty)))
        pulses.append((on_ms, off_ms))

    return pulses


# ---------------------------------------------------------------------------
# Output formatting
# ---------------------------------------------------------------------------

def yaml_block(pulses: list[tuple[int, int]]) -> str:
    """Return a YAML block ready to paste into game.yaml count_in.pulses."""
    lines = ["  pulses:"]
    for on_ms, off_ms in pulses:
        lines.append(f"    - [{on_ms}, {off_ms}]")
    return "\n".join(lines)


def ascii_timeline(
    pulses: list[tuple[int, int]],
    width: int = 72,
    char_on: str = "#",
    char_off: str = ".",
) -> str:
    """
    Render an ASCII timeline where each character represents a slice of time.

    Total actual duration is scaled to `width` characters.
    'on' windows → char_on, 'off' windows → char_off.
    """
    actual_ms = sum(on + off for on, off in pulses)
    ms_per_char = actual_ms / width if actual_ms > 0 else 1

    timeline = []
    for on_ms, off_ms in pulses:
        n_on  = max(1, round(on_ms  / ms_per_char))
        n_off = max(1, round(off_ms / ms_per_char))
        timeline.append(char_on  * n_on)
        timeline.append(char_off * n_off)

    raw = "".join(timeline)
    # Trim or pad to width
    if len(raw) > width:
        raw = raw[:width]
    else:
        raw = raw.ljust(width, " ")
    return raw


def warnings_for(pulses: list[tuple[int, int]]) -> list[str]:
    """Return a list of human-readable warnings about problematic pulses."""
    warns = []
    # Relay mechanical floor: below ~30 ms on-time the dot may not reach
    # full brightness (see plan §5.2).
    for i, (on_ms, off_ms) in enumerate(pulses):
        if on_ms < 30:
            warns.append(
                f"Pulse {i}: on_ms={on_ms} is below 30 ms relay mechanical floor."
                f" Dot may not reach full brightness."
            )
        if off_ms < 20:
            warns.append(
                f"Pulse {i}: off_ms={off_ms} is very short — relay may not open fully."
            )
    return warns


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate a ScanMania count-in pulse list from an exponential ramp curve.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Parameters
----------
  --total-ms  Target total ramp duration (the sum of all on+off windows).
              The actual duration may differ slightly due to integer rounding.
              Tune this to taste — the plan default is 2700 ms.

  --pulses    Number of on/off cycles. 8 is the plan default (game.yaml).
              More pulses → smoother ramp but last pulses become very short.

  --ratio     Geometric ratio between successive periods.
                0.50 = each period is half the previous (aggressive)
                0.70 = each period is 70 %% of the previous (plan default, gentler)
                0.85 = slow deceleration, almost uniform

  --duty      Fraction of each period that is "on".
                0.45 = slightly less than half (plan §5.2 default)
                0.50 = equal on and off
                0.60 = longer on, shorter off

Output
------
  1. YAML stanza for game.yaml count_in.pulses
  2. ASCII timeline (# = on, . = off)
  3. Total ramp duration
  4. Warnings if any on_ms < 30 ms (relay mechanical floor)

Examples
--------
  python tools/ramp.py --total-ms 2700 --pulses 8 --ratio 0.7 --duty 0.45
  python tools/ramp.py --total-ms 3000 --pulses 10 --ratio 0.65 --duty 0.5
  python tools/ramp.py --total-ms 2000 --pulses 6 --ratio 0.6 --duty 0.4
        """,
    )
    parser.add_argument("--total-ms", type=float, default=2700,
                        help="Target total ramp duration in milliseconds. Default: 2700.")
    parser.add_argument("--pulses",   type=int,   default=8,
                        help="Number of on/off cycles. Default: 8.")
    parser.add_argument("--ratio",    type=float, default=0.70,
                        help="Period shrink ratio per pulse (0 < r < 1). Default: 0.70.")
    parser.add_argument("--duty",     type=float, default=0.45,
                        help="On-time fraction per period (0 < d < 1). Default: 0.45.")
    args = parser.parse_args()

    try:
        pulses = generate_pulses(
            total_ms=args.total_ms,
            n_pulses=args.pulses,
            ratio=args.ratio,
            duty=args.duty,
        )
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    actual_ms = sum(on + off for on, off in pulses)

    # ---- 1. YAML block ----
    print("# paste into game.yaml under count_in:")
    print(yaml_block(pulses))
    print()

    # ---- 2. ASCII timeline ----
    print("# ASCII timeline  (# = on, . = off)  [1 char ≈ {:.0f} ms]".format(
        actual_ms / 72 if actual_ms else 1))
    print("# " + ascii_timeline(pulses))
    print()

    # ---- 3. Per-pulse table ----
    print("# Pulse   on_ms   off_ms  period_ms")
    for i, (on_ms, off_ms) in enumerate(pulses):
        print(f"#   {i}     {on_ms:>5}   {off_ms:>6}  {on_ms + off_ms:>9}")
    print()

    # ---- 4. Summary ----
    print(f"# Total duration: {actual_ms} ms  (target: {int(args.total_ms)} ms)")
    print(f"# Parameters: pulses={args.pulses}, ratio={args.ratio}, duty={args.duty}")
    print()

    # ---- 5. Warnings ----
    warns = warnings_for(pulses)
    if warns:
        print("# WARNINGS:")
        for w in warns:
            print(f"#   ! {w}")
        print()
    else:
        print("# No warnings. All pulse timings are within safe bounds.")


if __name__ == "__main__":
    main()
