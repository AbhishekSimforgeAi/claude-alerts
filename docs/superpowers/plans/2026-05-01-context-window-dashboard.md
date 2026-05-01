# Per-session context-window dashboard — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Show per-session context-window usage (e.g. `45% (90k/200k)`) in the daemon's TTY dashboard, sourced from Claude Code's statusLine input via a new per-session sidecar.

**Architecture:** `scripts/hooks/statusline.sh` (already runs on every prompt update) gets a one-shot extension to also write `<state>/contexts/<session_id>.json`. A new read-only `claude_alerts/contexts.py` module mirrors `claude_alerts/limits.py`. `dashboard.py` adds a `CTX` column. `daemon.py` deletes the per-session sidecar on `SessionEnd` and sweeps orphans on startup.

**Tech Stack:** Python 3.10+, bash + jq for the statusLine helper, pytest for tests.

**Spec:** `docs/superpowers/specs/2026-05-01-context-window-dashboard-design.md`

---

## File Structure

| File | Action | Responsibility |
|------|--------|----------------|
| `claude_alerts/contexts.py` | Create | Read-only sidecar loader: dataclass, `load`, `delete`, `sweep`, `default_contexts_dir`. |
| `tests/test_contexts.py` | Create | Unit tests for the new module. |
| `scripts/hooks/statusline.sh` | Modify | Extend the existing single `jq` pass; write per-session sidecar atomically. |
| `tests/test_statusline_sh.py` | Create | Subprocess tests for the shell script's new contexts behavior. |
| `claude_alerts/dashboard.py` | Modify | Add `_short_tokens`, `_format_ctx`; thread `contexts_dir` through the constructor; add `CTX` column to `_sessions_block`. |
| `tests/test_dashboard.py` | Modify | Add tests for the new helpers and the column rendering. |
| `claude_alerts/daemon.py` | Modify | Delete sidecar on `SessionEnd`; sweep orphaned sidecars on startup. |
| `tests/test_daemon_threading.py` or new `tests/test_daemon_contexts.py` | Modify/Create | Tests for cleanup and sweep wiring. |

---

## Task 1: contexts.py module — `load()`

**Files:**
- Create: `claude_alerts/contexts.py`
- Test: `tests/test_contexts.py`

- [ ] **Step 1.1: Create the failing test file**

Write `tests/test_contexts.py`:

```python
"""Tests for per-session context-window sidecar parsing."""
import json
from pathlib import Path

from claude_alerts import contexts
from claude_alerts.contexts import ContextUsage


def _write_sidecar(base: Path, sid: str, payload: dict) -> Path:
    base.mkdir(parents=True, exist_ok=True)
    p = base / f"{sid}.json"
    p.write_text(json.dumps(payload))
    return p


def _full_payload(saved_at: float = 1.0) -> dict:
    return {
        "saved_at": saved_at,
        "session_id": "abc",
        "context_window": {
            "total_input_tokens": 15234,
            "total_output_tokens": 4521,
            "context_window_size": 200000,
            "used_percentage": 7.9,
            "remaining_percentage": 92.1,
            "current_usage": {
                "input_tokens": 8500,
                "output_tokens": 1200,
                "cache_creation_input_tokens": 5000,
                "cache_read_input_tokens": 2000,
            },
        },
    }


def test_load_missing_file_returns_none(tmp_path):
    assert contexts.load("abc", tmp_path) is None


def test_load_full_payload(tmp_path):
    _write_sidecar(tmp_path, "abc", _full_payload(saved_at=42.0))
    cu = contexts.load("abc", tmp_path)
    assert cu is not None
    assert cu.saved_at == 42.0
    assert cu.used_percentage == 7.9
    # input + cache_creation + cache_read = 8500 + 5000 + 2000 = 15500
    # output_tokens (1200) is intentionally excluded.
    assert cu.used_tokens == 15500
    assert cu.total_tokens == 200000
```

- [ ] **Step 1.2: Run the tests — expect import failure**

Run: `cd /home/abhishek/claude-alerts && .venv/bin/pytest tests/test_contexts.py -v`
Expected: collection error / `ModuleNotFoundError: No module named 'claude_alerts.contexts'`.

- [ ] **Step 1.3: Create the module skeleton**

Write `claude_alerts/contexts.py`:

```python
"""Read per-session context-window data captured by scripts/hooks/statusline.sh.

statusline.sh writes one sidecar per active Claude Code session every
prompt update:

```json
{
  "saved_at": 1777643245.812,
  "session_id": "abc",
  "context_window": {
    "context_window_size": 200000,
    "used_percentage": 7.9,
    "current_usage": {
      "input_tokens": 8500,
      "output_tokens": 1200,
      "cache_creation_input_tokens": 5000,
      "cache_read_input_tokens": 2000
    }
  }
}
```

`current_usage` is null before the first API call. `used_percentage` and
`context_window_size` may also be null/missing early in the session. The
formatter renders `—` for any field that's None.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ContextUsage:
    saved_at: float
    used_percentage: Optional[float]   # 0–100, or None when not yet known
    used_tokens: Optional[int]         # input + cache_creation + cache_read
    total_tokens: Optional[int]        # context_window_size


def default_contexts_dir() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "claude-alerts" / "contexts"


def _sidecar_path(session_id: str, base_dir: Path) -> Path:
    return base_dir / f"{session_id}.json"


def load(session_id: str, base_dir: Path) -> Optional[ContextUsage]:
    """Read and parse the per-session sidecar.

    Returns None only when the file is missing, unreadable, or the JSON
    is malformed/wrong-shape. If the file parses but individual fields
    are missing or unusable (current_usage null, context_window_size <= 0,
    used_percentage null), the corresponding ContextUsage field is left
    as None and the formatter renders '—'.
    """
    path = _sidecar_path(session_id, base_dir)
    try:
        text = path.read_text()
    except FileNotFoundError:
        return None
    except OSError as e:
        log.debug("cannot read %s: %s", path, e)
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        log.debug("malformed sidecar %s", path)
        return None
    if not isinstance(data, dict):
        return None
    cw = data.get("context_window")
    if not isinstance(cw, dict):
        return None

    saved_at = float(data.get("saved_at", 0.0) or 0.0)

    total = cw.get("context_window_size")
    try:
        total_tokens: Optional[int] = int(total) if total is not None else None
    except (TypeError, ValueError):
        total_tokens = None
    if total_tokens is not None and total_tokens <= 0:
        total_tokens = None

    pct = cw.get("used_percentage")
    try:
        used_pct: Optional[float] = float(pct) if pct is not None else None
    except (TypeError, ValueError):
        used_pct = None

    cu = cw.get("current_usage")
    used_tokens: Optional[int] = None
    if isinstance(cu, dict):
        try:
            used_tokens = (
                int(cu.get("input_tokens") or 0)
                + int(cu.get("cache_creation_input_tokens") or 0)
                + int(cu.get("cache_read_input_tokens") or 0)
            )
        except (TypeError, ValueError):
            used_tokens = None

    return ContextUsage(
        saved_at=saved_at,
        used_percentage=used_pct,
        used_tokens=used_tokens,
        total_tokens=total_tokens,
    )
```

- [ ] **Step 1.4: Run the tests — expect them to pass**

Run: `cd /home/abhishek/claude-alerts && .venv/bin/pytest tests/test_contexts.py -v`
Expected: 3 passed.

- [ ] **Step 1.5: Add edge-case tests**

Append to `tests/test_contexts.py`:

```python
def test_load_malformed_json_returns_none(tmp_path):
    p = tmp_path / "abc.json"
    p.write_text("{not json")
    assert contexts.load("abc", tmp_path) is None


def test_load_top_level_not_object_returns_none(tmp_path):
    p = tmp_path / "abc.json"
    p.write_text("[]")
    assert contexts.load("abc", tmp_path) is None


def test_load_missing_context_window_returns_none(tmp_path):
    _write_sidecar(tmp_path, "abc", {"saved_at": 1.0, "session_id": "abc"})
    assert contexts.load("abc", tmp_path) is None


def test_load_null_current_usage_yields_none_used_tokens(tmp_path):
    payload = _full_payload()
    payload["context_window"]["current_usage"] = None
    _write_sidecar(tmp_path, "abc", payload)
    cu = contexts.load("abc", tmp_path)
    assert cu is not None
    assert cu.used_tokens is None
    assert cu.used_percentage == 7.9
    assert cu.total_tokens == 200000


def test_load_zero_context_window_size_yields_none_total(tmp_path):
    payload = _full_payload()
    payload["context_window"]["context_window_size"] = 0
    _write_sidecar(tmp_path, "abc", payload)
    cu = contexts.load("abc", tmp_path)
    assert cu is not None
    assert cu.total_tokens is None


def test_load_missing_context_window_size_yields_none_total(tmp_path):
    payload = _full_payload()
    del payload["context_window"]["context_window_size"]
    _write_sidecar(tmp_path, "abc", payload)
    cu = contexts.load("abc", tmp_path)
    assert cu is not None
    assert cu.total_tokens is None


def test_load_null_used_percentage_yields_none(tmp_path):
    payload = _full_payload()
    payload["context_window"]["used_percentage"] = None
    _write_sidecar(tmp_path, "abc", payload)
    cu = contexts.load("abc", tmp_path)
    assert cu is not None
    assert cu.used_percentage is None


def test_load_excludes_output_tokens_from_used(tmp_path):
    """Mirrors how Claude Code computes used_percentage: input-only."""
    payload = _full_payload()
    payload["context_window"]["current_usage"]["output_tokens"] = 9999999
    _write_sidecar(tmp_path, "abc", payload)
    cu = contexts.load("abc", tmp_path)
    assert cu.used_tokens == 8500 + 5000 + 2000
```

- [ ] **Step 1.6: Run the tests — expect them all to pass**

Run: `cd /home/abhishek/claude-alerts && .venv/bin/pytest tests/test_contexts.py -v`
Expected: 10 passed.

- [ ] **Step 1.7: Commit**

```bash
git add claude_alerts/contexts.py tests/test_contexts.py
git commit -m "feat(contexts): add per-session context-window sidecar loader"
```

---

## Task 2: contexts.py — `delete()` and `sweep()`

**Files:**
- Modify: `claude_alerts/contexts.py`
- Modify: `tests/test_contexts.py`

- [ ] **Step 2.1: Add failing tests**

Append to `tests/test_contexts.py`:

```python
def test_delete_removes_sidecar(tmp_path):
    _write_sidecar(tmp_path, "abc", _full_payload())
    contexts.delete("abc", tmp_path)
    assert not (tmp_path / "abc.json").exists()


def test_delete_is_idempotent_on_missing(tmp_path):
    # No exception when the file does not exist.
    contexts.delete("ghost", tmp_path)


def test_sweep_keeps_active_sessions_only(tmp_path):
    _write_sidecar(tmp_path, "alive", _full_payload())
    _write_sidecar(tmp_path, "dead",  _full_payload())
    removed = contexts.sweep({"alive"}, tmp_path)
    assert removed == 1
    assert (tmp_path / "alive.json").exists()
    assert not (tmp_path / "dead.json").exists()


def test_sweep_no_directory_returns_zero(tmp_path):
    assert contexts.sweep({"alive"}, tmp_path / "missing") == 0


def test_sweep_ignores_non_json_files(tmp_path):
    tmp_path.mkdir(exist_ok=True)
    (tmp_path / "junk.txt").write_text("not ours")
    assert contexts.sweep(set(), tmp_path) == 0
    assert (tmp_path / "junk.txt").exists()
```

- [ ] **Step 2.2: Run them — expect AttributeError**

Run: `cd /home/abhishek/claude-alerts && .venv/bin/pytest tests/test_contexts.py -v`
Expected: 5 failures with `AttributeError: module 'claude_alerts.contexts' has no attribute 'delete'`.

- [ ] **Step 2.3: Implement `delete` and `sweep`**

Append to `claude_alerts/contexts.py`:

```python
def delete(session_id: str, base_dir: Path) -> None:
    """Remove a per-session sidecar. Best-effort — missing file is not an error."""
    path = _sidecar_path(session_id, base_dir)
    try:
        path.unlink()
    except FileNotFoundError:
        return
    except OSError as e:
        log.debug("cannot delete %s: %s", path, e)


def sweep(active_session_ids: set[str], base_dir: Path) -> int:
    """Delete sidecars whose session_id is not in `active_session_ids`.

    Used at daemon startup to clean up files left behind by a crash before
    SessionEnd fired. Returns the number of files removed.
    """
    if not base_dir.is_dir():
        return 0
    removed = 0
    for path in base_dir.glob("*.json"):
        sid = path.stem
        if sid in active_session_ids:
            continue
        try:
            path.unlink()
            removed += 1
        except OSError as e:
            log.debug("sweep: cannot delete %s: %s", path, e)
    return removed
```

- [ ] **Step 2.4: Run the tests — expect all to pass**

Run: `cd /home/abhishek/claude-alerts && .venv/bin/pytest tests/test_contexts.py -v`
Expected: 15 passed.

- [ ] **Step 2.5: Commit**

```bash
git add claude_alerts/contexts.py tests/test_contexts.py
git commit -m "feat(contexts): add delete and sweep for per-session sidecars"
```

---

## Task 3: statusline.sh — write per-session contexts sidecar

**Files:**
- Modify: `scripts/hooks/statusline.sh`
- Create: `tests/test_statusline_sh.py`

- [ ] **Step 3.1: Write the failing tests**

Create `tests/test_statusline_sh.py`:

```python
"""Subprocess tests for scripts/hooks/statusline.sh — contexts sidecar behavior."""
import json
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "hooks" / "statusline.sh"


def has_jq():
    return shutil.which("jq") is not None


def _run(payload: dict, tmp_path: Path) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["CLAUDE_ALERTS_STATE_DIR"] = str(tmp_path)
    env.pop("CLAUDE_ALERTS_WRAPPED_STATUSLINE", None)
    return subprocess.run(
        ["bash", str(SCRIPT)],
        input=json.dumps(payload), text=True, env=env,
        capture_output=True, check=True,
    )


def _full_payload(session_id: str = "abc-123") -> dict:
    return {
        "session_id": session_id,
        "model": {"display_name": "Sonnet"},
        "workspace": {"current_dir": "/tmp/proj"},
        "rate_limits": {"five_hour": {"used_percentage": 1.0, "resets_at": 0}},
        "context_window": {
            "total_input_tokens": 100,
            "total_output_tokens": 50,
            "context_window_size": 200000,
            "used_percentage": 5.0,
            "remaining_percentage": 95.0,
            "current_usage": {
                "input_tokens": 8000,
                "output_tokens": 1000,
                "cache_creation_input_tokens": 4000,
                "cache_read_input_tokens": 2000,
            },
        },
    }


@pytest.mark.skipif(not has_jq(), reason="jq is required by the hook script")
def test_writes_contexts_sidecar(tmp_path):
    _run(_full_payload("abc-123"), tmp_path)
    sidecar = tmp_path / "contexts" / "abc-123.json"
    assert sidecar.exists()
    data = json.loads(sidecar.read_text())
    assert data["session_id"] == "abc-123"
    assert data["context_window"]["context_window_size"] == 200000
    assert data["context_window"]["current_usage"]["input_tokens"] == 8000
    # Mode 0600.
    assert stat.S_IMODE(sidecar.stat().st_mode) == 0o600


@pytest.mark.skipif(not has_jq(), reason="jq is required")
def test_no_context_window_does_not_write_sidecar(tmp_path):
    payload = _full_payload()
    del payload["context_window"]
    _run(payload, tmp_path)
    assert not (tmp_path / "contexts").exists() or not list((tmp_path / "contexts").glob("*.json"))
    # Rate-limits sidecar still gets written.
    assert (tmp_path / "rate_limits.json").exists()


@pytest.mark.skipif(not has_jq(), reason="jq is required")
def test_rejects_session_id_with_path_traversal(tmp_path):
    _run(_full_payload("../../etc/passwd"), tmp_path)
    # Nothing written outside the contexts dir.
    assert not (tmp_path.parent / "etc").exists()
    contexts_dir = tmp_path / "contexts"
    if contexts_dir.exists():
        assert list(contexts_dir.glob("*.json")) == []


@pytest.mark.skipif(not has_jq(), reason="jq is required")
def test_rejects_session_id_with_control_chars(tmp_path):
    _run(_full_payload("abc\nevil"), tmp_path)
    contexts_dir = tmp_path / "contexts"
    if contexts_dir.exists():
        assert list(contexts_dir.glob("*.json")) == []


@pytest.mark.skipif(not has_jq(), reason="jq is required")
def test_atomic_write_no_tmp_files_left(tmp_path):
    _run(_full_payload(), tmp_path)
    contexts_dir = tmp_path / "contexts"
    assert list(contexts_dir.glob("*.tmp")) == []
```

- [ ] **Step 3.2: Run them — expect all to fail (no contexts sidecar yet)**

Run: `cd /home/abhishek/claude-alerts && .venv/bin/pytest tests/test_statusline_sh.py -v`
Expected: failures on `test_writes_contexts_sidecar`, others may pass trivially.

- [ ] **Step 3.3: Read the current `statusline.sh`**

Run: `cat scripts/hooks/statusline.sh` — confirm it has the existing single-jq-pass block. We're going to add `session_id` and `context_window` extraction, then a write block.

- [ ] **Step 3.4: Modify `scripts/hooks/statusline.sh`**

Replace the existing jq pass + persistence block with the version below. Keep the wrapper-statusline tail and the default minimal status line untouched.

The full updated middle section of the script (between the `PAYLOAD=` line and the `WRAP=` line) should read:

```bash
PAYLOAD="$(cat || true)"

# Single jq pass to extract everything we need; cheaper than several invocations.
# `|| true` keeps `set -e` from killing us when jq fails (e.g. malformed JSON).
JQ_OUT="$(
    printf '%s' "$PAYLOAD" | jq -r '
        [
            (.rate_limits | tojson),
            (.model.display_name // .model.id // ""),
            (.workspace.current_dir // .cwd // ""),
            (.git.branch // ""),
            (.session_id // ""),
            (.context_window | tojson)
        ] | @tsv
    ' 2>/dev/null || printf '\t\t\t\t\t'
)"
IFS=$'\t' read -r RATE_LIMITS MODEL CWD BRANCH SESSION_ID CONTEXT_WINDOW <<< "${JQ_OUT:-$'\t\t\t\t\t'}"

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
```

- [ ] **Step 3.5: Run the tests — expect all to pass**

Run: `cd /home/abhishek/claude-alerts && .venv/bin/pytest tests/test_statusline_sh.py -v`
Expected: 5 passed (or skipped if `jq` is not installed).

- [ ] **Step 3.6: Commit**

```bash
git add scripts/hooks/statusline.sh tests/test_statusline_sh.py
git commit -m "feat(statusline): write per-session context_window sidecar"
```

---

## Task 4: Dashboard `_short_tokens` and `_format_ctx` helpers

**Files:**
- Modify: `claude_alerts/dashboard.py`
- Modify: `tests/test_dashboard.py`

- [ ] **Step 4.1: Write the failing tests**

Append to `tests/test_dashboard.py` (top imports first — extend the existing `from claude_alerts.dashboard import (...)` block to also import `_format_ctx` and `_short_tokens`):

```python
from claude_alerts.contexts import ContextUsage


def test_short_tokens_under_thousand():
    from claude_alerts.dashboard import _short_tokens
    assert _short_tokens(0) == "0"
    assert _short_tokens(850) == "850"


def test_short_tokens_thousands():
    from claude_alerts.dashboard import _short_tokens
    assert _short_tokens(1000) == "1k"
    assert _short_tokens(90_000) == "90k"
    assert _short_tokens(199_500) == "200k"   # rounds to nearest 1000
    assert _short_tokens(200_000) == "200k"


def test_short_tokens_millions():
    from claude_alerts.dashboard import _short_tokens
    assert _short_tokens(1_000_000) == "1.0M"
    assert _short_tokens(1_200_000) == "1.2M"


def test_format_ctx_none_returns_dash():
    from claude_alerts.dashboard import _format_ctx
    assert _format_ctx(None).strip() == "—"


def test_format_ctx_partial_data_returns_dash():
    from claude_alerts.dashboard import _format_ctx
    cu = ContextUsage(saved_at=1.0, used_percentage=None,
                      used_tokens=100, total_tokens=200000)
    assert _format_ctx(cu).strip() == "—"
    cu = ContextUsage(saved_at=1.0, used_percentage=10.0,
                      used_tokens=None, total_tokens=200000)
    assert _format_ctx(cu).strip() == "—"
    cu = ContextUsage(saved_at=1.0, used_percentage=10.0,
                      used_tokens=100, total_tokens=None)
    assert _format_ctx(cu).strip() == "—"


def test_format_ctx_normal():
    from claude_alerts.dashboard import _format_ctx
    cu = ContextUsage(saved_at=1.0, used_percentage=45.2,
                      used_tokens=90_000, total_tokens=200_000)
    assert _format_ctx(cu).strip() == "45% (90k/200k)"


def test_format_ctx_under_one_percent():
    from claude_alerts.dashboard import _format_ctx
    cu = ContextUsage(saved_at=1.0, used_percentage=0.4,
                      used_tokens=800, total_tokens=200_000)
    assert _format_ctx(cu).strip() == "<1% (800/200k)"


def test_format_ctx_extended_context():
    from claude_alerts.dashboard import _format_ctx
    cu = ContextUsage(saved_at=1.0, used_percentage=55.5,
                      used_tokens=550_000, total_tokens=1_000_000)
    assert _format_ctx(cu).strip() == "56% (550k/1.0M)"


def test_format_ctx_padded_to_fixed_width():
    """The CTX field is left-justified, padded to 16 chars so CWD stays aligned."""
    from claude_alerts.dashboard import _format_ctx
    cu = ContextUsage(saved_at=1.0, used_percentage=45.2,
                      used_tokens=90_000, total_tokens=200_000)
    assert _format_ctx(cu) == "45% (90k/200k)  "  # 14 + 2 padding = 16
    assert len(_format_ctx(cu)) == 16
    assert len(_format_ctx(None)) == 16
```

- [ ] **Step 4.2: Run them — expect ImportError on `_format_ctx`**

Run: `cd /home/abhishek/claude-alerts && .venv/bin/pytest tests/test_dashboard.py -v`
Expected: 9 failures with `ImportError: cannot import name '_format_ctx'`.

- [ ] **Step 4.3: Add the helpers to `claude_alerts/dashboard.py`**

Insert these two functions after the existing `_format_age` definition (around line 110), before the `class Dashboard` definition:

```python
def _short_tokens(n: int) -> str:
    """Compact token-count formatting matching what /context shows.

    <1000      -> '850'
    <1_000_000 -> '90k' (rounded to nearest thousand)
    >=1_000_000 -> '1.0M' (one decimal place)
    """
    if n < 1_000:
        return str(n)
    if n < 1_000_000:
        return f"{round(n / 1_000)}k"
    return f"{n / 1_000_000:.1f}M"


def _format_ctx(cu: "ContextUsage | None") -> str:
    """Format a ContextUsage as a fixed-width 16-char left-justified string.

    Returns the en-dash '—' (padded) when `cu` is None or any required
    field is None. Sub-1% non-zero usage renders as '<1% (...)' rather
    than '0% (...)' so the user knows tokens are accumulating.
    """
    width = 16
    if cu is None or cu.used_percentage is None or cu.used_tokens is None or cu.total_tokens is None:
        return f"{'—':<{width}}"
    used = _short_tokens(cu.used_tokens)
    total = _short_tokens(cu.total_tokens)
    if 0 < cu.used_percentage < 1:
        text = f"<1% ({used}/{total})"
    else:
        pct = int(round(cu.used_percentage))
        text = f"{pct}% ({used}/{total})"
    return f"{text:<{width}}"
```

Add the import at the top of `claude_alerts/dashboard.py` (next to the existing `from claude_alerts.limits import ...`):

```python
from claude_alerts import contexts
from claude_alerts.contexts import ContextUsage
```

- [ ] **Step 4.4: Run the tests — expect all to pass**

Run: `cd /home/abhishek/claude-alerts && .venv/bin/pytest tests/test_dashboard.py -v`
Expected: 9 new tests passing, all pre-existing tests still pass.

- [ ] **Step 4.5: Commit**

```bash
git add claude_alerts/dashboard.py tests/test_dashboard.py
git commit -m "feat(dashboard): add _short_tokens and _format_ctx helpers"
```

---

## Task 5: Dashboard — render the `CTX` column

**Files:**
- Modify: `claude_alerts/dashboard.py`
- Modify: `tests/test_dashboard.py`

- [ ] **Step 5.1: Write a failing test**

Append to `tests/test_dashboard.py`:

```python
def test_dashboard_renders_ctx_column_with_data(tmp_path):
    """When a session has a contexts sidecar, the CTX column shows the percent."""
    contexts_dir = tmp_path / "contexts"
    contexts_dir.mkdir()
    sid = "ctxtest1-aaaaaaaa"
    (contexts_dir / f"{sid}.json").write_text(json.dumps({
        "saved_at": 1.0,
        "session_id": sid,
        "context_window": {
            "context_window_size": 200000,
            "used_percentage": 45.2,
            "current_usage": {
                "input_tokens": 80_000,
                "output_tokens": 5_000,
                "cache_creation_input_tokens": 6_000,
                "cache_read_input_tokens": 4_000,
            },
        },
    }))

    store = SessionStore()
    store.apply_event(ClaudeEvent(
        event="UserPromptSubmit", session_id=sid, cwd=str(tmp_path),
        claude_pid=1, timestamp=1.0,
    ))

    d = Dashboard(
        store,
        sidecar_path=tmp_path / "absent.json",
        contexts_dir=contexts_dir,
        force_render=True,
    )
    text = d.render_string()
    assert "CTX" in text
    # 80_000 + 6_000 + 4_000 = 90_000 -> 90k. Percent rounds to 45.
    assert "45% (90k/200k)" in text


def test_dashboard_renders_em_dash_when_no_contexts_data(tmp_path):
    contexts_dir = tmp_path / "contexts"  # never created
    sid = "noctxd1-aaaaaaaa"
    store = SessionStore()
    store.apply_event(ClaudeEvent(
        event="UserPromptSubmit", session_id=sid, cwd=str(tmp_path),
        claude_pid=1, timestamp=1.0,
    ))
    d = Dashboard(
        store,
        sidecar_path=tmp_path / "absent.json",
        contexts_dir=contexts_dir,
        force_render=True,
    )
    text = d.render_string()
    # Em-dash appears in the rendered table (we don't assert exact alignment
    # here — the helper-level test already covers that).
    assert "—" in text


def test_dashboard_session_block_keeps_cwd_aligned(tmp_path):
    """Mixed rows (one with data, one without) — CWD column stays at the same
    horizontal position so the table stays readable."""
    contexts_dir = tmp_path / "contexts"
    contexts_dir.mkdir()
    sid_with = "withctx1-aaaaaaaa"
    (contexts_dir / f"{sid_with}.json").write_text(json.dumps({
        "saved_at": 1.0,
        "session_id": sid_with,
        "context_window": {
            "context_window_size": 200000,
            "used_percentage": 10.0,
            "current_usage": {
                "input_tokens": 18000, "output_tokens": 0,
                "cache_creation_input_tokens": 1000, "cache_read_input_tokens": 1000,
            },
        },
    }))
    sid_without = "noctx1aa-bbbbbbbb"

    store = SessionStore()
    for sid in (sid_with, sid_without):
        store.apply_event(ClaudeEvent(
            event="UserPromptSubmit", session_id=sid, cwd="/work",
            claude_pid=1, timestamp=1.0,
        ))

    d = Dashboard(
        store,
        sidecar_path=tmp_path / "absent.json",
        contexts_dir=contexts_dir,
        force_render=True,
    )
    text = d.render_string()
    rows = [r for r in text.splitlines() if "/work" in r]
    assert len(rows) == 2
    # CWD lives at the same column index in both rows.
    cwd_cols = [r.index("/work") for r in rows]
    assert cwd_cols[0] == cwd_cols[1]
```

- [ ] **Step 5.2: Run them — expect failure on the unknown `contexts_dir` constructor arg**

Run: `cd /home/abhishek/claude-alerts && .venv/bin/pytest tests/test_dashboard.py::test_dashboard_renders_ctx_column_with_data -v`
Expected: `TypeError: __init__() got an unexpected keyword argument 'contexts_dir'`.

- [ ] **Step 5.3: Update `Dashboard.__init__` to accept `contexts_dir`**

In `claude_alerts/dashboard.py`, change the constructor signature:

```python
def __init__(
    self,
    store: SessionStore,
    sidecar_path: Optional[Path] = None,
    out: Optional[TextIO] = None,
    force_render: bool = False,
    contexts_dir: Optional[Path] = None,
) -> None:
    self.store = store
    self.sidecar_path = sidecar_path or default_sidecar_path()
    self.contexts_dir = contexts_dir or contexts.default_contexts_dir()
    self.out = out if out is not None else sys.stdout
    self.enabled = force_render or self._is_tty(self.out)
    self._dirty = True
    self._last_paint = 0.0
    self._last_tick = 0.0
    self._cached_limits: Optional[RateLimits] = None
```

- [ ] **Step 5.4: Update `_sessions_block` to render the CTX column**

Replace the existing `_sessions_block` method in `claude_alerts/dashboard.py`:

```python
def _sessions_block(self, sessions: list[Session]) -> list[str]:
    if not sessions:
        return ["  no active sessions."]
    # CTX column is fixed-width 16 (see _format_ctx).
    rows = [f"  SESSION   STATUS     {'CTX':<16}  CWD"]
    for s in sessions:
        sid = _short_id(s.session_id)
        cwd = _short_cwd(s.cwd)
        cu = contexts.load(s.session_id, self.contexts_dir)
        ctx = _format_ctx(cu)
        rows.append(f"  {sid:<8}  {_status_marker(s.status):<9}  {ctx}  {cwd}")
    return rows
```

- [ ] **Step 5.5: Run the new tests — expect all to pass**

Run: `cd /home/abhishek/claude-alerts && .venv/bin/pytest tests/test_dashboard.py -v`
Expected: all dashboard tests pass.

- [ ] **Step 5.6: Run the full test suite — sanity check**

Run: `cd /home/abhishek/claude-alerts && .venv/bin/pytest -x`
Expected: all green.

- [ ] **Step 5.7: Commit**

```bash
git add claude_alerts/dashboard.py tests/test_dashboard.py
git commit -m "feat(dashboard): add CTX column to active-sessions block"
```

---

## Task 6: Daemon — cleanup on SessionEnd and startup sweep

**Files:**
- Modify: `claude_alerts/daemon.py`
- Create: `tests/test_daemon_contexts.py`

- [ ] **Step 6.1: Write failing tests**

Create `tests/test_daemon_contexts.py`:

```python
"""Tests for daemon-level lifecycle of the per-session contexts sidecar."""
import json
from pathlib import Path

from claude_alerts import contexts
from claude_alerts.events import ClaudeEvent
from claude_alerts.sessions import SessionStore


def _write_ctx(base: Path, sid: str) -> Path:
    base.mkdir(parents=True, exist_ok=True)
    p = base / f"{sid}.json"
    p.write_text(json.dumps({
        "saved_at": 1.0, "session_id": sid,
        "context_window": {
            "context_window_size": 200000, "used_percentage": 1.0,
            "current_usage": {
                "input_tokens": 1000, "output_tokens": 0,
                "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0,
            },
        },
    }))
    return p


def test_session_end_deletes_contexts_sidecar(tmp_path):
    """When SessionEnd fires, the per-session sidecar is removed.

    We don't spin up a real Daemon (X11). Instead we exercise the wiring:
    SessionStore.apply_event(SessionEnd) triggers an on_change callback
    that deletes the sidecar. Mirror what daemon.run() registers.
    """
    contexts_dir = tmp_path / "contexts"
    sid = "ending-sid"
    _write_ctx(contexts_dir, sid)
    store = SessionStore()

    # Pre-populate the session, then register the cleanup callback (matches
    # daemon wiring order — handler is added after restore, before events).
    store.apply_event(ClaudeEvent(
        event="UserPromptSubmit", session_id=sid, cwd="/x",
        claude_pid=1, timestamp=1.0,
    ))

    def cleanup(session_id: str) -> None:
        if store.get(session_id) is None:
            contexts.delete(session_id, contexts_dir)

    store.on_change(cleanup)

    store.apply_event(ClaudeEvent(
        event="SessionEnd", session_id=sid, cwd="/x",
        claude_pid=1, timestamp=2.0,
    ))

    assert not (contexts_dir / f"{sid}.json").exists()


def test_startup_sweep_removes_orphan_sidecars(tmp_path):
    """On startup, sidecars whose session_id isn't in the restored store are removed."""
    contexts_dir = tmp_path / "contexts"
    _write_ctx(contexts_dir, "alive")
    _write_ctx(contexts_dir, "orphan-1")
    _write_ctx(contexts_dir, "orphan-2")

    active = {"alive"}
    removed = contexts.sweep(active, contexts_dir)

    assert removed == 2
    assert (contexts_dir / "alive.json").exists()
    assert not (contexts_dir / "orphan-1.json").exists()
    assert not (contexts_dir / "orphan-2.json").exists()
```

- [ ] **Step 6.2: Run them — startup sweep test passes (it tests `contexts.sweep` directly, already implemented in Task 2); the SessionEnd test passes too because it exercises the pattern, not the daemon. So this step verifies the *helpers* behave correctly before we wire them into the daemon.**

Run: `cd /home/abhishek/claude-alerts && .venv/bin/pytest tests/test_daemon_contexts.py -v`
Expected: 2 passed.

- [ ] **Step 6.3: Wire cleanup into the daemon**

Modify `claude_alerts/daemon.py`. Add at the top, with the other imports:

```python
from claude_alerts import contexts
```

Add a new constant near the existing `IDLE_SWEEP_INTERVAL_S`:

```python
def default_contexts_dir() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "claude-alerts" / "contexts"
```

In `Daemon.__init__`, accept and store a `contexts_dir`:

```python
def __init__(
    self,
    events_dir: Path,
    config: Config,
    persistence_path: Path | None = None,
    dashboard_enabled: bool = True,
    contexts_dir: Path | None = None,
) -> None:
    ...
    self.contexts_dir = contexts_dir or default_contexts_dir()
    ...
    self.dashboard = (
        Dashboard(self.store, contexts_dir=self.contexts_dir)
        if dashboard_enabled else None
    )
```

Add a cleanup handler method on `Daemon`:

```python
def _cleanup_contexts(self, session_id: str) -> None:
    """Delete the per-session contexts sidecar when a session goes away."""
    if self.store.get(session_id) is None:
        contexts.delete(session_id, self.contexts_dir)
```

Register it in `__init__` next to `_reap_binder_queue`:

```python
self.store.on_change(self._cleanup_contexts)
```

In `Daemon.run()`, after the persister-driven restore loop and before `self.ingester_thread = ...`, add the startup sweep:

```python
# Remove any contexts/<sid>.json files left behind by a crash before
# SessionEnd. After this point, every write to contexts/ is paired with
# a matching delete on SessionEnd.
contexts.sweep({s.session_id for s in self.store.all()}, self.contexts_dir)
```

- [ ] **Step 6.4: Add an integration-style test that exercises the wiring**

Append to `tests/test_daemon_contexts.py`:

```python
def test_daemon_cleanup_handler_deletes_sidecar(tmp_path, monkeypatch):
    """Daemon._cleanup_contexts deletes the sidecar when the session is gone."""
    from claude_alerts.daemon import Daemon
    from claude_alerts.config import Config

    contexts_dir = tmp_path / "contexts"
    _write_ctx(contexts_dir, "gone-sid")

    # Construct a Daemon without running its main loop. We only need
    # __init__ side-effects, then we invoke the handler directly.
    monkeypatch.setattr("claude_alerts.daemon.X11Client", lambda: _DummyX11())
    monkeypatch.setattr(
        "claude_alerts.daemon.OverlayManager",
        lambda x11, store, config: _DummyOverlay(),
    )

    d = Daemon(
        events_dir=tmp_path / "events",
        config=Config(),
        persistence_path=None,
        dashboard_enabled=False,
        contexts_dir=contexts_dir,
    )

    # Session does not exist in the store -> handler deletes.
    d._cleanup_contexts("gone-sid")
    assert not (contexts_dir / "gone-sid.json").exists()


class _DummyX11:
    def fileno(self): return -1
    def subscribe_root_substructure(self): pass
    def get_visible_geometry(self, _wid): return None
    def pending_events(self): return False
    def next_event(self): return None


class _DummyOverlay:
    def sync_all(self): pass
    def on_window_configure(self, *a, **kw): pass
```

- [ ] **Step 6.5: Run the tests — expect all to pass**

Run: `cd /home/abhishek/claude-alerts && .venv/bin/pytest tests/test_daemon_contexts.py -v`
Expected: 3 passed.

- [ ] **Step 6.6: Run the full test suite — final regression check**

Run: `cd /home/abhishek/claude-alerts && .venv/bin/pytest`
Expected: all green.

- [ ] **Step 6.7: Commit**

```bash
git add claude_alerts/daemon.py tests/test_daemon_contexts.py
git commit -m "feat(daemon): cleanup contexts sidecar on SessionEnd and startup"
```

---

## Task 7: Manual smoke test

**Files:** none.

- [ ] **Step 7.1: Reinstall the package**

Run: `cd /home/abhishek/claude-alerts && ~/.local/share/claude-alerts/venv/bin/pip install -e .`
Expected: install succeeds (the helper script changed but the package import path is unchanged).

- [ ] **Step 7.2: Restart the daemon in the foreground**

Run (in a new terminal): `claude-alerts`
Expected: dashboard renders with the new `CTX` column header.

- [ ] **Step 7.3: Trigger a Claude Code session**

In another terminal, start a `claude` session. Send a message. Check that the dashboard shows `CTX` populated for that session within a few seconds (depends on when the next prompt update fires).

- [ ] **Step 7.4: Verify the per-session sidecar exists**

Run: `ls ~/.local/state/claude-alerts/contexts/`
Expected: a file named `<session_id>.json` for each active session.

Run: `cat ~/.local/state/claude-alerts/contexts/<session_id>.json | jq .context_window.context_window_size`
Expected: `200000` (or `1000000` if you opted into extended context).

- [ ] **Step 7.5: End the session and verify cleanup**

Exit the Claude Code session. Within ~5 seconds the daemon should receive `SessionEnd` and the file should disappear.

Run: `ls ~/.local/state/claude-alerts/contexts/`
Expected: the ended session's file is gone.

- [ ] **Step 7.6: (Optional) Test startup sweep**

Stop the daemon. Manually drop a fake orphan: `echo '{}' > ~/.local/state/claude-alerts/contexts/orphan-test.json`. Restart the daemon. Expected: `ls` shows no `orphan-test.json` after startup.

---

## Self-Review

**Spec coverage:**

| Spec section | Implemented in |
|---|---|
| Sidecar format | Task 3 (statusline.sh write) + Task 1 (contexts.py read) |
| `claude_alerts/contexts.py` module | Tasks 1 & 2 |
| Dashboard `_format_ctx`, `_short_tokens` | Task 4 |
| Dashboard CTX column rendering | Task 5 |
| Daemon SessionEnd cleanup | Task 6 |
| Daemon startup sweep | Task 6 |
| `statusline.sh` extension | Task 3 |
| Edge cases (no data, sub-1%, malformed JSON, null fields) | Covered in Task 1 (loader tests) and Task 4 (formatter tests) |
| Security (path traversal, control chars in session_id) | Task 3 (test_rejects_session_id_with_path_traversal, test_rejects_session_id_with_control_chars) |
| Atomic writes / mode 0600 | Task 3 (test_writes_contexts_sidecar checks mode) |
| Tests called out in spec | All present except `test_e2e_xvfb.py` (spec said it doesn't need to change) |

**Type & name consistency:** `ContextUsage` has `saved_at`, `used_percentage`, `used_tokens`, `total_tokens` throughout. `contexts.load(session_id, base_dir)` arity is consistent across module, dashboard call site, and daemon. `default_contexts_dir()` exists on both `claude_alerts.contexts` (canonical) and `claude_alerts.daemon` (local convenience matching the file's existing pattern of `default_events_dir`/`default_log_path`).

**No placeholders.** Every step has either runnable code or a concrete shell command.
