"""Сессии, коды, PIN преподавателя, QR (ТЗ 13).

Код сессии — только для поиска и ручного ввода; матчинг всегда по session_id.
Роль преподавателя в P2P-сети без сервера подтверждается PIN-кодом: создатель задаёт PIN, в событии
SESSION_CREATED распространяется только его хеш, а узел, знающий PIN, может заявить роль Teacher
со второго ноутбука. Это по-прежнему cooperative security (ТЗ 4.3): проверку делает клиент.
"""
from __future__ import annotations

import hashlib
import json
import secrets
import time
from typing import Any

from .events import new_id

CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"   # без 0/O, 1/I
QR_VERSION = 1
DEFAULT_PIN = "123"
SEEN_TTL = 15.0          # сессия исчезает из списка, если broadcast не приходил столько секунд


def generate_code() -> str:
    a = "".join(secrets.choice(CODE_ALPHABET) for _ in range(4))
    b = "".join(secrets.choice(CODE_ALPHABET) for _ in range(2))
    return f"{a}-{b}"


def normalize_code(code: str) -> str:
    c = "".join(ch for ch in code.upper() if ch.isalnum())
    return f"{c[:4]}-{c[4:6]}" if len(c) >= 6 else c


def generate_pin() -> str:
    return f"{secrets.randbelow(10000):04d}"


def pin_hash(session_id: str, pin: str) -> str:
    """Хеш PIN, привязанный к сессии: одинаковый PIN в разных сессиях даёт разные хеши."""
    pin = (pin or "").strip()
    if not pin:
        return ""
    return hashlib.sha256(f"localclass-pin:{session_id}:{pin}".encode()).hexdigest()


def check_pin(session_id: str, pin: str, expected_hash: str) -> bool:
    if not expected_hash:
        return False
    return secrets.compare_digest(pin_hash(session_id, pin), expected_hash)


def new_session(name: str, duration_sec: int, teacher_public_key: str, pin: str = DEFAULT_PIN,
                teacher_name: str = "") -> dict[str, Any]:
    sid = new_id()
    return {
        "session_id": sid,
        "name": name.strip()[:64] or "Урок",
        "code": generate_code(),
        "duration": int(duration_sec),
        "created_at": time.time(),
        "teacher_public_key": teacher_public_key,
        "teacher_pin_hash": pin_hash(sid, pin),
        "teacher_name": teacher_name.strip()[:64],
    }


def qr_payload(session: dict[str, Any], addresses: list[str], port: int) -> str:
    return json.dumps({
        "version": QR_VERSION,
        "session_id": session["session_id"],
        "session_code": session["code"],
        "session_name": session.get("name", ""),
        "teacher_public_key": session.get("teacher_public_key", ""),
        "addresses": [{"host": a, "port": port} for a in addresses],
    }, ensure_ascii=False, separators=(",", ":"))


def parse_qr(text: str) -> dict[str, Any]:
    d = json.loads(text)
    if not isinstance(d, dict) or "session_id" not in d:
        raise ValueError("это не QR-код LocalClass")
    addrs = []
    for a in d.get("addresses", []):
        if isinstance(a, dict) and isinstance(a.get("host"), str) and isinstance(a.get("port"), int):
            addrs.append((a["host"], a["port"]))
    return {
        "session_id": str(d["session_id"]),
        "session_code": str(d.get("session_code", "")),
        "session_name": str(d.get("session_name", "")),
        "teacher_public_key": str(d.get("teacher_public_key", "")),
        "addresses": addrs,
    }


def remaining_seconds(session: dict[str, Any], now: float | None = None) -> float | None:
    """Оставшееся время по длительности относительно момента создания (только для отображения)."""
    if not session.get("duration"):
        return None
    now = now or time.time()
    return max(0.0, session["created_at"] + session["duration"] - now)


def is_expired(session: dict[str, Any], now: float | None = None) -> bool:
    rem = remaining_seconds(session, now)
    return rem is not None and rem <= 0
