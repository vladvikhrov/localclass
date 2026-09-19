from pathlib import Path

from localclass.core.events import Event, EventType
from localclass.core.store import Store


def ev(dev, seq, lamport, typ, payload, sid="S", eid=None):
    return Event(event_id=eid or f"{dev}-{seq}", device_id=dev, device_seq=seq, lamport=lamport, timestamp=0,
                 type=typ, payload=payload, session_id=sid)


def test_counters_persist_across_restart(tmp_path: Path):
    s = Store(tmp_path / "db", "me")
    for i in range(3):
        s.create_event("S", EventType.MESSAGE_CREATED, {"message_id": f"m{i}", "channel": "general", "text": str(i)})
    assert s.device_seq("S") == 3 and s.lamport() == 3
    s.close()
    s2 = Store(tmp_path / "db", "me")
    e = s2.create_event("S", EventType.MESSAGE_CREATED, {"message_id": "m9", "channel": "general", "text": "x"})
    assert e.device_seq == 4 and e.lamport == 4
    assert s2.vector("S") == {"me": 4}


def test_deleted_before_created_tombstone(tmp_path: Path):
    s = Store(tmp_path / "db", "me")
    assert s.insert_remote_event(ev("A", 2, 10, EventType.MESSAGE_DELETED, {"message_id": "m42"}))
    assert s.insert_remote_event(ev("A", 1, 9, EventType.MESSAGE_CREATED, {"message_id": "m42", "channel": "general", "text": "zombie"}))
    assert s.messages("S", "general") == []
    assert s.vector("S") == {"A": 2}


def test_duplicates_ignored_and_lamport_advances(tmp_path: Path):
    s = Store(tmp_path / "db", "me")
    e = ev("A", 1, 100, EventType.MESSAGE_CREATED, {"message_id": "m1", "channel": "general", "text": "hi"})
    assert s.insert_remote_event(e) is True
    assert s.insert_remote_event(e) is False
    assert s.lamport() == 101
    assert len(s.messages("S", "general")) == 1


def test_gap_and_pagination(tmp_path: Path):
    s = Store(tmp_path / "db", "me")
    s.insert_remote_event(ev("A", 1, 1, "X", {}))
    s.insert_remote_event(ev("A", 3, 3, "X", {}))
    assert s.vector("S") == {"A": 1}
    assert s.has_gaps("S") == {"A": (1, 3)}
    s.insert_remote_event(ev("A", 2, 2, "X", {}))
    assert s.vector("S") == {"A": 3}
    events, more = s.events_missing_for("S", {"A": 1}, limit=1)
    assert [e.device_seq for e in events] == [2] and more
    events, more = s.events_missing_for("S", {"A": 1}, limit=10)
    assert [e.device_seq for e in events] == [2, 3] and not more


def test_order_by_lamport_not_timestamp(tmp_path: Path):
    s = Store(tmp_path / "db", "me")
    a = ev("A", 1, 5, EventType.MESSAGE_CREATED, {"message_id": "a", "channel": "general", "text": "second"})
    a.timestamp = 1
    b = ev("B", 1, 2, EventType.MESSAGE_CREATED, {"message_id": "b", "channel": "general", "text": "first"})
    b.timestamp = 999999
    s.insert_remote_event(a)
    s.insert_remote_event(b)
    assert [m["text"] for m in s.messages("S", "general")] == ["first", "second"]


def test_retention(tmp_path: Path):
    s = Store(tmp_path / "db", "me")
    s.create_event("S", EventType.SESSION_CREATED, {"name": "x", "code": "A", "duration": 1, "created_at": 0})
    s.db.execute("UPDATE sessions SET closed_at=0 WHERE session_id='S'")
    assert s.apply_retention(30) > 0
    assert s.event_count("S") == 0 and s.get_session("S") is None
