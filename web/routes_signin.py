"""
web/routes_signin.py — player sign-in endpoint.

Inputs:  POST /api/signin with {nickname: str, ...extra fields}.
Outputs: upserts player in DB, fires on_player_registered callback; returns player_id.
Invariant: big touch targets, one screen, no scrolling — this page is handed to the public.
           Nickname validated non-empty and max 30 chars. Player ID is a UUIDv4.
"""

from __future__ import annotations

import logging
import uuid
from typing import Callable

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, field_validator

from persist.db import Database

log = logging.getLogger(__name__)


class SignInBody(BaseModel):
    first_name: str = Field(..., min_length=1, max_length=30)
    surname: str = Field(default="", max_length=50)
    email: str = Field(default="", max_length=120)
    dob: str = Field(default="", max_length=10)      # YYYY-MM-DD
    gender: str = Field(default="", max_length=1)     # M or F

    @field_validator("first_name")
    @classmethod
    def strip_first_name(cls, v: str) -> str:
        stripped = v.strip()
        if not stripped:
            raise ValueError("first_name must not be blank")
        return stripped


def register_routes(
    router: APIRouter,
    db: Database,
    on_player_registered: Callable[[str, str], None],
) -> None:
    """Register the sign-in route on the given APIRouter."""

    @router.post("/api/signin")
    async def signin(body: SignInBody):
        """
        Create or update a player and notify the game runner.

        nickname (display name) is the first_name. Full contact details are
        stored in extra_json for prize draw / winner contact purposes.
        """
        player_id = str(uuid.uuid4())
        nickname = body.first_name

        extra = {
            "surname": body.surname,
            "email": body.email,
            "dob": body.dob,
            "gender": body.gender,
        }

        try:
            await db.upsert_player(
                id=player_id,
                nickname=nickname,
                extra=extra,
            )
        except Exception as exc:
            log.error("upsert_player failed: %s", exc)
            raise HTTPException(status_code=500, detail="Database error") from exc

        try:
            on_player_registered(player_id, nickname)
        except Exception as exc:
            log.error("on_player_registered callback raised: %s", exc)

        log.info("Player registered: id=%s nickname=%r", player_id, nickname)
        return {"ok": True, "player_id": player_id, "nickname": nickname}
