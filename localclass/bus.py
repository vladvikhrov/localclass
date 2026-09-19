"""Event Bus: единственный канал между ядром и UI (ТЗ 3.3, «правило разработки»)."""
from __future__ import annotations

import logging
import threading
from typing import Any, Callable

log = logging.getLogger("localclass.core.bus")
Subscriber = Callable[[str, dict], None]


class EventBus:
    def __init__(self) -> None:
        self._subs: list[Subscriber] = []
        self._lock = threading.Lock()

    def subscribe(self, cb: Subscriber) -> Callable[[], None]:
        with self._lock:
            self._subs.append(cb)

        def unsubscribe() -> None:
            with self._lock:
                if cb in self._subs:
                    self._subs.remove(cb)
        return unsubscribe

    def publish(self, topic: str, data: dict[str, Any] | None = None) -> None:
        with self._lock:
            subs = list(self._subs)
        for cb in subs:
            try:
                cb(topic, data or {})
            except Exception:  # noqa: BLE001
                log.exception("subscriber failed on %s", topic)
