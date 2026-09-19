"""Пакеты и версионирование протокола (ТЗ 7, 16). Подробности — docs/PROTOCOL.md."""
from __future__ import annotations

import json
import math
import uuid
from typing import Any

PROTOCOL_VERSION = 1
# Совместимость (ТЗ 7.3): принимаем соседние версии, неизвестные типы/поля игнорируем.
MIN_COMPATIBLE = 1
MAX_COMPATIBLE = 2


class MsgType:
    # Соединение
    HELLO = "HELLO"
    HELLO_ACK = "HELLO_ACK"
    AUTH = "AUTH"
    PEER_LIST = "PEER_LIST"
    PING = "PING"
    PONG = "PONG"
    GOODBYE = "GOODBYE"
    # Синхронизация
    SYNC_REQUEST = "SYNC_REQUEST"
    SYNC_RESPONSE = "SYNC_RESPONSE"
    ANTI_ENTROPY_PROBE = "ANTI_ENTROPY_PROBE"
    # События
    EVENT = "EVENT"
    EVENT_ACK = "EVENT_ACK"
    # Файлы
    FILE_OFFER = "FILE_OFFER"
    FILE_ACCEPT = "FILE_ACCEPT"
    FILE_REJECT = "FILE_REJECT"
    FILE_CHUNK = "FILE_CHUNK"
    FILE_CHUNK_ACK = "FILE_CHUNK_ACK"
    FILE_RESUME = "FILE_RESUME"
    FILE_FINISH = "FILE_FINISH"
    FILE_ABORT = "FILE_ABORT"
    # Служебные
    ERROR = "ERROR"
    PROTOCOL_VERSION_MISMATCH = "PROTOCOL_VERSION_MISMATCH"


class ProtocolError(Exception):
    pass


class VersionMismatch(ProtocolError):
    def __init__(self, remote_version: Any):
        super().__init__(f"несовместимая версия протокола: {remote_version}")
        self.remote_version = remote_version


def _check_numbers(obj: Any) -> None:
    """JSON без NaN/Infinity, целые ≤ 2^53 (ТЗ 7.4)."""
    if isinstance(obj, bool):
        return
    if isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj):
            raise ProtocolError("NaN/Infinity запрещены")
    elif isinstance(obj, int):
        if abs(obj) > 2 ** 53:
            raise ProtocolError("целое превышает 2^53")
    elif isinstance(obj, dict):
        for v in obj.values():
            _check_numbers(v)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            _check_numbers(v)


def make_packet(message_type: str, sender_id: str, payload: dict[str, Any] | None = None,
                request_id: str | None = None) -> dict[str, Any]:
    return {
        "protocol_version": PROTOCOL_VERSION,
        "message_type": message_type,
        "request_id": request_id or uuid.uuid4().hex,
        "sender_id": sender_id,
        "payload": payload or {},
    }


def encode_packet(packet: dict[str, Any]) -> bytes:
    _check_numbers(packet)
    return json.dumps(packet, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")


def decode_packet(raw: bytes) -> dict[str, Any]:
    """Разбор пакета. Неизвестные поля сохраняются и игнорируются логикой; версия проверяется."""
    try:
        packet = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as e:
        raise ProtocolError(f"невалидный JSON: {e}") from e
    if not isinstance(packet, dict):
        raise ProtocolError("пакет должен быть объектом")
    for field in ("protocol_version", "message_type", "request_id", "sender_id", "payload"):
        if field not in packet:
            raise ProtocolError(f"отсутствует обязательное поле {field}")
    v = packet["protocol_version"]
    if not isinstance(v, int) or v < MIN_COMPATIBLE or v > MAX_COMPATIBLE:
        raise VersionMismatch(v)
    if not isinstance(packet["payload"], dict):
        raise ProtocolError("payload должен быть объектом")
    if not isinstance(packet["message_type"], str) or not isinstance(packet["sender_id"], str):
        raise ProtocolError("некорректные типы полей")
    return packet
