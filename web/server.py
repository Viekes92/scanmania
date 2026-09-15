"""
web/server.py — FastAPI app, WebSocket broadcast hub, and static file serving.

Inputs:  AppConfig, Database instance, GameRunner reference.
Outputs: HTTP routes at /signin /gm /admin; WebSocket at /ws; static files at root.
Invariant: pure WebSocket consumers — a dead client never blocks or affects the game.
           Broadcasts game state at 10 Hz regardless of client count (0..N).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from persist.db import Database

log = logging.getLogger(__name__)

# A client that cannot absorb a state frame in this long is not keeping up at
# 10 Hz anyway. Dropping it costs a reconnect; waiting for it costs the game.
_SEND_TIMEOUT_S = 0.25

_STATIC_ROOT = Path(__file__).parent / "static"


# ---------------------------------------------------------------------------
# WebSocket hub
# ---------------------------------------------------------------------------

# Bodies larger than this are refused before anything reads them. The largest
# legitimate POST is a raw config file, capped at 1 MB by ConfigRawBody.
_MAX_BODY_BYTES = 2 * 1024 * 1024

# Concurrent websocket clients. Four frontends plus a spare laptop is the real
# load; the set was unbounded, and broadcast() gathers a send to every client
# ten times a second on the same loop the game runs on.
_MAX_WS_CLIENTS = 32


def _host_is_expected(host_header: str) -> bool:
    """
    True for a Host this box is actually reached by.

    Deliberately narrow: an IP literal, localhost, or a .local name. DNS
    rebinding needs a resolvable NAME to point at us, and there is no
    deployment where the container is reached that way.
    """
    if not host_header:
        return True                     # HTTP/1.0 and some probes omit it
    host = host_header.rsplit(":", 1)[0].strip("[]").lower()
    if host in ("localhost", "127.0.0.1", "::1") or host.endswith(".local"):
        return True
    import ipaddress
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


class WebSocketHub:
    """
    Tracks all connected WebSocket clients and broadcasts JSON messages.

    Dead / stale connections are silently removed on send failure.
    The hub never raises — a broken client is never visible to the game.
    """

    def __init__(self) -> None:
        self._clients: set[WebSocket] = set()

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        if len(self._clients) >= _MAX_WS_CLIENTS:
            # Refuse rather than grow. broadcast() fans out to every client at
            # 10 Hz inside the single event drain, so an unbounded set is a
            # remote freeze of the stop button and beam handling.
            log.warning("WS client refused: %d already connected",
                        len(self._clients))
            await ws.close(code=1013)   # try again later
            return
        self._clients.add(ws)
        log.debug("WS client connected; total=%d", len(self._clients))

    async def disconnect(self, ws: WebSocket) -> None:
        self._clients.discard(ws)
        log.debug("WS client disconnected; total=%d", len(self._clients))

    async def broadcast(self, message: dict) -> None:
        """
        Broadcast to all connected clients. Drop stale AND stalled connections.

        This used to await each client in turn with no timeout, and only an
        *exception* evicted anyone. A client that simply stops reading — an iPad
        asleep, a display on saturated wifi — fills its TCP window and blocks
        the await for the kernel retransmit timeout, minutes. Because this is
        awaited from BroadcastState inside the single event drain, that froze
        the whole game: the stop button, beam breaks and GM BUST all sat
        unprocessed in the queue, and the stopwatch recorded whenever the socket
        finally unwedged rather than when the player finished.

        Now: one shared serialisation, all sends concurrent, each with a hard
        deadline. A client that cannot keep up is dropped, not waited for.
        Invariant 6 — the game path never awaits the network — needs this.
        """
        if not self._clients:
            return
        # Serialise once, not once per client.
        payload = json.dumps(message, default=str)

        async def send(ws: WebSocket) -> bool:
            try:
                await asyncio.wait_for(ws.send_text(payload), timeout=_SEND_TIMEOUT_S)
                return True
            except Exception:
                return False

        clients = list(self._clients)
        results = await asyncio.gather(*(send(ws) for ws in clients),
                                       return_exceptions=True)
        dead = {ws for ws, ok in zip(clients, results) if ok is not True}
        for ws in dead:
            self._clients.discard(ws)
        if dead:
            log.info("Dropped %d unresponsive WS client(s)", len(dead))

    @property
    def client_count(self) -> int:
        return len(self._clients)


# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------

class ScanManiaApp:
    """
    Wires FastAPI routes, the WebSocket hub, and static file serving.

    Call build() to get the ASGI app ready for uvicorn.
    """

    def __init__(
        self,
        config: Any,
        db: Database,
        hub: WebSocketHub,
        config_dir: Any = None,
        fake_mode: bool = False,
    ) -> None:
        self._config = config
        self._db = db
        self._hub = hub
        # The admin portal must edit the SAME directory the process loaded from
        # (/etc/scanmania/config in production), not the repo checkout — see
        # CLAUDE.md invariant 3.
        if config_dir is None:
            import config.loader as loader_module
            config_dir = loader_module.CONFIG_DIR
        self._config_dir = Path(config_dir)
        self._fake_mode = fake_mode
        self.app = FastAPI(title="ScanMania", version="1.0")
        self._runner: Any = None  # Set by main after runner is initialised

    def set_runner(self, runner: Any) -> None:
        """Provide the GameRunner so routes can query / dispatch to it."""
        self._runner = runner

    def build(self) -> FastAPI:
        """Wire all routes and return the FastAPI app."""
        self._add_origin_guard()
        self._add_no_cache_guard()
        self._mount_static()
        self._add_page_routes()
        self._add_ws_route()
        self._register_api_routes()
        return self.app

    # ------------------------------------------------------------------
    # Cache policy
    # ------------------------------------------------------------------

    def _add_no_cache_guard(self) -> None:
        """
        Never let a browser cache the markup or the stylesheet.

        The frontends are single-file HTML with no build step, so there is no
        content hash in any filename. A kiosk browser is restarted far more
        often than it is cleared, and after a deploy Chromium kept serving the
        PREVIOUS build from disk cache — the box was running new code while the
        screens showed the old design. Nothing about that reads as a caching
        problem; it reads as a deploy that silently did nothing.

        Fonts and artwork are deliberately left cacheable: 3.4 MB that never
        changes, and re-fetching it on every kiosk restart is pure waste.
        """
        @self.app.middleware("http")
        async def _no_cache(request, call_next):
            response = await call_next(request)
            ctype = response.headers.get("content-type", "")
            if ctype.startswith("text/html") or ctype.startswith("text/css"):
                response.headers["Cache-Control"] = \
                    "no-store, no-cache, must-revalidate, max-age=0"
                response.headers["Pragma"] = "no-cache"
                response.headers["Expires"] = "0"
            return response

    # ------------------------------------------------------------------
    # Cross-origin guard
    # ------------------------------------------------------------------

    def _add_origin_guard(self) -> None:
        """
        Reject state-changing requests that carry a foreign Origin.

        The GM console has no password on purpose — operators need speed. But
        the seven bodyless /api/gm/* routes accepted cross-origin form POSTs, so
        any page loaded in a browser that can route to the NUC could bust a run.
        The GM iPad is on Wi-Fi, and docs/security.md already names the threat:
        "a guest who finds the venue Wi-Fi and starts poking at the NUC".

        A missing Origin still passes. Browsers always send it on cross-origin
        POSTs, while curl and tools/ do not send one at all, so the dev and
        operator workflows are unaffected.
        """

        @self.app.middleware("http")
        async def _origin_guard(request, call_next):
            # Body size FIRST, before anything reads or parses it.
            #
            # FastAPI reads and json-decodes the whole body while solving
            # dependencies — i.e. BEFORE the auth dependency runs — and there is
            # no reverse proxy in front of this. An unauthenticated guest could
            # POST a multi-gigabyte chunked body to a gated route: the NUC
            # buffered all of it, then json.loads blocked, then it returned 403.
            # uvicorn shares the event loop with the game runner, so that is an
            # OOM kill or a multi-second freeze of the FSM drain and the 10 Hz
            # stopwatch broadcast, mid-run.
            declared = request.headers.get("content-length")
            if declared is not None:
                try:
                    if int(declared) > _MAX_BODY_BYTES:
                        return JSONResponse(
                            status_code=413,
                            content={"detail": "request body too large"})
                except ValueError:
                    return JSONResponse(status_code=400,
                                        content={"detail": "bad content-length"})

            # Reject an unexpected Host. The allowlist below was derived FROM
            # the Host header, which the attacker controls, so a page on
            # evil.test whose DNS rebinds to this box passed its own check. The
            # box is reached by IP (or localhost) in every real deployment, so
            # a name-based Host is not something we ever need to honour.
            host_header = request.headers.get("host", "")
            if not _host_is_expected(host_header):
                log.warning("Blocked request with unexpected Host=%r for %s",
                            host_header, request.url.path)
                return JSONResponse(status_code=421,
                                    content={"detail": "unrecognised Host"})

            if request.method in ("GET", "HEAD", "OPTIONS"):
                return await call_next(request)

            origin = request.headers.get("origin")
            if origin:
                host = request.headers.get("host", "")
                allowed = {f"http://{host}", f"https://{host}"}
                if origin not in allowed:
                    log.warning(
                        "Blocked cross-origin %s %s from Origin=%r",
                        request.method, request.url.path, origin,
                    )
                    return JSONResponse(
                        status_code=403,
                        content={"detail": "cross-origin request rejected"},
                    )
            return await call_next(request)

    # ------------------------------------------------------------------
    # Static files
    # ------------------------------------------------------------------

    def _mount_static(self) -> None:
        """Mount subdirectory static trees and the root static tree."""
        for subdir in ("gm", "admin", "admin_beams", "display_in", "display_out"):
            path = _STATIC_ROOT / subdir
            path.mkdir(parents=True, exist_ok=True)
            # Each subdir gets its own mount so assets resolve correctly.
            self.app.mount(
                f"/static/{subdir}",
                StaticFiles(directory=str(path)),
                name=f"static_{subdir}",
            )
        # Shared assets (fonts, icons, etc.) at /static
        shared = _STATIC_ROOT / "shared"
        shared.mkdir(parents=True, exist_ok=True)
        self.app.mount("/static/shared", StaticFiles(directory=str(shared)), name="static_shared")

    # ------------------------------------------------------------------
    # Page routes — serve index.html from each subdir
    # ------------------------------------------------------------------

    def _add_page_routes(self) -> None:
        app = self.app

        @app.get("/gm")
        async def page_gm():
            return _serve_page("gm")

        @app.get("/admin")
        async def page_admin():
            return _serve_page("admin")

        @app.get("/admin/beams")
        async def page_admin_beams():
            return _serve_page("admin_beams")

        @app.get("/display/in")
        async def page_display_in():
            return _serve_page("display_in")

        @app.get("/display/out")
        async def page_display_out():
            return _serve_page("display_out")

        @app.get("/")
        async def page_root():
            return _serve_page("gm")

    # ------------------------------------------------------------------
    # WebSocket
    # ------------------------------------------------------------------

    def _add_ws_route(self) -> None:
        hub = self._hub

        @self.app.websocket("/ws")
        async def ws_endpoint(websocket: WebSocket):
            await hub.connect(websocket)
            try:
                # Keep the connection alive; the broadcast loop pushes state.
                while True:
                    # We read (and discard) client messages so the browser can
                    # send heartbeat pings without us needing to act on them.
                    try:
                        await asyncio.wait_for(websocket.receive_text(), timeout=30)
                    except asyncio.TimeoutError:
                        pass
            except WebSocketDisconnect:
                pass
            except Exception as exc:
                log.debug("WS connection error: %s", exc)
            finally:
                await hub.disconnect(websocket)

    # ------------------------------------------------------------------
    # API route registration
    # ------------------------------------------------------------------

    def _register_api_routes(self) -> None:
        from web.routes_signin import register_routes as reg_signin
        from web.routes_gm import register_routes as reg_gm
        from web.routes_admin import register_routes as reg_admin
        from fastapi import APIRouter

        signin_router = APIRouter(prefix="")
        reg_signin(signin_router, self._db, self._on_player_registered)
        self.app.include_router(signin_router)

        gm_router = APIRouter(prefix="")
        reg_gm(gm_router, self._on_gm_action)
        self.app.include_router(gm_router)

        admin_router = APIRouter(prefix="")
        reg_admin(
            admin_router,
            self._db,
            self,
            self._on_admin_action,
            str(self._config_dir),
            get_runner=lambda: self._runner,
            fake_mode=self._fake_mode,
        )
        self.app.include_router(admin_router)

    def set_hazer(self, hazer: Any) -> None:
        self._hazer = hazer

    def get_hazer(self) -> Any:
        return getattr(self, "_hazer", None)

    def _on_player_registered(self, player_id: str, nickname: str) -> None:
        if self._runner:
            self._runner.on_player_registered(player_id, nickname)

    def _on_gm_action(self, action: str, payload: dict) -> None:
        if self._runner:
            self._runner.on_gm_action(action, payload)

    def _on_admin_action(self, action: str, payload: dict) -> None:
        if self._runner:
            self._runner.on_admin_action(action, payload)


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

# The frontends are single-file HTML with no build step and therefore no
# content hash in their filenames. Chromium caches them, and a kiosk browser is
# restarted far more often than it is cleared — after a deploy it happily kept
# serving the previous build from disk cache, so the screens showed the old
# design while the code on the box was new. Nothing about that looks like a
# caching problem; it looks like the deploy silently failed.
_NO_STORE = {
    "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
    "Pragma": "no-cache",
    "Expires": "0",
}


def _serve_page(name: str) -> FileResponse:
    path = _STATIC_ROOT / name / "index.html"
    if path.exists():
        return FileResponse(str(path), headers=_NO_STORE)
    # Fallback: return a minimal placeholder so the route always resolves.
    return FileResponse(str(_make_placeholder(name)),
                        media_type="text/html", headers=_NO_STORE)


def _make_placeholder(name: str) -> Path:
    """Create a minimal index.html placeholder for pages not yet built."""
    path = _STATIC_ROOT / name / "index.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text(
            f"<!doctype html><html><head><title>ScanMania — {name}</title></head>"
            f"<body><h1>{name}</h1><p>Frontend not yet built.</p></body></html>\n",
            encoding="utf-8",
        )
    return path
