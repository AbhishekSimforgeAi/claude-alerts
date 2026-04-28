#!/usr/bin/env python3
"""Install claude-alerts hook entries into ~/.claude/settings.json without clobbering."""
from __future__ import annotations

import argparse
import json
import os
import stat
from pathlib import Path
from typing import Optional

HOOK_EVENTS = (
    "SessionStart",
    "UserPromptSubmit",
    "PreToolUse",
    "PostToolUse",
    "Stop",
    "Notification",
    "PermissionRequest",
    "Elicitation",
    "SessionEnd",
)


def _hook_entry(hook_path: str, event: str) -> dict:
    return {
        "hooks": [
            {
                "type": "command",
                "command": f"{hook_path} {event}",
            }
        ]
    }


def merge_hooks_into(settings_path: Path, hook_path: str) -> None:
    settings_path.parent.mkdir(parents=True, exist_ok=True)

    # Snapshot existing mode so we can restore it after rewriting (settings.json may be 600).
    old_mode: Optional[int] = None
    if settings_path.exists():
        try:
            old_mode = stat.S_IMODE(settings_path.stat().st_mode)
        except OSError:
            pass

    if settings_path.exists():
        try:
            data = json.loads(settings_path.read_text())
        except json.JSONDecodeError:
            data = {}
    else:
        data = {}

    if not isinstance(data, dict):
        data = {}

    # Guard against pre-existing "hooks" being something other than a dict.
    if not isinstance(data.get("hooks"), dict):
        data["hooks"] = {}
    hooks = data["hooks"]

    for event in HOOK_EVENTS:
        # Guard against pre-existing hooks[event] being something other than a list.
        bucket = hooks.get(event)
        if not isinstance(bucket, list):
            bucket = []
            hooks[event] = bucket

        target_command = f"{hook_path} {event}"
        already = False
        for item in bucket:
            if not isinstance(item, dict):
                continue
            inner = item.get("hooks", [])
            if not isinstance(inner, list):
                continue
            for h in inner:
                if isinstance(h, dict) and h.get("command") == target_command:
                    already = True
                    break
            if already:
                break

        if not already:
            bucket.append(_hook_entry(hook_path, event))

    settings_path.write_text(json.dumps(data, indent=2) + "\n")

    # Restore the previous mode if we had one.
    if old_mode is not None:
        try:
            settings_path.chmod(old_mode)
        except OSError:
            pass


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument(
        "--settings",
        default=str(Path.home() / ".claude" / "settings.json"),
        help="Path to Claude settings.json",
    )
    p.add_argument(
        "--hook-path",
        default=str(Path(__file__).resolve().parent / "hooks" / "emit-event.sh"),
        help="Absolute path to emit-event.sh",
    )
    args = p.parse_args()
    merge_hooks_into(Path(args.settings), os.path.abspath(args.hook_path))
    print(f"Installed claude-alerts hooks into {args.settings}")


if __name__ == "__main__":
    main()
