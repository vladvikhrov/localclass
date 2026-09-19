"""Fault injection (ТЗ 15.2): packet loss, latency, partition, connection drop — в debug-режиме.

Точка внедрения — транспорт: перед отправкой фрейма узел спрашивает инжектор, доставлять ли фрейм и с какой
задержкой; перед установкой соединения — не изолирована ли пара. Правила симметричны, если не указано иное.
"""
from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass


@dataclass
class FaultRule:
    loss: float = 0.0        # доля отбрасываемых фреймов 0..1
    latency: float = 0.0     # секунд задержки на фрейм
    partition: bool = False  # соединения между парой запрещены


class FaultInjector:
    def __init__(self, seed: int | None = None):
        self._rules: dict[tuple[str, str], FaultRule] = {}
        self._rng = random.Random(seed)
        self.enabled = True

    def set(self, a: str, b: str, *, loss: float | None = None, latency: float | None = None,
            partition: bool | None = None, symmetric: bool = True) -> None:
        for key in ((a, b), (b, a)) if symmetric else ((a, b),):
            rule = self._rules.setdefault(key, FaultRule())
            if loss is not None:
                rule.loss = loss
            if latency is not None:
                rule.latency = latency
            if partition is not None:
                rule.partition = partition

    def clear(self, a: str | None = None, b: str | None = None) -> None:
        if a is None:
            self._rules.clear()
        else:
            self._rules.pop((a, b), None)
            self._rules.pop((b, a), None)

    def rule(self, a: str, b: str) -> FaultRule | None:
        return self._rules.get((a, b))

    def partitioned(self, a: str, b: str) -> bool:
        r = self._rules.get((a, b))
        return bool(self.enabled and r and r.partition)

    async def outbound(self, a: str, b: str) -> bool:
        """Вызывается перед отправкой фрейма a→b. False — фрейм «потерян в эфире»."""
        r = self._rules.get((a, b)) if self.enabled else None
        if not r:
            return True
        if r.partition:
            return False
        if r.loss and self._rng.random() < r.loss:
            return False
        if r.latency:
            await asyncio.sleep(r.latency)
        return True

    def rules(self) -> dict[tuple[str, str], FaultRule]:
        return dict(self._rules)
