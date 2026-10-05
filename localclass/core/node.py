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
from ..net.interfaces import (hostname, interface_key, list_interfaces, local_addresses,
                              preferred_interface, virtual_interface_keys)
from ..net.mesh import Mesh, PeerInfo
from ..testing.faults import FaultInjector
from .events import Event, EventType, new_id
from .gossip import Gossip
from .identity import Identity
from .session import (DEFAULT_PIN, SEEN_TTL, check_pin, is_expired, new_session, normalize_code,
                      parse_qr, qr_payload, remaining_seconds)
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
        if self.settings.download_dir:
            self.paths.set_download_dir(self.settings.download_dir)
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
        if not self.settings.interfaces_configured and not config.dev_mode:
            self.settings.interfaces_configured = True
            self.auto_select_interfaces()   # первый запуск: шлюз по умолчанию, без виртуальных адаптеров
            self.save_settings()
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
        self._pending_teacher_pin: str = ""     # PIN, введённый до прихода SESSION_CREATED через sync
        self._watchdog: asyncio.Task | None = None
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

    def session_teacher_name(self) -> str:
        s = self.current_session()
        if not s:
            return ""
        if s.get("teacher_name"):
            return s["teacher_name"]
        tid = s.get("teacher_id")
        if tid:
            return self._name_of(tid)
        for m in self.store.members(self.session_id):
            if m["role"] == "teacher":
                return m["display_name"] or m["device_id"][:8]
        return ""

    def session_member_count(self) -> int:
        """Участников онлайн: мы сами плюс подключённые узлы этой сессии."""
        if not self.session_id:
            return 0
        known = {m["device_id"] for m in self.store.members(self.session_id) if not m["left"]}
        online = {d for d in self.mesh.connected_ids() if d in known or not known}
        return len(online | {self.device_id})

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
        self._watchdog = asyncio.create_task(self._session_watchdog(), name="session-watchdog")
        self.started = True
        self.bus.publish("node.started", {"device_id": self.device_id, "port": self.mesh.listen_port})
        self.bus.publish("session.changed", {"session_id": self.session_id})

    async def stop(self) -> None:
        if not self.started:
            return
        self.started = False
        if self._watchdog:
            self._watchdog.cancel()
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
            asyncio.create_task(self._on_remote_session_closed(ev.session_id))
        elif t == EventType.SESSION_CREATED and not local and self._pending_teacher_pin:
            asyncio.create_task(self._retry_pending_pin())

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
            if self.store.is_session_closed(info.session_id):
                self.store.mark_session_closed(info.session_id)   # закрытый урок не возвращается в список
            else:
                self.store.seen_session(info.session_id, info.session_name, info.session_code,
                                        info.session_teacher, info.session_members)
        self.mesh.learn(info)

    # ------------------------------------------------------------------ сессии (ТЗ 13)
    async def create_session(self, name: str, duration_minutes: int = 120, pin: str = DEFAULT_PIN) -> dict[str, Any]:
        """Создать урок. Создатель получает роль Teacher; PIN позволяет заявить её со второго ноутбука."""
        s = new_session(name, duration_minutes * 60, self.identity.public_key_b64, pin, self.display_name)
        await self._switch_session(None)
        self.store.upsert_session_stub(s["session_id"], s["name"], s["code"], s["teacher_public_key"])
        self.session_id = s["session_id"]
        self.settings.current_session_id = self.session_id
        self.save_settings()
        await self.emit(EventType.SESSION_CREATED, s)
        await self.emit(EventType.USER_JOINED, {"display_name": self.display_name, "role": "teacher"})
        await self._session_changed()
        log.info("создан урок «%s», код %s, PIN задан: %s", s["name"], s["code"], bool(s["teacher_pin_hash"]))
        return self.current_session() or s

    async def join_session(self, session_id: str, name: str = "", code: str = "", teacher_public_key: str = "",
                           addresses: list[tuple[str, int]] | None = None, teacher_pin: str = "") -> dict[str, Any]:
        """Подключение всегда матчится по session_id; код — только для поиска.

        Ученику пароль не нужен. teacher_pin — для преподавателя, подключающегося со второго ноутбука:
        роль присваивается сразу, если SESSION_CREATED уже есть локально, иначе — как только он придёт по sync.
        """
        if self.store.is_session_closed(session_id):
            raise PermissionDenied("этот урок уже завершён — выберите другой или попросите создать новый")
        await self._switch_session(None)
        self.store.upsert_session_stub(session_id, name, code, teacher_public_key)
        self.session_id = session_id
        self.settings.current_session_id = session_id
        self.save_settings()
        for host, port in addresses or []:
            self.mesh.add_manual(host, port)
        self._pending_teacher_pin = (teacher_pin or "").strip()
        role = "teacher" if self._verify_pending_pin() else "student"
        await self.emit(EventType.USER_JOINED, {"display_name": self.display_name, "role": role})
        if role == "teacher":
            self._pending_teacher_pin = ""
        await self._session_changed()
        return self.current_session() or {"session_id": session_id}

    async def join_by_code(self, code: str, teacher_pin: str = "") -> dict[str, Any]:
        code = normalize_code(code)
        candidates = [s for s in self.store.seen_sessions() if s["code"] == code]
        if not candidates:
            candidates = [{"session_id": p.session_id, "name": p.session_name, "code": p.session_code}
                          for p in self.mesh.known.values()
                          if p.session_code == code and p.session_id and not self.store.is_session_closed(p.session_id)]
        if not candidates:
            raise PermissionDenied(f"урок с кодом {code} не найден в сети — проверьте код или подключитесь по QR/IP")
        if len({c["session_id"] for c in candidates}) > 1:
            names = ", ".join(f"{c['name']} ({c['session_id'][:6]})" for c in candidates)
            raise PermissionDenied(f"код {code} совпал у нескольких уроков: {names}. Используйте QR.")
        c = candidates[0]
        return await self.join_session(c["session_id"], c.get("name", ""), code, teacher_pin=teacher_pin)

    async def join_by_qr(self, text: str, teacher_pin: str = "") -> dict[str, Any]:
        q = parse_qr(text)
        return await self.join_session(q["session_id"], q["session_name"], q["session_code"], q["teacher_public_key"],
                                       q["addresses"], teacher_pin=teacher_pin)

    # ---------- роль преподавателя по PIN
    def _verify_pending_pin(self) -> bool:
        s = self.current_session()
        if not s or not self._pending_teacher_pin:
            return False
        return check_pin(s["session_id"], self._pending_teacher_pin, s.get("teacher_pin_hash", ""))

    def pin_is_known(self) -> bool:
        """Известен ли локально хеш PIN (пришёл ли SESSION_CREATED) — иначе проверять нечего."""
        s = self.current_session()
        return bool(s and s.get("teacher_pin_hash"))

    async def claim_teacher(self, pin: str) -> bool:
        """Заявить роль Teacher по PIN. Возвращает True при успехе; при неверном PIN — PermissionDenied."""
        s = self.current_session()
        if not s:
            raise PermissionDenied("нет активного урока")
        if self.is_teacher:
            return True
        self._pending_teacher_pin = (pin or "").strip()
        if not s.get("teacher_pin_hash"):
            # данных урока ещё нет — запомним PIN и проверим, как только придёт SESSION_CREATED
            await self.sync.probe_all()
            self.bus.publish("role.pending", {"session_id": s["session_id"]})
            return False
        if not self._verify_pending_pin():
            self._pending_teacher_pin = ""
            raise PermissionDenied("неверный PIN преподавателя")
        self._pending_teacher_pin = ""
        await self.emit(EventType.USER_JOINED, {"display_name": self.display_name, "role": "teacher"})
        self.bus.publish("role.changed", {"role": "teacher"})
        log.info("роль Teacher подтверждена по PIN")
        return True

    async def _retry_pending_pin(self) -> None:
        """Вызывается после применения SESSION_CREATED: PIN мог быть введён раньше прихода данных урока."""
        if not self._pending_teacher_pin or self.is_teacher:
            return
        if self._verify_pending_pin():
            self._pending_teacher_pin = ""
            await self.emit(EventType.USER_JOINED, {"display_name": self.display_name, "role": "teacher"})
            self.bus.publish("role.changed", {"role": "teacher"})
            log.info("роль Teacher подтверждена по PIN (после синхронизации урока)")
        else:
            self._pending_teacher_pin = ""
            self.bus.publish("role.rejected", {"reason": "неверный PIN преподавателя"})

    # ---------- выход, завершение, автосброс
    async def leave_session(self) -> None:
        if self.session_id:
            try:
                await self.emit(EventType.USER_LEFT, {})
            except Exception:  # noqa: BLE001
                pass
        await self._switch_session(None)
        await self._session_changed()

    async def close_session(self) -> None:
        """Преподаватель завершает урок: SESSION_CLOSED расходится по mesh, все узлы сбрасывают состояние."""
        self._require_teacher()
        sid = self.session_id
        await self.emit(EventType.SESSION_CLOSED, {"reason": "teacher"})
        self.store.mark_session_closed(sid)
        await self._reset_session("teacher", sid)

    async def _reset_session(self, reason: str, session_id: str | None = None) -> None:
        """Сброс состояния урока: закрываем соединения, чистим кэш карточек, UI уходит на экран входа."""
        sid = session_id or self.session_id
        if sid:
            self.store.mark_session_closed(sid)
        self._pending_teacher_pin = ""
        await self._switch_session(None)
        await self._session_changed()
        self.bus.publish("session.closed", {"session_id": sid, "reason": reason})
        log.info("состояние урока сброшено (%s)", reason)

    async def _on_remote_session_closed(self, session_id: str) -> None:
        if session_id == self.session_id:
            await self._reset_session("remote", session_id)
        else:
            self.store.mark_session_closed(session_id)

    async def _session_watchdog(self) -> None:
        """Таймаут урока по длительности и страховка от «залипания» в закрытой сессии."""
        while True:
            await asyncio.sleep(3.0)
            try:
                if not self.session_id:
                    continue
                s = self.current_session()
                if s is None:
                    continue
                if s.get("closed_at") or self.store.is_session_closed(self.session_id):
                    await self._reset_session("closed")
                    continue
                if is_expired(s):
                    log.info("урок «%s» истёк по длительности", s.get("name"))
                    if self.is_teacher:
                        try:
                            await self.emit(EventType.SESSION_CLOSED, {"reason": "expired"})
                        except Exception:  # noqa: BLE001
                            pass
                    await self._reset_session("expired", self.session_id)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("session watchdog")

    async def _switch_session(self, new_id_: str | None) -> None:
        for c in list(self.mesh.connections.values()):
            c.close()
        self.session_id = new_id_
        self.settings.current_session_id = new_id_
        self.save_settings()

    async def _session_changed(self) -> None:
        await self.discovery.refresh_mdns()
        self.discovery.announce_once()
        self.bus.publish("session.changed", {"session_id": self.session_id, "role": self.role})

    def qr_text(self) -> str:
        s = self.current_session()
        if not s:
            raise PermissionDenied("нет активной сессии")
        return qr_payload(s, [a for a in self.local_addresses() if not a.startswith("127.") or self.config.dev_mode],
                          self.mesh.listen_port)

    def sessions_in_network(self) -> list[dict[str, Any]]:
        """Карточки активных уроков: TTL 15 c по последнему broadcast, закрытые отфильтрованы."""
        cards: dict[str, dict[str, Any]] = {}
        for s in self.store.seen_sessions(max_age=SEEN_TTL):
            if s["session_id"] == self.session_id:
                continue
            cards[s["session_id"]] = {"session_id": s["session_id"], "name": s["name"] or "Урок", "code": s["code"],
                                      "teacher_name": s["teacher_name"], "members": s["members"],
                                      "last_seen": s["last_seen"]}
        now = time.time()
        for p in self.mesh.known.values():   # живые данные свежее кэша
            sid = p.session_id
            if not sid or sid == self.session_id or now - p.last_seen > SEEN_TTL:
                continue
            if self.store.is_session_closed(sid):
                cards.pop(sid, None)
                continue
            card = cards.setdefault(sid, {"session_id": sid, "name": p.session_name or "Урок", "code": p.session_code,
                                          "teacher_name": "", "members": 0, "last_seen": p.last_seen})
            card["name"] = p.session_name or card["name"]
            card["code"] = p.session_code or card["code"]
            card["teacher_name"] = p.session_teacher or card["teacher_name"]
            card["members"] = max(card["members"], p.session_members)
            card["last_seen"] = max(card["last_seen"], p.last_seen)
        return sorted(cards.values(), key=lambda c: -c["last_seen"])

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
        ifaces = list_interfaces(include_loopback=self.config.dev_mode, disabled=self.settings.disabled_interfaces)
        pref = preferred_interface(ifaces)
        return [{"key": interface_key(i), "name": i.name, "ip": i.ip, "hint": i.hint, "enabled": i.enabled,
                 "loopback": i.loopback, "preferred": bool(pref and i.ip == pref.ip)} for i in ifaces]

    async def run_diagnostics(self) -> dict[str, Any]:
        report = await diagnostics.run(self)
        self.bus.publish("diagnostics.result", report)
        return report

    def set_display_name(self, name: str) -> None:
        self.settings.display_name = name.strip()[:64] or hostname()
        self.save_settings()

    def set_download_dir(self, path: str | None) -> None:
        """Папка для принятых файлов. Пустое значение — хранилище по умолчанию внутри данных приложения."""
        self.settings.download_dir = str(path or "")
        self.save_settings()
        self.paths.set_download_dir(self.settings.download_dir)
        log.info("папка для принятых файлов: %s", self.paths.files)

    def set_theme(self, theme: str) -> None:
        self.settings.theme = theme if theme in ("light", "dark") else "light"
        self.save_settings()
        self.bus.publish("settings.theme", {"theme": self.settings.theme})

    def auto_select_interfaces(self) -> list[str]:
        """Первый запуск: оставить интерфейс со шлюзом по умолчанию, отключить виртуальные адаптеры."""
        keys = virtual_interface_keys()
        if keys:
            self.settings.disabled_interfaces = sorted(set(self.settings.disabled_interfaces) | set(keys))
            self.save_settings()
            log.info("авто-настройка интерфейсов: отключены виртуальные адаптеры %s", keys)
        return keys

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
                "gaps": self.store.has_gaps(self.session_id) if self.session_id else {},
                "teacher_name": self.session_teacher_name(), "members": self.session_member_count(),
                "remaining": remaining_seconds(s) if s else None, "pin_known": self.pin_is_known()}
