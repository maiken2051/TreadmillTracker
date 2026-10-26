"""Parser tests: synthetic edge cases plus every notification in every Phase 0 capture."""

from pathlib import Path

import pytest

from gymtracker import units
from gymtracker.ble.hrm import HR_MEASUREMENT_UUID, parse_battery_level, parse_hr_measurement
from gymtracker.ble.treadmill import (
    DATA_UUID,
    STATE_UUID,
    InclineSample,
    TreadmillSample,
    TreadmillState,
    parse_data_frame,
    parse_state,
)
from gymtracker.sim.replay import read_fixture

FIXTURES = Path(__file__).parent / "fixtures"
ALL_FIXTURES = sorted(FIXTURES.glob("*.jsonl"))


# --------------------------------------------------------------------------- HRM


def test_hr_uint8_coros_style():
    s = parse_hr_measurement(bytes.fromhex("0054"))
    assert s.bpm == 84
    assert s.contact is None
    assert s.energy_kj is None
    assert s.rr_ms == []


def test_hr_zero_means_off_skin():
    assert parse_hr_measurement(bytes.fromhex("0000")).bpm is None


def test_hr_uint16_contact_energy_rr():
    # flags: uint16 HR | contact supported+detected | energy | RR
    data = bytes([0x1F]) + (150).to_bytes(2, "little") + (12).to_bytes(2, "little") \
        + (1024).to_bytes(2, "little") + (512).to_bytes(2, "little")
    s = parse_hr_measurement(data)
    assert s.bpm == 150
    assert s.contact is True
    assert s.energy_kj == 12
    assert s.rr_ms == [1000.0, 500.0]


def test_hr_contact_supported_not_detected():
    assert parse_hr_measurement(bytes([0x04, 70])).contact is False


@pytest.mark.parametrize("hex_data", ["", "00", "01aa", "0850"])
def test_hr_malformed(hex_data):
    assert parse_hr_measurement(bytes.fromhex(hex_data)) is None


def test_battery_level():
    assert parse_battery_level(bytes([80])) == 80
    assert parse_battery_level(bytes([101])) is None
    assert parse_battery_level(b"") is None


# --------------------------------------------------------------------------- treadmill


def test_workout_frame_layout():
    # elapsed 308 s, speed 643 (4.0 mph), 418 m, 29 kcal, METs 5.4 (DESIGN.md §4.3)
    data = bytes.fromhex("53cb1a" "34010000" "8302" "a201" "000000" "1d00" "36" "0000")
    s = parse_data_frame(data)
    assert s == TreadmillSample(elapsed_s=308, speed_mps=643 / 360, distance_m=418, kcal=29,
                                mets=5.4)
    assert round(units.mps_to_mph(s.speed_mps), 1) == 4.0


def test_incline_frame():
    assert parse_data_frame(bytes.fromhex("22803200")) == InclineSample(5.0)
    assert parse_data_frame(bytes.fromhex("22801400")) == InclineSample(2.0)


@pytest.mark.parametrize("hex_data", ["", "99", "53cb1a00", "2280140000"])
def test_unknown_or_wrong_length_frame(hex_data):
    assert parse_data_frame(bytes.fromhex(hex_data)) is None


@pytest.mark.parametrize("hex_data, state", [
    ("01", TreadmillState.IDLE),
    ("0a", TreadmillState.RUNNING),
    ("02", TreadmillState.PAUSED_OR_STOPPED),
    ("80", TreadmillState.ENDED),
    ("07", TreadmillState.UNKNOWN),
    ("", TreadmillState.UNKNOWN),
    ("0a0a", TreadmillState.UNKNOWN),
])
def test_state(hex_data, state):
    assert parse_state(bytes.fromhex(hex_data)) is state


# --------------------------------------------------------------------------- fixtures


def test_fixtures_present():
    assert len(ALL_FIXTURES) == 6


@pytest.mark.parametrize("path", ALL_FIXTURES, ids=lambda p: p.stem)
def test_every_notification_parses(path, caplog):
    notes = read_fixture(path)
    assert notes
    for n in notes:
        if n.char == HR_MEASUREMENT_UUID:
            assert parse_hr_measurement(n.data) is not None
        elif n.char == DATA_UUID:
            assert parse_data_frame(n.data) is not None
        elif n.char == STATE_UUID:
            assert parse_state(n.data) is not TreadmillState.UNKNOWN
        else:
            pytest.fail(f"unexpected characteristic {n.char}")
    assert not [r for r in caplog.records if r.levelname == "WARNING"]


def _frames(name):
    return [(n.t, parse_data_frame(n.data)) for n in read_fixture(FIXTURES / name)
            if n.char == DATA_UUID]


def test_walk_fixture_known_values():
    notes = read_fixture(FIXTURES / "treadmill_walk_5min.jsonl")
    t0 = notes[0].t
    states = [(round(n.t - t0, 1), parse_state(n.data)) for n in notes if n.char == STATE_UUID]
    assert states == [
        (25.9, TreadmillState.RUNNING),
        (77.4, TreadmillState.PAUSED_OR_STOPPED),
        (87.7, TreadmillState.RUNNING),
        (192.9, TreadmillState.PAUSED_OR_STOPPED),
        (199.1, TreadmillState.ENDED),
    ]
    workout = [s for _, s in _frames("treadmill_walk_5min.jsonl")
               if isinstance(s, TreadmillSample)]
    assert max(s.speed_mps for s in workout) == pytest.approx(305 / 360)  # 1.9 mph
    assert workout[-1].distance_m == 128
    assert workout[-1].elapsed_s == 156


def test_calibration_walk_matches_console():
    # Console showed 5:08 / 0.26 mi / 29 kcal at 2.0, 3.0, 4.0 mph and 2 %, 5 % incline.
    frames = [s for _, s in _frames("treadmill_calib.jsonl")]
    workout = [s for s in frames if isinstance(s, TreadmillSample)]
    last = workout[-1]
    assert (last.elapsed_s, last.distance_m, last.kcal) == (308, 418, 29)
    assert round(units.m_to_miles(last.distance_m), 2) == 0.26
    mph = {round(units.mps_to_mph(s.speed_mps), 1) for s in workout}
    assert {2.0, 3.0, 4.0} <= mph
    inclines = {s.incline_pct for s in frames if isinstance(s, InclineSample)}
    assert {2.0, 5.0} <= inclines


def test_strap_fixture_heart_rates_plausible():
    bpm = [parse_hr_measurement(n.data).bpm
           for n in read_fixture(FIXTURES / "combined_3min_b.jsonl")
           if n.char == HR_MEASUREMENT_UUID]
    assert len(bpm) == 180
    assert min(bpm) >= 84 and max(bpm) <= 94
