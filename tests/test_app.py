"""The app driven from captures (no Bluetooth), and the backlight."""

import asyncio
import json
from pathlib import Path

from gymtracker.app import CHECKPOINT, App
from gymtracker.archive import read_archive
from gymtracker.ble.hrm import HR_MEASUREMENT_UUID
from gymtracker.config import Config
from gymtracker.display import Backlight, NullBacklight, find_backlight
from gymtracker.sim.replay import drive, read_fixture
from gymtracker.state import SessionState

FIXTURES = Path(__file__).parent / "fixtures"


def make_app(tmp_path):
    cfg = Config()
    cfg.storage.data_dir = str(tmp_path)
    lines = []
    return App(cfg, backlight=NullBacklight(), out=lines.append), lines


def test_replay_saves_workout_with_raw_and_log(tmp_path):
    app, lines = make_app(tmp_path)
    asyncio.run(app.run_replay(FIXTURES / "combined_3min_b.jsonl", speed=0))
    assert any(line.startswith("saved workout 1:") for line in lines)
    assert any("recording" in line and "HR" in line and "mph" in line for line in lines)
    assert app.backlight.on is True  # FINISHED: the screen stays on until IDLE

    archives = list((tmp_path / "archive").rglob("*.json.gz"))
    assert len(archives) == 1
    a = read_archive(archives[0])
    assert a.workout.summary.avg_hr is not None
    assert len(a.raw) > 300 and HR_MEASUREMENT_UUID in {r["char"] for r in a.raw}
    logs = list((tmp_path / "logs").glob("*.jsonl"))
    assert len(logs) == 1
    first = json.loads(logs[0].read_text().splitlines()[0])
    assert set(first) == {"t", "dev", "char", "data"}  # replayable fixture format
    assert (tmp_path / "Workouts.xlsx").exists()
    assert not (tmp_path / CHECKPOINT).exists()


def test_close_while_recording_saves_the_workout(tmp_path):
    app, lines = make_app(tmp_path)
    app.open()
    notes = read_fixture(FIXTURES / "treadmill_calib.jsonl")
    steps = drive(notes, app, on_note=lambda n: app.note(n.t, n.dev, n.char, n.data))
    t = None
    for t in steps:
        if app.state is SessionState.RECORDING and t - app.recorder.session.start_t > 120:
            break
    assert (tmp_path / CHECKPOINT).exists()  # checkpointed every 30 s
    app.close(t)
    assert "stopping: saving the workout in progress" in lines
    assert any(line.startswith("saved workout 1:") for line in lines)
    assert not (tmp_path / CHECKPOINT).exists()


def test_old_checkpoint_is_kept_aside(tmp_path):
    (tmp_path / CHECKPOINT).write_text("{}")
    app, _ = make_app(tmp_path)
    app.open()
    app.close(0)
    assert not (tmp_path / CHECKPOINT).exists()
    assert len(list(tmp_path.glob("checkpoint-*.json"))) == 1


def test_user_discard_deletes_the_saved_workout(tmp_path):
    app, lines = make_app(tmp_path)
    asyncio.run(app.run_replay(FIXTURES / "treadmill_calib.jsonl", speed=0))
    app.open()
    app.recorder.user_discard(app.recorder.machine.since + 1)
    assert "deleted workout 1" in lines
    assert app.library.store.list() == []
    assert app.state is SessionState.IDLE
    app.close(0)


def test_notifications_while_idle_are_not_kept(tmp_path):
    app, _ = make_app(tmp_path)
    app.note(1.0, "AA", "x", b"\x01")
    assert app.raw == []


def test_backlight_writes_bl_power(tmp_path):
    dev = tmp_path / "11-0045"
    dev.mkdir()
    (dev / "bl_power").write_text("0")
    assert find_backlight("auto", tmp_path) == dev / "bl_power"
    b = Backlight("auto", tmp_path)
    b.set(False)
    assert (dev / "bl_power").read_text() == "4"
    b.set(True)
    assert (dev / "bl_power").read_text() == "0"


def test_backlight_missing_is_a_no_op(tmp_path):
    b = Backlight("auto", tmp_path / "nothing")
    b.set(True)
    assert b.path is None and b.on is True


def test_strap_only_connects_once_awake_and_is_released_on_sleep(tmp_path):
    from types import SimpleNamespace

    from gymtracker.state import Device

    async def go():
        app, _ = make_app(tmp_path)
        app.open()
        released = []

        async def disconnect():
            released.append(True)

        app.hrm = SimpleNamespace(busy=True, connected=True, battery_pct=None,
                                  disconnect=disconnect)
        assert app.wants(Device.TREADMILL) and not app.wants(Device.HRM)
        app.device_seen(1.0, Device.TREADMILL, "Treadmill 0001")
        assert app.wants(Device.HRM)
        app.device_connected(2.0, Device.HRM, "COROS")
        app.tick(2.0 + app.cfg.session.ready_timeout_s)  # nobody started: back to sleep
        await asyncio.sleep(0)
        assert app.state is SessionState.IDLE and released == [True]
        app.close(0)

    asyncio.run(go())
