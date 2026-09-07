"""
web/routes_admin.py — admin portal API endpoints.

Inputs:  Authenticated requests (X-Admin-Password header) from the /admin frontend.
Outputs: system state, config changes, outbox control, run management, snapshots, logs.
Invariant: every config change is logged to config_audit. Auth required for all write
           endpoints. Reads are unauthenticated (LAN-only binding is the outer boundary).
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import subprocess
from pathlib import Path
from typing import Callable

import yaml
from fastapi import APIRouter, Depends, HTTPException, Header, Query
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field

from persist.db import Database
from persist.sync import export_leaderboard_csv, export_snapshot

log = logging.getLogger(__name__)

_ENV_PASSWORD_KEY = "SCANMANIA_ADMIN_PASSWORD"
_DEFAULT_PASSWORD  = "michaeleettacos"

_CONFIG_FILES = {
    "game":     ("game.yaml",     "yaml"),
    "hardware": ("hardware.yaml", "yaml"),
    "mazes":    ("mazes.yaml",    "yaml"),
    "beams":    ("beams.json",    "json"),
}


# ---------------------------------------------------------------------------
# Auth dependency
# ---------------------------------------------------------------------------

def _hash_password(pwd: str) -> str:
    return hashlib.sha256(pwd.encode()).hexdigest()


def _require_admin(x_admin_password: str | None = Header(default=None)) -> None:
    """Validate the X-Admin-Password header against the hashed env var (or default)."""
    expected = os.environ.get(_ENV_PASSWORD_KEY, _DEFAULT_PASSWORD)
    expected_hash = _hash_password(expected)
    if not hmac.compare_digest(x_admin_password or "", expected_hash):
        raise HTTPException(status_code=403, detail="Forbidden")


# ---------------------------------------------------------------------------
# Request bodies
# ---------------------------------------------------------------------------

class VoidBody(BaseModel):
    reason: str = Field(default="", max_length=200)
    voided: bool = Field(default=True)


class ConfigRawBody(BaseModel):
    content: str = Field(max_length=1_000_000)


class GameConfigBody(BaseModel):
    updates: dict  # key-path → value pairs to apply to game.yaml


class MaskBeamBody(BaseModel):
    masked: bool


class SavePresetBody(BaseModel):
    name: str = Field(min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9_]+$")
    channels: list[int] = Field(default_factory=list)
    description: str = Field(default="", max_length=200)


class SaveShowBody(BaseModel):
    name: str = Field(min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9_]+$")
    steps: list[dict] = Field(default_factory=list)
    loop: bool = Field(default=False)


class MasterPresetBody(BaseModel):
    preset: str = Field(max_length=64)


class HazerBody(BaseModel):
    enabled: bool | None = None
    haze: int | None = None
    fan: int | None = None


class ApplyChannelsBody(BaseModel):
    channels: list[int] = Field(default_factory=list)


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
    app_ref: "Any",  # ScanManiaApp instance — call app_ref.get_outbox() for the worker
    on_action: Callable[[str, dict], None],
    config_path: str,
    get_runner: Callable | None = None,
) -> None:
    """Register all admin API routes."""

    cfg_dir = Path(config_path)

    def _get_outbox():
        """Get the outbox worker (may be None if not configured)."""
        return getattr(app_ref, "get_outbox", lambda: None)()

    # ------------------------------------------------------------------
    # Status / dashboard
    # ------------------------------------------------------------------

    @router.post("/api/admin/login")
    async def admin_login(x_admin_password: str | None = Header(default=None)):
        """Validate the admin password. Returns 200 on success, 403 on failure."""
        expected = os.environ.get(_ENV_PASSWORD_KEY, _DEFAULT_PASSWORD)
        expected_hash = _hash_password(expected)
        if not hmac.compare_digest(x_admin_password or "", expected_hash):
            raise HTTPException(status_code=403, detail="Invalid password")
        return {"ok": True}

    @router.get("/api/admin/status")
    async def admin_status():
        """Return state, uptime, run counts, outbox depth, and faults."""
        depth = await db.outbox_depth()
        all_runs = await db.list_runs(limit=500, offset=0)
        runs_today = 0
        clean = 0
        busted = 0
        aborted = 0
        from datetime import datetime, timezone
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        for r in all_runs:
            if (r.get("started_at") or "").startswith(today):
                runs_today += 1
            outcome = r.get("outcome", "")
            if outcome == "clean":
                clean += 1
            elif outcome == "busted":
                busted += 1
            elif outcome == "aborted":
                aborted += 1

        # Pull live state from runner if available
        runner_state = "unknown"
        if get_runner:
            runner = get_runner()
            if runner:
                runner_state = runner.state

        return {
            "ok": True,
            "state": runner_state,
            "uptime": None,
            "runs_today": runs_today,
            "clean": clean,
            "busted": busted,
            "aborted": aborted,
            "outbox_depth": depth,
            "last_push": _get_outbox().last_push_ok_at if _get_outbox() else None,
            "last_error": _get_outbox().last_error if _get_outbox() else None,
            "faults": [],
        }

    # ------------------------------------------------------------------
    # Runs
    # ------------------------------------------------------------------

    @router.get("/api/admin/runs")
    async def admin_runs(
        limit: int = Query(default=50, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
        q: str = Query(default=""),
    ):
        runs = await db.list_runs(limit=limit, offset=offset)
        if q:
            q_lower = q.lower()
            runs = [
                r for r in runs
                if q_lower in (r.get("player_nickname") or "").lower()
                or q_lower in (r.get("id") or "").lower()
                or q_lower in (r.get("outcome") or "").lower()
            ]
        return {"ok": True, "runs": runs, "count": len(runs)}

    @router.get("/api/admin/runs/{run_id}")
    async def admin_get_run(run_id: str):
        run = await db.get_run(run_id)
        if not run:
            raise HTTPException(status_code=404, detail="Run not found")
        events = await db.list_events(run_id, limit=500)
        beam_hits = await db.list_beam_hits(run_id)
        return {"ok": True, "run": run, "events": events, "beam_hits": beam_hits}

    @router.get("/api/admin/runs/{run_id}/events")
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
        await db.update_run(run_id, outcome="clean", voided_reason=None)
        await db.insert_config_audit(
            actor="admin",
            path=f"runs/{run_id}/unvoid",
            before={"outcome": "voided", "voided_reason": run.get("voided_reason")},
            after={"outcome": "clean"},
        )
        return {"ok": True, "run_id": run_id}

    @router.post(
        "/api/admin/runs/{run_id}/void",
        dependencies=[Depends(_require_admin)],
    )
    async def admin_void_run(run_id: str, body: VoidBody):
        run = await db.get_run(run_id)
        if not run:
            raise HTTPException(status_code=404, detail="Run not found")
        if body.voided:
            await db.void_run(run_id, body.reason)
        else:
            await db.update_run(run_id, outcome="clean", voided_reason=None)
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

    @router.get("/api/admin/export/runs.csv")
    async def admin_runs_csv():
        import csv
        import io as _io
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
            writer.writerow(r)
        return StreamingResponse(
            iter([buf.getvalue()]),
            media_type="text/csv",
            headers={"Content-Disposition": "attachment; filename=runs.csv"},
        )

    @router.get("/api/admin/export/leaderboard.csv")
    async def admin_leaderboard_csv():
        import csv
        import io as _io
        rows = await db.get_leaderboard(scope="all", limit=10_000)
        buf = _io.StringIO()
        fieldnames = ["rank", "player_nickname", "elapsed_ms", "started_at", "detection_mode", "segment_reached"]
        writer = csv.DictWriter(buf, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for i, r in enumerate(rows, 1):
            r["rank"] = i
            writer.writerow(r)
        return StreamingResponse(
            iter([buf.getvalue()]),
            media_type="text/csv",
            headers={"Content-Disposition": "attachment; filename=leaderboard.csv"},
        )

    # ------------------------------------------------------------------
    # Outbox
    # ------------------------------------------------------------------

    @router.get("/api/admin/outbox")
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

    @router.get("/api/admin/outbox/test")
    async def admin_outbox_test():
        outbox = _get_outbox()
        if not outbox:
            raise HTTPException(status_code=503, detail="Outbox worker not available")
        result = await outbox.test_endpoint()
        return {"ok": True, "result": result}

    # ------------------------------------------------------------------
    # Snapshots
    # ------------------------------------------------------------------

    @router.get("/api/admin/snapshot")
    async def admin_snapshot():
        import tempfile, sys
        if sys.platform == "darwin":
            backup_dir = os.path.join(tempfile.gettempdir(), "scanmania-backups")
        else:
            backup_dir = "/var/backups/scanmania"
        try:
            db_path = await export_snapshot(db, backup_dir)
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        return FileResponse(
            db_path,
            media_type="application/octet-stream",
            filename=os.path.basename(db_path),
        )

    # ------------------------------------------------------------------
    # Logs
    # ------------------------------------------------------------------

    @router.get("/api/admin/logs/{unit}", dependencies=[Depends(_require_admin)])
    async def admin_logs(unit: str, lines: int = Query(default=200, ge=1, le=2000)):
        allowed_units = {
            "scanmania-core", "scanmania-web", "scanmania-sync",
            "scanmania-io", "scanmania-vision", "scanmania-kiosk",
        }
        if unit not in allowed_units:
            raise HTTPException(status_code=400, detail=f"Unknown unit: {unit!r}")
        try:
            result = subprocess.run(
                ["journalctl", "-u", unit, "-n", str(lines), "--no-pager", "--output=short"],
                capture_output=True, text=True, timeout=10,
            )
            return {"ok": True, "unit": unit, "lines": result.stdout}
        except FileNotFoundError:
            return {"ok": True, "unit": unit, "lines": "(journalctl not available on this host)"}
        except subprocess.TimeoutExpired:
            raise HTTPException(status_code=504, detail="journalctl timed out")

    # ------------------------------------------------------------------
    # Hardware
    # ------------------------------------------------------------------

    @router.get("/api/admin/hardware")
    async def admin_hardware():
        """Return relay board states from the runner's cached hardware readback."""
        try:
            hw = yaml.safe_load((cfg_dir / "hardware.yaml").read_text(encoding="utf-8"))
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

        runner = get_runner() if get_runner else None
        cached = runner._board_states if runner else {}

        boards = []
        for b in hw.get("relay_boards", []):
            board_id = b["id"]
            state = cached.get(board_id, {})
            boards.append({
                "id": board_id,
                "ip": b.get("ip", ""),
                "hostname": b.get("hostname", ""),
                "status": state.get("status", "?"),
                "rtt_ms": state.get("rtt_ms"),
                "mismatch_count": None,
                "coils": state.get("coils", [False] * b.get("channels", 16)),
            })

        cameras = [
            {"id": c["id"], "fps": None, "stall_count": 0}
            for c in hw.get("cameras", [])
        ]
        pico = {"link": "?", "firmware": "?", "last_hb": "?", "bitmask": "?"}
        return {"ok": True, "boards": boards, "cameras": cameras, "pico": pico}

    # ------------------------------------------------------------------
    # Beams
    # ------------------------------------------------------------------

    @router.get("/api/admin/beams")
    async def admin_beams():
        """Return full beam list from beams.json. Triggers a WS state broadcast."""
        try:
            data = json.loads((cfg_dir / "beams.json").read_text(encoding="utf-8"))
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        try:
            on_action("get_beam_states", {})
        except Exception:
            pass
        return {"ok": True, "beams": data.get("beams", [])}

    @router.post("/api/admin/beams/{beam_id}/mask", dependencies=[Depends(_require_admin)])
    async def admin_beam_mask(beam_id: str, body: MaskBeamBody):
        """Toggle masked flag on a single beam in beams.json."""
        from datetime import datetime, timezone
        path = cfg_dir / "beams.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        beam = next((b for b in data.get("beams", []) if b["id"] == beam_id), None)
        if not beam:
            raise HTTPException(status_code=404, detail=f"Beam {beam_id!r} not found")
        old_masked = beam.get("masked", False)
        beam["masked"] = body.masked
        beam["masked_at"] = datetime.now(timezone.utc).isoformat() if body.masked else None
        beam["masked_reason"] = "admin" if body.masked else None
        tmp = path.with_suffix(".json.tmp")
        try:
            tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
            tmp.replace(path)
        except OSError as exc:
            tmp.unlink(missing_ok=True)
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        await db.insert_config_audit(
            actor="admin", path=f"beams/{beam_id}/mask",
            before={"masked": old_masked}, after={"masked": body.masked},
        )
        return {"ok": True, "beam_id": beam_id, "masked": body.masked}

    @router.post("/api/admin/beams/unmask_all", dependencies=[Depends(_require_admin)])
    async def admin_beams_unmask_all():
        """Clear the masked flag on every beam in beams.json."""
        path = cfg_dir / "beams.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        for b in data.get("beams", []):
            b["masked"] = False
            b["masked_at"] = None
            b["masked_reason"] = None
        tmp = path.with_suffix(".json.tmp")
        try:
            tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
            tmp.replace(path)
        except OSError as exc:
            tmp.unlink(missing_ok=True)
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        await db.insert_config_audit(
            actor="admin", path="beams/unmask_all",
            before={"note": "all beams"}, after={"masked": False},
        )
        return {"ok": True, "unmasked": len(data.get("beams", []))}

    # ------------------------------------------------------------------
    # Presets
    # ------------------------------------------------------------------

    @router.get("/api/admin/presets")
    async def admin_presets():
        """Return named presets from mazes.yaml."""
        try:
            data = yaml.safe_load((cfg_dir / "mazes.yaml").read_text(encoding="utf-8"))
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        presets = {
            name: {"channels": p.get("channels"), "description": p.get("description", "")}
            for name, p in data.get("presets", {}).items()
        }
        return {"ok": True, "presets": presets}

    @router.post("/api/admin/presets", dependencies=[Depends(_require_admin)])
    async def admin_save_preset(body: SavePresetBody):
        """Save or update a preset in mazes.yaml."""
        path = cfg_dir / "mazes.yaml"
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        if "presets" not in data:
            data["presets"] = {}
        old = data["presets"].get(body.name)
        data["presets"][body.name] = {
            "channels": body.channels,
            "description": body.description,
        }
        tmp = path.with_suffix(".yaml.tmp")
        try:
            tmp.write_text(yaml.dump(data, default_flow_style=False, sort_keys=False), encoding="utf-8")
            tmp.replace(path)
        except OSError as exc:
            tmp.unlink(missing_ok=True)
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        await db.insert_config_audit(
            actor="admin", path=f"presets/{body.name}",
            before={"preset": old}, after={"channels": body.channels},
        )
        return {"ok": True, "name": body.name, "channels": body.channels}

    @router.post("/api/admin/shows", dependencies=[Depends(_require_admin)])
    async def admin_save_show(body: SaveShowBody):
        """Save or update a show in mazes.yaml."""
        path = cfg_dir / "mazes.yaml"
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        if "shows" not in data:
            data["shows"] = {}
        old = data["shows"].get(body.name)
        data["shows"][body.name] = {
            "steps": [{"preset": s.get("preset", ""), "hold_ms": s.get("hold_ms", 300)} for s in body.steps],
            "loop": body.loop,
        }
        tmp = path.with_suffix(".yaml.tmp")
        try:
            tmp.write_text(yaml.dump(data, default_flow_style=False, sort_keys=False), encoding="utf-8")
            tmp.replace(path)
        except OSError as exc:
            tmp.unlink(missing_ok=True)
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        await db.insert_config_audit(
            actor="admin", path=f"shows/{body.name}",
            before={"show": old}, after={"steps": len(body.steps), "loop": body.loop},
        )
        return {"ok": True, "name": body.name, "steps": len(body.steps)}

    # ------------------------------------------------------------------
    # Hazer
    # ------------------------------------------------------------------

    @router.get("/api/admin/hazer")
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
        }

    @router.post("/api/admin/hazer")
    async def admin_hazer_set(body: HazerBody):
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

    # Also expose on GM routes for quick toggle
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

    @router.post("/api/admin/master/apply_preset", dependencies=[Depends(_require_admin)])
    async def admin_master_apply_preset(body: MasterPresetBody):
        on_action("master_apply_preset", {"preset": body.preset})
        return {"ok": True, "preset": body.preset}

    @router.post("/api/admin/master/toggle_channel", dependencies=[Depends(_require_admin)])
    async def admin_master_toggle_channel(body: MasterChannelBody):
        on_action("master_toggle_channel", {"board_id": body.board_id, "channel": body.channel, "state": body.state})
        return {"ok": True, "channel": body.channel, "state": body.state}

    @router.post("/api/admin/master/stopwatch", dependencies=[Depends(_require_admin)])
    async def admin_master_stopwatch(body: MasterStopwatchBody):
        on_action("master_stopwatch", {"action": body.action})
        return {"ok": True, "action": body.action}

    @router.post("/api/admin/master/apply_channels", dependencies=[Depends(_require_admin)])
    async def admin_master_apply_channels(body: ApplyChannelsBody):
        """Apply a raw channel list directly to hardware (no preset name needed)."""
        on_action("master_apply_channels", {"channels": body.channels})
        return {"ok": True, "channels": len(body.channels)}

    # ------------------------------------------------------------------
    # Dev — inject FSM events for testing (--fake-all mode only)
    # ------------------------------------------------------------------

    @router.post("/api/admin/dev/trigger", dependencies=[Depends(_require_admin)])
    async def admin_dev_trigger(body: DevTriggerBody):
        on_action("dev_trigger", body.model_dump())
        return {"ok": True, "event": body.event}

    # ------------------------------------------------------------------
    # Logs — SSE stream (no auth header possible with EventSource, uses
    #         query param token — omitted; rely on LAN-only binding)
    # ------------------------------------------------------------------

    _LOG_UNITS = {
        "scanmania-core", "scanmania-web", "scanmania-sync",
        "scanmania-io", "scanmania-vision", "scanmania-kiosk",
    }

    @router.get("/api/admin/logs/stream")
    async def admin_logs_stream(
        unit: str = Query(...),
        filter: str = Query(default=""),
        lines: int = Query(default=100, ge=1, le=500),
    ):
        """SSE stream of journalctl output for the given unit."""
        if unit not in _LOG_UNITS:
            raise HTTPException(status_code=400, detail=f"Unknown unit: {unit!r}")

        filt = filter.lower()

        async def event_stream():
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
            except Exception as exc:
                yield f"data: (error: {exc})\n\n"
            finally:
                try:
                    if proc.returncode is None:
                        proc.terminate()
                        await proc.wait()
                except Exception:
                    pass

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # ------------------------------------------------------------------
    # Config — audit log (must be registered BEFORE the {file_id} route)
    # ------------------------------------------------------------------

    @router.get("/api/admin/config/audit")
    async def admin_config_audit(limit: int = Query(default=50, ge=1, le=500)):
        """Return the most recent config_audit entries."""
        rows = await db.list_config_audit(limit=limit)
        return {"ok": True, "entries": rows}

    # ------------------------------------------------------------------
    # Config — read/write all 4 config files as raw text
    # ------------------------------------------------------------------

    @router.get("/api/admin/config/{file_id}")
    async def admin_config_read(file_id: str):
        """
        Return the raw text content of a config file.

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
        content = path.read_text(encoding="utf-8")
        return {"ok": True, "file": filename, "content": content}

    @router.post("/api/admin/config/{file_id}", dependencies=[Depends(_require_admin)])
    async def admin_config_write(file_id: str, body: ConfigRawBody):
        """
        Write raw text to a config file.

        Validates that the content parses as valid YAML or JSON before writing.
        Logs the change to config_audit.
        """
        if file_id == "audit" or file_id not in _CONFIG_FILES:
            raise HTTPException(
                status_code=404,
                detail=f"Unknown config file '{file_id}'. Valid: {list(_CONFIG_FILES)}",
            )
        filename, fmt = _CONFIG_FILES[file_id]
        path = cfg_dir / filename

        # Validate parse before writing
        try:
            if fmt == "yaml":
                yaml.safe_load(body.content)
            else:
                json.loads(body.content)
        except Exception as exc:
            raise HTTPException(
                status_code=422,
                detail=f"Invalid {fmt.upper()}: {exc}",
            ) from exc

        # Atomic write via temp file
        tmp = path.with_suffix(path.suffix + ".tmp")
        try:
            tmp.write_text(body.content, encoding="utf-8")
            tmp.replace(path)
        except OSError as exc:
            if tmp.exists():
                tmp.unlink(missing_ok=True)
            raise HTTPException(status_code=500, detail=str(exc)) from exc

        await db.insert_config_audit(
            actor="admin",
            path=f"config/{filename}",
            before={"note": "raw file overwrite — before not captured"},
            after={"bytes": len(body.content)},
        )
        log.info("Config %s written by admin (%d bytes)", filename, len(body.content))
        return {"ok": True, "file": filename, "bytes_written": len(body.content)}



# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _get_nested(d: dict, dotted_key: str) -> object:
    parts = dotted_key.split(".")
    for part in parts:
        if not isinstance(d, dict):
            return None
        d = d.get(part)
    return d
