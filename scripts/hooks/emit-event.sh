#!/usr/bin/env bash
# emit-event.sh — write a Claude Code hook event to the claude-alerts events directory.
# Usage: emit-event.sh <EVENT_NAME>
# Reads the hook JSON payload from stdin.

set -euo pipefail

EVENT="${1:?event name required}"
EVENTS_DIR="${CLAUDE_ALERTS_EVENTS_DIR:-$HOME/.local/state/claude-alerts/events}"
mkdir -p "$EVENTS_DIR"

PAYLOAD="$(cat || true)"
SESSION_ID="$(printf '%s' "$PAYLOAD" | jq -r '.session_id // empty')"
CWD="$(printf '%s' "$PAYLOAD" | jq -r '.cwd // empty')"
TOOL_NAME="$(printf '%s' "$PAYLOAD" | jq -r '.tool_name // empty')"
[ -z "$SESSION_ID" ] && SESSION_ID="unknown"
[ -z "$CWD" ] && CWD="$(pwd)"

TS="$(date +%s.%N)"
NAME="${TS}-${SESSION_ID}.json"
TMP="${EVENTS_DIR}/${NAME}.tmp"
FINAL="${EVENTS_DIR}/${NAME}"

jq -cn \
    --arg event "$EVENT" \
    --arg session_id "$SESSION_ID" \
    --arg cwd "$CWD" \
    --arg tool_name "$TOOL_NAME" \
    --argjson claude_pid "$$" \
    --argjson timestamp "$TS" \
    '{event:$event, session_id:$session_id, cwd:$cwd, claude_pid:$claude_pid, timestamp:$timestamp}
     + (if $tool_name == "" then {} else {tool_name:$tool_name} end)' \
    > "$TMP"
mv "$TMP" "$FINAL"
exit 0
