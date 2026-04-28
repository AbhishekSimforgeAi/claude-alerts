"""Stdout dashboard for the claude-alerts daemon.

When stdout is a TTY, render an auto-refreshing per-session table to the
controlling terminal. When stdout is not a TTY (systemd, pipes, log
redirection), behave as a no-op so the existing log stream is unaffected.

Data sources:
- SessionStore — current per-session status (working/waiting), bound state.
- JsonlTailer — token counts, cost, turn count, model, context-window %.

Refresh triggers:
- SessionStore.on_change → mark_dirty.
- Periodic tick (called from the daemon main loop) → poll JSONL files and
  re-render if dirty.

Painting uses ANSI cursor-home + clear-from-cursor (no alternate-screen
buffer, no curses dep) so Ctrl-C and shutdown logging stay legible.
"""
from __future__ import annotations

import datetime
import logging
import os
import sys
import time
from pathlib import Path
from typing import Optional, TextIO

from claude_alerts.sessions import Session, SessionStore, Status
from claude_alerts.transcripts import JsonlTailer, SessionUsage

log = logging.getLogger(__name__)


def _format_tokens(n: float) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return f"{int(n)}"


def _format_cost(usd: float) -> str:
    if usd >= 100:
        return f"${usd:.0f}"
    if usd >= 1:
        return f"${usd:.2f}"
    return f"${usd:.3f}"


def _short_model(model: str) -> str:
    """claude-opus-4-7 → opus-4-7, claude-haiku-4-5-20251001 → haiku-4-5."""
    if model.startswith("claude-"):
        model = model[len("claude-"):]
    parts = model.split("-")
    keep = []
    for p in parts:
        if p.isdigit() and len(p) >= 8:
            break  # date stamp at the end
        keep.append(p)
    return "-".join(keep) or model


def _short_cwd(cwd: str, max_chars: int = 36) -> str:
    home = str(Path.home())
    if cwd.startswith(home):
        cwd = "~" + cwd[len(home):]
    if len(cwd) <= max_chars:
        return cwd
    return "…" + cwd[-(max_chars - 1):]


def _short_id(session_id: str) -> str:
    return session_id.split("-", 1)[0]


def _status_marker(s: Status, idle: bool) -> str:
    if idle:
        return "○ idle   "
    return "● working" if s == Status.WORKING else "○ waiting"


class Dashboard:
    """Owns the rendering loop and a JsonlTailer for token data.

    The daemon constructs one of these, registers `mark_dirty` as a
    SessionStore listener, and calls `tick()` from its main loop. tick()
    is idempotent and cheap when nothing has changed.
    """

    IDLE_THRESHOLD_S = 300  # 5 minutes since last assistant message → "idle"
    PAINT_DEBOUNCE_S = 0.25
    TICK_INTERVAL_S = 2.0

    def __init__(
        self,
        store: SessionStore,
        projects_root: Optional[Path] = None,
        out: Optional[TextIO] = None,
        force_render: bool = False,
    ) -> None:
        self.store = store
        self.projects_root = projects_root or (Path.home() / ".claude" / "projects")
        self.out = out if out is not None else sys.stdout
        self.tailer = JsonlTailer(self.projects_root)
        self.enabled = force_render or self._is_tty(self.out)
        self._dirty = True
        self._last_paint = 0.0
        self._last_tick = 0.0

    @staticmethod
    def _is_tty(out: TextIO) -> bool:
        try:
            return out.isatty()
        except (AttributeError, ValueError):
            return False

    def mark_dirty(self) -> None:
        self._dirty = True

    def on_session_changed(self, session_id: str) -> None:
        # Pull this session's transcript on every state change so the
        # tokens column updates as soon as a new turn arrives.
        s = self.store.get(session_id)
        if s is not None:
            self.tailer.tail(s.session_id, s.cwd)
        self.mark_dirty()

    def tick(self) -> None:
        """Called from the daemon main loop. Cheap if nothing has changed."""
        if not self.enabled:
            return
        now = time.monotonic()
        if now - self._last_tick >= self.TICK_INTERVAL_S:
            self.tailer.tail_all_known()
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
        return self._build_lines(width=120, with_ansi=False)

    def _paint(self) -> None:
        try:
            width = os.get_terminal_size().columns
        except OSError:
            width = 120
        text = self._build_lines(width=width, with_ansi=True)
        try:
            # Cursor home, clear from cursor to end of screen, then content.
            self.out.write("\x1b[H\x1b[J" + text)
            self.out.flush()
        except OSError:
            pass

    def _build_lines(self, width: int, with_ansi: bool) -> str:
        sessions = self._snapshot_rows()
        totals = self.tailer.today_totals()
        header = self._header(len(sessions), totals)
        rule = "─" * min(width, 120)
        rows = [self._format_row(row, width) for row in sessions]
        footer = "press Ctrl-C to exit · log → stderr · refresh 2s"
        parts = [header, rule] + rows + [rule, footer]
        return "\n".join(parts) + "\n"

    def _header(self, active: int, totals: dict) -> str:
        return (
            f"claude-alerts daemon · {active} active · "
            f"today: {int(totals.get('turns', 0))} turns · "
            f"{_format_tokens(totals.get('tokens', 0))} tokens · "
            f"{_format_cost(totals.get('cost_usd', 0))}"
        )

    def _snapshot_rows(self) -> list[dict]:
        rows = []
        now_dt = datetime.datetime.now(datetime.timezone.utc)
        for s in self.store.all():
            usage = self.tailer.get(s.session_id) or SessionUsage(s.session_id, s.cwd)
            idle = False
            if usage.last_assistant_at is not None:
                idle = (now_dt - usage.last_assistant_at).total_seconds() > self.IDLE_THRESHOLD_S
            rows.append({"session": s, "usage": usage, "idle": idle})

        def sort_key(r):
            s: Session = r["session"]
            if s.status == Status.WORKING and not r["idle"]:
                bucket = 0
            elif s.status == Status.WAITING and not r["idle"]:
                bucket = 1
            else:
                bucket = 2
            last = r["usage"].last_assistant_at
            return (bucket, -(last.timestamp() if last else 0))

        rows.sort(key=sort_key)
        return rows

    def _format_row(self, row: dict, width: int) -> str:
        s: Session = row["session"]
        u: SessionUsage = row["usage"]
        sid = _short_id(s.session_id)
        cwd = _short_cwd(s.cwd)
        status = _status_marker(s.status, row["idle"])
        model = _short_model(u.model) if u.model else "—"

        if u.cost_unknown and u.model not in {""}:
            cost_cell = "?"
        else:
            cost_cell = _format_cost(u.cost_usd) if u.model else "—"

        tok_in = _format_tokens(u.input_tokens + u.cache_read_tokens + u.cache_write_tokens) if u.model else "—"
        tok_out = _format_tokens(u.output_tokens) if u.model else "—"
        turns = str(u.turns) if u.model else "—"

        ctx = u.context_window_pct()
        if ctx is None or u.last_input_window == 0:
            ctx_cell = "—"
        else:
            used, cap = ctx
            pct = (used / cap) * 100 if cap else 0
            ctx_cell = f"{pct:.0f}% {_format_tokens(used)}/{_format_tokens(cap)}"

        # Drop columns left-to-right as width shrinks.
        cells = [
            (sid,    8),
            (cwd,   36),
            (status, 9),
            (model, 12),
            (tok_in, 9),
            (tok_out, 9),
            (cost_cell, 8),
            (turns,   6),
            (ctx_cell, 18),
        ]
        if width < 100:
            cells = cells[:-1]  # drop ctx
        if width < 90:
            cells = cells[:-1]  # drop turns
        if width < 80:
            cells = cells[:-1]  # drop cost
        if width < 70:
            cells = cells[:-2] + [cells[-1]] if False else cells[:-2]  # drop tok cells
        return "  ".join(c[0].ljust(c[1]) for c in cells).rstrip()
