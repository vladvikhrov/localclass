"""Discovery (ТЗ 5): UDP broadcast (основной), mDNS (резервный), ручной ввод IP:Port (аварийный).

Собственный device_id всегда отбрасывается — фильтрация по device_id, а не по IP (несколько узлов на одном ПК).
UDP-сокет открывается с SO_REUSEADDR (+SO_REUSEPORT вне Windows), чтобы несколько экземпляров на одной машине
делили порт discovery.
"""
from __future__ import annotations

import asyncio
import json
import logging
import socket
import time
from typing import TYPE_CHECKING, Any

from .interfaces import list_interfaces
from .mesh import PeerInfo
from .protocol import PROTOCOL_VERSION, MAX_COMPATIBLE, MIN_COMPATIBLE

if TYPE_CHECKING:
    from ..core.node import Node

log = logging.getLogger("localclass.net.discovery")
MDNS_TYPE = "_localclass._tcp.local."


class _UDPProtocol(asyncio.DatagramProtocol):
    def __init__(self, discovery: "Discovery"):
        self.d = discovery

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        self.d._on_datagram(data, addr)

    def error_received(self, exc: Exception) -> None:
        log.debug("udp error: %s", exc)


class Discovery:
    def __init__(self, node: "Node"):
        self.node = node
        self._transport: asyncio.DatagramTransport | None = None
        self._task: asyncio.Task | None = None
        self.broadcast_ok = False
        self.broadcast_error = ""
        self.last_broadcast_at = 0.0
        self.received_count = 0
        self.mdns_ok = False
        self.mdns_error = ""
        self._zc = None
        self._browser = None
        self._mdns_info = None

    # ------------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        if self.node.settings.enable_broadcast:
            await self._start_udp()
        if self.node.settings.enable_mdns:
            await self._start_mdns()
        self._task = asyncio.create_task(self._announce_loop(), name="discovery-announce")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
        if self._transport:
            self._transport.close()
        await self._stop_mdns()

    # ------------------------------------------------------------------ UDP broadcast
    async def _start_udp(self) -> None:
        port = self.node.discovery_port
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if hasattr(socket, "SO_REUSEPORT"):
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            sock.bind(("", port))
            sock.setblocking(False)
            loop = asyncio.get_running_loop()
            self._transport, _ = await loop.create_datagram_endpoint(lambda: _UDPProtocol(self), sock=sock)
            self.broadcast_ok = True
            log.info("UDP discovery слушает порт %s", port)
        except OSError as e:
            self.broadcast_ok = False
            self.broadcast_error = str(e)
            log.error("UDP discovery не запущен (порт %s): %s", port, e)

    def packet(self) -> dict[str, Any]:
        s = self.node.current_session()
        return {
            "protocol_version": PROTOCOL_VERSION,
            "device_id": self.node.device_id,
            "public_key": self.node.identity.public_key_b64,
            "session_id": self.node.session_id,
            "session_code": s["code"] if s else "",
            "session_name": s["name"] if s else "",
            "session_teacher": self.node.session_teacher_name() if s else "",
            "session_members": self.node.session_member_count() if s else 0,
            "display_name": self.node.display_name,
            "port": self.node.mesh.listen_port,
            "addresses": self.node.local_addresses(),
        }

    def announce_once(self) -> None:
        if not self._transport:
            return
        data = json.dumps(self.packet(), ensure_ascii=False).encode("utf-8")
        targets = {("255.255.255.255", self.node.discovery_port)}
        for iface in list_interfaces(include_loopback=self.node.config.dev_mode,
                                     disabled=self.node.settings.disabled_interfaces):
            if iface.enabled:
                targets.add((iface.broadcast, self.node.discovery_port))
        sent = 0
        err = ""
        for t in targets:
            try:
                self._transport.sendto(data, t)
                sent += 1
            except OSError as e:
                err = f"{t[0]}: {e}"
        if sent:
            self.last_broadcast_at = time.time()
            self.broadcast_error = ""
        else:
            self.broadcast_error = err or "нет интерфейсов для broadcast"

    async def _announce_loop(self) -> None:
        while True:
            try:
                self.announce_once()
            except Exception:  # noqa: BLE001
                log.exception("announce")
            await asyncio.sleep(self.node.limits.discovery_interval)

    def _on_datagram(self, data: bytes, addr: tuple[str, int]) -> None:
        try:
            p = json.loads(data.decode("utf-8"))
            if not isinstance(p, dict):
                return
            v = p.get("protocol_version")
            if not isinstance(v, int) or v < MIN_COMPATIBLE or v > MAX_COMPATIBLE:
                log.debug("discovery от %s: несовместимая версия %s", addr, v)
                return
            device_id = str(p["device_id"])
        except (ValueError, KeyError, TypeError):
            return
        if device_id == self.node.device_id:
            return  # свой broadcast (на Windows приходит всегда)
        self.received_count += 1
        addresses = [a for a in p.get("addresses", []) if isinstance(a, str)]
        if addr[0] not in addresses:
            addresses.insert(0, addr[0])   # адрес, с которого реально пришёл пакет — самый ценный
        self._seen(device_id, addresses, p, "broadcast")

    def _seen(self, device_id: str, addresses: list[str], p: dict[str, Any], source: str) -> None:
        info = PeerInfo(
            device_id=device_id, addresses=addresses, port=int(p.get("port") or 0),
            session_id=p.get("session_id") or None, public_key=str(p.get("public_key", "")),
            display_name=str(p.get("display_name", ""))[:64], session_code=str(p.get("session_code", "")),
            session_name=str(p.get("session_name", ""))[:64], source=source, last_seen=time.time(),
            session_teacher=str(p.get("session_teacher", ""))[:64],
            session_members=int(p.get("session_members") or 0),
        )
        self.node.on_peer_discovered(info)

    # ------------------------------------------------------------------ mDNS
    async def _start_mdns(self) -> None:
        try:
            from zeroconf import IPVersion, ServiceStateChange
            from zeroconf.asyncio import AsyncServiceBrowser, AsyncServiceInfo, AsyncZeroconf

            self._zc = AsyncZeroconf(ip_version=IPVersion.V4Only)
            self._mdns_info = self._make_mdns_info()
            name = self._mdns_info.name
            await self._zc.async_register_service(self._mdns_info, allow_name_change=True)
            loop = asyncio.get_running_loop()

            def on_change(zeroconf, service_type, name, state_change):  # вызывается из потока zeroconf
                if state_change in (ServiceStateChange.Added, ServiceStateChange.Updated):
                    asyncio.run_coroutine_threadsafe(self._mdns_resolve(name, AsyncServiceInfo), loop)

            self._browser = AsyncServiceBrowser(self._zc.zeroconf, MDNS_TYPE, handlers=[on_change])
            self.mdns_ok = True
            log.info("mDNS: сервис зарегистрирован (%s)", name)
        except Exception as e:  # noqa: BLE001 — mDNS резервный, его отказ не критичен
            self.mdns_ok = False
            self.mdns_error = str(e)
            log.warning("mDNS недоступен: %s", e)

    def _mdns_props(self) -> dict[str, str]:
        p = self.packet()
        return {"did": p["device_id"], "pk": p["public_key"], "sid": p["session_id"] or "",
                "code": p["session_code"], "sname": p["session_name"], "dn": p["display_name"],
                "tname": p["session_teacher"], "mem": str(p["session_members"]), "v": str(PROTOCOL_VERSION)}

    async def _mdns_resolve(self, name: str, AsyncServiceInfo) -> None:
        try:
            info = AsyncServiceInfo(MDNS_TYPE, name)
            if not await info.async_request(self._zc.zeroconf, 3000):
                return
            props = {k.decode(): (v.decode() if isinstance(v, bytes) else "") for k, v in (info.properties or {}).items()}
            did = props.get("did", "")
            if not did or did == self.node.device_id:
                return
            v = int(props.get("v", "1"))
            if v < MIN_COMPATIBLE or v > MAX_COMPATIBLE:
                return
            addresses = info.parsed_addresses()
            self._seen(did, addresses, {"port": info.port, "session_id": props.get("sid") or None,
                                        "public_key": props.get("pk", ""), "display_name": props.get("dn", ""),
                                        "session_code": props.get("code", ""), "session_name": props.get("sname", ""),
                                        "session_teacher": props.get("tname", ""),
                                        "session_members": int(props.get("mem") or 0)},
                       "mdns")
        except Exception as e:  # noqa: BLE001
            log.debug("mdns resolve %s: %s", name, e)

    def _make_mdns_info(self):
        import socket as _socket
        from zeroconf import ServiceInfo
        addrs = [_socket.inet_aton(a) for a in self.node.local_addresses() if not a.startswith("127.")]
        name = f"lc-{self.node.device_id[:16]}.{MDNS_TYPE}"
        return ServiceInfo(MDNS_TYPE, name, addresses=addrs or [_socket.inet_aton("127.0.0.1")],
                           port=self.node.mesh.listen_port, properties=self._mdns_props(),
                           server=f"lc-{self.node.device_id[:16]}.local.")

    async def refresh_mdns(self) -> None:
        """Обновить TXT-запись (сменилась сессия/имя). ServiceInfo неизменяем — пересоздаём."""
        if self._zc and self._mdns_info:
            try:
                self._mdns_info = self._make_mdns_info()
                await self._zc.async_update_service(self._mdns_info)
            except Exception as e:  # noqa: BLE001
                log.debug("mdns update: %s", e)

    async def _stop_mdns(self) -> None:
        if self._zc:
            try:
                if self._browser:
                    await self._browser.async_cancel()
                if self._mdns_info:
                    await self._zc.async_unregister_service(self._mdns_info)
                await self._zc.async_close()
            except Exception:  # noqa: BLE001
                pass
            self._zc = None
