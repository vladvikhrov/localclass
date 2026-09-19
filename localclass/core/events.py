"""Модель события (ТЗ 8). Событие — единственный объект, передаваемый между узлами (кроме содержимого файлов)."""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any

EVENT_VERSION = 1


class EventType:
    MESSAGE_CREATED = "MESSAGE_CREATED"
    MESSAGE_DELETED = "MESSAGE_DELETED"
    LINK_CREATED = "LINK_CREATED"
    LINK_DELETED = "LINK_DELETED"
    FILE_OFFERED = "FILE_OFFERED"
    FILE_COMPLETED = "FILE_COMPLETED"
    FILE_FAILED = "FILE_FAILED"
    FILE_DELETED = "FILE_DELETED"
    USER_JOINED = "USER_JOINED"
    USER_LEFT = "USER_LEFT"
    PEER_CONNECTED = "PEER_CONNECTED"        # локальные (не реплицируются), только журнал событий
    PEER_DISCONNECTED = "PEER_DISCONNECTED"
    CHANNEL_CREATED = "CHANNEL_CREATED"
    CHANNEL_CLOSED = "CHANNEL_CLOSED"
    SESSION_CREATED = "SESSION_CREATED"
    SESSION_CLOSED = "SESSION_CLOSED"
    PERMISSIONS_UPDATED = "PERMISSIONS_UPDATED"

    LOCAL_ONLY = {PEER_CONNECTED, PEER_DISCONNECTED}
    KNOWN = {
        MESSAGE_CREATED, MESSAGE_DELETED, LINK_CREATED, LINK_DELETED, FILE_OFFERED, FILE_COMPLETED,
        FILE_FAILED, FILE_DELETED, USER_JOINED, USER_LEFT, CHANNEL_CREATED, CHANNEL_CLOSED,
        SESSION_CREATED, SESSION_CLOSED, PERMISSIONS_UPDATED,
    }


def new_id() -> str:
    return uuid.uuid4().hex


@dataclass
class Event:
    event_id: str
    device_id: str
    device_seq: int
    lamport: int
    timestamp: float          # только для показа человеку; в логике не участвует
    type: str
    payload: dict[str, Any]
    session_id: str
    version: int = EVENT_VERSION
    extra: dict[str, Any] = field(default_factory=dict)  # неизвестные поля — сохраняем, не падаем

    REQUIRED = ("event_id", "device_id", "device_seq", "lamport", "type", "payload", "session_id")

    def to_dict(self) -> dict[str, Any]:
        d = {
            "version": self.version,
            "event_id": self.event_id,
            "device_id": self.device_id,
            "device_seq": self.device_seq,
            "lamport": self.lamport,
            "timestamp": self.timestamp,
            "type": self.type,
            "payload": self.payload,
            "session_id": self.session_id,
        }
        d.update(self.extra)
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Event":
        for k in cls.REQUIRED:
            if k not in d:
                raise ValueError(f"event: отсутствует поле {k}")
        if not isinstance(d["payload"], dict):
            raise ValueError("event: payload должен быть объектом")
        for k in ("device_seq", "lamport"):
            if not isinstance(d[k], int) or isinstance(d[k], bool) or d[k] < 0 or d[k] > 2 ** 53:
                raise ValueError(f"event: некорректное значение {k}")
        known = {"version", "event_id", "device_id", "device_seq", "lamport", "timestamp", "type",
                 "payload", "session_id"}
        return cls(
            event_id=str(d["event_id"]),
            device_id=str(d["device_id"]),
            device_seq=int(d["device_seq"]),
            lamport=int(d["lamport"]),
            timestamp=float(d.get("timestamp") or time.time()),
            type=str(d["type"]),
            payload=d["payload"],
            session_id=str(d["session_id"]),
            version=int(d.get("version", EVENT_VERSION)),
            extra={k: v for k, v in d.items() if k not in known},
        )

    @property
    def sort_key(self) -> tuple[int, str, str]:
        """Порядок отображения: (lamport, device_id, event_id) — ТЗ 8.2."""
        return (self.lamport, self.device_id, self.event_id)
