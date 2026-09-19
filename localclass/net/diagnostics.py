"""Диагностика сети (ТЗ 6): часть MVP. «Обнаружен» ≠ «доступен»; никаких тихих ошибок."""
from __future__ import annotations

import asyncio
import ipaddress
import time
from typing import TYPE_CHECKING, Any

from .interfaces import list_interfaces

if TYPE_CHECKING:
    from ..core.node import Node


async def run(node: "Node") -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    ifaces = list_interfaces(include_loopback=node.config.dev_mode, disabled=node.settings.disabled_interfaces)
    active = [i for i in ifaces if i.enabled and not i.loopback]
    vpn_only = bool(active) and all(i.hint.startswith("VPN") for i in active)

    checks.append({"name": "Сетевой интерфейс с IPv4", "ok": bool(active) or node.config.dev_mode,
                   "detail": ", ".join(f"{i.ip} ({i.name}{' — ' + i.hint if i.hint else ''})" for i in active) or "нет"})
    d = node.discovery
    checks.append({"name": "UDP broadcast отправлен", "ok": d.broadcast_ok and not d.broadcast_error and d.last_broadcast_at > 0,
                   "detail": d.broadcast_error or (f"порт {node.discovery_port}, принято пакетов: {d.received_count}"
                                                   if d.broadcast_ok else "выключен в настройках")})
    checks.append({"name": "mDNS доступен", "ok": d.mdns_ok,
                   "detail": d.mdns_error or ("выключен в настройках" if not node.settings.enable_mdns else "работает")})
    tcp_ok = node.mesh.listen_port > 0
    self_test = ""
    if tcp_ok:
        try:
            r, w = await asyncio.wait_for(asyncio.open_connection("127.0.0.1", node.mesh.listen_port), 2)
            w.close()
            self_test = "порт отвечает"
        except (OSError, asyncio.TimeoutError) as e:
            tcp_ok = False
            self_test = f"локальное подключение не удалось: {e}"
    checks.append({"name": "TCP-порт слушается", "ok": tcp_ok, "detail": node.mesh.listen_error or f"порт {node.mesh.listen_port}; {self_test}"})
    checks.append({"name": "Сессия выбрана", "ok": bool(node.session_id),
                   "detail": (node.current_session() or {}).get("name", "") if node.session_id else "создайте или присоединитесь к сессии"})

    now = time.time()
    same = [p for p in node.mesh.known.values() if p.session_id == node.session_id and node.session_id]
    other = [p for p in node.mesh.known.values() if p.session_id != node.session_id and now - p.last_seen < 60]
    connected = set(node.mesh.connected_ids())
    reachable = [p for p in same if p.device_id in connected]
    unreachable = [p for p in same if p.device_id not in connected and (p.failures >= 1 or now - p.last_seen > 30)]

    reasons: list[str] = []
    if not active and not node.config.dev_mode:
        reasons.append("нет активного сетевого подключения (Wi-Fi / Ethernet)")
    if vpn_only:
        reasons.append("маршрутизация через VPN: единственный активный интерфейс похож на VPN")
    if any(i.hint in ("VirtualBox", "Docker", "VMware", "Hyper-V", "WSL") for i in active):
        reasons.append("виртуальные адаптеры (VirtualBox/Docker/Hyper-V) участвуют в discovery — отключите их в настройках")
    if node.session_id and not same:
        if other:
            reasons.append(f"узлы в сети есть ({len(other)}), но в других сессиях — проверьте код сессии")
        else:
            reasons.append("изоляция клиентов на точке доступа (AP/client isolation)")
            reasons.append(f"блокировка Firewall: UDP {node.discovery_port} и TCP {node.mesh.listen_port} должны быть разрешены")
            reasons.append("разные подсети: все компьютеры должны быть в одной сети Wi-Fi/LAN")
    if unreachable:
        nets = [i.network for i in active]
        foreign = 0
        for p in unreachable:
            try:
                if p.addresses and not any(ipaddress.ip_address(p.addresses[0]) in n for n in nets):
                    foreign += 1
            except ValueError:
                pass
        reasons.append("узлы обнаружены, но недоступны по TCP: блокировка Firewall на этом или удалённом компьютере "
                       f"(TCP {node.mesh.listen_port}) либо изоляция клиентов на точке доступа")
        if foreign:
            reasons.append(f"разные подсети: {foreign} узл. из другой подсети (broadcast виден, прямой TCP — нет)")
    if node.mesh.listen_error:
        reasons.append("TCP-порт занят другим приложением — измените порт в настройках")

    return {
        "checks": checks,
        "discovered": len(same),
        "reachable": len(reachable),
        "unreachable": len(unreachable),
        "other_sessions": len(other),
        "reasons": reasons,
        "interfaces": [{"name": i.name, "ip": i.ip, "hint": i.hint, "enabled": i.enabled, "loopback": i.loopback} for i in ifaces],
        "peers": [{"device_id": p.device_id, "display_name": p.display_name, "status": node.mesh.peer_status(p.device_id),
                   "addresses": p.addresses, "port": p.port, "error": p.last_error, "source": p.source} for p in same],
        "ts": now,
    }
