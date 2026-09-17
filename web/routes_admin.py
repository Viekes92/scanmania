"""
web/routes_admin.py — admin portal API endpoints.

Inputs:  Authenticated requests (X-Admin-Password header) from the /admin frontend.
Outputs: system state, config changes, run management, snapshots, logs.
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
import re
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import yaml
from fastapi import APIRouter, Depends, HTTPException, Header, Query, Request
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field

from core.events import ARM, COUNTDOWN, RUN_STATES
import persist.db as db_module
from persist.db import Database
from persist.backup import export_snapshot

# States where a config reload must not run. reload_config() builds a fresh
# PresetResolver whose desired state is all-off, and the reconciler then drives
# every laser dark within 500 ms. Import the real names — the earlier literal
# tuple used "RUN" and "HALTED", which are not states, so the guard failed open
# through the whole run.
_RELOAD_BLOCKED: frozenset[str] = frozenset({COUNTDOWN, ARM}) | RUN_STATES

from web.ratelimit import allow, retry_after

log = logging.getLogger(__name__)

# Slow a brute-force to a crawl without locking out a fat-fingered operator.
_LOGIN_LIMIT = 8
_LOGIN_WINDOW_S = 60.0

ENV_PASSWORD_KEY = "SCANMANIA_ADMIN_PASSWORD"

_CONFIG_FILES = {
    "game":     ("game.yaml",     "yaml"),
    "hardware": ("hardware.yaml", "yaml"),
    "mazes":    ("mazes.yaml",    "yaml"),
    "beams":    ("beams.json",    "json"),
}

# The units that actually exist on the box.
#
# This still listed scanmania-core / -web / -io / -vision, from the design
# before the services were consolidated into one. journalctl -u scanmania-core
# exits 0 with no output, so the route returned {"ok": true, "lines": ""} and
# the panel drew an empty box with no error — the log viewer looked broken in
# the one way that gives you nothing to go on.
_LOG_UNITS = frozenset({
    "scanmania",          # the game: FSM, vision, io, web — all of it
    "scanmania-kiosk",    # the two display browsers
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


# Failed admin auths per client address before it is throttled. Generous
# enough that an operator mistyping a password is never locked out, tight
# enough that guessing is hopeless.
# Mirrors iobackend.lightshow._DARK_STATES. Duplicated deliberately: the web
# layer must be able to refuse without importing the DMX stack.
_LIGHTS_FORCED_DARK = frozenset({
    "COUNTDOWN", "RUN_SEG_1", "RUN_SEG_2", "RUN_SEG_3",
})

_AUTH_FAIL_LIMIT = 10
_AUTH_FAIL_WINDOW_S = 60.0


def _require_admin(request: Request,
                   x_admin_password: str | None = Header(default=None)) -> None:
    """
    Validate the X-Admin-Password header against the hashed env var.

    Rate-limited and logged on failure. Only /api/admin/login was throttled, so
    an attacker skipped it and brute-forced any other gated route — a shared,
    human-memorable password, guessed as fast as the NUC could answer, with
    nothing in the journal to show for it. Success there is config write,
    master-mode relay control and the shutdown endpoint.
    """
    if not _expected_hash():
        raise HTTPException(
            status_code=503,
            detail=f"{ENV_PASSWORD_KEY} is not set — admin API is disabled",
        )
    if _password_ok(x_admin_password):
        return

    who = request.client.host if request.client else "unknown"
    log.warning("admin auth failed from %s for %s", who, request.url.path)
    if not allow("admin_auth", who, _AUTH_FAIL_LIMIT, _AUTH_FAIL_WINDOW_S):
        raise HTTPException(
            status_code=429,
            detail="too many failed attempts — wait before trying again")
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


_SECRET_LINE = re.compile(
    r"(?i)(rtsp://[^:\s]+:)([^@\s]+)(@)|((?:password|passwd|secret|token)\s*[:=]\s*)(\S+)"
)


def _audit_body(filename: str, content: str) -> str:
    """
    Redact credentials before a config file goes into the audit table.

    The audit row stores the whole file, and every one of the 48 rolling DB
    snapshots then contains it — so the camera RTSP passwords escaped a 0600
    config file into 48 copies under /var/backups. Plaintext in the config
    itself is a deliberate decision; multiplying it by 48 is not.
    """
    if not content:
        return content
    return _SECRET_LINE.sub(
        lambda m: (m.group(1) + "***" + m.group(3)) if m.group(1)
        else (m.group(4) + "***"),
        content,
    )


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


class LightsBody(BaseModel):
    """Either on= for all maze lights, or name= + level= for one fixture."""
    on: bool | None = None
    name: str | None = None
    level: int | None = Field(default=None, ge=0, le=255)


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
    # Carried through, not editable here. The editor has no sparkle control, so
    # without this a save of ANY show rewrote its steps as preset+hold_ms only
    # and silently deleted the attract sparkle.
    sparkle_off: int = Field(default=0, ge=0, le=45)


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


class AudioBody(BaseModel):
    """Mute the soundtrack, or audition one file through the real device."""
    muted: bool | None = None
    play: str | None = Field(default=None, max_length=120)


class RecalibrateBody(BaseModel):
    """Re-find the dot ROIs. Dry run unless apply is set."""
    apply: bool = Field(default=False)
    mazes: list[str] | None = Field(default=None)


class ShutdownBody(BaseModel):
    """End-of-day shutdown. `confirm` must be the literal string SHUTDOWN.

    A typed confirmation rather than a bare POST: this darkens a container that
    may still have people in it, and it is one request away from cutting mains.
    """
    confirm: str = Field(max_length=32)
    poweroff: bool = Field(default=True)


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
    app_ref: Any,  # ScanManiaApp instance
    on_action: Callable[[str, dict], None],
    config_path: str,
    get_runner: Callable | None = None,
    fake_mode: bool = False,
) -> None:
    """Register all admin API routes."""

    cfg_dir = Path(config_path)

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
    async def admin_login(request: Request,
                          x_admin_password: str | None = Header(default=None)):
        """
        Validate the admin password. 200 on success, 403 on failure.

        Rate limited and logged. It was neither: unlimited unauthenticated
        attempts from the venue wifi against one shared password over two
        months, and a successful break-in left no trace because failures were
        not recorded at all.
        """
        if not _expected_hash():
            raise HTTPException(
                status_code=503,
                detail=f"{ENV_PASSWORD_KEY} is not set — admin API is disabled",
            )
        client = request.client.host if request.client else "unknown"
        if not allow("admin_login", client, _LOGIN_LIMIT, _LOGIN_WINDOW_S):
            log.warning("admin login: rate limited %s", client)
            raise HTTPException(
                status_code=429, detail="Too many attempts.",
                headers={"Retry-After": str(retry_after("admin_login", client,
                                                        _LOGIN_WINDOW_S))},
            )
        if not _password_ok(x_admin_password):
            log.warning("admin login FAILED from %s", client)
            raise HTTPException(status_code=403, detail="Invalid password")
        log.info("admin login OK from %s", client)
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
        """Return state, uptime, run counts, and faults."""
        # The operating day, not the UTC calendar day. These counters rolled
        # at UTC midnight = 02:00 local in summer — mid-session on a late slot,
        # which is the exact failure SCANMANIA_DAY_START_HOUR was introduced to
        # fix — leaving the dashboard disagreeing with the Leaderboard tab on
        # the same page.
        from persist.db import day_bounds
        today = day_bounds()[0][:10]
        by_outcome = await db.count_runs_by_outcome(since_date=today)

        runner = _runner()
        runner_state = runner.state if runner else "unknown"
        uptime_s = int(time.monotonic() - runner.started_at_mono) if runner else None
        faults = runner.faults() if runner else []

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
    # Snapshots
    # ------------------------------------------------------------------

    @router.get(
        "/api/admin/snapshot",
        dependencies=[Depends(_require_download_token)],
    )
    async def admin_snapshot():
        from persist.backup import snapshot_dir
        backup_dir = snapshot_dir()
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
            from persist.backup import _prune_snapshots
            # The SAME retention the rolling loop uses. This hardcoded 14 while
            # the loop passes snapshot_keep (48), on the same directory with the
            # same glob — so taking a manual snapshot, the most safety-conscious
            # thing an operator can do, silently deleted 34 of them.
            runner = _runner()
            keep = 48
            if runner is not None and getattr(runner, "config", None) is not None:
                keep = getattr(runner.config.game, "snapshot_keep", 48)
            await asyncio.to_thread(_prune_snapshots, backup_dir, keep)
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
        text = stdout.decode("utf-8", errors="replace")
        if not text.strip():
            # Silence here is ambiguous — no logs, or a unit that does not
            # exist? Say which, rather than drawing an empty box.
            text = (f"(journalctl returned nothing for {unit!r} — the unit has "
                    f"logged nothing in this range, or it does not exist on "
                    f"this host)")
        return {"ok": True, "unit": unit, "lines": text}

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

    @router.get("/api/admin/mazes")
    async def admin_mazes():
        """
        Per-maze calibration summary: what each camera saw when that shape was
        lit. This is what detection actually watches; the 45 entries under
        /api/admin/beams are the relay wiring record and nothing reads them at
        runtime.

        Unauthenticated on purpose, like /api/admin/beams — the passwordless GM
        console reads it too.
        """
        data = await _load_json(cfg_dir / "beams.json")
        out = []
        for name, maze in sorted((data.get("mazes") or {}).items()):
            cams = []
            for cid, cap in sorted((maze.get("cameras") or {}).items()):
                dots = cap.get("dots") or []
                lit = [d for d in dots if not d.get("masked")]
                baselines = [d.get("baseline", 0.0) for d in lit]
                cams.append({
                    "camera": cid,
                    "dots": len(dots),
                    "masked": len(dots) - len(lit),
                    "w": cap.get("w", 0), "h": cap.get("h", 0),
                    "params": cap.get("params") or {},
                    "mean_baseline": round(sum(baselines) / len(baselines), 1)
                                     if baselines else 0.0,
                    "min_baseline": round(min(baselines), 1) if baselines else 0.0,
                })
            out.append({
                "maze": name,
                "captured_at": maze.get("captured_at", ""),
                "total": sum(c["dots"] for c in cams),
                "cameras": cams,
            })
        return {"ok": True, "mazes": out}

    @router.get(
        "/api/admin/export/day.csv",
        dependencies=[Depends(_require_download_token)],
    )
    async def admin_export_day(day: str | None = Query(default=None)):
        """
        End-of-day export: every run of one operating day, with player details.

        Opened by browser navigation, so it authenticates with a single-use
        download token like the other exports rather than a header.

        `day` is an ISO date (YYYY-MM-DD) selecting a past operating day;
        omitted means today. The operating day rolls over at
        SCANMANIA_DAY_START_HOUR (default 09:00 local), so a session running
        past midnight stays on one export.
        """
        if day:
            try:
                base = datetime.strptime(day, "%Y-%m-%d").astimezone()
            except ValueError as exc:
                raise HTTPException(status_code=400,
                                    detail="day must be YYYY-MM-DD") from exc
            start, end = db_module.day_bounds(
                base.replace(hour=db_module.DAY_START_HOUR, minute=1))
        else:
            start, end = db_module.day_bounds()

        rows = await db.get_day_export(start, end)

        buf = _io.StringIO()
        w = csv.writer(buf)
        # No email, no gender: they are not collected any more. Older rows may
        # still carry them in extra_json; they are deliberately NOT exported,
        # so the CSV cannot re-spread data we have stopped asking for.
        w.writerow([
            "run_id", "player_id", "first_name", "surname", "dob",
            "started_at_local", "time_of_day", "elapsed_ms",
            "elapsed", "busted", "outcome", "segment_reached",
            "detection_mode", "busting_dot", "voided", "voided_reason",
        ])
        for r in rows:
            try:
                extra = json.loads(r.get("extra_json") or "{}")
            except (ValueError, TypeError):
                extra = {}
            started = r.get("started_at") or ""
            local = ""
            tod = ""
            if started:
                try:
                    dt = datetime.fromisoformat(started).astimezone()
                    local, tod = dt.isoformat(timespec="seconds"), dt.strftime("%H:%M:%S")
                except ValueError:
                    local = started
            ms = r.get("elapsed_ms")
            outcome = r.get("outcome") or ""
            # A voided run keeps its original outcome in pre_void_outcome, so
            # "was this a bust" stays answerable after a void.
            effective = r.get("pre_void_outcome") or outcome
            w.writerow([_csv_safe(v) for v in [
                r.get("id", ""),
                r.get("player_id", ""),
                r.get("player_nickname", ""),
                extra.get("surname", ""),
                extra.get("dob", ""),
                local,
                tod,
                "" if ms is None else ms,
                "" if ms is None else f"{ms // 60000:d}:{(ms % 60000) / 1000:06.3f}",
                "true" if effective == "busted" else "false",
                outcome,
                r.get("segment_reached", ""),
                r.get("detection_mode", ""),
                r.get("busting_beam_id") or "",
                "true" if outcome == "voided" else "false",
                r.get("voided_reason") or "",
            ]])

        stamp = datetime.fromisoformat(start).astimezone().strftime("%Y-%m-%d")
        log.info("Day export: %d run(s) for %s", len(rows), stamp)
        return StreamingResponse(
            iter([buf.getvalue()]),
            media_type="text/csv",
            headers={"Content-Disposition":
                     f'attachment; filename="scanmania_runs_{stamp}.csv"'},
        )

    @router.post("/api/admin/beams/{beam_id}/mask", dependencies=[Depends(_require_admin)])
    async def admin_beam_mask(beam_id: str, body: MaskBeamBody):
        """
        Mask a single DOT, by its dot id (e.g. "SM-CAM-13:d17").

        This used to write `masked` onto a channel entry in `beams[]` — the
        array detection does not read — and then call set_masked() with a
        channel id against a dot-keyed lookup, which always returned False, and
        the return value was discarded. It reported success and changed nothing.
        Masked dots live under mazes.<name>.cameras.<cam>.dots, which is the
        only masked flag detection reads.
        """
        path = cfg_dir / "beams.json"
        stamp = datetime.now(timezone.utc).isoformat() if body.masked else None
        async with _CONFIG_LOCK:
            data = await _load_json(path)
            hits = 0
            old_masked = False
            for maze in (data.get("mazes") or {}).values():
                for cam in (maze.get("cameras") or {}).values():
                    for dot in cam.get("dots", []):
                        if dot.get("id") == beam_id:
                            old_masked = dot.get("masked", False)
                            dot["masked"] = body.masked
                            dot["masked_at"] = stamp
                            dot["masked_reason"] = "admin" if body.masked else None
                            hits += 1
            if not hits:
                raise HTTPException(
                    status_code=404,
                    detail=f"Dot {beam_id!r} is not in any maze capture. Mask by "
                           f"dot id (e.g. SM-CAM-13:d17), not channel id.",
                )
            try:
                await _write_config(path, json.dumps(data, indent=2))
            except OSError as exc:
                raise HTTPException(status_code=500, detail=str(exc)) from exc

        # Push into the live system so it takes effect now, not after a restart.
        applied = False
        runner = _runner()
        if runner:
            if body.masked:
                runner.context.beams_masked.add(beam_id)
            else:
                runner.context.beams_masked.discard(beam_id)
            vision = getattr(runner, "vision", None)
            if vision is not None and hasattr(vision, "set_masked"):
                try:
                    applied = bool(vision.set_masked(beam_id, body.masked))
                except Exception as exc:
                    log.warning("vision.set_masked(%s) failed: %s", beam_id, exc)

        await db.insert_config_audit(
            actor="admin", path=f"beams/{beam_id}/mask",
            before={"masked": old_masked}, after={"masked": body.masked},
        )
        return {"ok": True, "beam_id": beam_id, "masked": body.masked,
                "captures_updated": hits,
                # False means it was saved but is not in the maze currently lit,
                # so it takes effect when that maze next comes up.
                "applied_live": applied}

    @router.post("/api/admin/beams/unmask_all", dependencies=[Depends(_require_admin)])
    async def admin_beams_unmask_all():
        """Clear the masked flag on every beam in beams.json."""
        path = cfg_dir / "beams.json"
        async with _CONFIG_LOCK:
            data = await _load_json(path)
            cleared = 0
            # Clear the dot masks — the ones detection actually reads. The
            # channel entries are cleared too so nothing stale is left behind.
            for maze in (data.get("mazes") or {}).values():
                for cam in (maze.get("cameras") or {}).values():
                    for dot in cam.get("dots", []):
                        if dot.get("masked"):
                            cleared += 1
                        dot["masked"] = False
                        dot["masked_at"] = None
                        dot["masked_reason"] = None
            for b in data.get("beams", []):
                b["masked"] = False
                b["masked_at"] = None
                b["masked_reason"] = None
            try:
                await _write_config(path, json.dumps(data, indent=2))
            except OSError as exc:
                raise HTTPException(status_code=500, detail=str(exc)) from exc

        live_cleared = 0
        runner = _runner()
        if runner:
            runner.context.beams_masked.clear()
            vision = getattr(runner, "vision", None)
            if vision is not None and hasattr(vision, "clear_masks"):
                try:
                    live_cleared = int(vision.clear_masks() or 0)
                except Exception as exc:
                    log.warning("vision.clear_masks() failed: %s", exc)
            else:
                # clear_masks did not exist on EITHER backend, so this branch
                # was silently skipped and the route returned ok while every
                # auto-masked dot stayed masked until a restart.
                log.error("vision backend has no clear_masks() — masks NOT cleared")

        await db.insert_config_audit(
            actor="admin", path="beams/unmask_all",
            before={"note": "all beams"}, after={"masked": False},
        )
        # Report what was actually cleared, not the length of an unrelated list.
        return {"ok": True, "unmasked": cleared + live_cleared}

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
                    # sparkle_off only when set, so ordinary shows keep a
                    # two-key step and the file stays readable.
                    ({"preset": s.preset, "hold_ms": s.hold_ms,
                      "sparkle_off": s.sparkle_off} if s.sparkle_off
                     else {"preset": s.preset, "hold_ms": s.hold_ms})
                    for s in body.steps
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
            # The level has almost no usable range (1-2), so the duty cycle is
            # the real dose control and the operator needs to see it.
            "duty": getattr(hazer, "duty", None),
            "hazing": getattr(hazer, "hazing", None),
            # Drives the SM-NODE-DMX LED on the GM console. Only ever means
            # "our sendto did not raise" — Art-Net is fire-and-forget UDP and
            # cannot confirm the node received anything.
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

    # ------------------------------------------------------------------
    # Room lights (same Art-Net universe as the hazer)
    # ------------------------------------------------------------------

    def _cue_player():
        r = _runner()
        return getattr(r, "lights", None) if r else None

    async def _lights_status() -> dict:
        hazer = getattr(app_ref, "get_hazer", lambda: None)()
        if not hazer or not hasattr(hazer, "lights_state"):
            return {"ok": True, "available": False}
        st = hazer.lights_state()
        player = _cue_player()
        # "on" means the maze lights, not the entrance — the entrance cannot be
        # switched off, so including it would make this always read True.
        maze = [v for v in st.values() if not v["always_on"]]
        return {
            "ok": True, "available": True, "lights": st,
            "maze_on": any(v["target"] > 0 for v in maze),
            # The GM switch is the WORK-LIGHT override: solid on for loading and
            # unloading, which suspends the cue. Turning it off hands the lights
            # back to the state cues. Neither survives into a run.
            "work_lights": bool(player.work_lights) if player else None,
            "cue_state": getattr(player, "_state", None) if player else None,
        }

    @router.get("/api/gm/lights")
    async def gm_lights_status():
        """Unauthenticated, like the rest of /api/gm/* — the console has no password."""
        return await _lights_status()

    @router.post("/api/gm/lights")
    async def gm_lights_set(body: LightsBody):
        hazer = getattr(app_ref, "get_hazer", lambda: None)()
        if not hazer or not hasattr(hazer, "set_light"):
            raise HTTPException(status_code=503, detail="DMX lights not available")
        if body.name:
            if body.level is None:
                raise HTTPException(status_code=400, detail="level required with name")
            # The force-dark rule is a correctness property, not a look, and it
            # was only enforced inside the cue player's _restart() — i.e. on a
            # transition. This branch writes straight at the DMX, so raising a
            # level mid-run washed out the ceiling dots, detection read them as
            # dark, and the player was busted for the GM's slider. Refuse it
            # here too, for the same reason CLAUDE.md says "over any cue and
            # over the GM's work-light switch".
            runner = _runner()
            state = getattr(runner, "state", "") if runner else ""
            if state in _LIGHTS_FORCED_DARK and body.level > 0:
                raise HTTPException(
                    status_code=409,
                    detail=f"the container is forced dark in {state} — raising a "
                           f"light here would wash out the dots and bust the player")
            if not hazer.set_light(body.name, body.level):
                raise HTTPException(
                    status_code=400,
                    detail=f"unknown light {body.name!r}, or it is always_on and "
                           f"cannot be switched off")
        elif body.on is not None:
            player = _cue_player()
            if player is not None:
                # Go through the cue player, not straight at the DMX: a direct
                # set would be overwritten by the next cue step.
                player.set_work_lights(body.on)
            else:
                hazer.set_maze_lights(body.on)
            log.info("GM: work lights %s", "ON" if body.on else "OFF")
        else:
            raise HTTPException(status_code=400, detail="send on=, or name= and level=")
        return await _lights_status()

    def _audio():
        r = _runner()
        return getattr(r, "audio", None) if r else None

    @router.get("/api/admin/audio", dependencies=[Depends(_require_admin)])
    async def admin_audio_status():
        """What the soundtrack is doing, and which files did not load."""
        cues = _audio()
        if cues is None:
            return {"configured": False, "available": False,
                    "error": "audio not configured"}
        try:
            st = cues.status()
        except Exception as exc:
            raise HTTPException(status_code=503, detail=str(exc))
        st["configured"] = True
        return st

    @router.post("/api/admin/audio", dependencies=[Depends(_require_admin)])
    async def admin_audio_set(body: AudioBody):
        """
        Mute/unmute, and audition a file.

        Auditioning goes through the real player rather than the cue map, which
        is the only way to answer "is this file actually reaching the speakers"
        without waiting for a player to reach that state.
        """
        cues = _audio()
        if cues is None:
            raise HTTPException(status_code=503, detail="audio not configured")
        if body.muted is not None:
            cues.set_muted(body.muted)
        if body.play:
            player = getattr(cues, "_player", None)
            if player is None:
                raise HTTPException(status_code=503, detail="no audio player")
            # Only files the config actually names, and only ones already
            # decoded at startup. play_cue falls through to a blocking full
            # decode, so an arbitrary name let one request freeze the event
            # loop for seconds — the FSM drain and the stopwatch broadcast with
            # it — and pin the result in RAM permanently.
            allowed = set(getattr(cues, "_music", {}).values())
            allowed |= set(getattr(cues, "_cues", {}).values())
            if body.play not in allowed:
                raise HTTPException(
                    status_code=400,
                    detail="that file is not named in the audio config")
            # A cue, not the bed: auditioning must not knock the running
            # soundtrack off whatever it is doing.
            player.play_cue(body.play)
        return await admin_audio_status()

    @router.post("/api/admin/recalibrate", dependencies=[Depends(_require_admin)])
    async def admin_recalibrate(body: RecalibrateBody):
        """
        Re-find the dot ROIs from the live cameras.

        For "the container has been moved and the dots have drifted". It does
        not re-tune — the per-camera thresholds stay as they were — so it is
        the geometry half of what tools/capture.py does, and the half that
        actually changes when a box is trucked.

        Dry run unless apply=true: the answer to "do I need to recalibrate?"
        should not itself overwrite the calibration.
        """
        runner = _runner()
        if runner is None or not hasattr(runner, "recalibrate"):
            raise HTTPException(status_code=503, detail="runner not available")
        return await runner.recalibrate(mazes=body.mazes, apply=body.apply)

    @router.post("/api/admin/shutdown", dependencies=[Depends(_require_admin)])
    async def admin_shutdown(body: ShutdownBody):
        """
        End of day: save everything, then darken the container.

        Admin-gated and confirmation-typed on purpose. The GM console has no
        password, and this is not a button to put one mis-tap away from a
        queue of people in a dark box.

        With poweroff=true the sequence ends by stopping the kiosk, then the
        game, then halting the NUC. Stopping the service is what closes the
        database cleanly, and cutting mains under a running filesystem is how a
        box comes back with a corrupt database instead of a day's runs.

        With poweroff=false the box goes dark but stays up and stays usable:
        FORCE RESET on the GM console brings it back. That matters, because
        "dark and running" is otherwise a state only reachable out of by ssh.
        """
        if body.confirm != "SHUTDOWN":
            raise HTTPException(status_code=400,
                                detail="send confirm='SHUTDOWN' to proceed")
        runner = _runner()
        if runner is None or not hasattr(runner, "power_down"):
            raise HTTPException(status_code=503, detail="runner not available")

        # The halt is scheduled in a detached transient unit, so this response
        # still reaches the browser: the operator needs to SEE the step report,
        # and a dropped connection reads as a failed shutdown and invites a
        # second attempt.
        return await runner.power_down(poweroff=body.poweroff)

    @router.get("/api/admin/lights", dependencies=[Depends(_require_admin)])
    async def admin_lights_status():
        return await _lights_status()

    @router.post("/api/admin/lights", dependencies=[Depends(_require_admin)])
    async def admin_lights_set(body: LightsBody):
        return await gm_lights_set(body)

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
            before={"content": _audit_body(filename, before)},
            after={"content": _audit_body(filename, body.content)},
        )
        log.info("Config %s written by admin (%d bytes)", filename, len(body.content))
        return {
            "ok": True, "file": filename,
            "bytes_written": len(body.content), "reload_note": reload_note,
        }
