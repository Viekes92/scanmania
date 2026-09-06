"""
dev_server.py — minimal dev launcher for frontend development.

Starts the web server with an in-memory database and a stub game state.
No hardware, no Pico, no camera needed.

Usage:
    python dev_server.py
"""
import asyncio
import logging
import time

import uvicorn

from config.loader import load_all
from persist.db import Database
from web.server import ScanManiaApp, WebSocketHub

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

_BROADCAST_HZ = 10

# Stub game state — the broadcast loop sends this to all WS clients
_state = {
    "state": "ATTRACT",
    "detection_mode": "assisted",
    "run_id": None,
    "player_nickname": None,
    "elapsed_ms": 0,
    "started_at_mono_ns": None,
    "segment": 1,
    "beams_masked": [],
    "pending_break": None,
}


def get_state() -> dict:
    return {**_state, "server_mono_now_ns": time.monotonic_ns(), "timestamp": time.time()}


async def _broadcast_loop(hub: WebSocketHub) -> None:
    """Emit stub state to all WS clients at 10 Hz."""
    interval = 1.0 / _BROADCAST_HZ
    while True:
        t0 = time.monotonic()
        try:
            await hub.broadcast(get_state())
        except Exception:
            pass
        elapsed = time.monotonic() - t0
        await asyncio.sleep(max(0.0, interval - elapsed))


async def main() -> None:
    config = load_all()
    db = Database(":memory:")
    await db.init()

    hub = WebSocketHub()
    app_builder = ScanManiaApp(config, db, hub)
    app = app_builder.build()

    asyncio.create_task(_broadcast_loop(hub))

    cfg = uvicorn.Config(app, host="0.0.0.0", port=8000, log_level="info")
    server = uvicorn.Server(cfg)
    await server.serve()


if __name__ == "__main__":
    asyncio.run(main())
