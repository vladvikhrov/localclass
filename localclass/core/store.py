"""SQLite-хранилище (ТЗ 11). Счётчики персистентны и меняются в одной транзакции с записью события (ТЗ 8.3).

Отклонение от таблицы 11.2: device_seq ведётся на пару (device_id, session_id), а не глобально на устройство.
Причина: vector clock скоупится сессией (11.3), и для непрерывности нумерации внутри сессии счётчик должен
быть посессионным — иначе в векторе сессии появятся «дыры» от событий других сессий и sync зависнет.
Lamport — один на устройство.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable

from .events import Event, EventType, new_id
from .session import SEEN_TTL

log = logging.getLogger("localclass.core.store")

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS counters (
    device_id  TEXT NOT NULL,
    session_id TEXT NOT NULL,
    device_seq INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (device_id, session_id)
);
CREATE TABLE IF NOT EXISTS lamport (
    device_id TEXT PRIMARY KEY,
    lamport   INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS events (
    event_id    TEXT PRIMARY KEY,
    device_id   TEXT NOT NULL,
    device_seq  INTEGER NOT NULL,
    lamport     INTEGER NOT NULL,
    timestamp   REAL NOT NULL,
    type        TEXT NOT NULL,
    payload     TEXT NOT NULL,
    session_id  TEXT NOT NULL,
    received_at REAL NOT NULL,
    UNIQUE (device_id, device_seq, session_id)
);
CREATE INDEX IF NOT EXISTS ix_events_order ON events (session_id, lamport, device_id, event_id);
CREATE INDEX IF NOT EXISTS ix_events_device ON events (session_id, device_id, device_seq);
CREATE TABLE IF NOT EXISTS vectors (          -- vector clock: непрерывный префикс device_seq на устройство
    session_id TEXT NOT NULL,
    device_id  TEXT NOT NULL,
    seq        INTEGER NOT NULL,
    PRIMARY KEY (session_id, device_id)
);
CREATE TABLE IF NOT EXISTS tombstones (
    object_id   TEXT NOT NULL,
    object_type TEXT NOT NULL,
    session_id  TEXT NOT NULL,
    deleted_by  TEXT NOT NULL,
    lamport     INTEGER NOT NULL,
    PRIMARY KEY (object_id, object_type, session_id)
);
CREATE TABLE IF NOT EXISTS messages (
    message_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    channel    TEXT NOT NULL,
    device_id  TEXT NOT NULL,
    text       TEXT NOT NULL,
    kind       TEXT NOT NULL DEFAULT 'message',   -- message | announcement | dm
    to_device  TEXT,
    lamport    INTEGER NOT NULL,
    timestamp  REAL NOT NULL,
    event_id   TEXT NOT NULL,
    deleted    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_messages_channel ON messages (session_id, channel, lamport, device_id, message_id);
CREATE TABLE IF NOT EXISTS links (
    link_id    TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    channel    TEXT NOT NULL,
    device_id  TEXT NOT NULL,
    url        TEXT NOT NULL,
    title      TEXT NOT NULL DEFAULT '',
    lamport    INTEGER NOT NULL,
    timestamp  REAL NOT NULL,
    deleted    INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS channels (
    session_id TEXT NOT NULL,
    name       TEXT NOT NULL,
    title      TEXT NOT NULL DEFAULT '',
    created_by TEXT NOT NULL,
    lamport    INTEGER NOT NULL,
    closed     INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (session_id, name)
);
CREATE TABLE IF NOT EXISTS members (
    session_id   TEXT NOT NULL,
    device_id    TEXT NOT NULL,
    display_name TEXT NOT NULL DEFAULT '',
    role         TEXT NOT NULL DEFAULT 'student',
    blocked      INTEGER NOT NULL DEFAULT 0,
    left         INTEGER NOT NULL DEFAULT 0,
    lamport      INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (session_id, device_id)
);
CREATE TABLE IF NOT EXISTS shared_files (
    file_id     TEXT PRIMARY KEY,
    session_id  TEXT NOT NULL,
    owner_id    TEXT NOT NULL,
    channel     TEXT NOT NULL DEFAULT 'general',
    filename    TEXT NOT NULL,
    size        INTEGER NOT NULL,
    chunk_size  INTEGER NOT NULL,
    chunk_count INTEGER NOT NULL,
    full_sha256 TEXT NOT NULL,
    lamport     INTEGER NOT NULL,
    timestamp   REAL NOT NULL,
    local_path  TEXT,
    deleted     INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS file_chunks (     -- манифест файлов, которыми владеем (раздаём)
    file_id     TEXT NOT NULL,
    chunk_index INTEGER NOT NULL,
    hash        TEXT NOT NULL,
    PRIMARY KEY (file_id, chunk_index)
);
CREATE TABLE IF NOT EXISTS peers (
    device_id      TEXT PRIMARY KEY,
    public_key     TEXT NOT NULL,
    fingerprint    TEXT NOT NULL,
    last_addresses TEXT NOT NULL DEFAULT '[]',
    port           INTEGER NOT NULL DEFAULT 0,
    last_seen      REAL NOT NULL DEFAULT 0,
    trust_state    TEXT NOT NULL DEFAULT 'trusted',   -- trusted | conflict
    pending_key    TEXT,
    display_name   TEXT NOT NULL DEFAULT '',
    session_id     TEXT
);
CREATE TABLE IF NOT EXISTS sessions (
    session_id         TEXT PRIMARY KEY,
    name               TEXT NOT NULL,
    code               TEXT NOT NULL,
    created_at         REAL NOT NULL,
    duration           INTEGER NOT NULL,
    teacher_public_key TEXT NOT NULL DEFAULT '',
    teacher_pin_hash   TEXT NOT NULL DEFAULT '',
    teacher_id         TEXT NOT NULL DEFAULT '',
    teacher_name       TEXT NOT NULL DEFAULT '',
    closed_at          REAL,
    joined_at          REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS transfers (
    transfer_id     TEXT PRIMARY KEY,
    file_id         TEXT NOT NULL,
    peer_id         TEXT NOT NULL,
    direction       TEXT NOT NULL,            -- in | out
    status          TEXT NOT NULL,            -- queued | active | paused | done | failed
    filename        TEXT NOT NULL,
    size            INTEGER NOT NULL,
    chunk_size      INTEGER NOT NULL,
    chunk_count     INTEGER NOT NULL,
    received_chunks TEXT NOT NULL DEFAULT '',  -- hex-битмап полученных чанков
    full_sha256     TEXT NOT NULL,
    temp_path       TEXT,
    final_path      TEXT,
    session_id      TEXT NOT NULL,
    error           TEXT,
    updated_at      REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS chunk_hashes (
    transfer_id TEXT NOT NULL,
    chunk_index INTEGER NOT NULL,
    hash        TEXT NOT NULL,
    PRIMARY KEY (transfer_id, chunk_index)
);
CREATE TABLE IF NOT EXISTS seen_sessions (      -- сессии, замеченные в discovery (карточки на экране входа)
    session_id   TEXT PRIMARY KEY,
    name         TEXT NOT NULL DEFAULT '',
    code         TEXT NOT NULL DEFAULT '',
    teacher_name TEXT NOT NULL DEFAULT '',
    members      INTEGER NOT NULL DEFAULT 0,
    closed       INTEGER NOT NULL DEFAULT 0,   -- получили SESSION_CLOSED / узнали о закрытии
    last_seen    REAL NOT NULL
);
"""


class Bitmap:
    """Битовая карта полученных чанков, хранится hex-строкой."""

    def __init__(self, count: int, hex_data: str = ""):
        self.count = count
        self.bits = bytearray((count + 7) // 8)
        if hex_data:
            raw = bytes.fromhex(hex_data)
            self.bits[: len(raw)] = raw[: len(self.bits)]

    def has(self, i: int) -> bool:
        return bool(self.bits[i >> 3] & (1 << (i & 7)))

    def set(self, i: int) -> None:
        self.bits[i >> 3] |= 1 << (i & 7)

    def clear(self, i: int) -> None:
        self.bits[i >> 3] &= ~(1 << (i & 7)) & 0xFF

    def missing(self) -> list[int]:
        return [i for i in range(self.count) if not self.has(i)]

    def received(self) -> int:
        return sum(1 for i in range(self.count) if self.has(i))

    def hex(self) -> str:
        return self.bits.hex()


class Store:
    def __init__(self, db_file: Path, device_id: str):
        self.device_id = device_id
        self.path = db_file
        db_file.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.db = sqlite3.connect(str(db_file), check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self._migrate()
        self.db.execute("INSERT OR IGNORE INTO lamport (device_id, lamport) VALUES (?, 0)", (device_id,))

    def _migrate(self) -> None:
        """Добавляет колонки, появившиеся после первой версии схемы (базы обновляются на месте)."""
        added = [
            ("sessions", "teacher_pin_hash", "TEXT NOT NULL DEFAULT ''"),
            ("sessions", "teacher_name", "TEXT NOT NULL DEFAULT ''"),
            ("seen_sessions", "teacher_name", "TEXT NOT NULL DEFAULT ''"),
            ("seen_sessions", "members", "INTEGER NOT NULL DEFAULT 0"),
            ("seen_sessions", "closed", "INTEGER NOT NULL DEFAULT 0"),
        ]
        for table, column, decl in added:
            cols = {r[1] for r in self.db.execute(f"PRAGMA table_info({table})")}
            if column not in cols:
                self.db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
                log.info("миграция базы: %s.%s добавлена", table, column)

    def close(self) -> None:
        with self._lock:
            self.db.close()

    # ------------------------------------------------------------------ helpers
    def _row_event(self, r: sqlite3.Row) -> Event:
        return Event(
            event_id=r["event_id"], device_id=r["device_id"], device_seq=r["device_seq"],
            lamport=r["lamport"], timestamp=r["timestamp"], type=r["type"],
            payload=json.loads(r["payload"]), session_id=r["session_id"],
        )

    def lamport(self) -> int:
        with self._lock:
            return self.db.execute("SELECT lamport FROM lamport WHERE device_id=?", (self.device_id,)).fetchone()[0]

    def device_seq(self, session_id: str) -> int:
        with self._lock:
            r = self.db.execute("SELECT device_seq FROM counters WHERE device_id=? AND session_id=?",
                                (self.device_id, session_id)).fetchone()
            return r[0] if r else 0

    # ------------------------------------------------------------------ events
    def create_event(self, session_id: str, etype: str, payload: dict[str, Any]) -> Event:
        """Локальное событие: lamport += 1, device_seq += 1, INSERT — в одной транзакции (ТЗ 8.3)."""
        with self._lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                self.db.execute("UPDATE lamport SET lamport = lamport + 1 WHERE device_id=?", (self.device_id,))
                lamport = self.db.execute("SELECT lamport FROM lamport WHERE device_id=?",
                                          (self.device_id,)).fetchone()[0]
                self.db.execute(
                    "INSERT INTO counters (device_id, session_id, device_seq) VALUES (?, ?, 1) "
                    "ON CONFLICT(device_id, session_id) DO UPDATE SET device_seq = device_seq + 1",
                    (self.device_id, session_id))
                seq = self.db.execute("SELECT device_seq FROM counters WHERE device_id=? AND session_id=?",
                                      (self.device_id, session_id)).fetchone()[0]
                ev = Event(event_id=new_id(), device_id=self.device_id, device_seq=seq, lamport=lamport,
                           timestamp=time.time(), type=etype, payload=payload, session_id=session_id)
                self._insert(ev)
                self._apply(ev)
                self._advance_vector(ev)
                self.db.execute("COMMIT")
            except Exception:
                self.db.execute("ROLLBACK")
                raise
        return ev

    def insert_remote_event(self, ev: Event) -> bool:
        """Чужое событие. Возвращает True, если событие новое. lamport = max(lamport, remote) + 1."""
        with self._lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                if self.db.execute("SELECT 1 FROM events WHERE event_id=?", (ev.event_id,)).fetchone():
                    self.db.execute("COMMIT")
                    return False
                dup = self.db.execute(
                    "SELECT 1 FROM events WHERE device_id=? AND device_seq=? AND session_id=?",
                    (ev.device_id, ev.device_seq, ev.session_id)).fetchone()
                if dup:
                    # тот же (device, seq) с другим event_id — испорченный/подделанный узел; не воскрешаем
                    self.db.execute("COMMIT")
                    log.warning("event %s: конфликт (device_seq уже занят) от %s", ev.event_id, ev.device_id[:8])
                    return False
                self.db.execute(
                    "UPDATE lamport SET lamport = MAX(lamport, ?) + 1 WHERE device_id=?",
                    (ev.lamport, self.device_id))
                self._insert(ev)
                self._apply(ev)
                self._advance_vector(ev)
                self.db.execute("COMMIT")
                return True
            except Exception:
                self.db.execute("ROLLBACK")
                raise

    def _insert(self, ev: Event) -> None:
        self.db.execute(
            "INSERT INTO events (event_id, device_id, device_seq, lamport, timestamp, type, payload, session_id, received_at)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (ev.event_id, ev.device_id, ev.device_seq, ev.lamport, ev.timestamp, ev.type,
             json.dumps(ev.payload, ensure_ascii=False, allow_nan=False), ev.session_id, time.time()))

    def _advance_vector(self, ev: Event) -> None:
        r = self.db.execute("SELECT seq FROM vectors WHERE session_id=? AND device_id=?",
                            (ev.session_id, ev.device_id)).fetchone()
        cur = r[0] if r else 0
        if ev.device_seq != cur + 1:
            return
        cur += 1
        while self.db.execute("SELECT 1 FROM events WHERE session_id=? AND device_id=? AND device_seq=?",
                              (ev.session_id, ev.device_id, cur + 1)).fetchone():
            cur += 1
        self.db.execute(
            "INSERT INTO vectors (session_id, device_id, seq) VALUES (?,?,?) "
            "ON CONFLICT(session_id, device_id) DO UPDATE SET seq=excluded.seq",
            (ev.session_id, ev.device_id, cur))

    def vector(self, session_id: str) -> dict[str, int]:
        with self._lock:
            return {r["device_id"]: r["seq"] for r in
                    self.db.execute("SELECT device_id, seq FROM vectors WHERE session_id=?", (session_id,))}

    def has_gaps(self, session_id: str) -> dict[str, tuple[int, int]]:
        """device_id -> (непрерывный префикс, максимальный известный seq) там, где есть дыры."""
        with self._lock:
            vec = self.vector(session_id)
            out = {}
            for r in self.db.execute("SELECT device_id, MAX(device_seq) m FROM events WHERE session_id=? GROUP BY device_id",
                                     (session_id,)):
                if r["m"] > vec.get(r["device_id"], 0):
                    out[r["device_id"]] = (vec.get(r["device_id"], 0), r["m"])
            return out

    def events_missing_for(self, session_id: str, their_vector: dict[str, int], limit: int) -> tuple[list[Event], bool]:
        """События, которых нет у пира (device_seq > их вектора). Пагинация: (events, more)."""
        with self._lock:
            devices = [r[0] for r in self.db.execute(
                "SELECT DISTINCT device_id FROM events WHERE session_id=? ORDER BY device_id", (session_id,))]
            out: list[Event] = []
            for dev in devices:
                if len(out) >= limit:
                    return out, True
                after = int(their_vector.get(dev, 0))
                rows = self.db.execute(
                    "SELECT * FROM events WHERE session_id=? AND device_id=? AND device_seq>? "
                    "ORDER BY device_seq LIMIT ?", (session_id, dev, after, limit - len(out) + 1)).fetchall()
                if len(rows) > limit - len(out):
                    out.extend(self._row_event(r) for r in rows[: limit - len(out)])
                    return out, True
                out.extend(self._row_event(r) for r in rows)
            return out, False

    def get_event(self, event_id: str) -> Event | None:
        with self._lock:
            r = self.db.execute("SELECT * FROM events WHERE event_id=?", (event_id,)).fetchone()
            return self._row_event(r) if r else None

    def events(self, session_id: str, limit: int = 1000, types: Iterable[str] | None = None) -> list[Event]:
        with self._lock:
            q = "SELECT * FROM events WHERE session_id=?"
            args: list[Any] = [session_id]
            if types:
                tl = list(types)
                q += f" AND type IN ({','.join('?' * len(tl))})"
                args += tl
            q += " ORDER BY lamport DESC, device_id DESC, event_id DESC LIMIT ?"
            args.append(limit)
            rows = self.db.execute(q, args).fetchall()
            return [self._row_event(r) for r in reversed(rows)]

    def event_count(self, session_id: str) -> int:
        with self._lock:
            return self.db.execute("SELECT COUNT(*) FROM events WHERE session_id=?", (session_id,)).fetchone()[0]

    # ------------------------------------------------------------------ projections
    def _tombstoned(self, session_id: str, object_type: str, object_id: str) -> bool:
        return bool(self.db.execute(
            "SELECT 1 FROM tombstones WHERE session_id=? AND object_type=? AND object_id=?",
            (session_id, object_type, object_id)).fetchone())

    def _tombstone(self, ev: Event, object_type: str, object_id: str) -> None:
        self.db.execute(
            "INSERT OR IGNORE INTO tombstones (object_id, object_type, session_id, deleted_by, lamport) VALUES (?,?,?,?,?)",
            (object_id, object_type, ev.session_id, ev.device_id, ev.lamport))

    def _apply(self, ev: Event) -> None:
        """Применение события независимо от порядка поступления (ТЗ 10.2). Неизвестный тип игнорируется."""
        p = ev.payload
        t = ev.type
        sid = ev.session_id
        try:
            if t == EventType.MESSAGE_CREATED:
                mid = str(p.get("message_id") or ev.event_id)
                deleted = 1 if self._tombstoned(sid, "message", mid) else 0
                self.db.execute(
                    "INSERT OR IGNORE INTO messages (message_id, session_id, channel, device_id, text, kind, to_device,"
                    " lamport, timestamp, event_id, deleted) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (mid, sid, str(p.get("channel", "general")), ev.device_id, str(p.get("text", "")),
                     str(p.get("kind", "message")), p.get("to"), ev.lamport, ev.timestamp, ev.event_id, deleted))
            elif t == EventType.MESSAGE_DELETED:
                mid = str(p.get("message_id", ""))
                self._tombstone(ev, "message", mid)
                self.db.execute("UPDATE messages SET deleted=1 WHERE message_id=?", (mid,))
            elif t == EventType.LINK_CREATED:
                lid = str(p.get("link_id") or ev.event_id)
                deleted = 1 if self._tombstoned(sid, "link", lid) else 0
                self.db.execute(
                    "INSERT OR IGNORE INTO links (link_id, session_id, channel, device_id, url, title, lamport, timestamp, deleted)"
                    " VALUES (?,?,?,?,?,?,?,?,?)",
                    (lid, sid, str(p.get("channel", "general")), ev.device_id, str(p.get("url", "")),
                     str(p.get("title", "")), ev.lamport, ev.timestamp, deleted))
            elif t == EventType.LINK_DELETED:
                lid = str(p.get("link_id", ""))
                self._tombstone(ev, "link", lid)
                self.db.execute("UPDATE links SET deleted=1 WHERE link_id=?", (lid,))
            elif t == EventType.FILE_OFFERED:
                fid = str(p.get("file_id") or ev.event_id)
                deleted = 1 if self._tombstoned(sid, "file", fid) else 0
                self.db.execute(
                    "INSERT OR IGNORE INTO shared_files (file_id, session_id, owner_id, channel, filename, size, chunk_size,"
                    " chunk_count, full_sha256, lamport, timestamp, deleted) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (fid, sid, ev.device_id, str(p.get("channel", "general")), str(p.get("filename", "file")),
                     int(p.get("size", 0)), int(p.get("chunk_size", 0)), int(p.get("chunk_count", 0)),
                     str(p.get("full_sha256", "")), ev.lamport, ev.timestamp, deleted))
            elif t == EventType.FILE_DELETED:
                fid = str(p.get("file_id", ""))
                self._tombstone(ev, "file", fid)
                self.db.execute("UPDATE shared_files SET deleted=1 WHERE file_id=?", (fid,))
            elif t == EventType.USER_JOINED:
                self.db.execute(
                    "INSERT INTO members (session_id, device_id, display_name, role, lamport) VALUES (?,?,?,?,?) "
                    "ON CONFLICT(session_id, device_id) DO UPDATE SET display_name=excluded.display_name,"
                    " role=CASE WHEN excluded.lamport>=members.lamport THEN excluded.role ELSE members.role END,"
                    " left=0, lamport=MAX(members.lamport, excluded.lamport)",
                    (sid, ev.device_id, str(p.get("display_name", ""))[:64], str(p.get("role", "student")), ev.lamport))
            elif t == EventType.USER_LEFT:
                self.db.execute(
                    "INSERT INTO members (session_id, device_id, left, lamport) VALUES (?,?,1,?) "
                    "ON CONFLICT(session_id, device_id) DO UPDATE SET left=CASE WHEN excluded.lamport>=members.lamport THEN 1 ELSE members.left END,"
                    " lamport=MAX(members.lamport, excluded.lamport)",
                    (sid, ev.device_id, ev.lamport))
            elif t == EventType.CHANNEL_CREATED:
                name = str(p.get("channel", ""))
                if name:
                    closed = 1 if self._tombstoned(sid, "channel", name) else 0
                    self.db.execute(
                        "INSERT OR IGNORE INTO channels (session_id, name, title, created_by, lamport, closed) VALUES (?,?,?,?,?,?)",
                        (sid, name, str(p.get("title", "")), ev.device_id, ev.lamport, closed))
            elif t == EventType.CHANNEL_CLOSED:
                name = str(p.get("channel", ""))
                self._tombstone(ev, "channel", name)
                self.db.execute("UPDATE channels SET closed=1 WHERE session_id=? AND name=?", (sid, name))
            elif t == EventType.SESSION_CREATED:
                self.db.execute(
                    "INSERT INTO sessions (session_id, name, code, created_at, duration, teacher_public_key,"
                    " teacher_pin_hash, teacher_id, teacher_name, joined_at) VALUES (?,?,?,?,?,?,?,?,?,?)"
                    " ON CONFLICT(session_id) DO UPDATE SET name=excluded.name, code=excluded.code,"
                    " created_at=excluded.created_at, duration=excluded.duration, teacher_public_key=excluded.teacher_public_key,"
                    " teacher_pin_hash=excluded.teacher_pin_hash, teacher_id=excluded.teacher_id,"
                    " teacher_name=excluded.teacher_name",
                    (sid, str(p.get("name", "")), str(p.get("code", "")), float(p.get("created_at", ev.timestamp)),
                     int(p.get("duration", 0)), str(p.get("teacher_public_key", "")), str(p.get("teacher_pin_hash", "")),
                     ev.device_id, str(p.get("teacher_name", ""))[:64], time.time()))
                self.db.execute(
                    "INSERT OR IGNORE INTO channels (session_id, name, title, created_by, lamport) VALUES (?,?,?,?,?)",
                    (sid, "general", "Общий", ev.device_id, ev.lamport))
                if self._tombstoned(sid, "session", sid):
                    self.db.execute("UPDATE sessions SET closed_at=COALESCE(closed_at, ?) WHERE session_id=?",
                                    (ev.timestamp, sid))
                    self.db.execute("UPDATE seen_sessions SET closed=1 WHERE session_id=?", (sid,))
            elif t == EventType.SESSION_CLOSED:
                # tombstone, чтобы закрытая сессия не «ожила» из пришедшего позже SESSION_CREATED
                self._tombstone(ev, "session", sid)
                self.db.execute("UPDATE sessions SET closed_at=? WHERE session_id=?", (ev.timestamp, sid))
                self.db.execute("UPDATE seen_sessions SET closed=1 WHERE session_id=?", (sid,))
            elif t == EventType.PERMISSIONS_UPDATED:
                target = str(p.get("device_id", ""))
                if target:
                    sets, args = [], []
                    if "blocked" in p:
                        sets.append("blocked=?"); args.append(1 if p["blocked"] else 0)
                    if "role" in p:
                        sets.append("role=?"); args.append(str(p["role"]))
                    if sets:
                        self.db.execute(
                            "INSERT OR IGNORE INTO members (session_id, device_id, lamport) VALUES (?,?,0)", (sid, target))
                        self.db.execute(f"UPDATE members SET {', '.join(sets)} WHERE session_id=? AND device_id=?",
                                        (*args, sid, target))
            # прочие (FILE_COMPLETED, FILE_FAILED, неизвестные) — только журнал
        except (KeyError, TypeError, ValueError) as e:
            log.warning("event %s (%s): некорректный payload проигнорирован: %s", ev.event_id[:8], t, e)

    # ------------------------------------------------------------------ queries for UI
    def messages(self, session_id: str, channel: str, limit: int = 500) -> list[dict]:
        with self._lock:
            rows = self.db.execute(
                "SELECT m.*, COALESCE(mb.display_name,'') AS display_name FROM messages m"
                " LEFT JOIN members mb ON mb.session_id=m.session_id AND mb.device_id=m.device_id"
                " WHERE m.session_id=? AND m.channel=? AND m.deleted=0"
                " ORDER BY m.lamport DESC, m.device_id DESC, m.message_id DESC LIMIT ?",
                (session_id, channel, limit)).fetchall()
            return [dict(r) for r in reversed(rows)]

    def search_messages(self, session_id: str, text: str, limit: int = 200) -> list[dict]:
        with self._lock:
            rows = self.db.execute(
                "SELECT * FROM messages WHERE session_id=? AND deleted=0 AND text LIKE ? ORDER BY lamport DESC LIMIT ?",
                (session_id, f"%{text}%", limit)).fetchall()
            return [dict(r) for r in rows]

    def links(self, session_id: str, limit: int = 500) -> list[dict]:
        with self._lock:
            rows = self.db.execute(
                "SELECT l.*, COALESCE(mb.display_name,'') AS display_name FROM links l"
                " LEFT JOIN members mb ON mb.session_id=l.session_id AND mb.device_id=l.device_id"
                " WHERE l.session_id=? AND l.deleted=0 ORDER BY l.lamport DESC, l.device_id DESC LIMIT ?",
                (session_id, limit)).fetchall()
            return [dict(r) for r in rows]

    def channels(self, session_id: str, include_closed: bool = False) -> list[dict]:
        with self._lock:
            q = "SELECT * FROM channels WHERE session_id=?" + ("" if include_closed else " AND closed=0")
            return [dict(r) for r in self.db.execute(q + " ORDER BY lamport", (session_id,))]

    def members(self, session_id: str) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self.db.execute(
                "SELECT * FROM members WHERE session_id=? ORDER BY display_name", (session_id,))]

    def member(self, session_id: str, device_id: str) -> dict | None:
        with self._lock:
            r = self.db.execute("SELECT * FROM members WHERE session_id=? AND device_id=?",
                                (session_id, device_id)).fetchone()
            return dict(r) if r else None

    def shared_files(self, session_id: str) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self.db.execute(
                "SELECT f.*, COALESCE(mb.display_name,'') AS owner_name FROM shared_files f"
                " LEFT JOIN members mb ON mb.session_id=f.session_id AND mb.device_id=f.owner_id"
                " WHERE f.session_id=? AND f.deleted=0 ORDER BY f.lamport DESC", (session_id,))]

    def shared_file(self, file_id: str) -> dict | None:
        with self._lock:
            r = self.db.execute("SELECT * FROM shared_files WHERE file_id=?", (file_id,)).fetchone()
            return dict(r) if r else None

    def set_file_local_path(self, file_id: str, path: str | None) -> None:
        with self._lock:
            self.db.execute("UPDATE shared_files SET local_path=? WHERE file_id=?", (path, file_id))

    def save_file_chunks(self, file_id: str, hashes: list[str]) -> None:
        with self._lock:
            self.db.execute("BEGIN")
            self.db.execute("DELETE FROM file_chunks WHERE file_id=?", (file_id,))
            self.db.executemany("INSERT INTO file_chunks (file_id, chunk_index, hash) VALUES (?,?,?)",
                                [(file_id, i, h) for i, h in enumerate(hashes)])
            self.db.execute("COMMIT")

    def file_chunks(self, file_id: str) -> list[str]:
        with self._lock:
            return [r[0] for r in self.db.execute(
                "SELECT hash FROM file_chunks WHERE file_id=? ORDER BY chunk_index", (file_id,))]

    # ------------------------------------------------------------------ peers (TOFU)
    def get_peer(self, device_id: str) -> dict | None:
        with self._lock:
            r = self.db.execute("SELECT * FROM peers WHERE device_id=?", (device_id,)).fetchone()
            if not r:
                return None
            d = dict(r)
            d["last_addresses"] = json.loads(d["last_addresses"] or "[]")
            return d

    def peers(self) -> list[dict]:
        with self._lock:
            out = []
            for r in self.db.execute("SELECT * FROM peers ORDER BY last_seen DESC"):
                d = dict(r)
                d["last_addresses"] = json.loads(d["last_addresses"] or "[]")
                out.append(d)
            return out

    def upsert_peer(self, device_id: str, public_key: str, fingerprint: str, addresses: list[str], port: int,
                    display_name: str = "", session_id: str | None = None) -> None:
        with self._lock:
            self.db.execute(
                "INSERT INTO peers (device_id, public_key, fingerprint, last_addresses, port, last_seen, display_name, session_id)"
                " VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(device_id) DO UPDATE SET last_addresses=excluded.last_addresses,"
                " port=excluded.port, last_seen=excluded.last_seen, display_name=excluded.display_name, session_id=excluded.session_id",
                (device_id, public_key, fingerprint, json.dumps(addresses), port, time.time(), display_name, session_id))

    def set_peer_trust(self, device_id: str, state: str, pending_key: str | None = None) -> None:
        with self._lock:
            self.db.execute("UPDATE peers SET trust_state=?, pending_key=? WHERE device_id=?", (state, pending_key, device_id))

    def accept_new_key(self, device_id: str, public_key: str, fingerprint: str) -> None:
        with self._lock:
            self.db.execute("UPDATE peers SET public_key=?, fingerprint=?, trust_state='trusted', pending_key=NULL WHERE device_id=?",
                            (public_key, fingerprint, device_id))

    # ------------------------------------------------------------------ sessions
    def get_session(self, session_id: str) -> dict | None:
        with self._lock:
            r = self.db.execute("SELECT * FROM sessions WHERE session_id=?", (session_id,)).fetchone()
            return dict(r) if r else None

    def sessions(self) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self.db.execute("SELECT * FROM sessions ORDER BY joined_at DESC")]

    def upsert_session_stub(self, session_id: str, name: str, code: str, teacher_public_key: str = "") -> None:
        """Заглушка сессии при подключении по коду/QR — до прихода SESSION_CREATED через sync."""
        with self._lock:
            self.db.execute(
                "INSERT INTO sessions (session_id, name, code, created_at, duration, teacher_public_key, joined_at)"
                " VALUES (?,?,?,?,?,?,?) ON CONFLICT(session_id) DO UPDATE SET joined_at=excluded.joined_at",
                (session_id, name, code, time.time(), 0, teacher_public_key, time.time()))
            self.db.execute(
                "INSERT OR IGNORE INTO channels (session_id, name, title, created_by, lamport) VALUES (?,?,?,?,0)",
                (session_id, "general", "Общий", ""))

    def seen_session(self, session_id: str, name: str, code: str, teacher_name: str = "", members: int = 0) -> None:
        closed = 1 if self.is_session_closed(session_id) else 0
        with self._lock:
            self.db.execute(
                "INSERT INTO seen_sessions (session_id, name, code, teacher_name, members, closed, last_seen)"
                " VALUES (?,?,?,?,?,?,?) ON CONFLICT(session_id) DO UPDATE SET name=excluded.name, code=excluded.code,"
                " teacher_name=CASE WHEN excluded.teacher_name<>'' THEN excluded.teacher_name ELSE seen_sessions.teacher_name END,"
                " members=excluded.members, closed=MAX(seen_sessions.closed, excluded.closed), last_seen=excluded.last_seen",
                (session_id, name, code, teacher_name, int(members), closed, time.time()))

    def seen_sessions(self, max_age: float = SEEN_TTL, include_closed: bool = False) -> list[dict]:
        """Активные сессии в сети: TTL по последнему broadcast (по умолчанию 15 с) и без закрытых."""
        q = "SELECT * FROM seen_sessions WHERE last_seen>?"
        if not include_closed:
            q += " AND closed=0"
        with self._lock:
            self.db.execute("DELETE FROM seen_sessions WHERE last_seen<?", (time.time() - 3600,))
            return [dict(r) for r in self.db.execute(q + " ORDER BY last_seen DESC", (time.time() - max_age,))]

    def mark_session_closed(self, session_id: str, when: float | None = None) -> None:
        """Локальная отметка закрытия (таймаут длительности или решение преподавателя)."""
        with self._lock:
            self.db.execute("UPDATE sessions SET closed_at=COALESCE(closed_at, ?) WHERE session_id=?",
                            (when or time.time(), session_id))
            self.db.execute("UPDATE seen_sessions SET closed=1 WHERE session_id=?", (session_id,))

    def is_session_closed(self, session_id: str) -> bool:
        with self._lock:
            if self.db.execute("SELECT 1 FROM tombstones WHERE session_id=? AND object_type='session' AND object_id=?",
                               (session_id, session_id)).fetchone():
                return True
            r = self.db.execute("SELECT closed_at FROM sessions WHERE session_id=?", (session_id,)).fetchone()
            return bool(r and r[0])

    # ------------------------------------------------------------------ transfers (resume из SQLite, ТЗ 12.4)
    def upsert_transfer(self, t: dict) -> None:
        with self._lock:
            t = dict(t)
            t["updated_at"] = time.time()
            cols = ", ".join(t.keys())
            ph = ", ".join("?" * len(t))
            upd = ", ".join(f"{k}=excluded.{k}" for k in t if k != "transfer_id")
            self.db.execute(f"INSERT INTO transfers ({cols}) VALUES ({ph}) ON CONFLICT(transfer_id) DO UPDATE SET {upd}",
                            list(t.values()))

    def update_transfer(self, transfer_id: str, **fields: Any) -> None:
        with self._lock:
            fields["updated_at"] = time.time()
            sets = ", ".join(f"{k}=?" for k in fields)
            self.db.execute(f"UPDATE transfers SET {sets} WHERE transfer_id=?", (*fields.values(), transfer_id))

    def get_transfer(self, transfer_id: str) -> dict | None:
        with self._lock:
            r = self.db.execute("SELECT * FROM transfers WHERE transfer_id=?", (transfer_id,)).fetchone()
            return dict(r) if r else None

    def find_transfer(self, file_id: str, direction: str) -> dict | None:
        with self._lock:
            r = self.db.execute(
                "SELECT * FROM transfers WHERE file_id=? AND direction=? ORDER BY updated_at DESC LIMIT 1",
                (file_id, direction)).fetchone()
            return dict(r) if r else None

    def transfers(self, session_id: str | None = None) -> list[dict]:
        with self._lock:
            if session_id:
                rows = self.db.execute("SELECT * FROM transfers WHERE session_id=? ORDER BY updated_at DESC", (session_id,))
            else:
                rows = self.db.execute("SELECT * FROM transfers ORDER BY updated_at DESC")
            return [dict(r) for r in rows]

    def save_chunk_hashes(self, transfer_id: str, hashes: list[str]) -> None:
        with self._lock:
            self.db.execute("BEGIN")
            self.db.execute("DELETE FROM chunk_hashes WHERE transfer_id=?", (transfer_id,))
            self.db.executemany("INSERT INTO chunk_hashes (transfer_id, chunk_index, hash) VALUES (?,?,?)",
                                [(transfer_id, i, h) for i, h in enumerate(hashes)])
            self.db.execute("COMMIT")

    def chunk_hashes(self, transfer_id: str) -> list[str]:
        with self._lock:
            return [r[0] for r in self.db.execute(
                "SELECT hash FROM chunk_hashes WHERE transfer_id=? ORDER BY chunk_index", (transfer_id,))]

    # ------------------------------------------------------------------ retention (ТЗ 11.3)
    def apply_retention(self, days: int) -> int:
        cutoff = time.time() - days * 86400
        with self._lock:
            closed = [r[0] for r in self.db.execute(
                "SELECT session_id FROM sessions WHERE closed_at IS NOT NULL AND closed_at<?", (cutoff,))]
            n = 0
            for sid in closed:
                self.db.execute("BEGIN")
                for table in ("events", "messages", "links", "channels", "members", "tombstones", "vectors",
                              "counters", "shared_files", "transfers", "seen_sessions"):
                    n += self.db.execute(f"DELETE FROM {table} WHERE session_id=?", (sid,)).rowcount
                self.db.execute("DELETE FROM chunk_hashes WHERE transfer_id NOT IN (SELECT transfer_id FROM transfers)")
                self.db.execute("DELETE FROM file_chunks WHERE file_id NOT IN (SELECT file_id FROM shared_files)")
                self.db.execute("DELETE FROM sessions WHERE session_id=?", (sid,))
                self.db.execute("COMMIT")
            self.db.execute("DELETE FROM seen_sessions WHERE last_seen<?", (time.time() - 86400,))
            return n
