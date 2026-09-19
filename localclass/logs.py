"""Журналирование: два независимых потока (ТЗ 14).

* Application log — технический (`logging`, файл logs/YYYY-MM-DD.log, категории через имена логгеров
  localclass.net / files / chat / session / security).
* Event log — что произошло в приложении (файл logs/events-YYYY-MM-DD.log + шина событий для UI).
"""
from __future__ import annotations

import logging
import logging.handlers
import time
from collections import deque
from pathlib import Path
from typing import Callable

CATEGORIES = ("net", "files", "chat", "session", "security", "core")


class BusHandler(logging.Handler):
    """Передаёт записи технического лога в шину событий (для вкладки «Логи»)."""

    def __init__(self, publish: Callable[[str, dict], None], ring: int = 5000):
        super().__init__()
        self.publish = publish
        self.records: deque[dict] = deque(maxlen=ring)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            parts = record.name.split(".")
            category = parts[1] if len(parts) > 1 and parts[1] in CATEGORIES else "core"
            item = {
                "ts": record.created,
                "level": record.levelname,
                "category": category,
                "logger": record.name,
                "message": record.getMessage(),
                "peer": getattr(record, "peer", None),
            }
            self.records.append(item)
            self.publish("log.app", item)
        except Exception:  # noqa: BLE001 — логгер не должен ронять приложение
            pass


class DailyFileHandler(logging.Handler):
    """Пишет в logs/<prefix>YYYY-MM-DD.log, файл меняется при смене даты."""

    def __init__(self, directory: Path, prefix: str = ""):
        super().__init__()
        self.directory = directory
        self.prefix = prefix
        self._day = None
        self._fh = None

    def _ensure(self) -> None:
        day = time.strftime("%Y-%m-%d")
        if day != self._day:
            if self._fh:
                self._fh.close()
            self.directory.mkdir(parents=True, exist_ok=True)
            self._fh = open(self.directory / f"{self.prefix}{day}.log", "a", encoding="utf-8")
            self._day = day

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._ensure()
            self._fh.write(self.format(record) + "\n")
            self._fh.flush()
        except Exception:  # noqa: BLE001
            pass

    def close(self) -> None:
        if self._fh:
            self._fh.close()
            self._fh = None
        super().close()


class EventLog:
    """Пользовательский журнал событий приложения."""

    def __init__(self, directory: Path, publish: Callable[[str, dict], None], ring: int = 5000):
        self.publish = publish
        self.records: deque[dict] = deque(maxlen=ring)
        self._handler = DailyFileHandler(directory, prefix="events-")
        self._handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
        self._logger = logging.getLogger("localclass.eventlog")
        self._logger.propagate = False
        self._logger.setLevel(logging.INFO)
        self._logger.handlers = [self._handler]

    def write(self, event_type: str, text: str, *, category: str = "chat",
              device_id: str | None = None, event_id: str | None = None) -> None:
        item = {
            "ts": time.time(), "type": event_type, "text": text, "category": category,
            "device_id": device_id, "event_id": event_id,
        }
        self.records.append(item)
        self._logger.info("%s [%s] %s", event_type, category, text)
        self.publish("log.event", item)

    def close(self) -> None:
        self._handler.close()


def setup_app_logging(directory: Path, publish: Callable[[str, dict], None],
                      level: int = logging.DEBUG, label: str = "") -> BusHandler:
    root = logging.getLogger("localclass")
    root.setLevel(level)
    for h in list(root.handlers):
        root.removeHandler(h)
    fmt = logging.Formatter(f"%(asctime)s %(levelname)-8s {label}%(name)s: %(message)s")
    fh = DailyFileHandler(directory)
    fh.setFormatter(fmt)
    root.addHandler(fh)
    bus = BusHandler(publish)
    root.addHandler(bus)
    return bus
