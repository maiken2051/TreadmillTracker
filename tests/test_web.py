"""Touchscreen server and the App snapshot it serves."""
import asyncio
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer

from gymtracker.app import App, hr_zone
from gymtracker.config import Config
from gymtracker.display import NullBacklight
from gymtracker.sim.replay import drive, read_fixture
from gymtracker.state import SessionState
from gymtracker.web.server import WebServer

FIXTURES = Path(__file__).parent / "fixtures"


def make_app(tmp_path):
    cfg = Config()
    cfg.storage.data_dir = str(tmp_path)
    return App(cfg, backlight=NullBacklight(), out=lambda line: None)


def drive_until(app, notes, done):
    for t in drive(notes, app, on_note=lambda n: app.note(n.t, n.dev, n.char, n.data)):
        if done(t):
            return t
    return None


@pytest.mark.parametrize("hr, max_hr, zone", [
    (None, 0, None), (0, 0, None), (95, 0, 0), (100, 0, 1), (131, 0, 3), (170, 0, 5),
    (89, 180, 0), (90, 180, 1), (150, 180, 4),  # 50 % of 180 = 90; 80 % = 144
])
def test_hr_zone(hr, max_hr, zone):
    assert hr_zone(hr, max_hr, [100, 115, 130, 145, 160]) == zone


def test_snapshot_while_recording_and_finished(tmp_path):
    app = make_app(tmp_path)
    app.open()
    notes = read_fixture(FIXTURES / "combined_3min_b.jsonl")
    t = drive_until(app, notes, lambda t: app.state is SessionState.RECORDING
                    and t - app.recorder.session.start_t > 60)
    snap = app.snapshot(t)
    assert snap["state"] == "recording" and snap["units"]["speed"] == "mph"
    assert snap["hrm"]["connected"] and snap["treadmill"]["connected"]
    live = snap["live"]
    assert live["elapsed_s"] > 50 and live["speed"] > 1 and live["distance"] > 0
    assert snap["hr"] and snap["hr_zone"] is not None
    assert snap["summary"] is None

    drive_until(app, notes, lambda t: app.state is SessionState.FINISHED)
    snap = app.snapshot()
    assert snap["state"] == "finished" and snap["end_reason"]
    sm = snap["summary"]
    assert sm["workout_id"] == 1 and sm["moving_s"] > 60
    assert sm["min_speed"] <= sm["avg_speed"] <= sm["max_speed"]
    app.close(0)


def test_actions(tmp_path):
    app = make_app(tmp_path)
    asyncio.run(app.run_replay(FIXTURES / "treadmill_calib.jsonl", speed=0))
    app.open()
    assert app.state is SessionState.FINISHED
    assert not app.action("fly")
    assert app.action("done") and app.state is SessionState.IDLE
    app.close(0)


def test_server_routes_and_push(tmp_path):
    async def go():
        app = make_app(tmp_path)
        app.open()
        server = WebServer(app)
        client = TestClient(TestServer(server.make_app()))
        await client.start_server()
        app.listeners.append(server._dirty.set)
        server._sender = asyncio.create_task(server._send_loop())
        try:
            assert (await (await client.get("/healthz")).text()) == "ok"
            page = await client.get("/")
            assert page.status == 200 and "<title>Gym Tracker</title>" in await page.text()
            assert (await (await client.get("/state")).json())["state"] == "idle"
            bad = await client.post("/action", json={"action": "fly"})
            assert bad.status == 400
            assert (await client.post("/action", data="nope")).status == 400
            ok = await client.post("/action", json={"action": "done"})
            assert ok.status == 200 and (await ok.json())["state"] == "idle"

            ws = await client.ws_connect("/ws")
            first = await ws.receive_json(timeout=2)
            assert first["state"] == "idle"
            app.tick(1.0)  # the 1 Hz tick pushes a fresh snapshot
            assert (await ws.receive_json(timeout=2))["state"] == "idle"
            await ws.close()
        finally:
            server._sender.cancel()
            await client.close()
            app.close(0)

    asyncio.run(go())


def test_stop_is_quick_with_a_page_that_never_answers(tmp_path):
    import socket
    import time

    async def go():
        app = make_app(tmp_path)
        app.open()
        server = WebServer(app, port=0, host="127.0.0.1")
        await server.start()
        sock = socket.create_connection(("127.0.0.1", server.port))
        sock.sendall(b"GET /ws HTTP/1.1\r\nHost: x\r\nUpgrade: websocket\r\n"
                     b"Connection: Upgrade\r\nSec-WebSocket-Version: 13\r\n"
                     b"Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n\r\n")
        await asyncio.sleep(0.3)  # upgraded; this client never reads or answers again
        assert len(server.sockets) == 1
        t0 = time.monotonic()
        await server.stop()
        elapsed = time.monotonic() - t0
        sock.close()
        app.close(0)
        return elapsed

    assert asyncio.run(go()) < 5
