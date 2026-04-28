# Stdout dashboard for the claude-alerts daemon

**Date:** 2026-04-28
**Status:** Draft — pending review
**Owner:** Abhishek Shinde

## Problem

When the user runs `claude-alerts` in a foreground terminal, that terminal currently shows nothing useful — just startup banner and occasional INFO/WARNING log lines from the binder. The daemon already knows everything about every active session (it's the source of truth for the overlays), so the terminal is wasted real estate. The user wants it to render a live per-session usage table instead.

## Goals

- When stdout is a TTY, render a live, auto-refreshing dashboard with one row per active session: short id, cwd, status, model, tokens in/out, cost, turn count, context-window %.
- Footer row with today's totals (tokens, cost, turns) across all of the user's sessions, including ones that aren't currently bound.
- When stdout is not a TTY (systemd, log redirection, pipe), behave exactly as today — plain log lines, no escape codes, no clearing.
- No new hook events from Claude Code, no new runtime dependencies, no Anthropic API calls.

## Non-goals

- 5-hour and weekly Claude subscription limits. There is no documented local source for that data; computing it ourselves would require guessing the plan-tier denominator and risks misleading the user. Tracked as a separate follow-up.
- Real-time per-token streaming. The transcript file is appended one assistant message at a time; we update on file change at most.
- Editing or sending input to Claude from the dashboard. Read-only display.
- Cross-machine aggregation. Single user, single host.

## Design

### 1. TTY detection and split log/dashboard output

The daemon currently uses `logging` with the default StreamHandler going to stderr. We keep that — log lines continue to go to stderr, untouched. The dashboard is a separate thing that paints to stdout, only when `sys.stdout.isatty()`.

Implementation: a new `Dashboard` class with a `render()` method, owned by `Daemon`. On each render:

```
\x1b[H\x1b[J        # cursor home + clear-from-cursor (no full clear, avoids flicker)
header line
table rows (one per session)
footer (totals)
```

The dashboard does NOT take over the terminal — Ctrl-C still works, the daemon still logs to stderr, and on shutdown we restore the cursor and emit a final newline. No alternate-screen buffer, no curses dependency.

If stdout is not a TTY, the dashboard is constructed but `render()` is a no-op; the user sees the existing log stream.

### 2. Refresh trigger

Three triggers:

1. **Session change.** `OverlayManager.on_session_changed` already fires on every meaningful state change. The dashboard subscribes to the same `SessionStore.on_change` channel and re-renders. This makes status flips appear immediately.

2. **JSONL change.** A second inotify watcher on `~/.claude/projects/` (recursive) — every time a transcript file is appended to, re-parse the affected session and re-render. Only the *tail* needs reading: we keep a per-file byte offset and read from there forward.

3. **Periodic timer.** A 2-second tick re-renders even with no events, so wall-clock fields ("idle for 14s", "last activity 2m ago") stay current.

Render is debounced: at most one paint every 250ms. Bursts of events during a turn collapse into a single redraw.

### 3. Reading per-session usage from JSONL

For each Claude Code session, the transcript lives at:

```
~/.claude/projects/<encoded-cwd>/<session_id>.jsonl
```

The `<encoded-cwd>` mapping is `cwd.replace("/", "-")` (leading dash retained), e.g. `/home/abhishek/claude-alerts` → `-home-abhishek-claude-alerts`. The session id matches `Session.session_id` directly.

Each line in the JSONL is a JSON object. The relevant fields, derived from inspection:

- `type: "assistant"` lines carry `message.usage` with:
  - `input_tokens` — non-cached input tokens
  - `cache_creation_input_tokens` — tokens written to cache
  - `cache_read_input_tokens` — cache hits
  - `output_tokens`
- `message.model` — model id used for this turn (e.g. `claude-opus-4-7`).
- `type: "user"` lines indicate a turn boundary (count toward "turns").

Per-session totals are sums across all assistant lines. We do NOT recompute on every render — we keep `(file_path → (offset, partial_totals))` in memory, advance the offset on inotify, parse only the new lines, fold them into the totals.

Sessions whose JSONL hasn't been touched in >24h are not shown in the table (they're still tracked in `today_totals` if their last assistant message was today). Sessions present in `SessionStore` but with no JSONL on disk render with `--` placeholders.

### 4. Cost computation

A static dict `MODEL_PRICING_USD_PER_MTOK` keyed by model id with `(input, cached_input, cache_write, output)` rates per million tokens. Latest pricing as of 2026-04-28:

```python
MODEL_PRICING_USD_PER_MTOK = {
    # (input, cache_read, cache_write, output)
    "claude-opus-4-7":    (15.00, 1.50, 18.75, 75.00),
    "claude-opus-4-7[1m]":(15.00, 1.50, 18.75, 75.00),  # same model, 1M context
    "claude-sonnet-4-6":  ( 3.00, 0.30,  3.75, 15.00),
    "claude-haiku-4-5":   ( 0.80, 0.08,  1.00,  4.00),
    "claude-haiku-4-5-20251001": (0.80, 0.08, 1.00, 4.00),
}
```

Cost per assistant message is computed when we fold in its usage; per-session cost is the running sum. Unknown model ids fall back to `(0, 0, 0, 0)` and emit a single WARNING log line per model id (rate-limited by a `set` of seen-but-unknown ids). The dashboard cell shows `?` for that row.

Pricing drifts. The static table carries a `# Last updated: 2026-04-28` comment and a `TODO(pricing): refresh from docs.claude.com/en/docs/about-claude/pricing` marker.

### 5. Context-window % per session

Each row shows `tokens-in-current-conversation / context-window-cap`. The cap is determined per model:

```python
CONTEXT_WINDOW = {
    "claude-opus-4-7":     200_000,
    "claude-opus-4-7[1m]": 1_000_000,
    "claude-sonnet-4-6":   200_000,
    "claude-haiku-4-5":    200_000,
}
```

The numerator is the most recent assistant message's `input_tokens + cache_read_input_tokens + cache_creation_input_tokens` — that's what the model actually saw on its last turn, minus its own output.

Compaction: when Claude compacts, the next assistant message's input tokens drop sharply. We don't model compaction explicitly; the % naturally drops along with the input. The cell renders as "85% (170k/200k)".

### 6. Today's totals

A footer row sums:

- All tokens (in + out) across all sessions where any line was written today (UTC by default; `TZ` env var respected).
- Total $ across same.
- Total turns across same.
- Number of distinct sessions touched today.

This is computed from the same JSONL state we already keep in memory; we just bucket per-day instead of per-session.

### 7. Layout

```
claude-alerts daemon · 4 active · today: 42 turns · 1.2M tokens · $4.30
─────────────────────────────────────────────────────────────────────────
SESSION   CWD                                STATUS    MODEL          TOK IN    TOK OUT    COST    TURNS   CTX
5756986d  ~/proj/foo                         ● working opus-4-7        180.2k     12.4k   $2.40       8   42% 84k/200k
d80768ae  ~/code/bar                         ○ waiting opus-4-7         95.5k      8.1k   $1.30       5   18% 36k/200k
f4e0be88  ~/x/y                              ○ idle    sonnet-4-6       42.1k      3.2k   $0.18       3    9%  18k/200k
193e087c  ~/claude-alerts                    ● working opus-4-7         28.4k      1.9k   $0.40       2   12% 24k/200k
─────────────────────────────────────────────────────────────────────────
press Ctrl-C to exit · log → stderr · refresh 2s
```

- ● = green dot (working), ○ = red dot (waiting). Same colors as the borders.
- `~` substitution for `$HOME` to keep cwd cells short.
- Truncate cwd from the *left* with `…/foo/bar` if longer than 36 chars (preserves the part the user identifies the project by).
- Sort: working sessions first, then waiting, then idle (no recent assistant activity), each group by most recent activity descending.
- If terminal is narrower than ~120 cols, drop CTX cell first, then COST, then TOK IN/OUT (collapse to total). Footer always fits.

### 8. Idle/inactive handling

- A session in `SessionStore` whose JSONL has had no new assistant line for >5 minutes is shown with status `idle` (separate from `waiting` — the "user has gone away from this terminal" case).
- A session in `SessionStore` not in the JSONL set at all (orphan) renders with `--` for the usage cells.
- A JSONL session not in `SessionStore` (e.g. terminal closed but transcript exists) does NOT appear as a row; its tokens still contribute to today's totals.

### 9. Failure modes

- JSONL parse error on a single line → skip line, log DEBUG, continue. Bad lines should not break the dashboard.
- File disappears mid-tail → drop from in-memory state, log DEBUG.
- Inotify watcher fails to register on a new project dir → fall back to polling that dir at the 2s tick. Logged once per dir.
- Terminal resized → re-render on next event. We do not handle SIGWINCH explicitly; the next 2s tick paints to the new size.
- Pricing table is missing the model → row's COST cell shows `?`, single WARNING per unknown id.

## Data flow

```
hook → emit-event.sh → events/ → ingester → SessionStore.apply_event
                                                ├─ on_change → OverlayManager (existing)
                                                └─ on_change → Dashboard.mark_dirty (NEW)

inotify ~/.claude/projects → JsonlTailer.on_append → parse new lines → update per-session totals
                                                                       └─ Dashboard.mark_dirty

2s timer → Dashboard.mark_dirty

Dashboard.tick (debounced 250ms): if dirty, build rows from SessionStore + JsonlTailer state, paint to stdout
```

## Components changed

| File | Change |
|------|--------|
| `claude_alerts/dashboard.py` (new) | `Dashboard` class: render loop, layout, debounce, TTY detection, ANSI escapes. |
| `claude_alerts/transcripts.py` (new) | `JsonlTailer` class: per-file offset tracking, append-only parsing, model-aware token folding, per-day buckets. |
| `claude_alerts/pricing.py` (new) | `MODEL_PRICING_USD_PER_MTOK`, `CONTEXT_WINDOW`, `cost_for(model, usage)` helpers. Pure data + functions, no I/O. |
| `claude_alerts/daemon.py` | Construct dashboard + tailer, wire `SessionStore.on_change` to `dashboard.mark_dirty`, run dashboard tick from main loop alongside the existing idle sweep. |
| `claude_alerts/__main__.py` | New `--no-dashboard` flag for users who want the old log-only output even on a TTY. |
| `tests/test_pricing.py` (new) | Cost math for each model; cache-read vs cache-write differentiation; unknown-model fallback. |
| `tests/test_transcripts.py` (new) | Tail-from-offset on append; ignore malformed lines; per-day bucketing across midnight; missing-file handling. |
| `tests/test_dashboard.py` (new) | Layout snapshots at three terminal widths; degraded-mode column dropping; non-TTY noop. |

## Testing

Unit tests for the math (pricing, context-window %, today's bucketing) are pure-function and easy.

Layout/render tests use a fake `sys.stdout.write` capture; ANSI sequences are kept literal in expected strings.

Manual verification after deploy:

1. Run `claude-alerts` in a wide terminal, start a few claude sessions in other terminals, exercise them, watch rows populate and stats tick.
2. Pipe to `cat`: `claude-alerts | cat` — confirm no escape codes leak through, just plain log lines.
3. Run under `systemctl --user start claude-alerts.service` — confirm the systemd journal is unaffected (no escape garbage in `journalctl`).
4. Resize terminal narrower than 120 cols — confirm columns drop in priority order.
5. Edit pricing for an unknown model — confirm `?` in COST and one WARNING.

## Known limitations

- Pricing table will go stale. Not load-bearing — UI only — but worth a quarterly refresh chore. Captured as `TODO(pricing)`.
- The 5-hour and weekly subscription limits are not displayed. Tracked separately; do not block this work.
- If the user runs Claude Code via API key on metered billing rather than a subscription, "today's $" is the right number but plan limits are irrelevant — both states the dashboard handles correctly by simply not claiming to show subscription limits.
- JSONL format is undocumented and may change between Claude Code versions. Defensive parsing minimizes blast radius; in the worst case, rows show `--` until we patch the parser.

## Migration

No state migration. The dashboard is purely a presentation layer over data that already exists (in-memory `SessionStore` + on-disk JSONL files written by Claude Code). First-time start works on existing transcripts.

Bump version to `0.3.0` on release. (Persistence is `0.2.0`.) The two are independent and can ship in either order.
