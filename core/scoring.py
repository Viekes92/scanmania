"""
core/scoring.py — leaderboard ranking and time formatting for ScanMania.

Input:  elapsed_ms (int), player_id (str), leaderboard (list of run dicts).
Output: rank (int, 1-based), formatted time string, personal best flag (bool).
Invariant: pure functions, no I/O, no state. Leaderboard list comes from persist/db.py.
"""

from __future__ import annotations


def rank_on_leaderboard(elapsed_ms: int, leaderboard: list[dict]) -> int:
    """
    Return the 1-based rank this elapsed_ms would achieve among clean runs.

    Lower elapsed_ms is better (fastest time wins). Ties share the same rank:
    if two existing runs have the same elapsed_ms as the new run, the new run
    ranks one below them (the new run is "after" the ties, not "ahead").

    Parameters
    ----------
    elapsed_ms:  the elapsed time of the run being ranked (in milliseconds)
    leaderboard: list of run dicts with at least an "elapsed_ms" key and
                 outcome == "clean" (caller should pre-filter to clean runs only;
                 this function counts any dict in the list as a valid entry)

    Returns
    -------
    1-based rank (1 = fastest). A rank of 1 means no existing run is faster.
    If leaderboard is empty, returns 1.

    Examples
    --------
    >>> rank_on_leaderboard(30000, [])
    1
    >>> rank_on_leaderboard(30000, [{"elapsed_ms": 25000}])
    2
    >>> rank_on_leaderboard(20000, [{"elapsed_ms": 25000}])
    1
    """
    faster_count = sum(1 for run in leaderboard if run["elapsed_ms"] <= elapsed_ms)
    return faster_count + 1


def format_time(elapsed_ms: int) -> str:
    """
    Format elapsed milliseconds as MM:SS.mmm.

    Parameters
    ----------
    elapsed_ms: elapsed time in milliseconds (non-negative integer)

    Returns
    -------
    String in the form "MM:SS.mmm", zero-padded.

    Examples
    --------
    >>> format_time(0)
    '00:00.000'
    >>> format_time(61500)
    '01:01.500'
    >>> format_time(3723456)
    '62:03.456'
    """
    if elapsed_ms < 0:
        elapsed_ms = 0
    total_seconds, ms = divmod(elapsed_ms, 1000)
    minutes, seconds = divmod(total_seconds, 60)
    return f"{minutes:02d}:{seconds:02d}.{ms:03d}"


def is_personal_best(player_id: str, elapsed_ms: int, leaderboard: list[dict]) -> bool:
    """
    Return True if elapsed_ms is the player's fastest clean run.

    Parameters
    ----------
    player_id:   the player whose history to check
    elapsed_ms:  the elapsed time of the newly completed run
    leaderboard: list of run dicts. Each dict must have "player_id" and
                 "elapsed_ms" keys. Only clean runs should be passed in
                 (caller filters by outcome == "clean").

    Returns
    -------
    True if the player has no previous clean run, or if this elapsed_ms is
    strictly less than all of their previous clean runs' elapsed_ms values.
    False otherwise (equal or worse than their existing best).

    Examples
    --------
    >>> is_personal_best("p1", 30000, [])
    True
    >>> is_personal_best("p1", 30000, [{"player_id": "p1", "elapsed_ms": 35000}])
    True
    >>> is_personal_best("p1", 35000, [{"player_id": "p1", "elapsed_ms": 30000}])
    False
    >>> is_personal_best("p1", 30000, [{"player_id": "p1", "elapsed_ms": 30000}])
    False
    """
    player_runs = [
        run["elapsed_ms"]
        for run in leaderboard
        if run.get("player_id") == player_id
    ]
    if not player_runs:
        return True
    return elapsed_ms < min(player_runs)
