"""Транспорт: length-prefix framing (4 байта big-endian + payload), TCP через asyncio (ТЗ 7.2).

Строковые разделители запрещены. Тот же фрейминг используется файловым соединением, где первый байт
полезной нагрузки — тип фрейма: 0x00 JSON-пакет, 0x01 бинарный чанк.
"""
from __future__ import annotations

import asyncio
import struct
from typing import Any

from .protocol import ProtocolError, decode_packet, encode_packet

HEADER = struct.Struct("!I")
FRAME_JSON = b"\x00"
FRAME_BINARY = b"\x01"


class ConnectionClosed(Exception):
    pass


async def read_frame(reader: asyncio.StreamReader, max_size: int) -> bytes:
    try:
        head = await reader.readexactly(HEADER.size)
    except (asyncio.IncompleteReadError, ConnectionError, OSError) as e:
        raise ConnectionClosed(str(e)) from e
    (length,) = HEADER.unpack(head)
    if length > max_size:
        raise ProtocolError(f"фрейм {length} байт превышает лимит {max_size}")
    try:
        return await reader.readexactly(length)
    except (asyncio.IncompleteReadError, ConnectionError, OSError) as e:
        raise ConnectionClosed(str(e)) from e


async def write_frame(writer: asyncio.StreamWriter, data: bytes) -> None:
    try:
        writer.write(HEADER.pack(len(data)) + data)
        await writer.drain()   # backpressure: ждём, пока буфер сокета не освободится
    except (ConnectionError, OSError, RuntimeError) as e:
        raise ConnectionClosed(str(e)) from e


async def read_packet(reader: asyncio.StreamReader, max_size: int) -> dict[str, Any]:
    return decode_packet(await read_frame(reader, max_size))


async def write_packet(writer: asyncio.StreamWriter, packet: dict[str, Any]) -> None:
    await write_frame(writer, encode_packet(packet))


async def close_writer(writer: asyncio.StreamWriter) -> None:
    try:
        writer.close()
        await asyncio.wait_for(writer.wait_closed(), timeout=2)
    except Exception:  # noqa: BLE001
        pass
