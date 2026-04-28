"""Tail Claude Code JSONL transcripts and aggregate per-session usage.

Claude Code writes one JSONL file per session under
~/.claude/projects/<encoded-cwd>/<session_id>.jsonl, where <encoded-cwd>
is `cwd.replace("/", "-")` (leading dash retained). Each line is a JSON
object; `type: "assistant"` lines carry `message.usage` with the token
counts and `message.model` with the model id used for that turn.

This module reads new bytes from each tracked file (per-file offset) and
folds the new assistant lines into a per-session SessionUsage record.
"""
from __future__ import annotations

import datetime
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from claude_alerts.pricing import context_window_for, cost_usd

log = logging.getLogger(__name__)


def encoded_cwd(cwd: str) -> str:
    """Mirror Claude Code's path → directory-name encoding."""
    return cwd.replace("/", "-")


def jsonl_path_for(projects_root: Path, cwd: str, session_id: str) -> Path:
    return projects_root / encoded_cwd(cwd) / f"{session_id}.jsonl"


def _utc_date_of(iso_ts: str) -> Optional[datetime.date]:
    """Parse a JSONL timestamp ('2026-04-27T16:12:50.584Z') to a UTC date."""
    try:
        # fromisoformat handles 'Z' starting in 3.11; for 3.10 substitute +00:00.
        s = iso_ts.replace("Z", "+00:00")
        return datetime.datetime.fromisoformat(s).astimezone(datetime.timezone.utc).date()
    except (ValueError, TypeError):
        return None


@dataclass
class SessionUsage:
    """Running per-session totals folded from JSONL assistant lines."""
    session_id: str
    cwd: str
    model: str = ""
    input_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    turns: int = 0
    last_input_window: int = 0  # input_tokens + cache_* of the most recent assistant message
    last_assistant_at: Optional[datetime.datetime] = None
    cost_unknown: bool = False  # any line had an unknown model

    def context_window_pct(self) -> Optional[tuple[int, int]]:
        """Return (used, cap) for the most recent assistant turn, or None
        if model is unknown."""
        cap = context_window_for(self.model)
        if cap is None:
            return None
        return self.last_input_window, cap


@dataclass
class _FileState:
    path: Path
    offset: int = 0
    usage: SessionUsage = field(default_factory=lambda: SessionUsage("", ""))


class JsonlTailer:
    """Track per-session SessionUsage by tailing JSONL transcript files.

    Read patterns:

    - `tail(session_id, cwd)` (re-)reads new bytes from this session's JSONL
      and folds them into the session's SessionUsage. Idempotent — calling
      it twice with no new bytes is a no-op.

    - `tail_all_known()` calls tail() on every session it has ever seen.
      Used by the dashboard's periodic refresh.

    - `today_totals()` aggregates today's tokens / cost / turns across all
      tracked sessions.

    The tailer never reads files for sessions it has not been explicitly
    told about; the dashboard adds sessions when SessionStore notifies.
    """

    def __init__(self, projects_root: Path) -> None:
        self.projects_root = projects_root
        # Keyed by session_id, not path, so a missing-then-reappearing file
        # doesn't double-count.
        self._sessions: dict[str, _FileState] = {}
        # Per-day buckets aggregated across all sessions: date → totals.
        self._daily: dict[datetime.date, dict[str, float]] = {}

    def tail(self, session_id: str, cwd: str) -> SessionUsage:
        """Read new bytes for this session's JSONL and update its usage.
        Returns the up-to-date SessionUsage even if no new bytes arrived."""
        path = jsonl_path_for(self.projects_root, cwd, session_id)
        state = self._sessions.get(session_id)
        if state is None:
            state = _FileState(
                path=path,
                usage=SessionUsage(session_id=session_id, cwd=cwd),
            )
            self._sessions[session_id] = state
        elif state.path != path:
            # cwd changed mid-session — Claude Code reopens the JSONL under
            # a new project dir. Reset offset; counters carry over.
            state.path = path
            state.offset = 0

        try:
            with state.path.open("rb") as f:
                f.seek(state.offset)
                chunk = f.read()
                state.offset = f.tell()
        except FileNotFoundError:
            return state.usage
        except OSError as e:
            log.debug("tail %s: %s", state.path, e)
            return state.usage

        if not chunk:
            return state.usage

        # Lines may be split mid-buffer if the writer hasn't flushed; only
        # process complete lines and rewind to the last newline if needed.
        last_nl = chunk.rfind(b"\n")
        if last_nl == -1:
            # No complete line yet; rewind so we re-read this incomplete
            # chunk on the next tail.
            state.offset -= len(chunk)
            return state.usage
        if last_nl != len(chunk) - 1:
            state.offset -= len(chunk) - last_nl - 1
            chunk = chunk[: last_nl + 1]

        for line in chunk.splitlines():
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                log.debug("malformed JSONL line in %s, skipping", state.path)
                continue
            self._fold_line(state.usage, obj)

        return state.usage

    def tail_all_known(self) -> None:
        for sid, state in list(self._sessions.items()):
            self.tail(sid, state.usage.cwd)

    def get(self, session_id: str) -> Optional[SessionUsage]:
        state = self._sessions.get(session_id)
        return state.usage if state is not None else None

    def all_usage(self) -> list[SessionUsage]:
        return [state.usage for state in self._sessions.values()]

    def today_totals(self) -> dict[str, float]:
        today = datetime.datetime.now(datetime.timezone.utc).date()
        bucket = self._daily.get(today, {})
        return {
            "tokens": bucket.get("tokens", 0.0),
            "cost_usd": bucket.get("cost_usd", 0.0),
            "turns": bucket.get("turns", 0.0),
            "sessions": bucket.get("sessions", 0.0),
        }

    def _fold_line(self, usage: SessionUsage, obj: dict) -> None:
        t = obj.get("type")
        if t == "user":
            usage.turns += 1
            self._bucket_today(obj.get("timestamp"), turns=1)
            return
        if t != "assistant":
            return
        msg = obj.get("message")
        if not isinstance(msg, dict):
            return
        u = msg.get("usage")
        if not isinstance(u, dict):
            return
        model = msg.get("model") or usage.model or ""
        usage.model = model
        in_tok = int(u.get("input_tokens") or 0)
        cr_tok = int(u.get("cache_read_input_tokens") or 0)
        cw_tok = int(u.get("cache_creation_input_tokens") or 0)
        out_tok = int(u.get("output_tokens") or 0)

        usage.input_tokens += in_tok
        usage.cache_read_tokens += cr_tok
        usage.cache_write_tokens += cw_tok
        usage.output_tokens += out_tok
        usage.last_input_window = in_tok + cr_tok + cw_tok

        cost = cost_usd(model, in_tok, cr_tok, cw_tok, out_tok)
        if cost is None:
            usage.cost_unknown = True
        else:
            usage.cost_usd += cost

        ts = obj.get("timestamp")
        if isinstance(ts, str):
            try:
                usage.last_assistant_at = datetime.datetime.fromisoformat(
                    ts.replace("Z", "+00:00")
                )
            except ValueError:
                pass

        self._bucket_today(
            ts,
            tokens=in_tok + cr_tok + cw_tok + out_tok,
            cost_usd=cost or 0.0,
            session_id=usage.session_id,
        )

    def _bucket_today(
        self,
        timestamp: Optional[str],
        tokens: float = 0.0,
        cost_usd: float = 0.0,
        turns: int = 0,
        session_id: str = "",
    ) -> None:
        if not isinstance(timestamp, str):
            return
        d = _utc_date_of(timestamp)
        if d is None:
            return
        bucket = self._daily.setdefault(d, {
            "tokens": 0.0, "cost_usd": 0.0, "turns": 0.0,
            "sessions": 0.0, "_session_set": set(),
        })
        bucket["tokens"] += tokens
        bucket["cost_usd"] += cost_usd
        bucket["turns"] += turns
        if session_id:
            seen: set = bucket["_session_set"]  # type: ignore[assignment]
            if session_id not in seen:
                seen.add(session_id)
                bucket["sessions"] = float(len(seen))
