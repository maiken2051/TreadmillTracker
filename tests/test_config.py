"""Config loading and validation."""

from pathlib import Path

import pytest

from gymtracker.config import Config, load_config

EXAMPLE = Path(__file__).parent.parent / "config.example.toml"


def test_defaults_without_file():
    assert load_config() == Config()


def test_example_file_equals_defaults():
    assert load_config(EXAMPLE) == Config()


def test_override_and_int_to_float(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text('[session]\nmin_session_s = 120\n[user]\nunits = "metric"\n')
    cfg = load_config(p)
    assert cfg.session.min_session_s == 120.0
    assert isinstance(cfg.session.min_session_s, float)
    assert cfg.user.units == "metric"
    assert cfg.session.start_hold_s == 3  # untouched default


@pytest.mark.parametrize("text", [
    "[nope]\nx = 1\n",
    "[session]\nnope = 1\n",
    '[session]\nmin_session_s = "60"\n',
    "[web]\nport = 80.5\n",
    '[user]\nunits = "furlongs"\n',
])
def test_bad_config_rejected(tmp_path, text):
    p = tmp_path / "c.toml"
    p.write_text(text)
    with pytest.raises(ValueError):
        load_config(p)
