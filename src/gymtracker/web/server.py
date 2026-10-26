"""aiohttp server for the touchscreen page; the same page works on a phone on the LAN.

    GET  /          static/index.html
    GET  /state     the current snapshot (JSON)
    GET  /healthz   "ok" (the kiosk waits for it)
    WS   /ws        pushes the snapshot at 1 Hz and on every change
    POST /action    {"action": "end" | "done" | "discard" | "retry_export"}

No login: the Pi is only reachable on the home LAN (DESIGN.md §7).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from aiohttp import WSMsgType, web

if TYPE_CHECKING:
    from gymtracker.app import App

log = logging.getLogger(__name__)

STATIC = Path(__file__).parent / "static"
# A page that went away without closing (a phone asleep) may never answer a close; keep
# stopping quick instead of aiohttp's 10 s per socket plus 60 s for open connections.
CLOSE_TIMEOUT_S = 1.0
SHUTDOWN_TIMEOUT_S = 2.0


class WebServer:
    def __init__(self, app: App, port: int = 8080, host: str = "0.0.0.0") -> None:
        self.app = app
        self.port = port  # 0: any free port (tests); start() sets the real one
        self.host = host
        self.sockets: set[web.WebSocketResponse] = set()
        self._dirty = asyncio.Event()
        self._runner: web.AppRunner | None = None
        self._sender: asyncio.Task | None = None

    def make_app(self) -> web.Application:
        wa = web.Application()
        wa.add_routes([
            web.get("/", self._index),
            web.get("/state", self._state),
            web.get("/healthz", self._healthz),
            web.get("/ws", self._ws),
            web.post("/action", self._action),
            web.static("/static", STATIC),
        ])
        return wa

    async def start(self) -> None:
        self.app.listeners.append(self._dirty.set)
        self._sender = asyncio.create_task(self._send_loop())
        self._runner = web.AppRunner(self.make_app(), access_log=None,
                                     shutdown_timeout=SHUTDOWN_TIMEOUT_S)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self.host, self.port)
        await site.start()
        self.port = site._server.sockets[0].getsockname()[1]
        log.info("web: http://%s:%d/", self.host, self.port)

    async def stop(self) -> None:
        with contextlib.suppress(ValueError):
            self.app.listeners.remove(self._dirty.set)
        if self._sender is not None:
            self._sender.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._sender
        await asyncio.gather(*(ws.close() for ws in list(self.sockets)),
                             return_exceptions=True)
        if self._runner is not None:
            await self._runner.cleanup()

    # ------------------------------------------------------------------ handlers

    async def _index(self, request: web.Request) -> web.FileResponse:
        return web.FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})

    async def _state(self, request: web.Request) -> web.Response:
        return web.json_response(self.app.snapshot())

    async def _healthz(self, request: web.Request) -> web.Response:
        return web.Response(text="ok")

    async def _action(self, request: web.Request) -> web.Response:
        try:
            body = await request.json()
            name = body["action"]
        except (ValueError, KeyError, TypeError):
            return web.json_response({"error": "expected {\"action\": ...}"}, status=400)
        if not isinstance(name, str) or not self.app.action(name):
            return web.json_response({"error": f"unknown action {name!r}"}, status=400)
        return web.json_response(self.app.snapshot())

    async def _ws(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(heartbeat=10, timeout=CLOSE_TIMEOUT_S)
        await ws.prepare(request)
        self.sockets.add(ws)
        try:
            await ws.send_str(json.dumps(self.app.snapshot()))
            async for msg in ws:  # the page only listens; this waits for it to go away
                if msg.type is WSMsgType.ERROR:
                    break
        finally:
            self.sockets.discard(ws)
        return ws

    async def _send_loop(self) -> None:
        while True:
            await self._dirty.wait()
            self._dirty.clear()
            if not self.sockets:
                continue
            data = json.dumps(self.app.snapshot())
            for ws in list(self.sockets):
                try:
                    await ws.send_str(data)
                except (ConnectionError, RuntimeError):
                    self.sockets.discard(ws)
