"""Мост GUI ↔ ядро. Ядро живёт в своём потоке с asyncio; GUI посылает команды и получает события шины.

GUI никогда не трогает сеть и базу напрямую (ТЗ 3.3): команды — корутины Node, состояние — сигналы.
"""
from __future__ import annotations

import asyncio
import threading
import traceback
from typing import Any, Awaitable, Callable

from PySide6.QtCore import QObject, Signal

from ...core.node import Node


class Bridge(QObject):
    bus = Signal(str, dict)            # события шины ядра (в потоке GUI)
    result = Signal(object, object)    # (callback, value) — доставка результата команды в поток GUI
    failure = Signal(object, str)      # (callback, текст ошибки)

    def __init__(self, node: Node):
        super().__init__()
        self.node = node
        self.loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run, name="localclass-core", daemon=True)
        self._ready = threading.Event()
        self._start_error: str | None = None
        node.bus.subscribe(lambda topic, data: self.bus.emit(topic, dict(data)))
        self.result.connect(lambda cb, v: cb(v))
        self.failure.connect(lambda cb, msg: cb(msg))

    def start(self) -> None:
        self._thread.start()
        self._ready.wait(15)
        if self._start_error:
            raise RuntimeError(self._start_error)

    def _run(self) -> None:
        asyncio.set_event_loop(self.loop)
        try:
            self.loop.run_until_complete(self.node.start())
        except Exception as e:  # noqa: BLE001
            self._start_error = f"{e}"
            self._ready.set()
            return
        self._ready.set()
        self.loop.run_forever()

    def call(self, coro: Awaitable[Any], on_done: Callable[[Any], None] | None = None,
             on_error: Callable[[str], None] | None = None) -> None:
        """Запустить корутину в ядре; результат/ошибка вернутся в поток GUI через сигналы."""
        async def wrapper() -> None:
            try:
                value = await coro
                if on_done:
                    self.result.emit(on_done, value)
            except Exception as e:  # noqa: BLE001
                msg = str(e) or e.__class__.__name__
                if on_error:
                    self.failure.emit(on_error, msg)
                else:
                    traceback.print_exc()
        asyncio.run_coroutine_threadsafe(wrapper(), self.loop)

    def stop(self) -> None:
        if not self.loop.is_running():
            return
        fut = asyncio.run_coroutine_threadsafe(self.node.stop(), self.loop)
        try:
            fut.result(10)
        except Exception:  # noqa: BLE001
            pass
        self.loop.call_soon_threadsafe(self.loop.stop)
        self._thread.join(5)
