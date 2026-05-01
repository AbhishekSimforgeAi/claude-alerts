"""CLI entrypoint: `python -m claude_alerts` or `claude-alerts`."""
from __future__ import annotations

import argparse
import logging
import logging.handlers
import os
import sys
from pathlib import Path

from claude_alerts.config import load_config
from claude_alerts.daemon import (
    Daemon,
    default_config_path,
    default_events_dir,
    default_log_path,
    default_persistence_path,
)


def configure_logging(level: str, log_path: Path) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(
        log_path, maxBytes=10 * 1024 * 1024, backupCount=2,
    )
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    root = logging.getLogger()
    root.setLevel(level)
    root.addHandler(handler)
    if os.environ.get("CLAUDE_ALERTS_DEBUG") == "1":
        root.setLevel(logging.DEBUG)
        root.addHandler(logging.StreamHandler(sys.stderr))


def main() -> int:
    p = argparse.ArgumentParser(prog="claude-alerts")
    p.add_argument("--events-dir", type=Path, default=None)
    p.add_argument("--config", type=Path, default=None)
    p.add_argument(
        "--no-persistence",
        action="store_true",
        help="Don't load or save bindings across restarts.",
    )
    p.add_argument(
        "--persistence-path",
        type=Path,
        default=None,
        help="Override path to the sessions.json snapshot.",
    )
    p.add_argument(
        "--no-dashboard",
        action="store_true",
        help="Don't render the per-session usage dashboard, even on a TTY.",
    )
    args = p.parse_args()

    config_path = args.config or default_config_path()
    cfg = load_config(config_path)
    log_path = default_log_path()
    configure_logging(cfg.log_level, log_path)

    events_dir = args.events_dir or default_events_dir()
    persistence_path = (
        None if args.no_persistence
        else (args.persistence_path or default_persistence_path())
    )

    try:
        daemon = Daemon(
            events_dir=events_dir,
            config=cfg,
            persistence_path=persistence_path,
            dashboard_enabled=not args.no_dashboard,
        )
    except Exception as e:
        msg = f"claude-alerts: cannot start daemon: {e}"
        print(msg, file=sys.stderr)
        logging.getLogger().exception("daemon initialization failed")
        return 1

    try:
        daemon.run()
    except KeyboardInterrupt:
        pass
    except Exception:
        logging.getLogger().exception("daemon crashed")
        daemon.stop()
        return 1
    finally:
        daemon.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
