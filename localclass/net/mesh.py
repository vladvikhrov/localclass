"""Полносвязная mesh (ТЗ 3): TCP-соединения, HELLO/AUTH-рукопожатие с TOFU (ТЗ 4.2), heartbeat, reconnect.

Один TCP-порт обслуживает и управляющий канал (первый пакет HELLO), и файловые соединения
(первый пакет FILE_RESUME) — так в Firewall нужно открыть один порт.
"""
from __future__ import annotations

import asyncio
import logging
import os
import random
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from .. import __version__
from ..core.identity import Identity, b64, fingerprint, unb64
from .protocol import (PROTOCOL_VERSION, MsgType, ProtocolError, VersionMismatch, make_packet)
from .protocol import decode_packet
from .transport import FRAME_JSON, ConnectionClosed, close_writer, read_frame, read_packet, write_packet

if TYPE_CHECKING:
    from ..core.node import Node

log = logging.getLogger("localclass.net.mesh")
seclog = logging.getLogger("localclass.security.tofu")

HANDSHAKE_TIMEOUT = 10.0
CONNECT_TIMEOUT = 4.0
Handler = Callable[["PeerConnection", dict[str, Any]], Awaitable[None]]


class HandshakeError(Exception):
    pass


class TofuConflict(HandshakeError):
    pass


@dataclass
class PeerInfo:
    """Известный пир: откуда бы ни узнали (broadcast, mDNS, PEER_LIST, вручную, база)."""
    device_id: str
    addresses: list[str] = field(default_factory=list)
    port: int = 0
    session_id: str | None = None
    public_key: str = ""
    display_name: str = ""
    session_code: str = ""
    session_name: str = ""
    source: str = ""
    last_seen: float = 0.0
    failures: int = 0
    next_attempt: float = 0.0
    last_error: str = ""


class PeerConnection:
    def __init__(self, mesh: "Mesh", reader: asyncio.StreamReader, writer: asyncio.StreamWriter, initiated_by_us: bool):
        self.mesh = mesh
        self.reader = reader
        self.writer = writer
        self.initiated_by_us = initiated_by_us
        self.device_id: str = ""
        self.public_key_b64: str = ""
        self.session_id: str = ""
        self.addresses: list[str] = []
        self.port: int = 0
        self.display_name: str = ""
        self.app_version: str = ""
        self.protocol_version: int = PROTOCOL_VERSION
        peer = writer.get_extra_info("peername") or ("?", 0)
        self.remote_addr: tuple[str, int] = (peer[0], peer[1])
        self.connected_at = time.time()
        self.last_rx = time.time()
        self.rtt: float | None = None
        self._ping_sent: float | None = None
        self.alive = True
        self._send_lock = asyncio.Lock()
        self.bytes_in = 0
        self.bytes_out = 0
        self.rate_violations = 0

    @property
    def short(self) -> str:
        return f"{self.display_name or '?'}@{self.device_id[:8]}"

    async def send(self, packet: dict[str, Any]) -> bool:
        if not self.alive:
            return False
        if self.device_id and not await self.mesh.node.faults.outbound(self.mesh.node.device_id, self.device_id):
            log.debug("fault-injection: фрейм %s → %s отброшен", packet["message_type"], self.device_id[:8])
            return True   # «потерян в эфире» — для отправителя выглядит как успех
        try:
            async with self._send_lock:
                await write_packet(self.writer, packet)
            return True
        except (ConnectionClosed, ProtocolError) as e:
            log.debug("send %s → %s: %s", packet["message_type"], self.short, e)
            self.close()
            return False

    def close(self) -> None:
        if not self.alive:
            return
        self.alive = False
        try:
            self.writer.close()
        except Exception:  # noqa: BLE001
            pass


class Mesh:
    def __init__(self, node: "Node"):
        self.node = node
        self.connections: dict[str, PeerConnection] = {}
        self.known: dict[str, PeerInfo] = {}
        self.manual: dict[str, tuple[str, int]] = {}         # "host:port" -> (host, port)
        self.handlers: dict[str, Handler] = {}
        self.on_connected: list[Callable[[PeerConnection], Awaitable[None]]] = []
        self.on_disconnected: list[Callable[[PeerConnection], Awaitable[None]]] = []
        self.blocked_until: dict[str, float] = {}
        self._server: asyncio.AbstractServer | None = None
        self._tasks: list[asyncio.Task] = []
        self._connecting: set[str] = set()
        self.listen_port = 0
        self.listen_error: str = ""
        self.paused = False   # debug: приостановить reconnect

    # ------------------------------------------------------------------ lifecycle
    async def start(self, port: int) -> None:
        try:
            self._server = await asyncio.start_server(self._on_inbound, host="0.0.0.0", port=port, reuse_address=True)
        except OSError as e:
            self.listen_error = str(e)
            log.error("не удалось открыть TCP-порт %s: %s", port, e)
            raise
        self.listen_port = self._server.sockets[0].getsockname()[1]
        log.info("TCP слушает порт %s", self.listen_port)
        self._tasks = [
            asyncio.create_task(self._heartbeat_loop(), name="mesh-heartbeat"),
            asyncio.create_task(self._reconnect_loop(), name="mesh-reconnect"),
        ]

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        for c in list(self.connections.values()):
            try:
                await asyncio.wait_for(c.send(make_packet(MsgType.GOODBYE, self.node.device_id)), timeout=1)
            except Exception:  # noqa: BLE001
                pass
            c.close()
        if self._server:
            self._server.close()
            try:
                await asyncio.wait_for(self._server.wait_closed(), timeout=2)
            except Exception:  # noqa: BLE001
                pass

    # ------------------------------------------------------------------ known peers
    def learn(self, info: PeerInfo) -> None:
        if info.device_id == self.node.device_id:
            return  # собственный device_id всегда отбрасывается (ТЗ 5.2)
        cur = self.known.get(info.device_id)
        if cur is None:
            self.known[info.device_id] = info
            self.node.bus.publish("peer.discovered", {"device_id": info.device_id, "source": info.source})
            log.info("обнаружен узел %s (%s) %s:%s [%s]", info.display_name, info.device_id[:8],
                     info.addresses, info.port, info.source)
            return
        # объединяем адреса; свежая информация побеждает
        for a in info.addresses:
            if a not in cur.addresses:
                cur.addresses.append(a)
        if info.port:
            cur.port = info.port
        if info.session_id:
            cur.session_id = info.session_id
        if info.public_key:
            cur.public_key = info.public_key
        if info.display_name:
            cur.display_name = info.display_name
        if info.session_code:
            cur.session_code = info.session_code
        if info.session_name:
            cur.session_name = info.session_name
        cur.source = info.source or cur.source
        cur.last_seen = max(cur.last_seen, info.last_seen)

    def add_manual(self, host: str, port: int) -> None:
        self.manual[f"{host}:{port}"] = (host, port)

    def block(self, device_id: str, seconds: float, reason: str) -> None:
        self.blocked_until[device_id] = time.time() + seconds
        c = self.connections.get(device_id)
        log.warning("пир %s временно отключён на %.0f c: %s", device_id[:8], seconds, reason)
        if c:
            c.close()

    def peer_status(self, device_id: str) -> str:
        if device_id in self.connections and self.connections[device_id].alive:
            return "connected"
        if self.blocked_until.get(device_id, 0) > time.time():
            return "blocked"
        p = self.node.store.get_peer(device_id)
        if p and p["trust_state"] == "conflict":
            return "conflict"
        k = self.known.get(device_id)
        if k and k.failures >= 2:
            return "unreachable"
        if k and k.session_id and k.session_id != self.node.session_id:
            return "other_session"
        return "discovered"

    # ------------------------------------------------------------------ inbound
    async def _on_inbound(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        try:
            raw = await asyncio.wait_for(read_frame(reader, self.node.limits.max_packet_size), HANDSHAKE_TIMEOUT)
            # файловое соединение использует фреймы с байтом типа (0x00 = JSON), управляющее — чистый JSON
            first = decode_packet(raw[1:] if raw[:1] == FRAME_JSON else raw)
        except VersionMismatch as e:
            log.warning("входящее с %s: несовместимая версия протокола %s", peer, e.remote_version)
            try:
                await write_packet(writer, make_packet(MsgType.PROTOCOL_VERSION_MISMATCH, self.node.device_id,
                                                       {"protocol_version": PROTOCOL_VERSION}))
            finally:
                await close_writer(writer)
            return
        except (ProtocolError, ConnectionClosed, asyncio.TimeoutError) as e:
            log.debug("входящее с %s отклонено: %s", peer, e)
            await close_writer(writer)
            return
        mtype = first["message_type"]
        if mtype == MsgType.HELLO:
            conn = PeerConnection(self, reader, writer, initiated_by_us=False)
            try:
                await asyncio.wait_for(self._handshake_acceptor(conn, first), HANDSHAKE_TIMEOUT)
            except (HandshakeError, ProtocolError, ConnectionClosed, asyncio.TimeoutError) as e:
                log.info("рукопожатие с %s не удалось: %s", peer, e)
                conn.close()
                return
            await self._established(conn)
        elif mtype == MsgType.FILE_RESUME:
            await self.node.transfers.serve(reader, writer, first)
        else:
            log.debug("входящее с %s: неожиданный первый пакет %s", peer, mtype)
            await close_writer(writer)

    # ------------------------------------------------------------------ outbound
    async def connect(self, host: str, port: int, expected_id: str | None = None) -> PeerConnection | None:
        key = expected_id or f"{host}:{port}"
        if key in self._connecting:
            return None
        self._connecting.add(key)
        try:
            try:
                reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), CONNECT_TIMEOUT)
            except (OSError, asyncio.TimeoutError) as e:
                raise HandshakeError(f"TCP {host}:{port}: {e or 'timeout'}") from e
            conn = PeerConnection(self, reader, writer, initiated_by_us=True)
            try:
                await asyncio.wait_for(self._handshake_initiator(conn), HANDSHAKE_TIMEOUT)
            except (HandshakeError, ProtocolError, ConnectionClosed, asyncio.TimeoutError) as e:
                conn.close()
                raise HandshakeError(str(e) or "timeout") from e
            if expected_id and conn.device_id != expected_id:
                log.info("%s:%s ответил другой узел (%s, ожидали %s)", host, port, conn.device_id[:8], expected_id[:8])
            await self._established(conn)
            return conn
        finally:
            self._connecting.discard(key)

    async def connect_known(self, info: PeerInfo) -> bool:
        if info.device_id in self.connections or info.device_id in self._connecting:
            return True
        last_err = ""
        for addr in list(info.addresses):
            if self.node.faults.partitioned(self.node.device_id, info.device_id):
                info.last_error = "partition (fault injection)"
                return False
            try:
                c = await self.connect(addr, info.port, expected_id=info.device_id)
                if c:
                    info.failures = 0
                    info.last_error = ""
                    return True
            except HandshakeError as e:
                last_err = str(e)
        info.failures += 1
        info.last_error = last_err
        info.next_attempt = time.time() + min(2.0 * (2 ** min(info.failures, 4)), 30.0)
        if info.failures == 2:
            self.node.bus.publish("peer.unreachable", {"device_id": info.device_id, "error": last_err})
            log.warning("узел %s обнаружен, но недоступен по TCP: %s", info.device_id[:8], last_err)
        return False

    # ------------------------------------------------------------------ handshake
    def _hello_payload(self, nonce: bytes) -> dict[str, Any]:
        return {
            "device_id": self.node.device_id,
            "public_key": self.node.identity.public_key_b64,
            "session_id": self.node.session_id,
            "port": self.listen_port,
            "addresses": self.node.local_addresses(),
            "nonce": b64(nonce),
            "display_name": self.node.display_name,
            "app_version": __version__,
        }

    def _validate_hello(self, conn: PeerConnection, payload: dict[str, Any]) -> bytes:
        try:
            device_id = str(payload["device_id"])
            pk = unb64(str(payload["public_key"]))
            nonce = unb64(str(payload["nonce"]))
        except (KeyError, ValueError, TypeError) as e:
            raise HandshakeError(f"некорректный HELLO: {e}") from e
        if len(pk) != 32 or fingerprint(pk) != device_id:
            raise HandshakeError("device_id не соответствует публичному ключу")
        if device_id == self.node.device_id:
            raise HandshakeError("соединение с самим собой")
        if self.node.faults.partitioned(self.node.device_id, device_id):
            raise HandshakeError("partition (fault injection)")
        if self.blocked_until.get(device_id, 0) > time.time():
            raise HandshakeError("пир временно заблокирован (rate limit)")
        conn.device_id = device_id
        conn.public_key_b64 = str(payload["public_key"])
        conn.session_id = str(payload.get("session_id") or "")
        conn.addresses = [str(a) for a in payload.get("addresses", []) if isinstance(a, str)]
        conn.port = int(payload.get("port") or 0)
        conn.display_name = str(payload.get("display_name", ""))[:64]
        conn.app_version = str(payload.get("app_version", ""))
        if not self.node.session_id:
            raise HandshakeError("NO_SESSION: узел не состоит в сессии")
        if conn.session_id != self.node.session_id:
            raise HandshakeError("SESSION_MISMATCH: узел в другой сессии")
        self._tofu(conn)
        return nonce

    def _tofu(self, conn: PeerConnection) -> None:
        """Trust On First Use (ТЗ 4.2)."""
        known = self.node.store.get_peer(conn.device_id)
        fp = fingerprint(unb64(conn.public_key_b64))
        if known is None:
            return  # первый контакт — отпечаток сохранится при установлении соединения
        if known["fingerprint"] == fp:
            if known["trust_state"] == "conflict":
                # ключ вернулся к прежнему — конфликт снимаем
                self.node.store.set_peer_trust(conn.device_id, "trusted")
            return
        seclog.warning("SECURITY WARNING: у узла %s изменился отпечаток ключа (%s → %s)",
                       conn.device_id[:8], known["fingerprint"][:12], fp[:12])
        self.node.store.set_peer_trust(conn.device_id, "conflict", conn.public_key_b64)
        self.node.bus.publish("security.warning", {
            "device_id": conn.device_id, "old_fingerprint": known["fingerprint"], "new_fingerprint": fp,
            "display_name": conn.display_name,
        })
        raise TofuConflict("отпечаток ключа изменился — требуется решение пользователя")

    async def _handshake_initiator(self, conn: PeerConnection) -> None:
        my_nonce = os.urandom(16)
        await write_packet(conn.writer, make_packet(MsgType.HELLO, self.node.device_id, self._hello_payload(my_nonce)))
        try:
            ack = await read_packet(conn.reader, self.node.limits.max_packet_size)
        except VersionMismatch as e:
            raise HandshakeError(f"PROTOCOL_VERSION_MISMATCH: удалённая версия {e.remote_version}") from e
        t = ack["message_type"]
        if t == MsgType.PROTOCOL_VERSION_MISMATCH:
            raise HandshakeError("PROTOCOL_VERSION_MISMATCH: «Версия LocalClass на этом компьютере устарела»")
        if t == MsgType.ERROR:
            raise HandshakeError(f"отказ: {ack['payload'].get('reason', '?')}")
        if t != MsgType.HELLO_ACK:
            raise HandshakeError(f"ожидали HELLO_ACK, получили {t}")
        p = ack["payload"]
        their_nonce = self._validate_hello(conn, p)
        try:
            sig = unb64(str(p.get("signature", "")))
        except ValueError as e:
            raise HandshakeError("некорректная подпись") from e
        if not Identity.verify(unb64(conn.public_key_b64), sig, my_nonce):
            raise HandshakeError("подпись HELLO_ACK не прошла проверку")
        await write_packet(conn.writer, make_packet(MsgType.AUTH, self.node.device_id,
                                                    {"signature": b64(self.node.identity.sign(their_nonce))}))
        conn.protocol_version = int(ack["protocol_version"])

    async def _handshake_acceptor(self, conn: PeerConnection, hello: dict[str, Any]) -> None:
        try:
            their_nonce = self._validate_hello(conn, hello["payload"])
        except HandshakeError as e:
            reason = str(e)
            await write_packet(conn.writer, make_packet(MsgType.ERROR, self.node.device_id, {"reason": reason}))
            raise
        my_nonce = os.urandom(16)
        payload = self._hello_payload(my_nonce)
        payload["signature"] = b64(self.node.identity.sign(their_nonce))
        await write_packet(conn.writer, make_packet(MsgType.HELLO_ACK, self.node.device_id, payload))
        auth = await read_packet(conn.reader, self.node.limits.max_packet_size)
        if auth["message_type"] != MsgType.AUTH:
            raise HandshakeError(f"ожидали AUTH, получили {auth['message_type']}")
        try:
            sig = unb64(str(auth["payload"].get("signature", "")))
        except ValueError as e:
            raise HandshakeError("некорректная подпись") from e
        if not Identity.verify(unb64(conn.public_key_b64), sig, my_nonce):
            raise HandshakeError("подпись AUTH не прошла проверку")
        conn.protocol_version = int(hello["protocol_version"])

    # ------------------------------------------------------------------ established
    async def _established(self, conn: PeerConnection) -> None:
        old = self.connections.get(conn.device_id)
        if old is not None and old.alive and old is not conn:
            # Оба узла соединились одновременно. Выживает соединение, инициированное меньшим device_id.
            new_init = self.node.device_id if conn.initiated_by_us else conn.device_id
            old_init = self.node.device_id if old.initiated_by_us else old.device_id
            if new_init > old_init:
                log.debug("дубль соединения с %s: закрываем новое", conn.short)
                conn.close()
                return
            log.debug("дубль соединения с %s: закрываем старое", conn.short)
            old.close()
        self.connections[conn.device_id] = conn
        info = self.known.get(conn.device_id)
        if info is None:
            info = PeerInfo(device_id=conn.device_id, source="connection")
            self.known[conn.device_id] = info
        host = conn.remote_addr[0]
        if host not in info.addresses:
            info.addresses.insert(0, host)
        for a in conn.addresses:
            if a not in info.addresses:
                info.addresses.append(a)
        info.port = conn.port or info.port
        info.session_id = conn.session_id
        info.public_key = conn.public_key_b64
        info.display_name = conn.display_name
        info.last_seen = time.time()
        info.failures = 0
        self.node.store.upsert_peer(conn.device_id, conn.public_key_b64, fingerprint(unb64(conn.public_key_b64)),
                                    info.addresses, info.port, conn.display_name, conn.session_id)
        log.info("соединение установлено: %s (%s:%s, %s, v%s)", conn.short, host, conn.port,
                 "исходящее" if conn.initiated_by_us else "входящее", conn.app_version)
        self.node.bus.publish("peer.connected", {"device_id": conn.device_id, "display_name": conn.display_name,
                                                 "address": host})
        asyncio.create_task(self._reader_loop(conn), name=f"reader-{conn.device_id[:8]}")
        await conn.send(make_packet(MsgType.PEER_LIST, self.node.device_id, {"peers": self._peer_list_payload(conn)}))
        for cb in self.on_connected:
            try:
                await cb(conn)
            except Exception:  # noqa: BLE001
                log.exception("on_connected callback")

    def _peer_list_payload(self, exclude: PeerConnection) -> list[dict[str, Any]]:
        out = []
        for c in self.connections.values():
            if c is exclude or not c.alive:
                continue
            out.append({"device_id": c.device_id, "addresses": [c.remote_addr[0], *c.addresses], "port": c.port,
                        "public_key": c.public_key_b64, "display_name": c.display_name, "session_id": c.session_id})
        return out

    async def _reader_loop(self, conn: PeerConnection) -> None:
        max_size = self.node.limits.max_packet_size
        try:
            while conn.alive:
                try:
                    packet = await read_packet(conn.reader, max_size)
                except VersionMismatch as e:
                    log.warning("%s: пакет несовместимой версии %s", conn.short, e.remote_version)
                    await conn.send(make_packet(MsgType.PROTOCOL_VERSION_MISMATCH, self.node.device_id,
                                                {"protocol_version": PROTOCOL_VERSION}))
                    break
                except ProtocolError as e:
                    log.warning("%s: некорректный пакет проигнорирован: %s", conn.short, e)
                    continue
                except ConnectionClosed:
                    break
                conn.last_rx = time.time()
                mtype = packet["message_type"]
                if mtype == MsgType.PING:
                    await conn.send(make_packet(MsgType.PONG, self.node.device_id, {}, packet["request_id"]))
                elif mtype == MsgType.PONG:
                    if conn._ping_sent:
                        conn.rtt = time.time() - conn._ping_sent
                elif mtype == MsgType.GOODBYE:
                    break
                elif mtype == MsgType.PEER_LIST:
                    self._on_peer_list(conn, packet["payload"])
                elif mtype == MsgType.PROTOCOL_VERSION_MISMATCH:
                    log.error("%s сообщает о несовместимости протокола (у него v%s)", conn.short,
                              packet["payload"].get("protocol_version"))
                    self.node.bus.publish("protocol.mismatch", {"device_id": conn.device_id})
                    break
                else:
                    handler = self.handlers.get(mtype)
                    if handler is None:
                        log.debug("%s: неизвестный тип %s проигнорирован", conn.short, mtype)
                        continue
                    try:
                        await handler(conn, packet)
                    except Exception:  # noqa: BLE001
                        log.exception("обработчик %s упал (пакет от %s)", mtype, conn.short)
        finally:
            await self._unregister(conn)

    def _on_peer_list(self, conn: PeerConnection, payload: dict[str, Any]) -> None:
        for p in payload.get("peers", []):
            if not isinstance(p, dict) or "device_id" not in p:
                continue
            self.learn(PeerInfo(device_id=str(p["device_id"]),
                                addresses=[str(a) for a in p.get("addresses", []) if isinstance(a, str)],
                                port=int(p.get("port") or 0), session_id=p.get("session_id"),
                                public_key=str(p.get("public_key", "")), display_name=str(p.get("display_name", "")),
                                source="peerlist", last_seen=time.time()))

    async def _unregister(self, conn: PeerConnection) -> None:
        conn.close()
        await close_writer(conn.writer)
        if self.connections.get(conn.device_id) is conn:
            del self.connections[conn.device_id]
            log.info("соединение закрыто: %s", conn.short)
            self.node.bus.publish("peer.disconnected", {"device_id": conn.device_id, "display_name": conn.display_name})
            info = self.known.get(conn.device_id)
            if info:
                info.next_attempt = time.time() + 1.0
            for cb in self.on_disconnected:
                try:
                    await cb(conn)
                except Exception:  # noqa: BLE001
                    log.exception("on_disconnected callback")

    # ------------------------------------------------------------------ loops
    async def _heartbeat_loop(self) -> None:
        interval = self.node.limits.heartbeat_interval
        while True:
            await asyncio.sleep(interval)
            now = time.time()
            for c in list(self.connections.values()):
                if now - c.last_rx > self.node.limits.heartbeat_timeout:
                    log.warning("%s: нет ответа %.0f c — закрываем", c.short, now - c.last_rx)
                    c.close()
                    continue
                c._ping_sent = now
                await c.send(make_packet(MsgType.PING, self.node.device_id))

    async def _reconnect_loop(self) -> None:
        while True:
            await asyncio.sleep(1.0 + random.random())
            if self.paused or not self.node.session_id:
                continue
            now = time.time()
            for info in list(self.known.values()):
                if info.device_id in self.connections or info.device_id in self._connecting:
                    continue
                if info.session_id != self.node.session_id or not info.addresses or not info.port:
                    continue
                if info.next_attempt > now or self.blocked_until.get(info.device_id, 0) > now:
                    continue
                if now - info.last_seen > 600 and info.failures >= 5:
                    continue  # давно не виден и недоступен — не долбим сеть
                asyncio.create_task(self.connect_known(info), name=f"connect-{info.device_id[:8]}")
            for key, (host, port) in list(self.manual.items()):
                if any(c.remote_addr[0] == host and c.port == port for c in self.connections.values()):
                    continue
                if key in self._connecting:
                    continue
                asyncio.create_task(self._connect_manual(key, host, port), name=f"manual-{key}")
            await asyncio.sleep(4.0)

    async def _connect_manual(self, key: str, host: str, port: int) -> None:
        try:
            c = await self.connect(host, port)
            if c:
                self.manual.pop(key, None)   # дальше пир известен по device_id
        except HandshakeError as e:
            log.info("ручное подключение %s: %s", key, e)
            self.node.bus.publish("peer.manual_failed", {"address": key, "error": str(e)})

    # ------------------------------------------------------------------ send helpers
    async def broadcast(self, packet: dict[str, Any], exclude: str | None = None) -> int:
        conns = [c for c in self.connections.values() if c.alive and c.device_id != exclude]
        if not conns:
            return 0
        results = await asyncio.gather(*(c.send(packet) for c in conns), return_exceptions=True)
        return sum(1 for r in results if r is True)

    def connected_ids(self) -> list[str]:
        return [d for d, c in self.connections.items() if c.alive]

    def get(self, device_id: str) -> PeerConnection | None:
        c = self.connections.get(device_id)
        return c if c and c.alive else None

    def best_address(self, device_id: str) -> tuple[str, int] | None:
        """Адрес, по которому пир точно достижим: адрес живого управляющего соединения + его порт."""
        c = self.get(device_id)
        if c and c.port:
            return c.remote_addr[0], c.port
        info = self.known.get(device_id)
        if info and info.addresses and info.port:
            return info.addresses[0], info.port
        return None
