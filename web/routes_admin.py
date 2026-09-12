"""
web/routes_admin.py — admin portal API endpoints.

Inputs:  Authenticated requests (X-Admin-Password header) from the /admin frontend.
Outputs: system state, config changes, outbox control, run management, snapshots, logs.
Invariant: every config change is logged to config_audit. Auth is required for every
           write endpoint and for any read that discloses credentials or personal data
           (config files, CSV exports, DB snapshots, logs). Config mutations are
           serialised through _CONFIG_LOCK and written atomically with fsync.
"""

from __future__ import annotations

import asyncio
import csv
import hashlib
import hmac
import io as _io
import json
import logging
import os
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import yaml
from fastapi import APIRouter, Depends, HTTPException, Header, Query
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field

from core.events import ARM, COUNTDOWN, RUN_STATES
from persist.db import Database
from persist.sync import export_snapshot

# States where a config reload must not run. reload_config() builds a fresh
# PresetResolver whose desired state is all-off, and the reconciler then drives
# every laser dark within 500 ms. Import the real names — the earlier literal
# tuple used "RUN" and "HALTED", which are not states, so the guard failed open
# through the whole run.
_RELOAD_BLOCKED: frozenset[str] = frozenset({COUNTDOWN, ARM}) | RUN_STATES

log = logging.getLogger(__name__)

ENV_PASSWORD_KEY = "SCANMANIA_ADMIN_PASSWORD"

_CONFIG_FILES = {
    "game":     ("game.yaml",     "yaml"),
    "hardware": ("hardware.yaml", "yaml"),
    "mazes":    ("mazes.yaml",    "yaml"),
    "beams":    ("beams.json",    "json"),
}

_LOG_UNITS = frozenset({
    "scanmania-core", "scanmania-web", "scanmania-sync",
    "scanmania-io", "scanmania-vision", "scanmania-kiosk",
})

# Serialises every read-modify-write on a config file. Two concurrent beam-mask
# toggles would otherwise lose one update.
_CONFIG_LOCK = asyncio.Lock()

# Short-lived one-time tokens for downloads that cannot carry an auth header
# (browser navigations: CSV exports, DB snapshot, SSE log stream).
_DOWNLOAD_TOKEN_TTL_S = 60.0
_download_tokens: dict[str, float] = {}


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def _hash_password(pwd: str) -> str:
    return hashlib.sha256(pwd.encode()).hexdigest()


def _expected_hash() -> str:
    """
    Hash of the configured admin password.

    Fails closed: with no SCANMANIA_ADMIN_PASSWORD set there is no valid
    credential, rather than a shipped default that is public in the repo.
    """
    pwd = os.environ.get(ENV_PASSWORD_KEY)
    if not pwd:
        return ""
    return _hash_password(pwd)


def _password_ok(supplied: str | None) -> bool:
    expected = _expected_hash()
    if not expected:
        return False
    # Starlette decodes headers as latin-1, so a non-ASCII byte would make
    # compare_digest raise TypeError (a 500) instead of failing auth.
    supplied_bytes = (supplied or "").encode("utf-8", "ignore")
    return hmac.compare_digest(supplied_bytes, expected.encode("ascii"))


def _require_admin(x_admin_password: str | None = Header(default=None)) -> None:
    """Validate the X-Admin-Password header against the hashed env var."""
    if not _expected_hash():
        raise HTTPException(
            status_code=503,
            detail=f"{ENV_PASSWORD_KEY} is not set — admin API is disabled",
        )
    if not _password_ok(x_admin_password):
        raise HTTPException(status_code=403, detail="Forbidden")


def _issue_download_token() -> str:
    now = time.monotonic()
    for tok, expiry in list(_download_tokens.items()):
        if expiry < now:
            _download_tokens.pop(tok, None)
    token = secrets.token_urlsafe(32)
    _download_tokens[token] = now + _DOWNLOAD_TOKEN_TTL_S
    return token


def _require_download_token(token: str = Query(default="")) -> None:
    """Validate a one-time download token issued by POST /api/admin/download-token."""
    expiry = _download_tokens.pop(token, None)
    if expiry is None or expiry < time.monotonic():
        raise HTTPException(status_code=403, detail="Invalid or expired download token")


# ---------------------------------------------------------------------------
# Atomic config write
# ---------------------------------------------------------------------------

def _atomic_write(path: Path, content: str) -> None:
    """
    Write content to path atomically and durably.

    fsync on both the temp file and its directory: the container runs off a
    venue power drop, and a half-written beams.json takes detection down.
    """
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(content)
            fh.flush()
            os.fsync(fh.fileno())
        tmp.replace(path)
        dir_fd = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except OSError:
        tmp.unlink(missing_ok=True)
        raise


def _csv_safe(value: Any) -> Any:
    """Neutralise spreadsheet formula injection in free-text CSV fields."""
    if isinstance(value, str) and value[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + value
    return value


# ---------------------------------------------------------------------------
# Request bodies
# ---------------------------------------------------------------------------

class VoidBody(BaseModel):
    reason: str = Field(default="", max_length=200)
    voided: bool = Field(default=True)


class ConfigRawBody(BaseModel):
    content: str = Field(max_length=1_000_000)


class MaskBeamBody(BaseModel):
    masked: bool


# Upper bound on a global 1-indexed channel: 8 boards × 16 channels is well
# beyond the 3 boards actually installed, and the resolver rejects anything
# past the configured board count anyway.
_MAX_CHANNEL = 128


class SavePresetBody(BaseModel):
    name: str = Field(min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9_]+$")
    channels: list[int] = Field(default_factory=list, max_length=_MAX_CHANNEL)
    description: str = Field(default="", max_length=200)


class ShowStepBody(BaseModel):
    preset: str = Field(min_length=1, max_length=64)
    # Upper bound is a guard rail against a stray extra zero wedging the maze on
    # one step, not a design limit — the show player and loader have no cap.
    hold_ms: int = Field(default=300, ge=50, le=60_000)


class SaveShowBody(BaseModel):
    name: str = Field(min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9_]+$")
    steps: list[ShowStepBody] = Field(default_factory=list, max_length=64)
    loop: bool = Field(default=False)
    description: str = Field(default="", max_length=200)


class MasterPresetBody(BaseModel):
    preset: str = Field(max_length=64)


class HazerBody(BaseModel):
    enabled: bool | None = None
    haze: int | None = Field(default=None, ge=0, le=255)
    fan: int | None = Field(default=None, ge=0, le=255)


class ApplyChannelsBody(BaseModel):
    channels: list[int] = Field(default_factory=list, max_length=_MAX_CHANNEL)


class MasterChannelBody(BaseModel):
    board_id: str = Field(default="", max_length=32)
    channel: int = Field(ge=1, le=16)
    state: bool


class MasterStopwatchBody(BaseModel):
    action: str = Field(pattern=r"^(start|stop|reset)$")


class DevTriggerBody(BaseModel):
    event: str = Field(max_length=64)
    beam_id: str = Field(default="b001", max_length=16)
    ratio: float = Field(default=0.1)
    player_id: str = Field(default="dev-player", max_length=64)
    nickname: str = Field(default="Dev Player", max_length=30)


# ---------------------------------------------------------------------------
# Route registration
# ---------------------------------------------------------------------------

def register_routes(
    router: APIRouter,
    db: Database,
    app_ref: Any,  # ScanManiaApp instance — call app_ref.get_outbox() for the worker
    on_action: Callable[[str, dict], None],
    config_path: str,
    get_runner: Callable | None = None,
    fake_mode: bool = False,
) -> None:
    """Register all admin API routes."""

    cfg_dir = Path(config_path)

    def _get_outbox():
        """Get the outbox worker (may be None if not configured)."""
        return getattr(app_ref, "get_outbox", lambda: None)()

    def _runner():
        return get_runner() if get_runner else None

    async def _read_text(path: Path) -> str:
        """Read a file off the event loop — this loop also drives the 10 Hz broadcast."""
        return await asyncio.to_thread(path.read_text, encoding="utf-8")

    async def _write_config(path: Path, content: str) -> None:
        await asyncio.to_thread(_atomic_write, path, content)

    async def _load_yaml(path: Path) -> dict:
        """Parse a config YAML file, mapping malformed content to a 422."""
        try:
            data = yaml.safe_load(await _read_text(path))
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail=f"{path.name} not found")
        except yaml.YAMLError as exc:
            raise HTTPException(
                status_code=422, detail=f"{path.name} is not valid YAML: {exc}"
            ) from exc
        except OSError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        if data is None:
            return {}
        if not isinstance(data, dict):
            raise HTTPException(
                status_code=422, detail=f"{path.name} must contain a mapping at the top level"
            )
        return data

    async def _load_json(path: Path) -> dict:
        """Parse a config JSON file, mapping malformed content to a 422."""
        try:
            data = json.loads(await _read_text(path))
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail=f"{path.name} not found")
        except json.JSONDecodeError as exc:
            raise HTTPException(
                status_code=422, detail=f"{path.name} is not valid JSON: {exc}"
            ) from exc
        except OSError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        if not isinstance(data, dict):
            raise HTTPException(
                status_code=422, detail=f"{path.name} must contain a JSON object"
            )
        return data

    async def _reload_config() -> str | None:
        """
        Re-read all config files and swap them into the running system.

        Returns None on success, or a human-readable reason why the reload was
        skipped. Without this, every admin config write is a no-op until the
        service restarts.
        """
        runner = _runner()
        if runner is None:
            return "runner not available"
        if runner.state in _RELOAD_BLOCKED:
            return f"deferred — FSM is in {runner.state}; wait for the run to end"
        try:
            import config.loader as loader_module
            loader_module.CONFIG_DIR = cfg_dir
            new_cfg = await asyncio.to_thread(loader_module.load_all)
        except Exception as exc:
            log.error("Config reload failed: %s", exc)
            return f"reload failed: {exc}"
        await runner.reload_config(new_cfg)
        return None

    # ------------------------------------------------------------------
    # Status / dashboard
    # ------------------------------------------------------------------

    @router.post("/api/admin/login")
    async def admin_login(x_admin_password: str | None = Header(default=None)):
        """Validate the admin password. Returns 200 on success, 403 on failure."""
        if not _expected_hash():
            raise HTTPException(
                status_code=503,
                detail=f"{ENV_PASSWORD_KEY} is not set — admin API is disabled",
            )
        if not _password_ok(x_admin_password):
            raise HTTPException(status_code=403, detail="Invalid password")
        return {"ok": True}

    @router.post("/api/admin/download-token", dependencies=[Depends(_require_admin)])
    async def admin_download_token():
        """
        Mint a single-use, 60-second token for browser-navigation downloads.

        CSV exports, the DB snapshot and the SSE log stream are opened by the
        browser directly, so they cannot carry the X-Admin-Password header.
        """
        return {"ok": True, "token": _issue_download_token(), "ttl_s": _DOWNLOAD_TOKEN_TTL_S}

    @router.get(
        "/api/admin/status",
        dependencies=[Depends(_require_admin)],
    )
    async def admin_status():
        """Return state, uptime, run counts, outbox depth, and faults."""
        depth = await db.outbox_depth()
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        by_outcome = await db.count_runs_by_outcome(since_date=today)

        runner = _runner()
        runner_state = runner.state if runner else "unknown"
        uptime_s = int(time.monotonic() - runner.started_at_mono) if runner else None
        faults = runner.faults() if runner else []

        outbox = _get_outbox()
        last_push_mono = outbox.last_push_ok_at if outbox else None
        # last_push_ok_at is monotonic (correct for durations, meaningless as a
        # timestamp), so derive a wall-clock ISO value for display.
        last_push_iso = None
        if last_push_mono is not None:
            age_s = time.monotonic() - last_push_mono
            last_push_iso = datetime.fromtimestamp(
                time.time() - age_s, tz=timezone.utc
            ).isoformat()

        return {
            "ok": True,
            "state": runner_state,
            "uptime_s": uptime_s,
            "scope": "today",
            "runs_today": sum(by_outcome.values()),
            "clean": by_outcome.get("clean", 0),
            "busted": by_outcome.get("busted", 0),
            "aborted": by_outcome.get("aborted", 0),
            "voided": by_outcome.get("voided", 0),
            "outbox_depth": depth,
            "outbox_paused": outbox.is_paused if outbox else False,
            "last_push": last_push_mono,
            "last_push_iso": last_push_iso,
            "last_error": outbox.last_error if outbox else None,
            "faults": faults,
        }

    # ------------------------------------------------------------------
    # Runs
    #
    # /runs, /beams and /leaderboard are deliberately NOT admin-gated: the GM
    # console and the outdoor display consume them and have no password. Every
    # other admin read is gated, and every mutation is. Keep it that way — see
    # docs/security.md.
    # ------------------------------------------------------------------

    @router.get("/api/admin/runs")
    async def admin_runs(
        limit: int = Query(default=50, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
        q: str = Query(default="", max_length=100),
    ):
        runs = await db.list_runs(limit=limit, offset=offset, q=q)
        total = await db.count_runs(q=q)
        return {"ok": True, "runs": runs, "count": len(runs), "total": total}

    @router.get(
        "/api/admin/runs/{run_id}",
        dependencies=[Depends(_require_admin)],
    )
    async def admin_get_run(run_id: str):
        run = await db.get_run(run_id)
        if not run:
            raise HTTPException(status_code=404, detail="Run not found")
        events = await db.list_events(run_id, limit=500)
        beam_hits = await db.list_beam_hits(run_id)
        return {"ok": True, "run": run, "events": events, "beam_hits": beam_hits}

    @router.get(
        "/api/admin/runs/{run_id}/events",
        dependencies=[Depends(_require_admin)],
    )
    async def admin_run_events(run_id: str):
        events = await db.list_events(run_id, limit=500)
        return {"ok": True, "events": events}

    @router.post(
        "/api/admin/runs/{run_id}/unvoid",
        dependencies=[Depends(_require_admin)],
    )
    async def admin_unvoid_run(run_id: str):
        run = await db.get_run(run_id)
        if not run:
            raise HTTPException(status_code=404, detail="Run not found")
        if run.get("outcome") != "voided":
            raise HTTPException(status_code=400, detail="Run is not voided")
        restored = run.get("pre_void_outcome")
        if not restored:
            # Voided before the pre_void_outcome column existed. Guessing 'clean'
            # would silently promote a bust into the leaderboard, so refuse.
            raise HTTPException(
                status_code=409,
                detail="Original outcome for this run is unknown — cannot unvoid safely",
            )
        await db.update_run(
            run_id, outcome=restored, voided_reason=None, pre_void_outcome=None
        )
        await db.insert_config_audit(
            actor="admin",
            path=f"runs/{run_id}/unvoid",
            before={"outcome": "voided", "voided_reason": run.get("voided_reason")},
            after={"outcome": restored},
        )
        return {"ok": True, "run_id": run_id, "outcome": restored}

    @router.post(
        "/api/admin/runs/{run_id}/void",
        dependencies=[Depends(_require_admin)],
    )
    async def admin_void_run(run_id: str, body: VoidBody):
        run = await db.get_run(run_id)
        if not run:
            raise HTTPException(status_code=404, detail="Run not found")
        if body.voided:
            if not body.reason.strip():
                raise HTTPException(status_code=422, detail="A void reason is required")
            await db.void_run(run_id, body.reason)
        else:
            restored = run.get("pre_void_outcome")
            if not restored:
                raise HTTPException(
                    status_code=409,
                    detail="Original outcome for this run is unknown — cannot unvoid safely",
                )
            await db.update_run(
                run_id, outcome=restored, voided_reason=None, pre_void_outcome=None
            )
        await db.insert_config_audit(
            actor="admin",
            path=f"runs/{run_id}/void",
            before={"outcome": run["outcome"], "voided_reason": run.get("voided_reason")},
            after={"voided": body.voided, "reason": body.reason},
        )
        return {"ok": True, "run_id": run_id, "voided": body.voided}

    # ------------------------------------------------------------------
    # Leaderboard
    # ------------------------------------------------------------------

    @router.get("/api/admin/leaderboard")
    async def admin_leaderboard(
        scope: str = Query(default="daily", pattern=r"^(daily|activation|all)$"),
        limit: int = Query(default=50, ge=1, le=500),
    ):
        rows = await db.get_leaderboard(scope=scope, limit=limit)
        return {"ok": True, "scope": scope, "leaderboard": rows}

    # ------------------------------------------------------------------
    # CSV exports
    # ------------------------------------------------------------------

    @router.get(
        "/api/admin/export/runs.csv",
        dependencies=[Depends(_require_download_token)],
    )
    async def admin_runs_csv():
        runs = await db.list_runs(limit=10_000, offset=0)
        buf = _io.StringIO()
        fieldnames = [
            "id", "player_id", "started_at", "ended_at", "elapsed_ms",
            "outcome", "detection_mode", "busting_beam_id", "segment_reached",
            "voided_reason",
        ]
        writer = csv.DictWriter(buf, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for r in runs:
            writer.writerow({k: _csv_safe(v) for k, v in r.items()})
        return StreamingResponse(
            iter([buf.getvalue()]),
            media_type="text/csv",
            headers={"Content-Disposition": "attachment; filename=runs.csv"},
        )

    @router.get(
        "/api/admin/export/leaderboard.csv",
        dependencies=[Depends(_require_download_token)],
    )
    async def admin_leaderboard_csv():
        rows = await db.get_leaderboard(scope="all", limit=10_000)
        buf = _io.StringIO()
        fieldnames = ["rank", "player_nickname", "elapsed_ms", "started_at", "detection_mode", "segment_reached"]
        writer = csv.DictWriter(buf, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for i, r in enumerate(rows, 1):
            row = {k: _csv_safe(v) for k, v in r.items()}
            row["rank"] = i
            writer.writerow(row)
        return StreamingResponse(
            iter([buf.getvalue()]),
            media_type="text/csv",
            headers={"Content-Disposition": "attachment; filename=leaderboard.csv"},
        )

    # ------------------------------------------------------------------
    # Outbox
    # ------------------------------------------------------------------

    @router.get(
        "/api/admin/outbox",
        dependencies=[Depends(_require_admin)],
    )
    async def admin_outbox_status():
        depth = await db.outbox_depth()
        rows = await db.get_pending_outbox(limit=5)
        paused = _get_outbox().is_paused if _get_outbox() else False
        last_push = _get_outbox().last_push_ok_at if _get_outbox() else None
        last_error = _get_outbox().last_error if _get_outbox() else None
        return {
            "ok": True,
            "depth": depth,
            "paused": paused,
            "last_push_ok_at": last_push,
            "last_error": last_error,
            "pending_sample": rows,
        }

    @router.post("/api/admin/outbox/push", dependencies=[Depends(_require_admin)])
    async def admin_outbox_push():
        outbox = _get_outbox()
        if not outbox:
            raise HTTPException(status_code=503, detail="Outbox worker not available")
        pushed = await outbox.force_push()
        return {"ok": True, "pushed": pushed}

    @router.post("/api/admin/outbox/pause", dependencies=[Depends(_require_admin)])
    async def admin_outbox_pause():
        outbox = _get_outbox()
        if not outbox:
            raise HTTPException(status_code=503, detail="Outbox worker not available")
        await outbox.pause()
        return {"ok": True, "paused": True}

    @router.post("/api/admin/outbox/resume", dependencies=[Depends(_require_admin)])
    async def admin_outbox_resume():
        outbox = _get_outbox()
        if not outbox:
            raise HTTPException(status_code=503, detail="Outbox worker not available")
        await outbox.resume()
        return {"ok": True, "paused": False}

    @router.post("/api/admin/outbox/reset-backoff", dependencies=[Depends(_require_admin)])
    async def admin_outbox_reset_backoff():
        outbox = _get_outbox()
        if not outbox:
            raise HTTPException(status_code=503, detail="Outbox worker not available")
        await outbox.reset_backoff()
        return {"ok": True}

    @router.post("/api/admin/outbox/test", dependencies=[Depends(_require_admin)])
    async def admin_outbox_test():
        outbox = _get_outbox()
        if not outbox:
            raise HTTPException(status_code=503, detail="Outbox worker not available")
        result = await outbox.test_endpoint()
        return {"ok": True, "result": result}

    # ------------------------------------------------------------------
    # Snapshots
    # ------------------------------------------------------------------

    @router.get(
        "/api/admin/snapshot",
        dependencies=[Depends(_require_download_token)],
    )
    async def admin_snapshot():
        import tempfile, sys
        if sys.platform == "darwin":
            backup_dir = os.path.join(tempfile.gettempdir(), "scanmania-backups")
        else:
            backup_dir = "/var/backups/scanmania"
        try:
            db_path = await export_snapshot(db, backup_dir)
        except OSError as exc:
            raise HTTPException(
                status_code=503,
                detail=f"Backup directory {backup_dir} is not writable: {exc}",
            ) from exc
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        try:
            from persist.sync import _prune_snapshots
            await asyncio.to_thread(_prune_snapshots, backup_dir, 14)
        except Exception as exc:
            log.debug("Snapshot prune skipped: %s", exc)
        return FileResponse(
            db_path,
            media_type="application/octet-stream",
            filename=os.path.basename(db_path),
        )

    # ------------------------------------------------------------------
    # Logs
    #
    # /logs/stream MUST be registered before /logs/{unit}: Starlette matches in
    # registration order, so the path parameter would otherwise swallow "stream".
    # ------------------------------------------------------------------

    @router.get(
        "/api/admin/logs/stream",
        dependencies=[Depends(_require_download_token)],
    )
    async def admin_logs_stream(
        unit: str = Query(...),
        filter_text: str = Query(default="", alias="filter", max_length=100),
        lines: int = Query(default=100, ge=1, le=500),
    ):
        """SSE stream of journalctl output for the given unit."""
        if unit not in _LOG_UNITS:
            raise HTTPException(status_code=400, detail=f"Unknown unit: {unit!r}")

        filt = filter_text.lower()

        async def event_stream():
            proc = None
            try:
                proc = await asyncio.create_subprocess_exec(
                    "journalctl", "-u", unit, "-n", str(lines), "-f",
                    "--no-pager", "--output=short",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                async for raw in proc.stdout:
                    text = raw.decode("utf-8", errors="replace").rstrip()
                    if filt and filt not in text.lower():
                        continue
                    yield f"data: {text}\n\n"
            except FileNotFoundError:
                yield "data: (journalctl not available on this host)\n\n"
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                yield f"data: (error: {exc})\n\n"
            finally:
                if proc is not None and proc.returncode is None:
                    try:
                        proc.terminate()
                        await proc.wait()
                    except Exception:
                        pass

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @router.get("/api/admin/logs/{unit}", dependencies=[Depends(_require_admin)])
    async def admin_logs(unit: str, lines: int = Query(default=200, ge=1, le=2000)):
        if unit not in _LOG_UNITS:
            raise HTTPException(status_code=400, detail=f"Unknown unit: {unit!r}")
        try:
            proc = await asyncio.create_subprocess_exec(
                "journalctl", "-u", unit, "-n", str(lines), "--no-pager", "--output=short",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except FileNotFoundError:
            return {"ok": True, "unit": unit, "lines": "(journalctl not available on this host)"}
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
        except asyncio.TimeoutError:
            proc.kill()
            raise HTTPException(status_code=504, detail="journalctl timed out")
        return {"ok": True, "unit": unit, "lines": stdout.decode("utf-8", errors="replace")}

    # ------------------------------------------------------------------
    # Hardware
    # ------------------------------------------------------------------

    @router.get(
        "/api/admin/hardware",
        dependencies=[Depends(_require_admin)],
    )
    async def admin_hardware():
        """Return relay board states from the runner's cached hardware readback."""
        hw = await _load_yaml(cfg_dir / "hardware.yaml")

        runner = _runner()
        cached = runner._board_states if runner else {}
        io_backend = getattr(runner, "io", None) if runner else None
        reconciler = getattr(runner, "reconciler", None) if runner else None
        mismatches = getattr(reconciler, "mismatch_counts", {}) or {}

        boards = []
        for b in hw.get("relay_boards", []) or []:
            board_id = b.get("id")
            if not board_id:
                log.warning("hardware.yaml: relay board entry without an 'id' — skipping")
                continue
            state = cached.get(board_id, {})
            error_count = None
            if io_backend is not None:
                try:
                    error_count = getattr(io_backend.get_board(board_id), "error_count", None)
                except Exception:
                    error_count = None
            boards.append({
                "id": board_id,
                "ip": b.get("ip", ""),
                "hostname": b.get("hostname", ""),
                "status": state.get("status", "?"),
                "rtt_ms": state.get("rtt_ms"),
                "error_count": error_count,
                "mismatch_count": mismatches.get(board_id, 0),
                "coils": state.get("coils", [False] * b.get("channels", 16)),
            })

        vision = getattr(runner, "vision", None) if runner else None
        cameras = []
        for c in hw.get("cameras", []) or []:
            cam_id = c.get("id")
            if not cam_id:
                continue
            stats = {}
            if vision is not None and hasattr(vision, "camera_stats"):
                try:
                    stats = vision.camera_stats(cam_id) or {}
                except Exception:
                    stats = {}
            cameras.append({
                "id": cam_id,
                "fps": stats.get("fps"),
                "stall_count": stats.get("stall_count", 0),
            })

        inputs = getattr(runner, "inputs", None) if runner else None
        opta = {
            "connected": bool(getattr(inputs, "is_connected", False)) if inputs else False,
            "ip": getattr(inputs, "_ip", None) if inputs else None,
        }
        return {"ok": True, "boards": boards, "cameras": cameras, "opta": opta}

    # ------------------------------------------------------------------
    # Beams
    # ------------------------------------------------------------------

    @router.get("/api/admin/beams")
    async def admin_beams():
        """
        Return the full beam list from beams.json.

        Beams the system auto-masked live in runner.context.beams_masked and are
        never written to disk, so they are merged in here — otherwise the admin
        list omits exactly the beams that were masked automatically.
        """
        data = await _load_json(cfg_dir / "beams.json")
        beams = data.get("beams", [])
        runner = _runner()
        auto_masked = set(runner.context.beams_masked) if runner else set()
        for b in beams:
            if b.get("id") in auto_masked and not b.get("masked"):
                b["masked"] = True
                b["masked_reason"] = "auto"
        return {"ok": True, "beams": beams, "auto_masked": sorted(auto_masked)}

    @router.post("/api/admin/beams/{beam_id}/mask", dependencies=[Depends(_require_admin)])
    async def admin_beam_mask(beam_id: str, body: MaskBeamBody):
        """Toggle the masked flag on a single beam in beams.json."""
        path = cfg_dir / "beams.json"
        async with _CONFIG_LOCK:
            data = await _load_json(path)
            beam = next(
                (b for b in data.get("beams", []) if b.get("id") == beam_id), None
            )
            if not beam:
                raise HTTPException(status_code=404, detail=f"Beam {beam_id!r} not found")
            old_masked = beam.get("masked", False)
            beam["masked"] = body.masked
            beam["masked_at"] = (
                datetime.now(timezone.utc).isoformat() if body.masked else None
            )
            beam["masked_reason"] = "admin" if body.masked else None
            try:
                await _write_config(path, json.dumps(data, indent=2))
            except OSError as exc:
                raise HTTPException(status_code=500, detail=str(exc)) from exc

        # Push the change into the live system so masking takes effect now.
        runner = _runner()
        if runner:
            if body.masked:
                runner.context.beams_masked.add(beam_id)
            else:
                runner.context.beams_masked.discard(beam_id)
            vision = getattr(runner, "vision", None)
            if vision is not None and hasattr(vision, "set_masked"):
                try:
                    vision.set_masked(beam_id, body.masked)
                except Exception as exc:
                    log.warning("vision.set_masked(%s) failed: %s", beam_id, exc)

        await db.insert_config_audit(
            actor="admin", path=f"beams/{beam_id}/mask",
            before={"masked": old_masked}, after={"masked": body.masked},
        )
        return {"ok": True, "beam_id": beam_id, "masked": body.masked}

    @router.post("/api/admin/beams/unmask_all", dependencies=[Depends(_require_admin)])
    async def admin_beams_unmask_all():
        """Clear the masked flag on every beam in beams.json."""
        path = cfg_dir / "beams.json"
        async with _CONFIG_LOCK:
            data = await _load_json(path)
            for b in data.get("beams", []):
                b["masked"] = False
                b["masked_at"] = None
                b["masked_reason"] = None
            try:
                await _write_config(path, json.dumps(data, indent=2))
            except OSError as exc:
                raise HTTPException(status_code=500, detail=str(exc)) from exc

        runner = _runner()
        if runner:
            runner.context.beams_masked.clear()
            vision = getattr(runner, "vision", None)
            if vision is not None and hasattr(vision, "clear_masks"):
                try:
                    vision.clear_masks()
                except Exception as exc:
                    log.warning("vision.clear_masks() failed: %s", exc)

        await db.insert_config_audit(
            actor="admin", path="beams/unmask_all",
            before={"note": "all beams"}, after={"masked": False},
        )
        return {"ok": True, "unmasked": len(data.get("beams", []))}

    # ------------------------------------------------------------------
    # Presets
    # ------------------------------------------------------------------

    def _max_channel(hw: dict) -> int:
        boards = hw.get("relay_boards") or []
        return max(1, len(boards)) * 16

    @router.get(
        "/api/admin/presets",
        dependencies=[Depends(_require_admin)],
    )
    async def admin_presets():
        """Return named presets from mazes.yaml."""
        data = await _load_yaml(cfg_dir / "mazes.yaml")
        presets = {
            name: {"channels": p.get("channels"), "description": p.get("description", "")}
            for name, p in (data.get("presets") or {}).items()
        }
        return {"ok": True, "presets": presets}

    @router.post("/api/admin/presets", dependencies=[Depends(_require_admin)])
    async def admin_save_preset(body: SavePresetBody):
        """Save or update a preset in mazes.yaml."""
        path = cfg_dir / "mazes.yaml"
        hw = await _load_yaml(cfg_dir / "hardware.yaml")
        ceiling = _max_channel(hw)
        rejected = [c for c in body.channels if c < 1 or c > ceiling]
        if rejected:
            raise HTTPException(
                status_code=422,
                detail=f"Channels out of range 1..{ceiling}: {rejected}",
            )

        async with _CONFIG_LOCK:
            data = await _load_yaml(path)
            data.setdefault("presets", {})
            old = data["presets"].get(body.name)
            data["presets"][body.name] = {
                "channels": sorted(set(body.channels)),
                "description": body.description,
            }
            try:
                await _write_config(
                    path, yaml.dump(data, default_flow_style=False, sort_keys=False)
                )
            except OSError as exc:
                raise HTTPException(status_code=500, detail=str(exc)) from exc

        reload_note = await _reload_config()
        await db.insert_config_audit(
            actor="admin", path=f"presets/{body.name}",
            before={"preset": old}, after={"channels": body.channels},
        )
        return {
            "ok": True, "name": body.name,
            "channels": body.channels, "reload_note": reload_note,
        }

    @router.get(
        "/api/admin/shows",
        dependencies=[Depends(_require_admin)],
    )
    async def admin_shows():
        """Return named shows from mazes.yaml so the editor can load them."""
        data = await _load_yaml(cfg_dir / "mazes.yaml")
        shows = {
            name: {
                "description": s.get("description", ""),
                "steps": s.get("steps", []),
                "loop": bool(s.get("loop", False)),
            }
            for name, s in (data.get("shows") or {}).items()
        }
        return {"ok": True, "shows": shows}

    @router.post("/api/admin/shows", dependencies=[Depends(_require_admin)])
    async def admin_save_show(body: SaveShowBody):
        """Save or update a show in mazes.yaml."""
        path = cfg_dir / "mazes.yaml"
        async with _CONFIG_LOCK:
            data = await _load_yaml(path)
            known_presets = set((data.get("presets") or {}).keys())
            missing = sorted(
                {s.preset for s in body.steps} - known_presets
            )
            if missing:
                # Writing these would make load_all() raise on the next boot.
                raise HTTPException(
                    status_code=422,
                    detail=f"Unknown preset(s) referenced by this show: {missing}",
                )
            data.setdefault("shows", {})
            old = data["shows"].get(body.name) or {}
            data["shows"][body.name] = {
                # Preserve the existing description when the client doesn't send one.
                "description": body.description or old.get("description", ""),
                "steps": [
                    {"preset": s.preset, "hold_ms": s.hold_ms} for s in body.steps
                ],
                "loop": body.loop,
            }
            try:
                await _write_config(
                    path, yaml.dump(data, default_flow_style=False, sort_keys=False)
                )
            except OSError as exc:
                raise HTTPException(status_code=500, detail=str(exc)) from exc

        reload_note = await _reload_config()
        await db.insert_config_audit(
            actor="admin", path=f"shows/{body.name}",
            before={"show": old}, after={"steps": len(body.steps), "loop": body.loop},
        )
        return {
            "ok": True, "name": body.name,
            "steps": len(body.steps), "reload_note": reload_note,
        }

    # ------------------------------------------------------------------
    # Hazer
    # ------------------------------------------------------------------

    @router.get(
        "/api/admin/hazer",
        dependencies=[Depends(_require_admin)],
    )
    async def admin_hazer_status():
        hazer = getattr(app_ref, "get_hazer", lambda: None)()
        if not hazer:
            return {"ok": True, "available": False}
        return {
            "ok": True,
            "available": True,
            "enabled": hazer.enabled,
            "haze": hazer.haze,
            "fan": hazer.fan,
            # Drives the SM-NODE-DMX LED on the GM console.
            "link_ok": hazer.link_ok,
        }

    async def _apply_hazer(body: HazerBody) -> dict:
        hazer = getattr(app_ref, "get_hazer", lambda: None)()
        if not hazer:
            raise HTTPException(status_code=503, detail="Hazer not available")
        if body.enabled is not None:
            hazer.set_enabled(body.enabled)
        if body.haze is not None:
            hazer.set_haze(body.haze)
        if body.fan is not None:
            hazer.set_fan(body.fan)
        return {"ok": True, "enabled": hazer.enabled, "haze": hazer.haze, "fan": hazer.fan}

    @router.post("/api/admin/hazer", dependencies=[Depends(_require_admin)])
    async def admin_hazer_set(body: HazerBody):
        return await _apply_hazer(body)

    # GM-facing hazer control. Deliberately unauthenticated, consistent with the
    # rest of /api/gm/* — the GM console is the operator tablet and has no
    # password. The /api/admin/hazer equivalents above stay admin-gated.
    @router.get("/api/gm/hazer")
    async def gm_hazer_status():
        return await admin_hazer_status()

    @router.post("/api/gm/hazer")
    async def gm_hazer_set(body: HazerBody):
        return await _apply_hazer(body)

    @router.post("/api/gm/hazer-toggle")
    async def gm_hazer_toggle():
        hazer = getattr(app_ref, "get_hazer", lambda: None)()
        if not hazer:
            raise HTTPException(status_code=503, detail="Hazer not available")
        hazer.set_enabled(not hazer.enabled)
        return {"ok": True, "enabled": hazer.enabled, "haze": hazer.haze, "fan": hazer.fan}

    # ------------------------------------------------------------------
    # Master mode
    # ------------------------------------------------------------------

    @router.post("/api/admin/master/engage", dependencies=[Depends(_require_admin)])
    async def admin_master_engage():
        on_action("master_engage", {})
        return {"ok": True}

    @router.post("/api/admin/master/exit", dependencies=[Depends(_require_admin)])
    async def admin_master_exit():
        on_action("master_exit", {})
        return {"ok": True}

    def _master_runner():
        """
        Return the runner, or raise 503.

        Master operations are awaited (not fire-and-forget) so that a rejected
        state, an unknown board or a dead relay surfaces as an HTTP error rather
        than a log line under an `ok: true` response.
        """
        runner = _runner()
        if runner is None:
            raise HTTPException(status_code=503, detail="Runner not available")
        return runner

    def _master_error(exc: Exception) -> HTTPException:
        from core.runner import MasterModeRequired
        if isinstance(exc, MasterModeRequired):
            return HTTPException(status_code=409, detail=str(exc))
        if isinstance(exc, KeyError):
            return HTTPException(status_code=404, detail=f"Unknown: {exc}")
        if isinstance(exc, ValueError):
            return HTTPException(status_code=422, detail=str(exc))
        return HTTPException(status_code=502, detail=f"Relay write failed: {exc}")

    @router.post("/api/admin/master/apply_preset", dependencies=[Depends(_require_admin)])
    async def admin_master_apply_preset(body: MasterPresetBody):
        runner = _master_runner()
        try:
            await runner._master_apply_preset(body.preset)
        except Exception as exc:
            raise _master_error(exc) from exc
        return {"ok": True, "preset": body.preset}

    @router.post("/api/admin/master/toggle_channel", dependencies=[Depends(_require_admin)])
    async def admin_master_toggle_channel(body: MasterChannelBody):
        runner = _master_runner()
        try:
            await runner._master_toggle_channel(body.board_id, body.channel, body.state)
        except Exception as exc:
            raise _master_error(exc) from exc
        return {"ok": True, "channel": body.channel, "state": body.state}

    @router.post("/api/admin/master/stopwatch", dependencies=[Depends(_require_admin)])
    async def admin_master_stopwatch(body: MasterStopwatchBody):
        runner = _master_runner()
        try:
            runner._master_stopwatch(body.action)
        except Exception as exc:
            raise _master_error(exc) from exc
        return {"ok": True, "action": body.action}

    @router.post("/api/admin/master/apply_channels", dependencies=[Depends(_require_admin)])
    async def admin_master_apply_channels(body: ApplyChannelsBody):
        """Apply a raw channel list directly to hardware (no preset name needed)."""
        runner = _master_runner()
        try:
            rejected = await runner._master_apply_channels(body.channels)
        except Exception as exc:
            raise _master_error(exc) from exc
        if rejected:
            raise HTTPException(
                status_code=422,
                detail=f"Channels out of range for the configured boards: {rejected}",
            )
        return {"ok": True, "channels": len(body.channels)}

    # ------------------------------------------------------------------
    # Dev — inject FSM events for testing (--fake-all mode only)
    # ------------------------------------------------------------------

    @router.post("/api/admin/dev/trigger", dependencies=[Depends(_require_admin)])
    async def admin_dev_trigger(body: DevTriggerBody):
        if not fake_mode:
            # On the live floor this would inject StopPressed or BreakConfirmed
            # straight into a real player's run.
            raise HTTPException(
                status_code=404,
                detail="Dev triggers are only available in --fake-all mode",
            )
        on_action("dev_trigger", body.model_dump())
        return {"ok": True, "event": body.event}

    @router.get("/api/admin/dev/available")
    async def admin_dev_available():
        """Lets the frontend hide the Dev tab outside fake mode."""
        return {"ok": True, "available": fake_mode}

    # ------------------------------------------------------------------
    # Config — audit log (must be registered BEFORE the {file_id} route)
    # ------------------------------------------------------------------

    @router.get(
        "/api/admin/config/audit",
        dependencies=[Depends(_require_admin)],
    )
    async def admin_config_audit(limit: int = Query(default=50, ge=1, le=500)):
        """Return the most recent config_audit entries."""
        rows = await db.list_config_audit(limit=limit)
        return {"ok": True, "entries": rows}

    # ------------------------------------------------------------------
    # Config — read/write all 4 config files as raw text
    # ------------------------------------------------------------------

    @router.get("/api/admin/config/{file_id}", dependencies=[Depends(_require_admin)])
    async def admin_config_read(file_id: str):
        """
        Return the raw text content of a config file.

        Admin-gated: hardware.yaml carries relay board IPs and camera RTSP URLs,
        which typically embed credentials.

        file_id: one of game | hardware | mazes | beams
        """
        if file_id == "audit" or file_id not in _CONFIG_FILES:
            raise HTTPException(
                status_code=404,
                detail=f"Unknown config file '{file_id}'. Valid: {list(_CONFIG_FILES)}",
            )
        filename, _ = _CONFIG_FILES[file_id]
        path = cfg_dir / filename
        if not path.exists():
            raise HTTPException(status_code=404, detail=f"{filename} not found")
        return {"ok": True, "file": filename, "content": await _read_text(path)}

    def _validate_config_set(candidates: dict[str, str]) -> None:
        """
        Run the real loader over a candidate config set in a temp directory.

        A syntax check alone lets an admin save a file that parses but fails
        config/loader.py's cross-validation — and that failure only surfaces as
        a service that won't come back up.
        """
        import tempfile
        import config.loader as loader_module

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            for fid, (filename, _fmt) in _CONFIG_FILES.items():
                src = cfg_dir / filename
                content = candidates.get(fid)
                if content is None:
                    content = src.read_text(encoding="utf-8") if src.exists() else ""
                (tmp_path / filename).write_text(content, encoding="utf-8")

            original = loader_module.CONFIG_DIR
            try:
                loader_module.CONFIG_DIR = tmp_path
                loader_module.load_all()
            finally:
                loader_module.CONFIG_DIR = original

    @router.post("/api/admin/config/{file_id}", dependencies=[Depends(_require_admin)])
    async def admin_config_write(file_id: str, body: ConfigRawBody):
        """
        Write raw text to a config file.

        Validates the content both syntactically and against config/loader.py's
        cross-file rules before writing, then reloads the running config.
        Logs the change to config_audit.
        """
        if file_id == "audit" or file_id not in _CONFIG_FILES:
            raise HTTPException(
                status_code=404,
                detail=f"Unknown config file '{file_id}'. Valid: {list(_CONFIG_FILES)}",
            )
        filename, fmt = _CONFIG_FILES[file_id]
        path = cfg_dir / filename

        try:
            if fmt == "yaml":
                yaml.safe_load(body.content)
            else:
                json.loads(body.content)
        except Exception as exc:
            raise HTTPException(
                status_code=422, detail=f"Invalid {fmt.upper()}: {exc}"
            ) from exc

        try:
            await asyncio.to_thread(_validate_config_set, {file_id: body.content})
        except Exception as exc:
            raise HTTPException(
                status_code=422,
                detail=f"Config validation failed: {exc}",
            ) from exc

        async with _CONFIG_LOCK:
            before = await _read_text(path) if path.exists() else ""
            try:
                await _write_config(path, body.content)
            except OSError as exc:
                raise HTTPException(status_code=500, detail=str(exc)) from exc

        reload_note = await _reload_config()
        await db.insert_config_audit(
            actor="admin",
            path=f"config/{filename}",
            before={"content": before},
            after={"content": body.content},
        )
        log.info("Config %s written by admin (%d bytes)", filename, len(body.content))
        return {
            "ok": True, "file": filename,
            "bytes_written": len(body.content), "reload_note": reload_note,
        }
