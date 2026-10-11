from localclass.net import interfaces
from localclass.net.interfaces import Interface, interface_key, preferred_interface, virtual_interface_keys


def _ifaces():
    return [
        Interface("Happ Tunnel", "172.19.0.1", 30, "172.19.0.3", hint=interfaces._hint("Happ Tunnel", "172.19.0.1")),
        Interface("Microsoft Wi-Fi Direct Virtual Adapter", "169.254.231.34", 16, "169.254.255.255",
                  hint=interfaces._hint("Microsoft Wi-Fi Direct Virtual Adapter", "169.254.231.34")),
        Interface("Intel(R) Wireless-AC 9560 160MHz", "192.168.1.121", 24, "192.168.1.255"),
    ]


def test_vpn_default_route_is_not_preferred(monkeypatch):
    """Полнотуннельный VPN забрал шлюз по умолчанию — выбираем Wi-Fi, а VPN и APIPA отключаем."""
    monkeypatch.setattr(interfaces, "default_route_ip", lambda: "172.19.0.1")
    ifaces = _ifaces()
    assert preferred_interface(ifaces).ip == "192.168.1.121"
    assert set(virtual_interface_keys(ifaces)) == {interface_key(ifaces[0]), interface_key(ifaces[1])}


def test_real_default_route_is_preferred(monkeypatch):
    monkeypatch.setattr(interfaces, "default_route_ip", lambda: "192.168.1.121")
    assert preferred_interface(_ifaces()).ip == "192.168.1.121"
