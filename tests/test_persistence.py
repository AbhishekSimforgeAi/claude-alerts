"""Tests for BindingPersister."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from claude_alerts.persistence import (
    SCHEMA_VERSION,
    BindingPersister,
    _dict_to_session,
    _session_to_dict,
)
from claude_alerts.sessions import Session, Status


def _make_session(
    session_id: str = "s1",
    bound: int | None = 0xFF22,
    client: int | None = 0xCC11,
) -> Session:
    return Session(
        session_id=session_id,
        cwd="/p",
        claude_pid=42,
        status=Status.WORKING,
        last_event_at=1.5,
        bound_window_id=bound,
        client_window_id=client,
        last_event="UserPromptSubmit",
        background_active=False,
    )


def _settle(persister: BindingPersister, timeout_s: float = 1.0) -> None:
    """Wait for the throttled timer to flush. Calls stop() so all pending
    work completes deterministically."""
    persister.stop()


def test_save_writes_atomic_file(tmp_path):
    p = BindingPersister(tmp_path / "sessions.json", throttle_s=0.0)
    p.save([_make_session()])
    _settle(p)

    data = json.loads((tmp_path / "sessions.json").read_text())
    assert data["version"] == SCHEMA_VERSION
    assert "saved_at" in data
    assert len(data["sessions"]) == 1
    assert data["sessions"][0]["session_id"] == "s1"
    assert data["sessions"][0]["bound_window_id"] == 0xFF22
    assert data["sessions"][0]["client_window_id"] == 0xCC11


def test_save_filters_out_unbound_sessions(tmp_path):
    p = BindingPersister(tmp_path / "sessions.json", throttle_s=0.0)
    bound = _make_session("bound", bound=0xAAAA, client=0xAAAA)
    unbound = _make_session("unbound", bound=None, client=None)
    p.save([bound, unbound])
    _settle(p)

    data = json.loads((tmp_path / "sessions.json").read_text())
    ids = [s["session_id"] for s in data["sessions"]]
    assert ids == ["bound"]


def test_load_round_trips_all_fields(tmp_path):
    p = BindingPersister(tmp_path / "sessions.json", throttle_s=0.0)
    original = Session(
        session_id="abc",
        cwd="/x/y",
        claude_pid=7,
        status=Status.WAITING,
        last_event_at=12.5,
        bound_window_id=0x100,
        client_window_id=0x200,
        last_event="PermissionRequest",
        background_active=True,
    )
    p.save([original])
    _settle(p)

    p2 = BindingPersister(tmp_path / "sessions.json", throttle_s=0.0)
    loaded = p2.load()
    assert len(loaded) == 1
    s = loaded[0]
    assert s.session_id == "abc"
    assert s.cwd == "/x/y"
    assert s.claude_pid == 7
    assert s.status == Status.WAITING
    assert s.last_event_at == 12.5
    assert s.bound_window_id == 0x100
    assert s.client_window_id == 0x200
    assert s.last_event == "PermissionRequest"
    assert s.background_active is True


def test_load_missing_file_returns_empty(tmp_path):
    p = BindingPersister(tmp_path / "absent.json")
    assert p.load() == []


def test_load_malformed_json_returns_empty(tmp_path, caplog):
    path = tmp_path / "sessions.json"
    path.write_text("{not json")
    p = BindingPersister(path)
    assert p.load() == []
    assert any("malformed" in rec.message for rec in caplog.records)


def test_load_wrong_version_returns_empty(tmp_path, caplog):
    path = tmp_path / "sessions.json"
    path.write_text(json.dumps({"version": 999, "sessions": []}))
    p = BindingPersister(path)
    assert p.load() == []
    assert any("unsupported persistence version" in rec.message for rec in caplog.records)


def test_load_drops_unbound_entries(tmp_path):
    """Defensive: even if someone hand-edits the file to include an unbound
    entry, we should drop it on load — there is nothing to paint."""
    path = tmp_path / "sessions.json"
    path.write_text(json.dumps({
        "version": SCHEMA_VERSION,
        "saved_at": 0,
        "sessions": [
            _session_to_dict(_make_session("ok", bound=0x1, client=0x1)),
            _session_to_dict(_make_session("orphan", bound=None, client=None)),
        ],
    }))
    p = BindingPersister(path)
    loaded = p.load()
    assert [s.session_id for s in loaded] == ["ok"]


def test_load_skips_individual_bad_entries(tmp_path):
    """A single malformed entry must not poison sibling entries."""
    path = tmp_path / "sessions.json"
    path.write_text(json.dumps({
        "version": SCHEMA_VERSION,
        "saved_at": 0,
        "sessions": [
            {"session_id": "bad"},  # missing required fields
            _session_to_dict(_make_session("good", bound=0x1, client=0x1)),
        ],
    }))
    p = BindingPersister(path)
    loaded = p.load()
    assert [s.session_id for s in loaded] == ["good"]


def test_save_writes_file_with_0600_permissions(tmp_path):
    """sessions.json contains pids/cwds/session-ids and should not be
    world-readable on a multi-user box."""
    import stat
    p = BindingPersister(tmp_path / "sessions.json", throttle_s=0.0)
    p.save([_make_session()])
    _settle(p)
    mode = stat.S_IMODE((tmp_path / "sessions.json").stat().st_mode)
    assert mode == 0o600, f"expected 0o600, got {oct(mode)}"


def test_save_uses_atomic_rename(tmp_path):
    """Crash mid-write should leave the previous content intact, never a partial.
    We can't actually crash; we verify the tmp+rename pattern by checking
    that the output file is created via os.replace (i.e. final file size
    matches the JSON it contains, no stray .tmp left behind)."""
    p = BindingPersister(tmp_path / "sessions.json", throttle_s=0.0)
    p.save([_make_session()])
    _settle(p)

    files = sorted(f.name for f in tmp_path.iterdir())
    assert files == ["sessions.json"], f"unexpected leftover files: {files}"


def test_throttle_collapses_burst_into_at_most_two_writes(tmp_path):
    """A burst of save() calls within the throttle window should collapse —
    we don't care whether the first one fires immediately or batches with
    the rest, but we must never see N writes for N saves. The final write
    must contain the latest snapshot."""
    p = BindingPersister(tmp_path / "sessions.json", throttle_s=10.0)
    write_calls = {"count": 0}
    real_write = p._write_atomic

    def counting_write(snapshot):
        write_calls["count"] += 1
        real_write(snapshot)
    p._write_atomic = counting_write  # type: ignore[method-assign]

    for i in range(5):
        p.save([_make_session(f"s{i}")])
    # stop() forces any pending write synchronously.
    _settle(p)

    assert write_calls["count"] <= 2, (
        f"expected ≤2 writes for 5 saves, got {write_calls['count']}"
    )
    data = json.loads((tmp_path / "sessions.json").read_text())
    # Last snapshot wins.
    assert [s["session_id"] for s in data["sessions"]] == ["s4"]


def test_stop_flushes_pending(tmp_path):
    """stop() should write any pending snapshot synchronously."""
    p = BindingPersister(tmp_path / "sessions.json", throttle_s=10.0)
    p.save([_make_session()])
    # Without stop, the timer would wait 10 s. stop() must flush now.
    p.stop()
    assert (tmp_path / "sessions.json").exists()


def test_save_failure_is_logged_not_raised(tmp_path, caplog):
    """A disk error during write must not crash the daemon."""
    bad_path = tmp_path / "no" / "such" / "dir" / "sessions.json"
    p = BindingPersister(bad_path, throttle_s=0.0)
    # Sabotage the write by making the parent unwritable AFTER mkdir.
    p.save([_make_session()])

    # Replace _write_atomic to raise.
    def boom(_):
        raise OSError("disk full")
    p._write_atomic = boom  # type: ignore[method-assign]
    p.save([_make_session()])
    _settle(p)
    assert any("failed to write" in rec.message for rec in caplog.records)


def test_session_to_dict_round_trips(tmp_path):
    s = _make_session()
    d = _session_to_dict(s)
    s2 = _dict_to_session(d)
    assert s2 == s


def test_dict_to_session_rejects_bad_status():
    bad = {
        "session_id": "x",
        "cwd": "/p",
        "claude_pid": 1,
        "status": "frobnicated",
        "last_event_at": 0.0,
        "last_event": "",
        "background_active": False,
        "bound_window_id": 1,
        "client_window_id": 1,
    }
    assert _dict_to_session(bad) is None
