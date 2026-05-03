"""Stdout dashboard for the claude-alerts daemon.

When stdout is a TTY, render a live view of the user's Claude.ai
subscription rate limits — the same numbers `/usage` shows — alongside a
short list of currently-active sessions. When stdout is not a TTY (systemd,
pipes, log redirection), behave as a no-op so the existing log stream is
unaffected.

Data sources:

- claude_alerts.limits.load(sidecar_path): rate-limit data captured by
  scripts/hooks/statusline.sh on every Claude Code prompt update.
- SessionStore: the list of currently-bound and idle Claude sessions, for
  the "active sessions" footer.

Refresh triggers:

- SessionStore.on_change → mark_dirty (so a session flipping working ↔
  waiting paints immediately).
- Periodic 2 s tick called from the daemon main loop (re-reads the
  sidecar + paints).

Painting uses ANSI cursor-home + clear-from-cursor (no alternate-screen,
no curses) so Ctrl-C and shutdown logging stay legible.
"""
from __future__ import annotations

import logging
import os
import re
import sys
import time
from pathlib import Path
from typing import Optional, TextIO

from claude_alerts import contexts, limits
from claude_alerts.contexts import ContextUsage
from claude_alerts.limits import (
    Limit,
    RateLimits,
    WINDOW_KEYS,
    WINDOW_LABELS,
    default_sidecar_path,
)
from claude_alerts.sessions import Session, SessionStore, Status

log = logging.getLogger(__name__)


_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")


def _strip_control(s: str) -> str:
    """Replace ASCII control characters (incl. ESC) with '?'.

    Anything that flows from a hostile or buggy hook payload to the
    daemon's TTY must be defanged: ESC sequences could rewrite the
    terminal title, move the cursor, or trigger RCE on vulnerable
    terminals.
    """
    return _CONTROL_CHARS.sub("?", s)


def _short_id(session_id: str) -> str:
    return _strip_control(session_id.split("-", 1)[0])


def _short_cwd(cwd: str, max_chars: int = 50) -> str:
    cwd = _strip_control(cwd)
    home = str(Path.home())
    if cwd.startswith(home):
        cwd = "~" + cwd[len(home):]
    if len(cwd) <= max_chars:
        return cwd
    return "…" + cwd[-(max_chars - 1):]


def _status_marker(status: Status) -> str:
    return "● working" if status == Status.WORKING else "○ waiting"


def _bar(pct: float, width: int) -> str:
    """Render a progress bar of given character width filled to pct (0-100)."""
    pct = max(0.0, min(100.0, pct))
    filled = int(round(pct / 100 * width))
    return "█" * filled + "░" * (width - filled)


def _format_resets_in(now_s: float, resets_at: int) -> str:
    delta = int(resets_at - now_s)
    if delta <= 0:
        return "any moment"
    days, rem = divmod(delta, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m"
    return f"{delta}s"


def _format_age(now_s: float, saved_at: float) -> str:
    delta = max(0, int(now_s - saved_at))
    if delta < 60:
        return f"{delta}s ago"
    if delta < 3600:
        return f"{delta // 60}m ago"
    return f"{delta // 3600}h ago"


def _short_tokens(n: int) -> str:
    """Compact token-count formatting matching what /context shows.

    <1000      -> '850'
    <1_000_000 -> '90k' (rounded to nearest thousand)
    >=1_000_000 -> '1.0M' (one decimal place)
    """
    if n < 1_000:
        return str(n)
    if n < 1_000_000:
        return f"{round(n / 1_000)}k"
    return f"{n / 1_000_000:.1f}M"


def _format_ctx(cu: "ContextUsage | None") -> str:
    """Format a ContextUsage as a fixed-width 16-char left-justified string.

    Returns the en-dash '—' (padded) when `cu` is None or any required
    field is None. Sub-1% non-zero usage renders as '<1% (...)' rather
    than '0% (...)' so the user knows tokens are accumulating.
    """
    width = 16
    if cu is None or cu.used_percentage is None or cu.used_tokens is None or cu.total_tokens is None:
        return f"{'—':<{width}}"
    used = _short_tokens(cu.used_tokens)
    total = _short_tokens(cu.total_tokens)
    if 0 < cu.used_percentage < 1:
        text = f"<1% ({used}/{total})"
    else:
        pct = int(round(cu.used_percentage))
        text = f"{pct}% ({used}/{total})"
    return f"{text:<{width}}"


class Dashboard:
    """Owns the rendering loop and reads limits from the statusLine sidecar.

    The daemon constructs one of these, registers `on_session_changed` as
    a SessionStore listener, and calls `tick()` from its main loop. tick()
    is idempotent and cheap when nothing has changed.
    """

    PAINT_DEBOUNCE_S = 0.25
    TICK_INTERVAL_S = 2.0
    SIDECAR_STALE_AFTER_S = 1800  # 30 minutes — beyond this, mark "stale"

    def __init__(
        self,
        store: SessionStore,
        sidecar_path: Optional[Path] = None,
        out: Optional[TextIO] = None,
        force_render: bool = False,
        contexts_dir: Optional[Path] = None,
    ) -> None:
        self.store = store
        self.sidecar_path = sidecar_path or default_sidecar_path()
        self.contexts_dir = contexts_dir or contexts.default_contexts_dir()
        self.out = out if out is not None else sys.stdout
        self.enabled = force_render or self._is_tty(self.out)
        self._dirty = True
        self._last_paint = 0.0
        self._last_tick = 0.0
        self._cached_limits: Optional[RateLimits] = None

    @staticmethod
    def _is_tty(out: TextIO) -> bool:
        try:
            return out.isatty()
        except (AttributeError, ValueError):
            return False

    def on_session_changed(self, session_id: str) -> None:
        self._dirty = True

    def mark_dirty(self) -> None:
        self._dirty = True

    def tick(self) -> None:
        if not self.enabled:
            return
        now = time.monotonic()
        if now - self._last_tick >= self.TICK_INTERVAL_S:
            self._cached_limits = limits.load(self.sidecar_path)
            self._last_tick = now
            self._dirty = True

        if not self._dirty:
            return
        if now - self._last_paint < self.PAINT_DEBOUNCE_S:
            return
        self._paint()
        self._last_paint = now
        self._dirty = False

    def shutdown(self) -> None:
        if not self.enabled:
            return
        try:
            self.out.write("\n")
            self.out.flush()
        except OSError:
            pass

    def render_string(self) -> str:
        """Return the dashboard text without ANSI escapes — used by tests."""
        return self._build_lines(width=100)

    def _paint(self) -> None:
        try:
            width = os.get_terminal_size().columns
        except OSError:
            width = 100
        text = self._build_lines(width=width)
        try:
            # Cursor home + clear from cursor to end of screen, then content.
            self.out.write("\x1b[H\x1b[J" + text)
            self.out.flush()
        except OSError:
            pass

    def _build_lines(self, width: int) -> str:
        rl = self._cached_limits
        # Sort by first_seen_at ascending so a row's position is stable across
        # status flips (WORKING ↔ WAITING) and event refreshes — once a session
        # registers, it stays where it is until it ends.
        sessions = sorted(self.store.all(), key=lambda s: s.first_seen_at)
        rule = "─" * min(width, 100)
        lines: list[str] = []
        lines.append(self._header(rl, len(sessions)))
        lines.append(rule)
        lines.extend(self._limits_block(rl, width))
        lines.append("")
        lines.extend(self._sessions_block(sessions))
        lines.append(rule)
        lines.append("press Ctrl-C to exit · log → stderr · refresh 2s")
        # Hard-cap each line to terminal width so a narrow terminal doesn't
        # wrap and corrupt the cursor-home repaint.
        clipped = [line[:width] if len(line) > width else line for line in lines]
        return "\n".join(clipped) + "\n"

    def _header(self, rl: Optional[RateLimits], active: int) -> str:
        now_s = time.time()
        if rl is None:
            tail = "no statusLine data yet — see scripts/hooks/statusline.sh"
        else:
            age = _format_age(now_s, rl.saved_at)
            stale = (
                "stale "
                if rl.saved_at and now_s - rl.saved_at > self.SIDECAR_STALE_AFTER_S
                else ""
            )
            tail = f"limits {stale}updated {age}"
        return f"claude-alerts daemon · {active} session{'' if active == 1 else 's'} · {tail}"

    def _limits_block(self, rl: Optional[RateLimits], width: int) -> list[str]:
        if rl is None or not rl.any_present():
            return [
                "  rate limits: not yet captured.",
                "  install scripts/hooks/statusline.sh as your statusLine and",
                "  send a message in any Claude Code session to populate.",
            ]
        bar_width = max(10, min(20, width - 50))
        now_s = time.time()
        rows: list[str] = []
        for key in WINDOW_KEYS:
            limit = rl.get(key)
            if limit is None:
                continue
            label = WINDOW_LABELS[key]
            bar = _bar(limit.used_percentage, bar_width)
            pct = f"{limit.used_percentage:5.1f}%"
            resets = _format_resets_in(now_s, limit.resets_at)
            rows.append(f"  {label:<8}  {bar}  {pct}  resets in {resets}")
        return rows

    def _sessions_block(self, sessions: list[Session]) -> list[str]:
        if not sessions:
            return ["  no active sessions."]
        # CTX column is fixed-width 16 (see _format_ctx).
        rows = [f"  SESSION   STATUS     {'CTX':<16}  CWD"]
        for s in sessions:
            sid = _short_id(s.session_id)
            cwd = _short_cwd(s.cwd)
            cu = contexts.load(s.session_id, self.contexts_dir)
            ctx = _format_ctx(cu)
            rows.append(f"  {sid:<8}  {_status_marker(s.status):<9}  {ctx}  {cwd}")
        return rows
