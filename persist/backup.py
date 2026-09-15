"""
persist/backup.py — snapshot exports and rolling local backups.

Inputs:  Database instance, backup output directory path.
Outputs: dated .db snapshot files via VACUUM INTO; leaderboard CSV files.
Invariant: snapshot on demand (admin portal) and on service stop.
           Rolling snapshots every snapshot_interval_min, keep last N (default 48).
"""

from __future__ import annotations

import asyncio
import csv
import glob
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

import aiosqlite

from persist.db import Database

log = logging.getLogger(__name__)

# Retention. Events are forensics — long enough to settle a dispute weeks later.
# PII is contact detail for a prize draw; it should not outlive the activation.
_EVENT_RETENTION_DAYS = int(os.environ.get("SCANMANIA_EVENT_RETENTION_DAYS", "30"))
_PII_RETENTION_DAYS = int(os.environ.get("SCANMANIA_PII_RETENTION_DAYS", "90"))


def _timestamp_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")


def snapshot_dir() -> str:
    """
    Where snapshots go on this machine.

    /var/backups/scanmania on the box; a temp directory on a developer Mac,
    which has no such path and should not need sudo to run the game. This was
    open-coded in three places and had already drifted into a shutdown path
    that failed on a laptop for no reason worth debugging.
    """
    import sys
    import tempfile
    if sys.platform == "darwin":
        return os.path.join(tempfile.gettempdir(), "scanmania-backups")
    return "/var/backups/scanmania"


async def export_snapshot(
    db: Database, output_dir: str = "/var/backups/scanmania"
) -> str:
    """
    VACUUM INTO a dated .db file.

    Returns the absolute path to the created snapshot.
    Raises on failure — callers should handle and surface the error.
    """
    Path(output_dir).mkdir(parents=True, exist_ok=True)
    ts = _timestamp_str()
    dest = str(Path(output_dir) / f"scanmania_{ts}.db")

    # VACUUM INTO creates a clean, defragmented copy without touching the live DB.
    # We go through aiosqlite rather than via db._db to keep the interface clean.
    async with aiosqlite.connect(db._db_path) as conn:
        await conn.execute(f"VACUUM INTO '{dest}'")

    log.info("Snapshot exported to %s", dest)
    return dest


async def export_leaderboard_csv(
    db: Database, output_dir: str = "/var/backups/scanmania"
) -> str:
    """
    Export the all-time leaderboard as a UTF-8 CSV file.

    Returns the absolute path to the created file.
    """
    Path(output_dir).mkdir(parents=True, exist_ok=True)
    ts = _timestamp_str()
    dest = str(Path(output_dir) / f"leaderboard_{ts}.csv")

    rows = await db.get_leaderboard(scope="all", limit=10_000)

    fieldnames = [
        "rank",
        "nickname",
        "elapsed_ms",
        "elapsed_s",
        "started_at",
        "run_id",
        "detection_mode",
        "segment_reached",
    ]

    with open(dest, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for i, row in enumerate(rows, start=1):
            elapsed_ms = row.get("elapsed_ms") or 0
            writer.writerow(
                {
                    "rank": i,
                    "nickname": row.get("player_nickname", ""),
                    "elapsed_ms": elapsed_ms,
                    "elapsed_s": round(elapsed_ms / 1000, 3),
                    "started_at": row.get("started_at", ""),
                    "run_id": row.get("id", ""),
                    "detection_mode": row.get("detection_mode", ""),
                    "segment_reached": row.get("segment_reached", ""),
                }
            )

    log.info("Leaderboard CSV exported to %s (%d rows)", dest, len(rows))
    return dest


async def rolling_snapshot_loop(
    db: Database,
    output_dir: str,
    interval_min: int = 60,
    keep: int = 48,
) -> None:
    """
    Loop: snapshot every interval_min minutes, prune to keep last N snapshots.

    Runs indefinitely; cancel the task to stop it.
    Does not export a leaderboard CSV on each pass (use export_leaderboard_csv
    separately if needed — e.g. on service stop).
    """
    log.info(
        "Rolling snapshot loop started: every %d min, keep=%d, dir=%s",
        interval_min,
        keep,
        output_dir,
    )
    first = True
    while True:
        # Snapshot immediately on start, then on the interval. Sleeping first
        # meant a service restarting more often than the interval — a crash
        # loop, or an operator restarting after config edits — produced no
        # snapshots at all, so after a power cut the newest could be a day old.
        if not first:
            await asyncio.sleep(interval_min * 60)
        first = False
        try:
            # Prune BEFORE writing, not after. Pruning only ran on success, so
            # once the partition filled, VACUUM INTO raised ENOSPC forever and
            # the loop could never free the space it needed to recover.
            await asyncio.to_thread(_prune_snapshots, output_dir, keep)
            path = await export_snapshot(db, output_dir)
            # Retention runs on the same schedule. All three of these existed and
            # had no callers, so the documented 30-day event rotation never ran
            # and player PII accumulated for the whole tour with no purge path.
            try:
                await db.rotate_old_events(_EVENT_RETENTION_DAYS)
                await db.purge_old_players(_PII_RETENTION_DAYS)
                await db.rotate_config_audit()
            except Exception as exc:
                log.warning("retention pass failed: %s", exc)
            log.info("Rolling snapshot complete: %s", path)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.error("Rolling snapshot failed: %s", exc)


def _prune_snapshots(output_dir: str, keep: int) -> None:
    """Delete oldest snapshots beyond the keep limit."""
    pattern = str(Path(output_dir) / "scanmania_*.db")
    # By mtime, not filename. A session recorded with the clock set ahead leaves
    # a future-named file that sorts last forever, permanently occupying a keep
    # slot while genuinely newer snapshots are deleted.
    files = sorted(glob.glob(pattern), key=os.path.getmtime)
    excess = files[: max(0, len(files) - keep)]
    for path in excess:
        try:
            os.remove(path)
            log.debug("Pruned old snapshot: %s", path)
        except OSError as exc:
            log.warning("Failed to prune snapshot %s: %s", path, exc)
