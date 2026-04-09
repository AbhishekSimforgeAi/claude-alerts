#!/usr/bin/env python3
"""Install claude-alerts hook entries into ~/.claude/settings.json without clobbering."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

HOOK_EVENTS = (
    "SessionStart",
    "UserPromptSubmit",
    "PreToolUse",
    "PostToolUse",
    "Stop",
    "Notification",
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
    if settings_path.exists():
        try:
            data = json.loads(settings_path.read_text())
        except json.JSONDecodeError:
            data = {}
    else:
        data = {}

    if not isinstance(data, dict):
        data = {}

    hooks = data.setdefault("hooks", {})
    for event in HOOK_EVENTS:
        bucket = hooks.setdefault(event, [])
        already = any(hook_path in json.dumps(item) for item in bucket)
        if not already:
            bucket.append(_hook_entry(hook_path, event))

    settings_path.write_text(json.dumps(data, indent=2) + "\n")


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
