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


# ---------- unfocused border colour overrides (#12) ----------
#
# Four matrix cells: both keys present, only working_unfocused, only
# waiting_unfocused, neither. Plus malformed-value cases.


def test_unfocused_neither_present_defaults_to_none(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text('[colors]\nworking = "#22c55e"\nwaiting = "#ef4444"\n')
    cfg = load_config(p)
    assert cfg.color_working_unfocused is None
    assert cfg.color_waiting_unfocused is None


def test_unfocused_only_working_present(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text('[colors]\nworking_unfocused = "#0a0a0a"\n')
    cfg = load_config(p)
    assert cfg.color_working_unfocused == "#0a0a0a"
    assert cfg.color_waiting_unfocused is None


def test_unfocused_only_waiting_present(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text('[colors]\nwaiting_unfocused = "#1b0000"\n')
    cfg = load_config(p)
    assert cfg.color_working_unfocused is None
    assert cfg.color_waiting_unfocused == "#1b0000"


def test_unfocused_both_present(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text(
        '[colors]\n'
        'working_unfocused = "#0a0a0a"\n'
        'waiting_unfocused = "#1b0000"\n'
    )
    cfg = load_config(p)
    assert cfg.color_working_unfocused == "#0a0a0a"
    assert cfg.color_waiting_unfocused == "#1b0000"


def test_unfocused_working_non_string_raises(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text('[colors]\nworking_unfocused = 42\n')
    with pytest.raises(ConfigError, match="colors.working_unfocused"):
        load_config(p)


def test_unfocused_waiting_non_string_raises(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text('[colors]\nwaiting_unfocused = true\n')
    with pytest.raises(ConfigError, match="colors.waiting_unfocused"):
        load_config(p)


def test_unfocused_working_bad_hex_raises(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text('[colors]\nworking_unfocused = "#zz0000"\n')
    with pytest.raises(ConfigError, match="colors.working_unfocused"):
        load_config(p)


def test_unfocused_waiting_wrong_length_raises(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text('[colors]\nwaiting_unfocused = "#abcde"\n')
    with pytest.raises(ConfigError, match="colors.waiting_unfocused"):
        load_config(p)
