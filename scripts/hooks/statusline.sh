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

set -e

STATE_DIR="${CLAUDE_ALERTS_STATE_DIR:-${XDG_STATE_HOME:-$HOME/.local/state}/claude-alerts}"
mkdir -p "$STATE_DIR"

PAYLOAD="$(cat || true)"

# Single jq pass to extract everything we need; cheaper than four invocations.
JQ_OUT="$(
    printf '%s' "$PAYLOAD" | jq -r '
        [
            (.rate_limits | tojson),
            (.model.display_name // .model.id // ""),
            (.workspace.current_dir // .cwd // ""),
            (.git.branch // "")
        ] | @tsv
    ' 2>/dev/null || printf '\t\t\t'
)"
IFS=$'\t' read -r RATE_LIMITS MODEL CWD BRANCH <<< "$JQ_OUT"

# Persist rate_limits if the input had any. Atomic write via tmp + mv.
if [ -n "$RATE_LIMITS" ] && [ "$RATE_LIMITS" != "null" ]; then
    TS="$(date +%s.%N)"
    TMP="$STATE_DIR/rate_limits.json.tmp"
    OUT="$STATE_DIR/rate_limits.json"
    printf '{"saved_at": %s, "rate_limits": %s}\n' "$TS" "$RATE_LIMITS" > "$TMP"
    mv "$TMP" "$OUT"
fi

# Optionally chain to an existing statusLine command.
if [ -n "${CLAUDE_ALERTS_WRAPPED_STATUSLINE:-}" ]; then
    printf '%s' "$PAYLOAD" | "$CLAUDE_ALERTS_WRAPPED_STATUSLINE"
    exit 0
fi

# Default minimal status line.
CWD_SHORT="${CWD/#$HOME/~}"
if [ -n "$BRANCH" ]; then
    printf '%s · %s · %s\n' "$MODEL" "$CWD_SHORT" "$BRANCH"
else
    printf '%s · %s\n' "$MODEL" "$CWD_SHORT"
fi
