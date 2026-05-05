#!/usr/bin/env bash
# emit-event.sh — write a Claude Code hook event to the claude-alerts events directory.
# Usage: emit-event.sh <EVENT_NAME>
# Reads the hook JSON payload from stdin.

set -euo pipefail

# OpenClaw filter: any OPENCLAW_* env var marks the Claude session as
# agent-runtime-managed, so the daemon should never see its events (no
# overlay, no dashboard row). Exit 0 silently — OpenClaw's spawn pipeline
# expects success — and write nothing to the events directory.
if [ -n "${!OPENCLAW_*}" ]; then
    exit 0
fi

EVENT="${1:?event name required}"
EVENTS_DIR="${CLAUDE_ALERTS_EVENTS_DIR:-$HOME/.local/state/claude-alerts/events}"
mkdir -p -m 700 "$EVENTS_DIR"
chmod 700 "$EVENTS_DIR" 2>/dev/null || true

PAYLOAD="$(cat || true)"
SESSION_ID="$(printf '%s' "$PAYLOAD" | jq -r '.session_id // empty')"
CWD="$(printf '%s' "$PAYLOAD" | jq -r '.cwd // empty')"
TOOL_NAME="$(printf '%s' "$PAYLOAD" | jq -r '.tool_name // empty')"
[ -z "$SESSION_ID" ] && SESSION_ID="unknown"
[ -z "$CWD" ] && CWD="$(pwd)"

# Sanitize session_id before it goes into a filesystem path: replace anything
# outside [A-Za-z0-9_.-] with '_'. Claude Code session ids are uuids, so this
# is a no-op in practice — but a hostile or buggy hook payload must not be
# able to coerce us into writing outside EVENTS_DIR.
SAFE_SESSION_ID="$(printf '%s' "$SESSION_ID" | tr -c 'A-Za-z0-9_.-' '_' | head -c 128)"
[ -z "$SAFE_SESSION_ID" ] && SAFE_SESSION_ID="unknown"

TS="$(date +%s.%N)"
NAME="${TS}-${SAFE_SESSION_ID}.json"
TMP="${EVENTS_DIR}/${NAME}.tmp"
FINAL="${EVENTS_DIR}/${NAME}"

# Restrictive umask so the event file is created 0600 — these files contain
# pids, cwd, session-id and shouldn't be world-readable on a multi-user box.
(umask 077 && jq -cn \
    --arg event "$EVENT" \
    --arg session_id "$SESSION_ID" \
    --arg cwd "$CWD" \
    --arg tool_name "$TOOL_NAME" \
    --argjson claude_pid "$$" \
    --argjson timestamp "$TS" \
    '{event:$event, session_id:$session_id, cwd:$cwd, claude_pid:$claude_pid, timestamp:$timestamp}
     + (if $tool_name == "" then {} else {tool_name:$tool_name} end)' \
    > "$TMP")
mv "$TMP" "$FINAL"
exit 0
