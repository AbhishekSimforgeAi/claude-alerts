# Per-session context-window usage in the dashboard

**Status:** design
**Author:** Abhishek Shinde
**Date:** 2026-05-01

## Goal

Show the current context-window usage of every active Claude Code session
in the daemon's TTY dashboard, next to that session's status indicator. The
existing sessions block looks like:

```
  SESSION   STATUS     CWD
  5756986d  ● working  ~/claude-alerts
```

After this change:

```
  SESSION   STATUS     CTX             CWD
  5756986d  ● working  45% (90k/200k)  ~/claude-alerts
```

`CTX` shows percentage of the model's context window in use, plus the raw
input-token count and the window's total. This is the same number Claude
Code's `/context` slash command shows.

## Non-goals

- No color on the `CTX` column. The dashboard's existing color story is
  "rate-limit bars only", and per-cell thresholds invite scope creep.
- No display on the X11 window border. Out of scope; the border stays a
  pure status indicator.
- No history / time-series of context usage. The sidecar is overwritten on
  every prompt update.
- No transcript-file parsing. We rely on Claude Code's statusLine input
  exposing `context_window` directly.

## Data source

Claude Code's statusLine subprocess receives a JSON object on stdin every
prompt update (documented at
<https://code.claude.com/docs/en/statusline>). The relevant fields:

```json
{
  "session_id": "5756986d-…",
  "context_window": {
    "total_input_tokens": 15234,
    "total_output_tokens": 4521,
    "context_window_size": 200000,
    "used_percentage": 7.9,
    "remaining_percentage": 92.1,
    "current_usage": {
      "input_tokens": 8500,
      "output_tokens": 1200,
      "cache_creation_input_tokens": 5000,
      "cache_read_input_tokens": 2000
    }
  }
}
```

`context_window_size` is `200000` by default and `1000000` for sessions
that opted into the extended-context header. `used_percentage` is
calculated by Claude Code as
`(input_tokens + cache_creation_input_tokens + cache_read_input_tokens) / context_window_size`
— output tokens are excluded. `current_usage` is `null` before the first
API call in a session.

## Architecture

```
Claude Code  ─── stdin (JSON, every prompt update) ───►  statusline.sh
                                                              │
                                                              ├─► rate_limits.json    (existing)
                                                              │
                                                              └─► contexts/<sid>.json (new)
                                                                       │
claude-alerts daemon main loop                                         │
   │                                                                   │
   ├─ inotify on events/  ── apply_event ── SessionStore               │
   │                          │                                       │
   │                          └─ on SessionEnd: delete contexts/<sid>.json
   │                                                                  │
   └─ tick (2s):  limits.load(rate_limits.json)                        │
                  contexts.load(sid) per active session ◄─────────────┘
                  → dashboard.tick() repaints sessions block
```

`statusline.sh` is the single capture point. The daemon never writes to
`contexts/`; it only reads on the dashboard tick and deletes on
`SessionEnd`. No new threads, no inotify on `contexts/`. The data is
volatile — `sessions.json` does not gain any context fields.

## Sidecar format

Path: `~/.local/state/claude-alerts/contexts/<sanitized_session_id>.json`,
mode `0600`. Directory mode `0700`.

```json
{
  "saved_at": 1777643245.812,
  "session_id": "5756986d-…",
  "context_window": {
    "total_input_tokens": 15234,
    "total_output_tokens": 4521,
    "context_window_size": 200000,
    "used_percentage": 7.9,
    "remaining_percentage": 92.1,
    "current_usage": {
      "input_tokens": 8500,
      "output_tokens": 1200,
      "cache_creation_input_tokens": 5000,
      "cache_read_input_tokens": 2000
    }
  }
}
```

The upstream `context_window` object is stored verbatim. `saved_at` is
the wall-clock seconds when the sidecar was written, used only for
debugging — staleness is not surfaced in the dashboard for this feature.

## Module layout

### `claude_alerts/contexts.py` (new)

Mirrors `claude_alerts/limits.py`:

```python
@dataclass(frozen=True)
class ContextUsage:
    saved_at: float
    used_percentage: Optional[float]   # 0–100 or None
    used_tokens: Optional[int]         # input-only sum, or None
    total_tokens: Optional[int]        # context_window_size, or None

def default_contexts_dir() -> Path: ...
def load(session_id: str, base_dir: Path) -> Optional[ContextUsage]: ...
def delete(session_id: str, base_dir: Path) -> None: ...
def sweep(active_session_ids: set[str], base_dir: Path) -> int: ...
```

`used_tokens` is computed from `current_usage` as
`input_tokens + cache_creation_input_tokens + cache_read_input_tokens` so
the absolute number stays consistent with `used_percentage`. `output_tokens`
is excluded.

`load` returns `None` only when the file is missing, unreadable, or
malformed JSON. If the file parses but individual fields are missing or
unusable (`current_usage` is null, `context_window_size <= 0`,
`used_percentage` is null), the corresponding `ContextUsage` field is
left as `None` and the formatter renders `—`. Errors are logged at
DEBUG.

### `claude_alerts/dashboard.py` (extended)

- `_sessions_block` calls `contexts.load(s.session_id, contexts_dir)` per
  row.
- New `_format_ctx(ContextUsage | None) -> str` helper. Formatting rules:
  - `None` or `used_percentage is None` or `used_tokens is None` or
    `total_tokens is None` → `—`
  - `0 < used_percentage < 1` → `<1% (Nk/Mk)`
  - otherwise → `{int(round(pct))}% ({used_short}/{total_short})`
- New `_short_tokens(n: int) -> str`:
  - `n < 1000` → `str(n)` (e.g. `850`)
  - `n < 1_000_000` → `f"{round(n/1000)}k"` (e.g. `200_000 → 200k`)
  - else → `f"{n/1_000_000:.1f}M"` (e.g. `1_000_000 → 1.0M`)
- The `CTX` field is left-justified and padded on the right to a fixed
  width of 16 characters so the `CWD` column stays aligned regardless of
  contents. 16 covers the worst case `100% (200k/200k)` and
  `100% (1.0M/1.0M)`. The existing line-clip in `_build_lines` keeps
  narrow terminals safe.
- `Dashboard.__init__` accepts an optional `contexts_dir: Path` argument
  (defaults to `contexts.default_contexts_dir()`) for testability.

### `claude_alerts/daemon.py` (extended)

- On `SessionEnd`, after the existing `SessionStore` removal, call
  `contexts.delete(session_id, contexts_dir)`. Best-effort — missing file
  is not an error.
- On daemon startup, after `SessionStore` is restored from
  `sessions.json`, call `contexts.sweep({s.session_id for s in store.all()}, contexts_dir)`
  to delete orphaned sidecars left behind by a crash before `SessionEnd`.

### `scripts/hooks/statusline.sh` (extended)

- Single `jq` pass adds two fields to the existing tab-separated output:
  `session_id` and `context_window` (as `tojson`).
- After the existing rate-limits write block, add a contexts write block:
  - Validate `session_id` matches `^[A-Za-z0-9._-]+$`. If not, skip and
    log to stderr.
  - Validate `context_window` is parseable JSON via `jq -e .`. If not,
    skip.
  - Atomic write: `(umask 077 && printf … > "$TMP") && mv "$TMP" "$OUT"`
    where `$OUT = $STATE_DIR/contexts/<session_id>.json`.
  - `mkdir -p -m 700 "$STATE_DIR/contexts"` once at script start, next to
    the existing state-dir creation.

## Edge cases

| Case | Behavior |
|------|----------|
| Session tracked by daemon but no sidecar yet | `—` |
| Sidecar present, `current_usage` is `null` | `—` |
| Sidecar present, `context_window_size` missing or `<= 0` | `—` |
| Sidecar JSON malformed | `—`, log at DEBUG |
| `used_percentage` rounds to 0 but is non-zero | `<1% (Nk/Mk)` |
| 1M-context session | percent and total render normally; total shows `1.0M` |
| Session ends | sidecar deleted by daemon |
| Daemon killed before `SessionEnd` | sidecar swept on next startup |
| Rapid concurrent statusLine invocations for the same session | last writer wins; `tmp + mv` makes each write atomic |
| `session_id` contains `/` or control chars | rejected by the regex; no write |

## Testing

`tests/test_contexts.py` (new):

- `load` returns `None` when the file is missing.
- `load` returns a populated `ContextUsage` for a well-formed sidecar.
- `load` returns `None` for malformed JSON.
- `load` returns a `ContextUsage` with `used_tokens=None` when
  `current_usage` is `null`.
- `load` returns a `ContextUsage` with `total_tokens=None` when
  `context_window_size <= 0` or missing.
- `load` correctly excludes `output_tokens` from `used_tokens`.
- `delete` is idempotent on a missing file.
- `sweep` deletes only files for inactive session ids.

`tests/test_dashboard.py` (extended):

- Sessions block with no contexts dir → all rows show `—` in the `CTX`
  column, `CWD` column still aligned.
- Mixed: one session has data, one doesn't — column alignment preserved.
- `<1%` rounding case renders `<1% (Nk/Mk)`.
- 1M-context session renders `…/1.0M`.
- Snapshot test: a fixture set of sessions + sidecars → exact expected
  table text (regression guard for column alignment).

`tests/test_statusline_sh.py` (new or extended if present):

- Run `statusline.sh` as a subprocess with sample JSON on stdin and
  `CLAUDE_ALERTS_STATE_DIR` pointed at `tmp_path`.
- Assert `contexts/<session_id>.json` is written, has mode `0600`, and
  parses back into the expected shape.
- Assert a payload with no `context_window` does not write a contexts
  file but still writes `rate_limits.json`.
- Assert a payload with a malicious `session_id` (`../../etc/passwd`,
  control chars) is rejected and no file is written outside the state
  dir.

`tests/test_e2e_xvfb.py` does not need to change — it does not exercise
the dashboard sessions table beyond presence.

## Security & privacy

- The new sidecar lives under `~/.local/state/claude-alerts/contexts/`,
  mode `0600`. Same protections as `rate_limits.json` and
  `sessions.json`.
- `session_id` is filename-sanitized at the write site (regex allowlist)
  and the load site treats it as opaque.
- No additional fields with message content are captured. Only token
  counts and `context_window_size`.
- No outbound network calls. The daemon's "zero outbound network"
  invariant is unchanged.

## Out of scope follow-ups

- Color/threshold styling on the `CTX` column (e.g., red when >90%).
- Showing context usage on the X11 border itself.
- Plotting context usage over time.
- A `/usage`-style breakdown of cache vs. fresh input tokens.
