"""Перечисление сетевых интерфейсов (ТЗ 5.3).

Список НЕ отсортирован по полезности и не фильтруется эвристикой — рабочий адрес определяется только
фактически установленным соединением. Здесь лишь подсказки для пользователя (VirtualBox/Docker/VPN) и
возможность вручную отключить интерфейс в настройках.
"""
from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass

import ifaddr


@dataclass
class Interface:
    name: str
    ip: str
    prefix: int
    broadcast: str
    loopback: bool = False
    hint: str = ""          # подсказка: "VirtualBox", "Docker", "VPN?" — только для отображения
    enabled: bool = True

    @property
    def network(self) -> ipaddress.IPv4Network:
        return ipaddress.ip_network(f"{self.ip}/{self.prefix}", strict=False)


_HINTS = (
    ("192.168.56.", "VirtualBox"),
    ("172.17.", "Docker"),
    ("172.18.", "Docker"),
    ("10.8.", "VPN?"),
    ("100.64.", "VPN/CGNAT?"),
    ("169.254.", "APIPA (нет DHCP)"),
)
_NAME_HINTS = (("vbox", "VirtualBox"), ("virtualbox", "VirtualBox"), ("docker", "Docker"), ("br-", "Docker"),
               ("tun", "VPN?"), ("tap", "VPN?"), ("wg", "VPN?"), ("zt", "VPN?"), ("vmware", "VMware"),
               ("hyper-v", "Hyper-V"), ("vethernet", "Hyper-V"), ("wsl", "WSL"))


def _hint(name: str, ip: str) -> str:
    low = name.lower()
    for k, h in _NAME_HINTS:
        if k in low:
            return h
    for pfx, h in _HINTS:
        if ip.startswith(pfx):
            return h
    return ""


def list_interfaces(include_loopback: bool = False, disabled: list[str] | None = None) -> list[Interface]:
    disabled = set(disabled or [])
    out: list[Interface] = []
    seen: set[str] = set()
    for adapter in ifaddr.get_adapters():
        for ipinfo in adapter.ips:
            if not isinstance(ipinfo.ip, str):
                continue  # IPv6 (tuple) — не используем
            ip = ipinfo.ip
            if ip in seen:
                continue
            try:
                net = ipaddress.ip_network(f"{ip}/{ipinfo.network_prefix}", strict=False)
            except ValueError:
                continue
            loop = ip.startswith("127.")
            if loop and not include_loopback:
                continue
            seen.add(ip)
            name = adapter.nice_name or adapter.name
            if isinstance(name, bytes):
                name = name.decode(errors="replace")
            key = f"{name}|{ip}"
            out.append(Interface(name=name, ip=ip, prefix=ipinfo.network_prefix,
                                 broadcast=str(net.broadcast_address), loopback=loop,
                                 hint="loopback" if loop else _hint(name, ip),
                                 enabled=key not in disabled and name not in disabled))
    return out


def interface_key(iface: Interface) -> str:
    return f"{iface.name}|{iface.ip}"


def default_route_ip() -> str:
    """IP интерфейса, через который идёт маршрут по умолчанию (шлюз).

    UDP-сокет только «подключается» к внешнему адресу — ни одного пакета не отправляется, интернет не нужен:
    ядро само выбирает исходящий интерфейс по таблице маршрутизации. Работает одинаково на Windows и Linux.
    """
    for probe in ("8.8.8.8", "192.168.1.1"):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.settimeout(0.3)
                s.connect((probe, 9))
                ip = s.getsockname()[0]
            if ip and not ip.startswith("0."):
                return ip
        except OSError:
            continue
    return ""


def preferred_interface(interfaces: list[Interface] | None = None) -> Interface | None:
    """Интерфейс со шлюзом по умолчанию — то, что приложение выбирает автоматически."""
    ifaces = interfaces if interfaces is not None else list_interfaces()
    ip = default_route_ip()
    if ip:
        for i in ifaces:
            if i.ip == ip:
                return i
    real = [i for i in ifaces if not i.loopback and not i.hint]
    return real[0] if real else (ifaces[0] if ifaces else None)


def virtual_interface_keys(interfaces: list[Interface] | None = None) -> list[str]:
    """Ключи виртуальных адаптеров (VirtualBox, Docker, Hyper-V, VMware, WSL, VPN) — мусор для discovery."""
    ifaces = interfaces if interfaces is not None else list_interfaces()
    preferred = preferred_interface(ifaces)
    out = []
    for i in ifaces:
        if i.loopback or (preferred and i.ip == preferred.ip):
            continue
        if i.hint in ("VirtualBox", "Docker", "VMware", "Hyper-V", "WSL") or i.hint.startswith("VPN"):
            out.append(interface_key(i))
    return out


def local_addresses(include_loopback: bool = False, disabled: list[str] | None = None) -> list[str]:
    """Включённые адреса; первым — адрес интерфейса со шлюзом по умолчанию (наиболее вероятно рабочий).

    Это порядок предпочтения для discovery, а не утверждение: рабочий адрес подтверждается
    только фактически установленным соединением (ТЗ 5.3).
    """
    ifaces = [i for i in list_interfaces(include_loopback, disabled) if i.enabled]
    gw = default_route_ip()
    ifaces.sort(key=lambda i: (i.ip != gw, i.loopback, bool(i.hint)))
    return [i.ip for i in ifaces]


def hostname() -> str:
    try:
        return socket.gethostname()
    except OSError:
        return "pc"
