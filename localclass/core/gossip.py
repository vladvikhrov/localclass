"""Gossip (ТЗ 9.2) без TTL: дедупликация по event_id, не отправлять обратно источнику. Rate limiting (ТЗ 9.5)."""
from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any

from ..net.protocol import MsgType, encode_packet, make_packet
from .events import Event

if TYPE_CHECKING:
    from ..net.mesh import PeerConnection
    from .node import Node

log = logging.getLogger("localclass.net.gossip")


class TokenBucket:
    def __init__(self, rate: float, burst: float):
        self.rate = rate
        self.burst = burst
        self.tokens = burst
        self.ts = time.monotonic()

    def allow(self) -> bool:
        now = time.monotonic()
        self.tokens = min(self.burst, self.tokens + (now - self.ts) * self.rate)
        self.ts = now
        if self.tokens >= 1:
            self.tokens -= 1
            return True
        return False


class Gossip:
    def __init__(self, node: "Node"):
        self.node = node
        self.queue: asyncio.Queue[tuple[Event, str]] = asyncio.Queue(maxsize=node.limits.inbound_queue)
        self._buckets: dict[str, TokenBucket] = {}
        self._worker: asyncio.Task | None = None
        self.dropped_rate = 0
        self.dropped_queue = 0
        self.dropped_size = 0
        node.mesh.handlers[MsgType.EVENT] = self.on_event_packet

    def start(self) -> None:
        self._worker = asyncio.create_task(self._work(), name="gossip-worker")

    def stop(self) -> None:
        if self._worker:
            self._worker.cancel()

    def _bucket(self, device_id: str) -> TokenBucket:
        b = self._buckets.get(device_id)
        if b is None:
            rate = self.node.limits.max_events_per_sec
            b = self._buckets[device_id] = TokenBucket(rate, rate * 2)
        return b

    async def on_event_packet(self, conn: "PeerConnection", packet: dict[str, Any]) -> None:
        raw = packet["payload"].get("event")
        if not isinstance(raw, dict):
            return
        if len(encode_packet(raw)) > self.node.limits.max_event_size:
            self.dropped_size += 1
            log.warning("событие от %s превышает лимит размера — отброшено", conn.short)
            return
        try:
            ev = Event.from_dict(raw)
        except ValueError as e:
            log.warning("событие от %s отброшено: %s", conn.short, e)
            return
        if ev.session_id != self.node.session_id:
            return
        if not self._bucket(ev.device_id).allow():
            self.dropped_rate += 1
            conn.rate_violations += 1
            if conn.rate_violations % 50 == 1:
                log.warning("rate limit: события %s (через %s) отбрасываются", ev.device_id[:8], conn.short)
            if conn.rate_violations >= self.node.limits.max_events_per_sec * 5:
                self.node.mesh.block(conn.device_id, self.node.limits.rate_limit_cooldown,
                                     "стабильно превышает лимит событий")
                conn.rate_violations = 0
            return
        try:
            self.queue.put_nowait((ev, conn.device_id))
        except asyncio.QueueFull:
            self.dropped_queue += 1
            log.warning("очередь приёма переполнена, событие %s отброшено (восстановит anti-entropy)", ev.event_id[:8])

    async def _work(self) -> None:
        while True:
            ev, src = await self.queue.get()
            try:
                await self.node.apply_remote_event(ev, src)
            except Exception:  # noqa: BLE001
                log.exception("apply event %s", ev.event_id[:8])

    async def forward(self, ev: Event, exclude: str | None) -> int:
        pkt = make_packet(MsgType.EVENT, self.node.device_id, {"event": ev.to_dict()})
        return await self.node.mesh.broadcast(pkt, exclude=exclude)
