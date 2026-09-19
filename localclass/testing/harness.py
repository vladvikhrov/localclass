"""Harness (ТЗ 15.1): запуск N узлов в одном процессе на loopback, без GUI, с fault injection."""
from __future__ import annotations

import asyncio
import shutil
import socket
import time
from pathlib import Path
from typing import Callable

from ..config import Limits, NodeConfig, Settings
from ..core.node import Node


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Cluster:
    def __init__(self, base_dir: Path, labels: list[str], *, discovery: bool = False, limits: Limits | None = None,
                 discovery_port: int | None = None):
        self.base = Path(base_dir)
        self.labels = labels
        self.discovery = discovery
        self.discovery_port = discovery_port or free_port()
        self.limits_template = limits
        self.nodes: dict[str, Node] = {}
        self.ports: dict[str, int] = {}
        self.session_id: str | None = None

    def _make(self, label: str) -> Node:
        port = self.ports.setdefault(label, free_port())
        cfg = NodeConfig(data_dir=self.base / label, device_label=label, listen_port=port,
                         discovery_port=self.discovery_port, dev_mode=True, enable_discovery=self.discovery)
        settings = Settings.load(cfg.data_dir / "config" / "settings.json")
        settings.display_name = label
        settings.enable_mdns = False
        if self.limits_template:
            for k, v in vars(self.limits_template).items():
                setattr(settings.limits, k, v)
        return Node(cfg, settings)

    async def start(self, label: str | None = None) -> None:
        for lb in ([label] if label else self.labels):
            n = self._make(lb)
            await n.start()
            self.nodes[lb] = n

    async def stop(self, label: str | None = None) -> None:
        for lb in ([label] if label else list(self.nodes)):
            n = self.nodes.pop(lb, None)
            if n:
                await n.stop()

    async def restart(self, label: str, downtime: float = 0.2) -> Node:
        """Перезапуск процесса узла с существующей базой (счётчики и состояние — из SQLite)."""
        await self.stop(label)
        await asyncio.sleep(downtime)
        await self.start(label)
        return self.nodes[label]

    def wipe(self) -> None:
        shutil.rmtree(self.base, ignore_errors=True)

    # ------------------------------------------------------------------ сессия
    async def create_session(self, teacher: str, name: str = "test") -> str:
        s = await self.nodes[teacher].create_session(name, 60)
        self.session_id = s["session_id"]
        return self.session_id

    async def join_all(self, exclude: str | None = None) -> None:
        assert self.session_id
        for lb, n in self.nodes.items():
            if lb != exclude and n.session_id != self.session_id:
                await n.join_session(self.session_id, "test", "")

    # ------------------------------------------------------------------ связи
    async def connect(self, a: str, b: str) -> None:
        await self.nodes[a].mesh.connect("127.0.0.1", self.ports[b])

    async def connect_chain(self, *labels: str) -> None:
        for x, y in zip(labels, labels[1:]):
            await self.connect(x, y)

    async def connect_all(self) -> None:
        labs = list(self.nodes)
        for i, a in enumerate(labs):
            for b in labs[i + 1:]:
                if b not in self.nodes[a].mesh.connections:
                    try:
                        await self.connect(a, b)
                    except Exception:  # noqa: BLE001
                        pass

    def connected(self, a: str, b: str) -> bool:
        na, nb = self.nodes.get(a), self.nodes.get(b)
        return bool(na and nb and na.mesh.get(nb.device_id) and nb.mesh.get(na.device_id))

    async def wait_connected(self, a: str, b: str, timeout: float = 10) -> None:
        await self.wait_until(lambda: self.connected(a, b), timeout, f"{a}–{b} не соединились")

    async def wait_until(self, pred: Callable[[], bool], timeout: float = 10, msg: str = "условие не выполнено") -> None:
        t0 = time.monotonic()
        while time.monotonic() - t0 < timeout:
            if pred():
                return
            await asyncio.sleep(0.05)
        raise AssertionError(f"timeout: {msg}")

    # ------------------------------------------------------------------ fault injection
    def _ids(self, a: str, b: str) -> tuple[str, str]:
        return self.nodes[a].device_id, self.nodes[b].device_id

    def partition(self, a: str, b: str, on: bool = True) -> None:
        ia, ib = self._ids(a, b)
        for n in (self.nodes[a], self.nodes[b]):
            n.faults.set(ia, ib, partition=on)
        if on:
            for x, y in ((a, b), (b, a)):
                c = self.nodes[x].mesh.get(self.nodes[y].device_id)
                if c:
                    c.close()

    def packet_loss(self, a: str, b: str, loss: float) -> None:
        ia, ib = self._ids(a, b)
        for n in (self.nodes[a], self.nodes[b]):
            n.faults.set(ia, ib, loss=loss)

    def latency(self, a: str, b: str, seconds: float) -> None:
        ia, ib = self._ids(a, b)
        for n in (self.nodes[a], self.nodes[b]):
            n.faults.set(ia, ib, latency=seconds)

    def drop_connection(self, a: str, b: str) -> None:
        for x, y in ((a, b), (b, a)):
            c = self.nodes[x].mesh.get(self.nodes[y].device_id)
            if c:
                c.close()

    def clear_faults(self) -> None:
        for n in self.nodes.values():
            n.faults.clear()

    # ------------------------------------------------------------------ проверки
    def messages(self, label: str, channel: str = "general") -> list[str]:
        n = self.nodes[label]
        return [m["text"] for m in n.store.messages(n.session_id, channel)]

    def event_ids(self, label: str) -> set[str]:
        n = self.nodes[label]
        return {e.event_id for e in n.store.events(n.session_id, limit=100000)}

    async def wait_consistent(self, labels: list[str] | None = None, timeout: float = 30) -> None:
        labels = labels or list(self.nodes)

        def same() -> bool:
            sets = [self.event_ids(lb) for lb in labels]
            return all(s == sets[0] for s in sets)
        await self.wait_until(same, timeout, "узлы не сошлись к одному набору событий")
