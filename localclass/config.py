"""Конфигурация: пути хранения (раздел 11.1), лимиты (9.5, 12.9) и настройки пользователя."""
from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

DEFAULT_TCP_PORT = 45821
DEFAULT_DISCOVERY_PORT = 45820


def default_downloads_dir() -> Path:
    r"""Системная папка загрузок пользователя + подпапка LocalClass (ТЗ v1.3, 3.2).

    На Windows путь берётся из реестра (учитывает перенесённую папку «Загрузки»), с запасным вариантом
    %USERPROFILE%\Downloads; на Linux — XDG_DOWNLOAD_DIR или ~/Downloads (либо ~/Загрузки).
    """
    base: Path | None = None
    if sys.platform == "win32":
        try:
            import winreg
            key = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Explorer\Shell Folders"
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as k:
                base = Path(winreg.QueryValueEx(k, "{374DE290-123F-4565-9164-39C4925E467B}")[0])
        except Exception:  # noqa: BLE001 — реестр недоступен, берём стандартный путь
            base = None
        if base is None or not base.is_absolute():
            base = Path(os.environ.get("USERPROFILE", str(Path.home()))) / "Downloads"
    else:
        xdg = os.environ.get("XDG_DOWNLOAD_DIR", "").strip()
        if xdg:
            base = Path(os.path.expandvars(xdg)).expanduser()
        else:
            for name in ("Downloads", "Загрузки"):
                candidate = Path.home() / name
                if candidate.is_dir():
                    base = candidate
                    break
            else:
                base = Path.home() / "Downloads"
    return base / "LocalClass"


def default_data_dir() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", str(Path.home())))
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local" / "share")))
    return base / "LocalClass"


@dataclass
class Limits:
    """Все пороги конфигурируемы (ТЗ 9.5, 12.9)."""

    max_event_size: int = 256 * 1024          # байт
    max_events_per_sec: int = 100             # на device_id
    inbound_queue: int = 2000                 # очередь приёма событий
    rate_limit_cooldown: float = 30.0         # секунд отключения пира-нарушителя
    max_file_size: int = 8 * 1024 ** 3        # 8 ГиБ
    max_concurrent_outgoing: int = 3
    max_concurrent_incoming: int = 3
    max_chunk_size: int = 4 * 1024 ** 2
    chunk_size: int = 1024 ** 2
    ack_window: int = 16                      # чанков в полёте без подтверждения
    max_storage_size: int = 20 * 1024 ** 3
    retention_days: int = 30
    anti_entropy_interval: float = 15.0       # 10–30 c по ТЗ
    heartbeat_interval: float = 5.0
    heartbeat_timeout: float = 20.0
    discovery_interval: float = 3.0
    sync_page_size: int = 200
    max_packet_size: int = 1024 ** 2          # управляющий канал


@dataclass
class Settings:
    display_name: str = ""
    listen_port: int = DEFAULT_TCP_PORT
    discovery_port: int = DEFAULT_DISCOVERY_PORT
    enable_broadcast: bool = True
    enable_mdns: bool = True
    disabled_interfaces: list[str] = field(default_factory=list)
    auto_accept_files: bool = True
    current_session_id: str | None = None
    manual_peers: list[str] = field(default_factory=list)   # "host:port"
    advanced_logs: bool = False
    download_dir: str = ""              # пусто → storage/files внутри данных приложения
    theme: str = "light"                # light | dark
    interfaces_configured: bool = False  # авто-выбор интерфейсов уже выполнялся
    limits: Limits = field(default_factory=Limits)

    @classmethod
    def load(cls, path: Path) -> "Settings":
        s = cls()
        if path.exists():
            try:
                data = json.loads(path.read_text("utf-8"))
            except (OSError, ValueError):
                data = {}
            s.apply(data)
        return s

    def apply(self, data: dict[str, Any]) -> None:
        known = {f.name for f in fields(self)}
        for k, v in data.items():
            if k not in known:
                continue  # неизвестное поле — игнорируем, не падаем
            if k == "limits" and isinstance(v, dict):
                lk = {f.name for f in fields(Limits)}
                for lkk, lv in v.items():
                    if lkk in lk:
                        setattr(self.limits, lkk, lv)
            else:
                setattr(self, k, v)

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(self), ensure_ascii=False, indent=2), "utf-8")
        os.replace(tmp, path)


@dataclass
class Paths:
    """%APPDATA%/LocalClass/... (ТЗ 11.1). files может быть перенаправлен в папку пользователя."""

    root: Path
    download_dir: str = ""

    @property
    def config(self) -> Path: return self.root / "config"
    @property
    def settings_file(self) -> Path: return self.config / "settings.json"
    @property
    def identity(self) -> Path: return self.root / "identity"
    @property
    def key_file(self) -> Path: return self.identity / "device.key"
    @property
    def database(self) -> Path: return self.root / "database"
    @property
    def db_file(self) -> Path: return self.database / "local.db"
    @property
    def incoming(self) -> Path: return self.root / "storage" / "incoming"
    @property
    def files(self) -> Path:
        if self.download_dir:
            return Path(self.download_dir).expanduser()
        return self.root / "storage" / "files"

    def set_download_dir(self, path: str) -> None:
        self.download_dir = str(path or "")
        self.files.mkdir(parents=True, exist_ok=True)
    @property
    def logs(self) -> Path: return self.root / "logs"

    def ensure(self) -> "Paths":
        for p in (self.config, self.identity, self.database, self.incoming, self.files, self.logs):
            p.mkdir(parents=True, exist_ok=True)
        return self


@dataclass
class NodeConfig:
    """Параметры запуска одного узла (ТЗ 5.4 / 15.1: несколько узлов на одном ПК)."""

    data_dir: Path
    device_label: str = ""          # человекочитаемая метка для dev-режима (--device A)
    listen_port: int | None = None  # переопределяет settings.listen_port
    discovery_port: int | None = None
    dev_mode: bool = False          # loopback-интерфейс участвует в discovery
    headless: bool = True
    enable_discovery: bool = True
