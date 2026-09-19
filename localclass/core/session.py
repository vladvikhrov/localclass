"""Сессии, коды, QR (ТЗ 13). Код — только для поиска и ручного ввода; матчинг всегда по session_id."""
from __future__ import annotations

import json
import secrets
import time
from typing import Any

from .events import new_id

CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"   # без 0/O, 1/I
QR_VERSION = 1


def generate_code() -> str:
    a = "".join(secrets.choice(CODE_ALPHABET) for _ in range(4))
    b = "".join(secrets.choice(CODE_ALPHABET) for _ in range(2))
    return f"{a}-{b}"


def normalize_code(code: str) -> str:
    c = "".join(ch for ch in code.upper() if ch.isalnum())
    return f"{c[:4]}-{c[4:6]}" if len(c) >= 6 else c


def new_session(name: str, duration_sec: int, teacher_public_key: str) -> dict[str, Any]:
    return {
        "session_id": new_id(),
        "name": name.strip()[:64] or "Сессия",
        "code": generate_code(),
        "duration": int(duration_sec),
        "created_at": time.time(),
        "teacher_public_key": teacher_public_key,
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
