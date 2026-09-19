"""Application Core (ТЗ 3.3): Session · Permissions · Commands.

Единственная точка входа для UI и CLI: команды — методы Node, изменения состояния — через EventBus.
Узел запускается без интерфейса (headless), что нужно для автотестов и harness.
"""
from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Any

from .. import __version__
from ..bus import EventBus
from ..config import Limits, NodeConfig, Paths, Settings
from ..files.transfer import TransferManager
from ..logs import EventLog, setup_app_logging
from ..net import diagnostics
from ..net.discovery import Discovery
from ..net.interfaces import hostname, interface_key, list_interfaces, local_addresses
from ..net.mesh import Mesh, PeerInfo
from ..testing.faults import FaultInjector
from .events import Event, EventType, new_id
from .gossip import Gossip
from .identity import Identity
from .session import new_session, normalize_code, parse_qr, qr_payload
from .store import Store
from .sync import Sync

log = logging.getLogger("localclass.core.node")


class PermissionDenied(Exception):
    pass


class Node:
    def __init__(self, config: NodeConfig, settings: Settings | None = None):
        self.config = config
        self.paths = Paths(Path(config.data_dir).expanduser().resolve()).ensure()
        self.settings = settings or Settings.load(self.paths.settings_file)
        if config.listen_port is not None:
            self.settings.listen_port = config.listen_port
        if config.discovery_port is not None:
            self.settings.discovery_port = config.discovery_port
        self.limits: Limits = self.settings.limits
        self.bus = EventBus()
        self.app_log = setup_app_logging(self.paths.logs, self.bus, label=f"[{config.device_label}] " if config.device_label else "")
        self.eventlog = EventLog(self.paths.logs, self.bus.publish)
        self.identity = Identity.load_or_create(self.paths.key_file)
        self.device_id = self.identity.device_id
        if not self.settings.display_name:
            self.settings.display_name = config.device_label or hostname()
        self.store = Store(self.paths.db_file, self.device_id)
        self.faults = FaultInjector()
        self.session_id: str | None = self.settings.current_session_id
        s = self.store.get_session(self.session_id) if self.session_id else None
        if not s or s.get("closed_at"):
            self.session_id = None
        self.mesh = Mesh(self)
        self.discovery = Discovery(self)
        self.gossip = Gossip(self)
        self.sync = Sync(self)
        self.transfers = TransferManager(self)
        self.loop: asyncio.AbstractEventLoop | None = None
        self.started = False
        self._blocked_notice: set[str] = set()
        log.info("LocalClass %s, device_id=%s, data=%s", __version__, self.device_id[:16], self.paths.root)

    # ------------------------------------------------------------------ свойства
    @property
    def display_name(self) -> str:
        return self.settings.display_name

    @property
    def discovery_port(self) -> int:
        return self.settings.discovery_port

    def local_addresses(self) -> list[str]:
        return local_addresses(include_loopback=self.config.dev_mode, disabled=self.settings.disabled_interfaces)

    def current_session(self) -> dict[str, Any] | None:
        return self.store.get_session(self.session_id) if self.session_id else None

    @property
    def role(self) -> str:
        s = self.current_session()
        if not s:
            return "student"
        if s.get("teacher_id") == self.device_id or (s.get("teacher_public_key") and s["teacher_public_key"] == self.identity.public_key_b64):
            return "teacher"
        m = self.store.member(self.session_id, self.device_id)
        return m["role"] if m else "student"

    @property
    def is_teacher(self) -> bool:
        return self.role == "teacher"

    # ------------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        self.loop = asyncio.get_running_loop()
        deleted = self.store.apply_retention(self.limits.retention_days)
        if deleted:
            log.info("retention: удалено %d записей закрытых сессий", deleted)
        await self.mesh.start(self.settings.listen_port)
        self.gossip.start()
        self.sync.start()
        if self.config.enable_discovery:
            await self.discovery.start()
        for entry in self.settings.manual_peers:
            host, _, port = entry.rpartition(":")
            if host and port.isdigit():
                self.mesh.add_manual(host, int(port))
        for p in self.store.peers():   # известные пиры из базы — пробуем переподключиться
            if p["session_id"] == self.session_id and p["last_addresses"]:
                self.mesh.learn(PeerInfo(device_id=p["device_id"], addresses=list(p["last_addresses"]), port=p["port"],
                                         session_id=p["session_id"], public_key=p["public_key"],
                                         display_name=p["display_name"], source="db", last_seen=p["last_seen"]))
        self.started = True
        self.bus.publish("node.started", {"device_id": self.device_id, "port": self.mesh.listen_port})
        self.bus.publish("session.changed", {"session_id": self.session_id})

    async def stop(self) -> None:
        if not self.started:
            return
        self.started = False
        self.transfers.stop()
        self.sync.stop()
        self.gossip.stop()
        await self.discovery.stop()
        await self.mesh.stop()
        self.settings.save(self.paths.settings_file)
        self.store.close()
        self.eventlog.close()

    def save_settings(self) -> None:
        self.settings.save(self.paths.settings_file)

    # ------------------------------------------------------------------ события
    async def emit(self, etype: str, payload: dict[str, Any]) -> Event:
        """Локальное событие: транзакция в SQLite, затем gossip всем пирам."""
        if not self.session_id:
            raise PermissionDenied("нет активной сессии")
        ev = self.store.create_event(self.session_id, etype, payload)
        self._on_event_applied(ev, local=True)
        await self.gossip.forward(ev, exclude=None)
        return ev

    async def apply_remote_event(self, ev: Event, src_device: str | None) -> bool:
        if ev.session_id != self.session_id:
            return False
        new = self.store.insert_remote_event(ev)
        if new:
            self._on_event_applied(ev, local=False)
            await self.gossip.forward(ev, exclude=src_device)
        return new

    def _on_event_applied(self, ev: Event, local: bool) -> None:
        self.bus.publish("event", {"event": ev.to_dict(), "local": local})
        who = self._name_of(ev.device_id)
        t, p = ev.type, ev.payload
        cat = "chat"
        if t == EventType.MESSAGE_CREATED:
            text = f"{who} → #{p.get('channel', '?')}: {str(p.get('text', ''))[:80]}"
        elif t == EventType.MESSAGE_DELETED:
            text = f"{who} удалил сообщение {str(p.get('message_id', ''))[:8]}"
        elif t == EventType.LINK_CREATED:
            text = f"{who} поделился ссылкой {p.get('url', '')}"
        elif t in (EventType.FILE_OFFERED, EventType.FILE_COMPLETED, EventType.FILE_FAILED, EventType.FILE_DELETED):
            cat = "files"
            text = f"{who}: {t.lower().replace('_', ' ')} {p.get('filename', p.get('file_id', ''))}"
        elif t in (EventType.USER_JOINED, EventType.USER_LEFT, EventType.SESSION_CREATED, EventType.SESSION_CLOSED,
                   EventType.CHANNEL_CREATED, EventType.CHANNEL_CLOSED, EventType.PERMISSIONS_UPDATED):
            cat = "session"
            text = f"{who}: {t} {p.get('display_name') or p.get('name') or p.get('channel') or ''}"
        else:
            text = f"{who}: {t}"
        self.eventlog.write(t, text, category=cat, device_id=ev.device_id, event_id=ev.event_id)
        if t == EventType.SESSION_CLOSED and not local:
            self.bus.publish("session.closed", {"session_id": ev.session_id})

    def _name_of(self, device_id: str) -> str:
        if device_id == self.device_id:
            return self.display_name or "я"
        if self.session_id:
            m = self.store.member(self.session_id, device_id)
            if m and m["display_name"]:
                return m["display_name"]
        k = self.mesh.known.get(device_id)
        return (k.display_name if k and k.display_name else device_id[:8])

    # ------------------------------------------------------------------ discovery callbacks
    def on_peer_discovered(self, info: PeerInfo) -> None:
        if info.session_id and info.session_code:
            self.store.seen_session(info.session_id, info.session_name, info.session_code)
        self.mesh.learn(info)

    # ------------------------------------------------------------------ сессии (ТЗ 13)
    async def create_session(self, name: str, duration_minutes: int = 120) -> dict[str, Any]:
        s = new_session(name, duration_minutes * 60, self.identity.public_key_b64)
        await self._switch_session(None)
        self.store.upsert_session_stub(s["session_id"], s["name"], s["code"], s["teacher_public_key"])
        self.session_id = s["session_id"]
        self.settings.current_session_id = self.session_id
        self.save_settings()
        await self.emit(EventType.SESSION_CREATED, s)
        await self.emit(EventType.USER_JOINED, {"display_name": self.display_name, "role": "teacher"})
        await self._session_changed()
        return self.current_session() or s

    async def join_session(self, session_id: str, name: str = "", code: str = "", teacher_public_key: str = "",
                           addresses: list[tuple[str, int]] | None = None) -> dict[str, Any]:
        """Подключение всегда матчится по session_id; код — только для поиска."""
        await self._switch_session(None)
        self.store.upsert_session_stub(session_id, name, code, teacher_public_key)
        self.session_id = session_id
        self.settings.current_session_id = session_id
        self.save_settings()
        for host, port in addresses or []:
            self.mesh.add_manual(host, port)
        await self.emit(EventType.USER_JOINED, {"display_name": self.display_name, "role": "student"})
        await self._session_changed()
        return self.current_session() or {"session_id": session_id}

    async def join_by_code(self, code: str) -> dict[str, Any]:
        code = normalize_code(code)
        candidates = [s for s in self.store.seen_sessions() if s["code"] == code]
        if not candidates:
            candidates = [{"session_id": p.session_id, "name": p.session_name, "code": p.session_code}
                          for p in self.mesh.known.values() if p.session_code == code and p.session_id]
        if not candidates:
            raise PermissionDenied(f"сессия с кодом {code} не найдена в сети — проверьте код или подключитесь по QR/IP")
        if len({c["session_id"] for c in candidates}) > 1:
            names = ", ".join(f"{c['name']} ({c['session_id'][:6]})" for c in candidates)
            raise PermissionDenied(f"код {code} совпал у нескольких сессий: {names}. Используйте QR.")
        c = candidates[0]
        return await self.join_session(c["session_id"], c.get("name", ""), code)

    async def join_by_qr(self, text: str) -> dict[str, Any]:
        q = parse_qr(text)
        return await self.join_session(q["session_id"], q["session_name"], q["session_code"], q["teacher_public_key"],
                                       q["addresses"])

    async def leave_session(self) -> None:
        if self.session_id:
            try:
                await self.emit(EventType.USER_LEFT, {})
            except Exception:  # noqa: BLE001
                pass
        await self._switch_session(None)
        await self._session_changed()

    async def close_session(self) -> None:
        self._require_teacher()
        await self.emit(EventType.SESSION_CLOSED, {})
        self.store.db.execute("UPDATE sessions SET closed_at=? WHERE session_id=?", (time.time(), self.session_id))
        await self._switch_session(None)
        await self._session_changed()

    async def _switch_session(self, new_id_: str | None) -> None:
        for c in list(self.mesh.connections.values()):
            c.close()
        self.session_id = new_id_
        self.settings.current_session_id = new_id_
        self.save_settings()

    async def _session_changed(self) -> None:
        await self.discovery.refresh_mdns()
        self.discovery.announce_once()
        self.bus.publish("session.changed", {"session_id": self.session_id})

    def qr_text(self) -> str:
        s = self.current_session()
        if not s:
            raise PermissionDenied("нет активной сессии")
        return qr_payload(s, [a for a in self.local_addresses() if not a.startswith("127.") or self.config.dev_mode],
                          self.mesh.listen_port)

    def sessions_in_network(self) -> list[dict[str, Any]]:
        return self.store.seen_sessions()

    # ------------------------------------------------------------------ права (cooperative security, ТЗ 4.3)
    def _require_teacher(self) -> None:
        if not self.is_teacher:
            raise PermissionDenied("доступно только преподавателю")

    def _require_not_blocked(self) -> None:
        m = self.store.member(self.session_id, self.device_id) if self.session_id else None
        if m and m["blocked"]:
            raise PermissionDenied("преподаватель ограничил ваш доступ к чату")

    def _require_channel_open(self, channel: str) -> None:
        if channel.startswith("dm:"):
            return
        for ch in self.store.channels(self.session_id, include_closed=True):
            if ch["name"] == channel:
                if ch["closed"]:
                    raise PermissionDenied(f"канал #{channel} закрыт")
                return

    # ------------------------------------------------------------------ чат
    async def send_message(self, text: str, channel: str = "general", kind: str = "message", to: str | None = None) -> Event:
        text = text.strip()
        if not text:
            raise PermissionDenied("пустое сообщение")
        self._require_not_blocked()
        if kind == "announcement":
            self._require_teacher()
        if to:
            channel = f"dm:{':'.join(sorted([self.device_id, to]))}"
        self._require_channel_open(channel)
        payload: dict[str, Any] = {"message_id": new_id(), "channel": channel, "text": text[:16000], "kind": kind}
        if to:
            payload["to"] = to
        return await self.emit(EventType.MESSAGE_CREATED, payload)

    async def delete_message(self, message_id: str) -> Event:
        row = self.store.db.execute("SELECT device_id FROM messages WHERE message_id=?", (message_id,)).fetchone()
        if not (self.is_teacher or (row and row[0] == self.device_id)):
            raise PermissionDenied("удалять чужие сообщения может только преподаватель")
        return await self.emit(EventType.MESSAGE_DELETED, {"message_id": message_id})

    async def send_link(self, url: str, title: str = "", channel: str = "general") -> Event:
        url = url.strip()
        if not (url.startswith("http://") or url.startswith("https://")):
            raise PermissionDenied("ссылка должна начинаться с http:// или https://")
        self._require_not_blocked()
        return await self.emit(EventType.LINK_CREATED, {"link_id": new_id(), "url": url[:2048], "title": title[:200], "channel": channel})

    async def create_channel(self, name: str, title: str = "") -> Event:
        self._require_teacher()
        name = "".join(ch for ch in name.strip().lower() if ch.isalnum() or ch in "-_")[:32]
        if not name:
            raise PermissionDenied("некорректное имя канала")
        return await self.emit(EventType.CHANNEL_CREATED, {"channel": name, "title": title})

    async def close_channel(self, name: str) -> Event:
        self._require_teacher()
        if name == "general":
            raise PermissionDenied("общий канал закрыть нельзя")
        return await self.emit(EventType.CHANNEL_CLOSED, {"channel": name})

    async def set_blocked(self, device_id: str, blocked: bool) -> Event:
        self._require_teacher()
        return await self.emit(EventType.PERMISSIONS_UPDATED, {"device_id": device_id, "blocked": blocked})

    async def delete_file(self, file_id: str) -> Event:
        f = self.store.shared_file(file_id)
        if not (self.is_teacher or (f and f["owner_id"] == self.device_id)):
            raise PermissionDenied("удалять чужие файлы может только преподаватель")
        return await self.emit(EventType.FILE_DELETED, {"file_id": file_id})

    # ------------------------------------------------------------------ сеть
    async def connect_manual(self, host: str, port: int) -> None:
        self.mesh.add_manual(host.strip(), int(port))
        entry = f"{host.strip()}:{int(port)}"
        if entry not in self.settings.manual_peers:
            self.settings.manual_peers.append(entry)
            self.save_settings()
        asyncio.create_task(self.mesh._connect_manual(entry, host.strip(), int(port)))

    async def trust_peer(self, device_id: str, accept: bool) -> None:
        """Решение пользователя при смене отпечатка (ТЗ 4.2)."""
        p = self.store.get_peer(device_id)
        if not p:
            return
        if accept and p.get("pending_key"):
            from .identity import fingerprint, unb64
            self.store.accept_new_key(device_id, p["pending_key"], fingerprint(unb64(p["pending_key"])))
            log.warning("пользователь принял новый ключ узла %s", device_id[:8])
        else:
            self.store.set_peer_trust(device_id, "conflict", p.get("pending_key"))
            self.mesh.blocked_until[device_id] = time.time() + 3600

    def set_interface_enabled(self, key: str, enabled: bool) -> None:
        d = set(self.settings.disabled_interfaces)
        (d.discard if enabled else d.add)(key)
        self.settings.disabled_interfaces = sorted(d)
        self.save_settings()

    def interfaces(self) -> list[dict[str, Any]]:
        return [{"key": interface_key(i), "name": i.name, "ip": i.ip, "hint": i.hint, "enabled": i.enabled, "loopback": i.loopback}
                for i in list_interfaces(include_loopback=self.config.dev_mode, disabled=self.settings.disabled_interfaces)]

    async def run_diagnostics(self) -> dict[str, Any]:
        report = await diagnostics.run(self)
        self.bus.publish("diagnostics.result", report)
        return report

    def set_display_name(self, name: str) -> None:
        self.settings.display_name = name.strip()[:64] or hostname()
        self.save_settings()

    # ------------------------------------------------------------------ запросы для UI
    def peers_view(self) -> list[dict[str, Any]]:
        out = []
        for p in self.mesh.known.values():
            c = self.mesh.get(p.device_id)
            out.append({"device_id": p.device_id, "display_name": p.display_name, "status": self.mesh.peer_status(p.device_id),
                        "addresses": p.addresses, "port": p.port, "session_id": p.session_id, "source": p.source,
                        "last_seen": p.last_seen, "error": p.last_error,
                        "rtt": c.rtt if c else None, "address": c.remote_addr[0] if c else None,
                        "app_version": c.app_version if c else ""})
        return sorted(out, key=lambda x: (x["status"] != "connected", x["display_name"]))

    def status(self) -> dict[str, Any]:
        s = self.current_session()
        return {"device_id": self.device_id, "display_name": self.display_name, "port": self.mesh.listen_port,
                "session": s, "role": self.role, "connected": len(self.mesh.connected_ids()),
                "known": len(self.mesh.known), "lamport": self.store.lamport(),
                "device_seq": self.store.device_seq(self.session_id) if self.session_id else 0,
                "events": self.store.event_count(self.session_id) if self.session_id else 0,
                "sync": dict(self.sync.stats), "addresses": self.local_addresses(), "version": __version__,
                "gaps": self.store.has_gaps(self.session_id) if self.session_id else {}}
