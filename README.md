# claude-alerts

A small daemon that watches Claude Code agents and tells you what they're doing
without you having to look at every terminal.

- **Click-through colored borders** around your Claude Code terminal windows on
  X11 — green when the agent is working, red when it needs you (permission
  prompt, sandbox approval, MCP elicitation, idle turn).
- **A live dashboard** in the daemon's own terminal showing your Claude.ai
  rate-limit usage (5-hour, weekly, per-model) — the same numbers `/usage`
  shows, captured locally with no extra API calls.
- **Survives restarts**: bindings persist to disk, so killing and relaunching
  the daemon doesn't lose the borders for sessions mid-conversation.

> Linux + X11 only. No Wayland support. Tested on Pop!_OS 22.04 with
> gnome-terminal; other terminals with a known `WM_CLASS` work too (kitty,
> alacritty, wezterm, xterm, urxvt, konsole, tilix, terminator).

## Why

When you have several Claude Code agents running at once, the only way to know
which one is waiting for your input is to alt-tab through every terminal. This
tool draws a red border around any window that needs you, leaves the others
green, and surfaces your subscription rate-limit usage in one place — all
without making any extra network calls.

## Status

`v0.4.0`. Works for the author daily on Pop!_OS 22.04. Open-source as of
2026-04. Bug reports welcome.

## Requirements

- Linux + X11 (`echo $XDG_SESSION_TYPE` should print `x11`).
- Python 3.10+
- `jq` (used by the hook scripts)
- `Xvfb` (only for the e2e test)

```sh
sudo apt install -y python3-venv python3-dev libx11-dev jq
# optional, for tests:
sudo apt install -y xvfb
```

## Install

```sh
git clone https://github.com/AbhishekSimforgeAi/claude-alerts ~/src/claude-alerts
cd ~/src/claude-alerts

# install into a dedicated venv
python3 -m venv ~/.local/share/claude-alerts/venv
~/.local/share/claude-alerts/venv/bin/pip install .
ln -sf ~/.local/share/claude-alerts/venv/bin/claude-alerts ~/.local/bin/claude-alerts

# wire the hooks + statusLine into your Claude Code settings.json
~/.local/share/claude-alerts/venv/bin/python scripts/install-hooks.py
```

`scripts/install-hooks.py` does a JSON-aware merge — your existing settings
are preserved. It also sets `statusLine.command` to the bundled
`scripts/hooks/statusline.sh`, but only if you don't already have a custom
`statusLine`. If you do, see [statusLine integration](#statusline-integration)
below.

## Run

Foreground (recommended for first run — you'll see the dashboard):

```sh
claude-alerts
```

As a systemd user unit (no dashboard, logs only):

```sh
mkdir -p ~/.config/systemd/user
cp scripts/claude-alerts.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now claude-alerts.service
journalctl --user -u claude-alerts -f
```

Logs always go to `~/.local/state/claude-alerts/daemon.log` regardless of how
you start it.

## What you'll see

In any Claude Code terminal:

```
┌──────────────────────┐
│ Claude is working... │   ← green border
└──────────────────────┘
```

…flips to red when the agent emits a `Notification`, `PermissionRequest`
(sandbox prompts), or `Elicitation` (MCP user-input dialog), or when it's
sitting idle at `Stop`. Stays green during autonomous wake-ups (`Monitor`,
`CronCreate`, `RemoteTrigger`, `ScheduleWakeup`) so you don't get a false
"needs me" signal while a background task is alive.

The border is also **modulated by focus**: only the currently-focused
Claude terminal paints at full brightness — every other bound terminal
dims its border to ¼ intensity. When focus moves to a non-Claude window
(another app, the dashboard) all Claude borders go dim. This makes a
single needs-me terminal jump out across a screen full of Claude sessions
without any of them being individually loud.

In the daemon's own terminal:

```
claude-alerts daemon · 2 sessions · limits updated 4s ago
────────────────────────────────────────────────────────────────────
  5-hour    ████████░░░░░░░░░░░░   42.5%  resets in 1h 59m
  weekly    ████░░░░░░░░░░░░░░░░   18.3%  resets in 4d 23h
  Opus      ███████████░░░░░░░░░   56.0%  resets in 4d 23h

  SESSION   STATUS     CWD
  5756986d  ● working  ~/claude-alerts
  d80768ae  ○ waiting  ~/city-digital-twin
────────────────────────────────────────────────────────────────────
press Ctrl-C to exit · log → stderr · refresh 2s
```

## Configuration

Optional — defaults are baked in. Override at
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

CLI flags:

```
--no-persistence              don't load/save bindings across restarts
--persistence-path PATH       override the sessions.json path
--no-dashboard                don't render the rate-limit dashboard, even on a TTY
--events-dir DIR              where hook events land (default ~/.local/state/claude-alerts/events)
--config FILE                 path to config.toml
```

## statusLine integration

The dashboard's rate-limit numbers come from Claude Code's statusLine: every
prompt update, Claude Code pipes a JSON blob (containing
`rate_limits.five_hour`, `rate_limits.seven_day`, etc.) to whatever command
you've configured as your `statusLine`. We ship a tiny helper that captures
that JSON to a sidecar file the daemon reads.

`install-hooks.py` configures it for you on a fresh install. If you already
have a custom statusLine, install-hooks won't overwrite it — chain to the
helper via:

```sh
export CLAUDE_ALERTS_WRAPPED_STATUSLINE=/abs/path/to/your/statusline.sh
```

The wrapped command must be an absolute path that's executable; relative paths
and shell-metacharacter values are rejected.

## Troubleshooting

**No border appears around a new agent.** The binder fires on
`UserPromptSubmit` and only when it can identify the active window as a
known terminal. Check `~/.local/state/claude-alerts/daemon.log` for
`queueing manual bind` entries. If your terminal isn't on the allowlist
(`gnome-terminal-server`, `kitty`, `alacritty`, `wezterm`, `xterm`, `urxvt`,
`konsole`, `tilix`, `terminator`), file an issue.

**Daemon won't start with `Xlib.error.DisplayConnectionError`.** You're not in
an X11 session. This tool does not support Wayland.

**Border lost after a daemon restart.** Send any new prompt in the affected
terminal — the binding re-establishes on the next `UserPromptSubmit`. The
daemon also restores bindings from `~/.local/state/claude-alerts/sessions.json`
on startup, so if the daemon was running when those windows last received
events, they should rebind automatically.

**Dashboard says "no statusLine data yet".** Run a Claude Code session and
send any message. The first API response populates the sidecar.

**Border stayed red after the user replied.** Slash commands don't fire
`UserPromptSubmit` — they fire `UserPromptExpansion`, which the daemon
doesn't subscribe to. Type a real message instead.

## How it works

```
                                      ┌─────────────────┐
Claude Code  ─── hook ────►  emit-event.sh  ─── JSON ──►  events/<ts>-<sid>.json
                                                                  │
                                                                  ▼ inotify
                                                      ┌──────────────────────┐
                                                      │   claude-alerts      │
                                                      │      daemon          │
                                                      │                      │
                                  ┌─── X11 root substructure events ───────►  │
                                  │                   │                      │
                                  ▼                   ▼                      │
                            terminal              SessionStore               │
                            window IDs            (state machine,            │
                                  ▲                background_active,        │
                                  │                last_event)               │
                                  │                   │                      │
                                  └──── overlay ──────┘                      │
                                       (X11 click-through                    │
                                        bordered window)                     │
                                                      │                      │
                                                      ▼                      │
                                                 sessions.json                │
                                                 (on every change)            │
                                                                              │
Claude Code statusLine ──── stdin ──► statusline.sh ──► rate_limits.json ─────┘
                                                              │
                                                              ▼ tick (2s)
                                                       Dashboard (TTY only)
```

Three independent data flows:

1. **Hooks → state machine → overlays.** Claude Code's hook events are written
   as JSON files into a watched directory. The daemon picks them up via
   inotify, runs them through the session state machine, and updates X11
   borders.
2. **statusLine → sidecar → dashboard.** Claude Code feeds its statusLine
   command a JSON blob with `rate_limits` data on every prompt refresh. Our
   helper captures it to `rate_limits.json`. The daemon reads the sidecar on
   each 2-second tick.
3. **Persistence.** Every meaningful state change is snapshotted (atomic, with
   fsync, mode 0600) to `sessions.json`. On startup, the daemon restores
   bindings whose windows still exist.

The daemon makes **zero outbound network calls.** All data is data Claude
Code already has and chose to expose to subprocesses.

## Privacy / data handling

State stored on disk under `~/.local/state/claude-alerts/`:

- `daemon.log` — rotating log, INFO/WARNING level. No message content.
- `sessions.json` — pids, cwds, session-ids, status, bound window-ids.
  Mode `0600`.
- `rate_limits.json` — copy of the `rate_limits` block Claude Code feeds
  the statusLine. Mode `0600`.
- `events/` — transient hook event files; consumed and deleted within
  milliseconds. Directory mode `0700`.

No telemetry, no calls home, no auth tokens are read or written.

## Development

```sh
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/pytest
```

The end-to-end test under `tests/test_e2e_xvfb.py` requires `Xvfb` and is
skipped if it's not installed:

```sh
sudo apt install -y xvfb
.venv/bin/pytest tests/test_e2e_xvfb.py
```

Source layout:

```
claude_alerts/
├── __main__.py        CLI entrypoint
├── daemon.py          main loop: select() over X11 fd + event queue
├── ingester.py        inotify watcher → main thread queue
├── events.py          ClaudeEvent + JSON parser
├── sessions.py        state machine, background_active lifecycle
├── binder.py          maps sessions to X11 windows
├── overlay.py         OverlayManager + click-through bordered windows
├── x11.py             python-xlib wrapper
├── persistence.py     BindingPersister (atomic, throttled, fsynced)
├── dashboard.py       stdout TTY dashboard
├── limits.py          rate_limits.json sidecar parser
└── config.py          config.toml loader
scripts/
├── hooks/
│   ├── emit-event.sh  hook → events/ JSON file
│   └── statusline.sh  statusLine → rate_limits.json sidecar
├── install-hooks.py   merge hooks + statusLine into ~/.claude/settings.json
└── claude-alerts.service   systemd user unit
docs/superpowers/specs/    design docs for major features
```

## Contributing

Bug reports, patches, and terminal-allowlist additions welcome.

A few non-obvious project conventions:

- **All X11 calls happen on the main thread.** `python-xlib` is not
  thread-safe. The ingester thread marshals events through a `queue.SimpleQueue`
  and the main loop drains it.
- **The session state machine is two-state (WORKING / WAITING).** Color
  decisions also depend on `last_event` and `background_active`, which the
  overlay layer reads — not the state itself.
- **Bindings only fire on `UserPromptSubmit`.** That's the only event that
  proves the user is focused on the Claude terminal.
- **Hook scripts are user-trusted.** They write JSON to a state directory the
  daemon owns; the daemon validates everything before using it. `session_id`
  is sanitized before going into a filesystem path; control chars are
  stripped from anything that flows to the TTY.

For larger changes, please open an issue first to discuss design. The specs
under `docs/superpowers/specs/` are the canonical record of how the existing
features are structured.

## License

MIT — see [LICENSE](LICENSE).
