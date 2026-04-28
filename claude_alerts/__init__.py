"""claude-alerts — visual status overlays and rate-limit dashboard for Claude Code."""
from importlib import metadata

try:
    __version__ = metadata.version("claude-alerts")
except metadata.PackageNotFoundError:
    # Editable install from a fresh checkout where dist-info isn't present yet.
    __version__ = "0.0.0+local"
