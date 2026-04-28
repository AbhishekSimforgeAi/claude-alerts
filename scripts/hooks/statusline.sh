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

# Single jq pass to extract everything we need; cheaper than four invocations.
# `|| true` keeps `set -e` from killing us when jq fails (e.g. malformed JSON).
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
IFS=$'\t' read -r RATE_LIMITS MODEL CWD BRANCH <<< "${JQ_OUT:-$'\t\t\t'}"

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
