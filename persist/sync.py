"""
persist/sync.py — snapshot exports and rolling local backups.

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


def _timestamp_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


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
                    "nickname": row.get("nickname", ""),
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
    while True:
        await asyncio.sleep(interval_min * 60)
        try:
            path = await export_snapshot(db, output_dir)
            _prune_snapshots(output_dir, keep)
            log.info("Rolling snapshot complete: %s", path)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.error("Rolling snapshot failed: %s", exc)


def _prune_snapshots(output_dir: str, keep: int) -> None:
    """Delete oldest snapshots beyond the keep limit."""
    pattern = str(Path(output_dir) / "scanmania_*.db")
    files = sorted(glob.glob(pattern))  # ISO timestamps sort lexicographically
    excess = files[: max(0, len(files) - keep)]
    for path in excess:
        try:
            os.remove(path)
            log.debug("Pruned old snapshot: %s", path)
        except OSError as exc:
            log.warning("Failed to prune snapshot %s: %s", path, exc)
