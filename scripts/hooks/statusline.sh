#!/usr/bin/env bash
# statusline.sh — claude-alerts statusLine helper.
#
# Claude Code invokes the user's statusLine command on every prompt update,
# piping a JSON blob to stdin and rendering the script's stdout in the
# bottom-of-prompt status area. That JSON includes a `rate_limits` object
# (subscribers, after first API response) with 5h, 7d, and per-model
# windows — the data behind the `/usage` slash command.
#
# We capture rate_limits to ~/.local/state/claude-alerts/rate_limits.json
# so the daemon's dashboard can render it without making any API calls of
# its own. We then emit a minimal one-line status (model · cwd · branch);
# edit this file or set CLAUDE_ALERTS_WRAPPED_STATUSLINE to chain to an
# existing statusLine command.
#
# Wire it up by setting your ~/.claude/settings.json `statusLine.command`
# to the absolute path of this file (or run scripts/install-hooks.py
# --install-statusline).

set -euo pipefail

STATE_DIR="${CLAUDE_ALERTS_STATE_DIR:-${XDG_STATE_HOME:-$HOME/.local/state}/claude-alerts}"
mkdir -p -m 700 "$STATE_DIR"
chmod 700 "$STATE_DIR" 2>/dev/null || true

PAYLOAD="$(cat || true)"

# Single jq pass to extract everything we need; cheaper than several invocations.
# `|| true` keeps `set -e` from killing us when jq fails (e.g. malformed JSON).
# Fields are joined with U+0001 rather than tab because bash `read` collapses
# runs of whitespace IFS chars — an empty git.branch would shift session_id and
# context_window into the wrong slots.
# Relies on jq never emitting a literal U+0001 in raw output: tojson escapes
# control chars, and the other extracted fields (model name, cwd, git branch,
# session_id) cannot contain one in practice.
JQ_OUT="$(
    printf '%s' "$PAYLOAD" | jq -rj '
        [
            (.rate_limits | tojson),
            (.model.display_name // .model.id // ""),
            (.workspace.current_dir // .cwd // ""),
            (.git.branch // ""),
            (.session_id // ""),
            (.context_window | tojson)
        ] | join("")
    ' 2>/dev/null || printf '\x01\x01\x01\x01\x01'
)"
IFS=$'\x01' read -r RATE_LIMITS MODEL CWD BRANCH SESSION_ID CONTEXT_WINDOW <<< "${JQ_OUT:-$'\x01\x01\x01\x01\x01'}"

# Persist rate_limits if the input had any. Validate that it's parseable JSON
# before writing — defends against jq output with malformed `tojson` results.
if [ -n "${RATE_LIMITS:-}" ] && [ "$RATE_LIMITS" != "null" ] \
        && printf '%s' "$RATE_LIMITS" | jq -e . >/dev/null 2>&1; then
    TS="$(date +%s.%N)"
    TMP="$STATE_DIR/rate_limits.json.tmp"
    OUT="$STATE_DIR/rate_limits.json"
    # Open with restrictive umask so the sidecar inherits 0600.
    (umask 077 && printf '{"saved_at": %s, "rate_limits": %s}\n' "$TS" "$RATE_LIMITS" > "$TMP")
    mv "$TMP" "$OUT"
fi

# Persist per-session context_window. session_id is sanitized against an
# allowlist before being interpolated into the filename — it must match
# ^[A-Za-z0-9._-]+$, otherwise a hostile session id could write outside
# the contexts dir or smuggle shell metacharacters.
CONTEXTS_DIR="$STATE_DIR/contexts"
if [ -n "${SESSION_ID:-}" ] && [ -n "${CONTEXT_WINDOW:-}" ] \
        && [ "$CONTEXT_WINDOW" != "null" ] \
        && [[ "$SESSION_ID" =~ ^[A-Za-z0-9._-]+$ ]] \
        && printf '%s' "$CONTEXT_WINDOW" | jq -e . >/dev/null 2>&1; then
    mkdir -p -m 700 "$CONTEXTS_DIR"
    chmod 700 "$CONTEXTS_DIR" 2>/dev/null || true
    TS="$(date +%s.%N)"
    TMP="$CONTEXTS_DIR/$SESSION_ID.json.tmp"
    OUT="$CONTEXTS_DIR/$SESSION_ID.json"
    (umask 077 && printf '{"saved_at": %s, "session_id": "%s", "context_window": %s}\n' \
        "$TS" "$SESSION_ID" "$CONTEXT_WINDOW" > "$TMP")
    mv "$TMP" "$OUT"
fi

# Optionally chain to an existing statusLine command. The wrapped command must
# be an absolute path the user trusts; we refuse anything containing whitespace
# or shell metacharacters to harden against environment-injection attacks
# (direnv, malicious .env, etc.).
WRAP="${CLAUDE_ALERTS_WRAPPED_STATUSLINE:-}"
if [ -n "$WRAP" ]; then
    if [[ "$WRAP" =~ ^/[A-Za-z0-9._/-]+$ ]] && [ -x "$WRAP" ]; then
        printf '%s' "$PAYLOAD" | "$WRAP"
        exit 0
    fi
    printf 'claude-alerts: ignoring CLAUDE_ALERTS_WRAPPED_STATUSLINE %q (must be absolute, executable, no shell metacharacters)\n' "$WRAP" >&2
fi

# Default minimal status line.
CWD_SHORT="${CWD/#$HOME/~}"
if [ -n "$BRANCH" ]; then
    printf '%s · %s · %s\n' "$MODEL" "$CWD_SHORT" "$BRANCH"
else
    printf '%s · %s\n' "$MODEL" "$CWD_SHORT"
fi
