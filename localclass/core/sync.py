"""Vector clock и anti-entropy (ТЗ 8.2, 9.4). Vector clock скоупится сессией (11.3).

Одновременный anti-entropy двух узлов даёт два независимых обмена — это выбрано явно (ТЗ 16.2):
обмен идемпотентен благодаря дедупликации по event_id.
Пагинация SYNC_RESPONSE: флаг more; запрашивающий шлёт следующий SYNC_REQUEST уже с обновлённым вектором.
Обрыв в середине ответа: применённые события считаются полученными (каждое — своя транзакция), остальное
доберёт следующий probe.
"""
from __future__ import annotations

import asyncio
import logging
import random
import time
from typing import TYPE_CHECKING, Any

from ..net.protocol import MsgType, make_packet
from .events import Event

if TYPE_CHECKING:
    from ..net.mesh import PeerConnection
    from .node import Node

log = logging.getLogger("localclass.net.sync")


class Sync:
    def __init__(self, node: "Node"):
        self.node = node
        self._task: asyncio.Task | None = None
        self.last_probe: dict[str, float] = {}
        self.last_received: dict[str, float] = {}
        self.stats = {"probes": 0, "events_sent": 0, "events_received": 0}
        m = node.mesh
        m.handlers[MsgType.SYNC_REQUEST] = self.on_sync_request
        m.handlers[MsgType.ANTI_ENTROPY_PROBE] = self.on_probe
        m.handlers[MsgType.SYNC_RESPONSE] = self.on_sync_response
        m.on_connected.append(self.on_connected)

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop(), name="anti-entropy")

    def stop(self) -> None:
        if self._task:
            self._task.cancel()

    def _vector_packet(self, mtype: str) -> dict[str, Any]:
        return make_packet(mtype, self.node.device_id,
                           {"session_id": self.node.session_id, "vector": self.node.store.vector(self.node.session_id)})

    async def on_connected(self, conn: "PeerConnection") -> None:
        # Реконнект — сразу запрашиваем пропущенное
        await conn.send(self._vector_packet(MsgType.SYNC_REQUEST))

    async def request_sync(self, conn: "PeerConnection") -> None:
        await conn.send(self._vector_packet(MsgType.SYNC_REQUEST))

    async def _respond(self, conn: "PeerConnection", their_vector: dict[str, Any], request_id: str) -> None:
        sid = self.node.session_id
        if not sid:
            return
        vec = {str(k): int(v) for k, v in their_vector.items() if isinstance(v, int) and not isinstance(v, bool)}
        events, more = self.node.store.events_missing_for(sid, vec, self.node.limits.sync_page_size)
        self.stats["events_sent"] += len(events)
        await conn.send(make_packet(MsgType.SYNC_RESPONSE, self.node.device_id, {
            "session_id": sid, "events": [e.to_dict() for e in events], "more": more,
            "vector": self.node.store.vector(sid),
        }, request_id))

    async def on_sync_request(self, conn: "PeerConnection", packet: dict[str, Any]) -> None:
        p = packet["payload"]
        if p.get("session_id") != self.node.session_id or not isinstance(p.get("vector"), dict):
            return
        await self._respond(conn, p["vector"], packet["request_id"])

    async def on_probe(self, conn: "PeerConnection", packet: dict[str, Any]) -> None:
        p = packet["payload"]
        if p.get("session_id") != self.node.session_id or not isinstance(p.get("vector"), dict):
            return
        await self._respond(conn, p["vector"], packet["request_id"])
        # обмен в обе стороны: запрашиваем то, чего не хватает нам
        await conn.send(self._vector_packet(MsgType.SYNC_REQUEST))

    async def on_sync_response(self, conn: "PeerConnection", packet: dict[str, Any]) -> None:
        p = packet["payload"]
        if p.get("session_id") != self.node.session_id:
            return
        events = p.get("events")
        if not isinstance(events, list):
            return
        applied = 0
        for raw in events:
            if not isinstance(raw, dict):
                continue
            try:
                ev = Event.from_dict(raw)
            except ValueError as e:
                log.debug("sync: событие отброшено: %s", e)
                continue
            if await self.node.apply_remote_event(ev, conn.device_id):
                applied += 1
        self.stats["events_received"] += applied
        self.last_received[conn.device_id] = time.time()
        if applied:
            log.info("sync с %s: получено %d новых событий%s", conn.short, applied, " (ещё есть)" if p.get("more") else "")
        if p.get("more"):
            await conn.send(self._vector_packet(MsgType.SYNC_REQUEST))
        self.node.bus.publish("sync.state", {"device_id": conn.device_id, "applied": applied, "more": bool(p.get("more"))})

    async def probe(self, conn: "PeerConnection") -> None:
        self.stats["probes"] += 1
        self.last_probe[conn.device_id] = time.time()
        await conn.send(self._vector_packet(MsgType.ANTI_ENTROPY_PROBE))

    async def probe_all(self) -> None:
        for c in list(self.node.mesh.connections.values()):
            if c.alive:
                await self.probe(c)

    async def _loop(self) -> None:
        base = self.node.limits.anti_entropy_interval
        while True:
            await asyncio.sleep(base * (0.7 + 0.6 * random.random()))
            if not self.node.session_id:
                continue
            conns = [c for c in self.node.mesh.connections.values() if c.alive]
            if not conns:
                continue
            try:
                await self.probe(random.choice(conns))
            except Exception:  # noqa: BLE001
                log.exception("anti-entropy probe")
