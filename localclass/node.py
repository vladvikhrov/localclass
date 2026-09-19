"""Точка входа без GUI (ТЗ 15.1): `python -m localclass.node --device A --port 5001 --data ./run/A`."""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import signal
import sys
import threading
from pathlib import Path

from . import __version__
from .config import NodeConfig, Settings
from .core.node import Node, PermissionDenied
from .files.transfer import TransferError

HELP = """Команды:
  /status              состояние узла        /peers          пиры и их статус
  /diag                диагностика сети      /connect IP:PORT ручное подключение
  /create NAME         создать сессию        /join CODE      подключиться по коду
  /joinid SESSION_ID   подключиться по id    /qr             показать содержимое QR
  /leave               выйти из сессии       /sessions       сессии, видимые в сети
  /send TEXT           сообщение в #general  /ch NAME TEXT   сообщение в канал
  /link URL            поделиться ссылкой    /msgs [CH]      история канала
  /share PATH          опубликовать файл     /files          файлы сессии
  /get FILE_ID         скачать файл          /transfers      передачи
  /events              журнал событий        /log            последние строки тех. лога
  /fault A B loss 0.2  fault injection (ids) /quit           выход
Текст без слэша отправляется в #general."""


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="localclass-node", description=f"LocalClass {__version__} — узел без GUI")
    p.add_argument("--device", default="", help="метка узла (dev-режим), например A")
    p.add_argument("--port", type=int, default=None, help="TCP-порт узла")
    p.add_argument("--data", default=None, help="каталог данных узла")
    p.add_argument("--discovery-port", type=int, default=None)
    p.add_argument("--name", default="", help="отображаемое имя")
    p.add_argument("--dev", action="store_true", help="dev-режим: loopback участвует в discovery")
    p.add_argument("--no-discovery", action="store_true")
    p.add_argument("--no-mdns", action="store_true")
    p.add_argument("--create-session", metavar="NAME")
    p.add_argument("--join-code", metavar="CODE")
    p.add_argument("--join-session", metavar="SESSION_ID")
    p.add_argument("--connect", action="append", default=[], metavar="HOST:PORT")
    p.add_argument("--no-repl", action="store_true", help="не читать команды со stdin (сервисный режим)")
    p.add_argument("--verbose", "-v", action="store_true", help="технический лог в консоль")
    return p


def make_node(args: argparse.Namespace) -> Node:
    from .config import default_data_dir
    data = Path(args.data) if args.data else default_data_dir()
    cfg = NodeConfig(data_dir=data, device_label=args.device, listen_port=args.port, discovery_port=args.discovery_port,
                     dev_mode=args.dev or bool(args.device), enable_discovery=not args.no_discovery)
    settings = Settings.load(cfg.data_dir / "config" / "settings.json")
    if args.name:
        settings.display_name = args.name
    if args.no_mdns:
        settings.enable_mdns = False
    return Node(cfg, settings)


async def run(args: argparse.Namespace) -> None:
    node = make_node(args)
    if args.verbose:
        h = logging.StreamHandler(sys.stderr)
        h.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
        logging.getLogger("localclass").addHandler(h)

    def on_bus(topic: str, data: dict) -> None:
        if topic == "event" and not data.get("local"):
            ev = data["event"]
            if ev["type"] == "MESSAGE_CREATED":
                print(f"\r[{ev['payload'].get('channel')}] {node._name_of(ev['device_id'])}: {ev['payload'].get('text')}")
        elif topic in ("peer.connected", "peer.disconnected", "peer.unreachable", "security.warning", "transfer.done",
                       "transfer.failed", "protocol.mismatch"):
            print(f"\r* {topic}: {data}")
    node.bus.subscribe(on_bus)
    await node.start()
    print(f"LocalClass node {node.display_name} id={node.device_id[:16]} port={node.mesh.listen_port} data={node.paths.root}")
    for hp in args.connect:
        host, _, port = hp.rpartition(":")
        await node.connect_manual(host, int(port))
    if args.create_session:
        s = await node.create_session(args.create_session)
        print(f"сессия создана: {s['name']} код={s['code']} id={s['session_id']}")
    elif args.join_session:
        await node.join_session(args.join_session)
    elif args.join_code:
        for _ in range(10):
            try:
                s = await node.join_by_code(args.join_code)
                print(f"подключились к сессии {s.get('name')} ({s['session_id'][:8]})")
                break
            except PermissionDenied as e:
                print(f"... {e}; ждём discovery")
                await asyncio.sleep(2)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    if sys.platform != "win32":
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop.set)

    if not args.no_repl:
        q: asyncio.Queue[str | None] = asyncio.Queue()

        def reader() -> None:
            for line in sys.stdin:
                loop.call_soon_threadsafe(q.put_nowait, line.rstrip("\n"))
            loop.call_soon_threadsafe(q.put_nowait, None)
        threading.Thread(target=reader, daemon=True).start()
        print(HELP)

        async def repl() -> None:
            while True:
                line = await q.get()
                if line is None:
                    stop.set()
                    return
                try:
                    if await handle(node, line):
                        stop.set()
                        return
                except (PermissionDenied, TransferError, ValueError) as e:
                    print(f"! {e}")
                except Exception as e:  # noqa: BLE001
                    print(f"! ошибка: {e!r}")
        asyncio.create_task(repl())
    try:
        await stop.wait()
    finally:
        await node.stop()


async def handle(node: Node, line: str) -> bool:
    line = line.strip()
    if not line:
        return False
    if not line.startswith("/"):
        await node.send_message(line)
        return False
    cmd, _, arg = line[1:].partition(" ")
    if cmd in ("quit", "exit"):
        return True
    if cmd == "help":
        print(HELP)
    elif cmd == "status":
        print(json.dumps(node.status(), ensure_ascii=False, indent=1, default=str))
    elif cmd == "peers":
        for p in node.peers_view():
            print(f"  {p['status']:12} {p['display_name']:16} {p['device_id'][:8]} {p['address'] or p['addresses']}:{p['port']} {p['error']}")
        if not node.peers_view():
            print("  (пиры не обнаружены)")
    elif cmd == "diag":
        r = await node.run_diagnostics()
        for c in r["checks"]:
            print(f"  [{'✓' if c['ok'] else '✗'}] {c['name']:32} {c['detail']}")
        print(f"  обнаружено {r['discovered']}, доступно {r['reachable']}, недоступно {r['unreachable']}")
        for reason in r["reasons"]:
            print(f"   • {reason}")
    elif cmd == "connect":
        host, _, port = arg.rpartition(":")
        await node.connect_manual(host, int(port))
    elif cmd == "create":
        s = await node.create_session(arg or "Сессия")
        print(f"сессия: {s['name']} код={s['code']} id={s['session_id']}")
    elif cmd == "join":
        s = await node.join_by_code(arg)
        print(f"подключились: {s}")
    elif cmd == "joinid":
        print(await node.join_session(arg))
    elif cmd == "leave":
        await node.leave_session()
    elif cmd == "qr":
        print(node.qr_text())
    elif cmd == "sessions":
        for s in node.sessions_in_network():
            print(f"  {s['code']}  {s['name']}  {s['session_id']}")
    elif cmd == "send":
        await node.send_message(arg)
    elif cmd == "ch":
        ch, _, text = arg.partition(" ")
        await node.send_message(text, channel=ch)
    elif cmd == "link":
        await node.send_link(arg)
    elif cmd == "msgs":
        for m in node.store.messages(node.session_id, arg or "general"):
            print(f"  [{m['lamport']}] {m['display_name'] or m['device_id'][:8]}: {m['text']}")
    elif cmd == "share":
        fid = await node.transfers.share_file(arg)
        print(f"file_id={fid}")
    elif cmd == "files":
        for f in node.store.shared_files(node.session_id):
            print(f"  {f['file_id']}  {f['filename']}  {f['size']} B  от {f['owner_name'] or f['owner_id'][:8]}  {f['local_path'] or ''}")
    elif cmd == "get":
        print(await node.transfers.download(arg))
    elif cmd == "transfers":
        for t in node.store.transfers(node.session_id):
            print(f"  {t['direction']:3} {t['status']:7} {t['filename']} {t['transfer_id'][:8]} {t['error'] or ''}")
    elif cmd == "events":
        for r in list(node.eventlog.records)[-30:]:
            print(f"  {r['type']:20} {r['text']}")
    elif cmd == "log":
        for r in list(node.app_log.records)[-30:]:
            print(f"  {r['level']:7} {r['category']:8} {r['message']}")
    elif cmd == "fault":
        a, b, kind, val = arg.split()
        node.faults.set(a, b, **{kind: float(val) if kind != "partition" else val.lower() in ("1", "true", "on")})
        print("ok")
    else:
        print("неизвестная команда, /help")
    return False


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
