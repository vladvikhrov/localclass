import asyncio
import pytest

from localclass.core.events import Event
from localclass.net.protocol import (PROTOCOL_VERSION, ProtocolError, VersionMismatch, decode_packet, encode_packet, make_packet)
from localclass.net.transport import read_frame, write_frame


def test_roundtrip_and_unknown_fields_ignored():
    p = make_packet("PING", "me", {"x": 1})
    p["future_field"] = {"a": [1, 2]}
    d = decode_packet(encode_packet(p))
    assert d["message_type"] == "PING" and d["future_field"]["a"] == [1, 2]


def test_version_mismatch():
    p = make_packet("PING", "me")
    p["protocol_version"] = 99
    with pytest.raises(VersionMismatch):
        decode_packet(encode_packet(p))


def test_v2_accepted():
    p = make_packet("PING", "me")
    p["protocol_version"] = PROTOCOL_VERSION + 1
    assert decode_packet(encode_packet(p))["protocol_version"] == PROTOCOL_VERSION + 1


def test_nan_forbidden():
    with pytest.raises(ProtocolError):
        encode_packet(make_packet("PING", "me", {"v": float("nan")}))
    with pytest.raises(ProtocolError):
        encode_packet(make_packet("PING", "me", {"v": 2 ** 60}))


def test_event_unknown_fields_kept():
    e = Event.from_dict({"event_id": "1", "device_id": "d", "device_seq": 1, "lamport": 1, "type": "X", "payload": {},
                         "session_id": "s", "new_field": 5})
    assert e.extra == {"new_field": 5} and e.to_dict()["new_field"] == 5
    with pytest.raises(ValueError):
        Event.from_dict({"event_id": "1"})


async def test_framing_with_newlines():
    """Фрейминг не зависит от переводов строк и бинарных данных."""
    r = asyncio.StreamReader()
    class W:
        def __init__(self): self.buf = b""
        def write(self, d): self.buf += d
        async def drain(self): pass
    w = W()
    msgs = [b"line1\nline2\n", b"\x00\xff" * 100, b""]
    for m in msgs:
        await write_frame(w, m)
    r.feed_data(w.buf)
    r.feed_eof()
    for m in msgs:
        assert await read_frame(r, 10_000) == m
