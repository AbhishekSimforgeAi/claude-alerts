from pathlib import Path

import pytest

from claude_alerts.config import Config, ConfigError, load_config


def test_defaults_when_no_file(tmp_path):
    cfg = load_config(tmp_path / "missing.toml")
    assert cfg.color_working == "#22c55e"
    assert cfg.color_waiting == "#ef4444"
    assert cfg.border_thickness_px == 4
    assert cfg.log_level == "INFO"


def test_overrides_from_file(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text(
        '[colors]\n'
        'working = "#00ff00"\n'
        'waiting = "#ff0000"\n'
        '\n'
        '[border]\n'
        'thickness_px = 8\n'
        '\n'
        '[debug]\n'
        'log_level = "DEBUG"\n'
    )
    cfg = load_config(p)
    assert cfg.color_working == "#00ff00"
    assert cfg.color_waiting == "#ff0000"
    assert cfg.border_thickness_px == 8
    assert cfg.log_level == "DEBUG"


def test_partial_override(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text('[border]\nthickness_px = 12\n')
    cfg = load_config(p)
    assert cfg.border_thickness_px == 12
    assert cfg.color_working == "#22c55e"  # default preserved


def test_empty_file(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text("")
    cfg = load_config(p)
    assert cfg == Config()


def test_unknown_section_ignored(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text('[future_feature]\nfoo = "bar"\n')
    cfg = load_config(p)
    assert cfg == Config()


def test_malformed_toml_raises(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text("this is not valid toml ===\n")
    with pytest.raises(ConfigError, match="cannot parse"):
        load_config(p)


def test_wrong_type_thickness_string_raises(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text('[border]\nthickness_px = "four"\n')
    with pytest.raises(ConfigError, match="border.thickness_px"):
        load_config(p)


def test_wrong_type_thickness_float_raises(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text('[border]\nthickness_px = 3.7\n')
    with pytest.raises(ConfigError, match="border.thickness_px"):
        load_config(p)


def test_wrong_type_color_int_raises(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text('[colors]\nworking = 42\n')
    with pytest.raises(ConfigError, match="colors.working"):
        load_config(p)


def test_config_is_frozen():
    from dataclasses import FrozenInstanceError
    cfg = Config()
    with pytest.raises(FrozenInstanceError):
        cfg.color_working = "#000000"


def test_wrong_type_colors_section_raises(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text('colors = "not a table"\n')
    with pytest.raises(ConfigError, match="colors must be a TOML table"):
        load_config(p)


def test_wrong_type_border_section_raises(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text('border = "not a table"\n')
    with pytest.raises(ConfigError, match="border must be a TOML table"):
        load_config(p)


def test_wrong_type_debug_section_raises(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text('debug = "not a table"\n')
    with pytest.raises(ConfigError, match="debug must be a TOML table"):
        load_config(p)
