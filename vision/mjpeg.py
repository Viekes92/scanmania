"""
vision/mjpeg.py — serves a downscaled MJPEG stream for the outdoor display.

Inputs:  decoded frames pushed via push_frame() by CameraStream's on_frame callback
Outputs: MJPEG multipart/x-mixed-replace stream at http://<host>:<port>/<camera_id>.mjpg
Invariant: never opens the RTSP stream itself — decode happens once in vision service.
           Frames are re-encoded at a lower resolution for bandwidth efficiency.
           Slow clients are dropped (non-blocking push) so they never stall encoding.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

import cv2
import numpy as np
from aiohttp import web

log = logging.getLogger(__name__)

_MJPEG_QUALITY = 70        # JPEG quality for the MJPEG stream
_SCALE_WIDTH  = 640        # downscale target width (height proportional)
_BOUNDARY = b"--frame"
_CONTENT_TYPE = b"Content-Type: image/jpeg"

# Multipart header template
_PART_HEADER = (
    b"--frame\r\n"
    b"Content-Type: image/jpeg\r\n"
    b"Content-Length: {length}\r\n"
    b"\r\n"
)


class MjpegServer:
    """
    Lightweight aiohttp HTTP server that serves one MJPEG endpoint per camera.

    Clients connect to http://<host>:<port>/<camera_id>.mjpg and receive a
    continuous multipart/x-mixed-replace JPEG stream. Each camera's latest
    frame is stored; new clients always start from the most recent frame.
    """

    def __init__(self, host: str = "127.0.0.1", port: int = 8081) -> None:
        self._host = host
        self._port = port
        # Per camera: asyncio.Event (new frame available) + latest JPEG bytes
        self._frames: dict[str, bytes] = {}
        self._events: dict[str, asyncio.Event] = {}
        self._app: Optional[web.Application] = None

    # ------------------------------------------------------------------
    # Frame ingestion (called from vision decode loop)
    # ------------------------------------------------------------------

    def push_frame(self, frame: np.ndarray, camera_id: str) -> None:
        """
        Accept a decoded frame, downscale it, JPEG-encode it, and notify waiting clients.

        Called from the camera decode coroutine. Encoding is synchronous here;
        at 30 fps and 640 px this takes ~1-2 ms, well within budget.
        """
        # Downscale preserving aspect ratio
        h, w = frame.shape[:2]
        if w > _SCALE_WIDTH:
            scale = _SCALE_WIDTH / w
            new_w = _SCALE_WIDTH
            new_h = int(h * scale)
            small = cv2.resize(frame, (new_w, new_h), interpolation=cv2.INTER_LINEAR)
        else:
            small = frame

        ok, buf = cv2.imencode(
            ".jpg", small, [cv2.IMWRITE_JPEG_QUALITY, _MJPEG_QUALITY]
        )
        if not ok:
            log.warning("MjpegServer: imencode failed for camera '%s'", camera_id)
            return

        jpeg_bytes = buf.tobytes()
        self._frames[camera_id] = jpeg_bytes

        # Signal waiting client coroutines (non-blocking)
        event = self._events.get(camera_id)
        if event is not None:
            event.set()

    # ------------------------------------------------------------------
    # HTTP server
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """Start the aiohttp MJPEG server. Runs until cancelled."""
        self._app = web.Application()
        self._app.router.add_get("/{camera_id}.mjpg", self._handle_stream)

        runner = web.AppRunner(self._app)
        await runner.setup()
        site = web.TCPSite(runner, self._host, self._port)
        await site.start()
        log.info("MjpegServer listening on http://%s:%d/", self._host, self._port)

        # Keep running until cancelled
        try:
            while True:
                await asyncio.sleep(3600)
        except asyncio.CancelledError:
            log.info("MjpegServer shutting down")
            await runner.cleanup()
            raise

    async def _handle_stream(self, request: web.Request) -> web.StreamResponse:
        """Handle one MJPEG streaming client."""
        camera_id = request.match_info["camera_id"]

        if camera_id not in self._events:
            self._events[camera_id] = asyncio.Event()

        response = web.StreamResponse(
            headers={
                "Content-Type": "multipart/x-mixed-replace; boundary=frame",
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
            }
        )
        await response.prepare(request)

        log.info("MjpegServer: new client for '%s' from %s", camera_id, request.remote)

        event = self._events[camera_id]

        try:
            while True:
                # Wait for a new frame (with timeout to detect dead clients)
                event.clear()
                try:
                    await asyncio.wait_for(
                        asyncio.shield(event.wait()),
                        timeout=5.0,
                    )
                except asyncio.TimeoutError:
                    # Send a keep-alive; if the write fails the client is gone
                    pass

                jpeg = self._frames.get(camera_id)
                if jpeg is None:
                    await asyncio.sleep(0.05)
                    continue

                header = _PART_HEADER.replace(b"{length}", str(len(jpeg)).encode())
                await response.write(header + jpeg + b"\r\n")
        except (ConnectionResetError, asyncio.CancelledError, Exception) as exc:
            log.debug("MjpegServer: client for '%s' disconnected (%s)", camera_id, exc)

        return response
