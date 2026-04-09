# claude-alerts

Background daemon that draws colored borders around Claude Code agent terminal
windows on X11 — **green when the agent is working, red when it is waiting on
you for any reason** (a permission prompt, an idle turn, anything).

Target environment: Pop!_OS 22.04, X11, gnome-terminal. Other terminals with a
distinctive `WM_CLASS` may work but are not tested.

## Install

Requires Python 3.10+, `jq`, and `Xvfb` for tests.

    sudo apt install -y python3-venv python3-dev libx11-dev jq
    git clone <this repo> ~/src/claude-alerts
    cd ~/src/claude-alerts
    python3 -m venv ~/.local/share/claude-alerts/venv
    ~/.local/share/claude-alerts/venv/bin/pip install .
    ln -sf ~/.local/share/claude-alerts/venv/bin/claude-alerts ~/.local/bin/claude-alerts

Then install the hook entries into your Claude settings:

    python3 scripts/install-hooks.py

This is a JSON-aware merge — your existing settings are preserved.

## Run

One-shot:

    claude-alerts

Background as a systemd user unit:

    mkdir -p ~/.config/systemd/user
    cp scripts/claude-alerts.service ~/.config/systemd/user/
    systemctl --user daemon-reload
    systemctl --user enable --now claude-alerts.service
    systemctl --user status claude-alerts.service

Logs:

    tail -f ~/.local/state/claude-alerts/daemon.log

## Configuration

Optional. Defaults are baked in. To override, create
`~/.config/claude-alerts/config.toml`:

```toml
[colors]
working = "#22c55e"
waiting = "#ef4444"

[border]
thickness_px = 4

[debug]
log_level = "INFO"
```

## Troubleshooting

- **No border appears around a new agent.** Check `~/.local/state/claude-alerts/daemon.log`. Most common cause: the binder couldn't identify the active window as a terminal at SessionStart. The daemon will queue a manual bind — click your terminal window when prompted.
- **Daemon won't start with `Xlib.error.DisplayConnectionError`.** You're not in an X11 session. This tool does not support Wayland.
- **Border drawn under the terminal after raise-on-click.** The daemon re-raises overlays on `VisibilityNotify`, but if your window manager is aggressive, file an issue.

## Development

    python3 -m venv .venv
    .venv/bin/pip install -e ".[dev]"
    .venv/bin/pytest

The end-to-end test under `tests/test_e2e_xvfb.py` requires Xvfb (`sudo apt install -y xvfb`).
