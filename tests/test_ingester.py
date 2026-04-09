import json
import threading
import time
from pathlib import Path

from claude_alerts.events import ClaudeEvent
from claude_alerts.ingester import EventIngester


def write_event_atomically(events_dir: Path, payload: dict, name: str = "evt") -> None:
    tmp = events_dir / f"{name}.json.tmp"
    final = events_dir / f"{name}.json"
    tmp.write_text(json.dumps(payload))
    tmp.rename(final)


def good_payload(session_id="s1"):
    return {
        "event": "Stop",
        "session_id": session_id,
        "cwd": "/p",
        "claude_pid": 99,
        "timestamp": 1.0,
    }


def run_ingester_in_thread(ingester):
    t = threading.Thread(target=ingester.run, daemon=True)
    t.start()
    return t


def wait_for(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def test_dispatches_event_when_file_arrives(tmp_path):
    events_dir = tmp_path / "events"
    events_dir.mkdir()
    received = []
    ingester = EventIngester(events_dir, on_event=received.append)
    run_ingester_in_thread(ingester)
    try:
        write_event_atomically(events_dir, good_payload())
        assert wait_for(lambda: len(received) == 1)
        assert isinstance(received[0], ClaudeEvent)
        assert received[0].session_id == "s1"
    finally:
        ingester.stop()


def test_drains_backlog_on_start(tmp_path):
    events_dir = tmp_path / "events"
    events_dir.mkdir()
    write_event_atomically(events_dir, good_payload("backlog1"), name="b1")
    write_event_atomically(events_dir, good_payload("backlog2"), name="b2")
    received = []
    ingester = EventIngester(events_dir, on_event=received.append)
    run_ingester_in_thread(ingester)
    try:
        assert wait_for(lambda: len(received) == 2)
        assert {e.session_id for e in received} == {"backlog1", "backlog2"}
    finally:
        ingester.stop()


def test_deletes_event_file_after_processing(tmp_path):
    events_dir = tmp_path / "events"
    events_dir.mkdir()
    received = []
    ingester = EventIngester(events_dir, on_event=received.append)
    run_ingester_in_thread(ingester)
    try:
        write_event_atomically(events_dir, good_payload())
        assert wait_for(lambda: len(received) == 1)
        # The .json file should be gone
        assert list(events_dir.glob("*.json")) == []
    finally:
        ingester.stop()


def test_moves_malformed_file_to_rejected(tmp_path):
    events_dir = tmp_path / "events"
    events_dir.mkdir()
    received = []
    ingester = EventIngester(events_dir, on_event=received.append)
    run_ingester_in_thread(ingester)
    try:
        bad = events_dir / "bad.json.tmp"
        bad.write_text("{not json")
        bad.rename(events_dir / "bad.json")
        assert wait_for(lambda: (events_dir / "rejected" / "bad.json").exists())
        assert received == []
    finally:
        ingester.stop()
