from pathlib import Path

from claude_alerts.config import Config, load_config


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


def test_config_is_a_dataclass():
    cfg = Config()
    assert cfg.color_working == "#22c55e"
