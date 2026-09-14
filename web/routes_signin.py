"""
web/routes_signin.py — player sign-in endpoint.

Inputs:  POST /api/signin with {nickname: str, ...extra fields}.
Outputs: upserts player in DB, fires on_player_registered callback; returns player_id.
Invariant: big touch targets, one screen, no scrolling — this page is handed to the public.
           Nickname validated non-empty and max 30 chars. Player ID is a UUIDv4.
"""

from __future__ import annotations

import logging
import unicodedata
import uuid
from typing import Callable

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field, field_validator

from persist.db import Database

from web.ratelimit import allow, retry_after

log = logging.getLogger(__name__)

# A queue cannot physically sign in faster than this.
_SIGNIN_LIMIT = 10
_SIGNIN_WINDOW_S = 60.0


# Bidi controls, zero-width joiners and combining marks. A nickname goes on a
# public outdoor display; 30 characters of RTL-override or stacked combining
# diacritics renders as garbage over the rest of the board. Escaping is already
# correct everywhere (there is no XSS) — this is a content-policy gap, not a
# rendering one.
_BIDI_AND_INVISIBLE = {
    0x200B, 0x200C, 0x200D, 0x200E, 0x200F, 0x2028, 0x2029,
    0x202A, 0x202B, 0x202C, 0x202D, 0x202E,
    0x2066, 0x2067, 0x2068, 0x2069, 0xFEFF,
}
_MAX_COMBINING_RUN = 2


def _clean_display_name(v: str) -> str:
    """Normalise, strip control/bidi codepoints, and cap combining-mark runs."""
    v = unicodedata.normalize("NFC", v)
    out: list[str] = []
    run = 0
    for ch in v:
        cp = ord(ch)
        cat = unicodedata.category(ch)
        if cp in _BIDI_AND_INVISIBLE or cat in ("Cc", "Cf", "Co", "Cs"):
            continue
        if cat == "Mn":
            run += 1
            if run > _MAX_COMBINING_RUN:
                continue
        else:
            run = 0
        out.append(ch)
    return "".join(out).strip()


class SignInBody(BaseModel):
    first_name: str = Field(..., min_length=1, max_length=30)
    surname: str = Field(default="", max_length=50)
    email: str = Field(default="", max_length=120)
    dob: str = Field(default="", max_length=10)      # YYYY-MM-DD
    gender: str = Field(default="", max_length=1)     # M or F

    @field_validator("first_name")
    @classmethod
    def strip_first_name(cls, v: str) -> str:
        stripped = _clean_display_name(v)
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
    async def signin(body: SignInBody, request: Request):
        """
        Create or update a player and notify the game runner.

        nickname (display name) is the first_name. Full contact details are
        stored in extra_json for prize draw / winner contact purposes.
        """
        # Unauthenticated and reachable from the venue wifi. Without a limit,
        # anyone can spam the players table, and each insert commits on the same
        # connection the game path uses.
        client = request.client.host if request.client else "unknown"
        if not allow("signin", client, _SIGNIN_LIMIT, _SIGNIN_WINDOW_S):
            log.warning("signin: rate limited %s", client)
            raise HTTPException(
                status_code=429, detail="Too many sign-ins; wait a moment.",
                headers={"Retry-After": str(retry_after("signin", client,
                                                        _SIGNIN_WINDOW_S))},
            )

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
