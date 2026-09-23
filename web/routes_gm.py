"""
web/routes_gm.py — gamemaster console API endpoints.

Inputs:  POST actions dispatched from the /gm frontend iPad.
Outputs: events forwarded to the game runner via on_action callback.
Invariant: all urgent actions (COUNT IN, BUST, ABORT, VOID, FORCE RESET) are single
           POST calls with no required body. No action requires more than one tap.
"""

from __future__ import annotations

import logging
from typing import Callable

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

log = logging.getLogger(__name__)


class VoidBody(BaseModel):
    reason: str = Field(default="", max_length=200)


class DetectionModeBody(BaseModel):
    mode: str = Field(..., pattern=r"^(auto|assisted|manual)$")




class MasterModeBody(BaseModel):
    engage: bool


class MasterModeBody(BaseModel):
    """engage=True drops into MASTER (E-stop); False returns to ATTRACT."""
    engage: bool = True


def register_routes(
    router: APIRouter,
    on_action: Callable[[str, dict], None],
) -> None:
    """Register all gamemaster action routes."""

    def _dispatch(action: str, payload: dict | None = None) -> dict:
        try:
            on_action(action, payload or {})
        except Exception as exc:
            log.error("GM action %r dispatch error: %s", action, exc)
            raise HTTPException(status_code=500, detail=str(exc))
        return {"ok": True, "action": action}

    @router.post("/api/gm/count-in")
    async def gm_count_in():
        """Start the count-in sequence. Player must already be on the start plate."""
        return _dispatch("count_in")

    @router.post("/api/gm/bust")
    async def gm_bust():
        """Manually bust the current run (manual detection mode or override)."""
        return _dispatch("bust")

    @router.post("/api/gm/abort")
    async def gm_abort():
        """Abort the current run (recorded as 'aborted', not 'busted')."""
        return _dispatch("abort")

    @router.post("/api/gm/void")
    async def gm_void(body: VoidBody):
        """Mark the current or most-recent run as voided with a reason."""
        return _dispatch("void", {"reason": body.reason})

    @router.post("/api/gm/force-reset")
    async def gm_force_reset():
        """Force the system back to ATTRACT regardless of current state."""
        return _dispatch("force_reset")

    @router.post("/api/gm/cancel")
    async def gm_cancel():
        """Cancel the count-in or abort the current run and black out."""
        return _dispatch("cancel")

    @router.post("/api/gm/detection-mode")
    async def gm_detection_mode(body: DetectionModeBody):
        """Switch detection mode live. Logged and reflected in the run record."""
        return _dispatch("detection_mode", {"mode": body.mode})

    @router.post("/api/gm/confirm-break")
    async def gm_confirm_break():
        """In 'assisted' mode: confirm the pending break — bust the run."""
        return _dispatch("confirm_break")

    @router.post("/api/gm/master-mode")
    async def gm_master_mode(body: MasterModeBody):
        """
        Emergency stop, and the way back out of it.

        engage=true blacks out every laser, stops the clock, disarms detection
        and brings the house lights up — the same state the admin panel's
        master mode gives you, reachable from the console the gamemaster is
        actually holding. It is on the GM page precisely because the moment you
        need it is the moment you do not want to be finding a laptop and typing
        a password.

        engage=false hands the box back to ATTRACT.
        """
        return _dispatch("master_mode", {"engage": bool(body.engage)})

    @router.post("/api/gm/veto-break")
    async def gm_veto_break():
        """In 'assisted' mode: veto the pending break — resume the run."""
        return _dispatch("veto_break")


