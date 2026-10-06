"""Передача файлов (ТЗ 12): отдельное TCP-соединение, чанки, оконный ACK, backpressure, resume из SQLite.

Модель pull: получатель всегда инициирует файловое соединение (FILE_RESUME с картой имеющихся чанков),
владелец отвечает манифестом (FILE_OFFER) или отказом (FILE_REJECT: BUSY/NOT_FOUND), получатель просит
недостающие чанки (FILE_ACCEPT {missing}), владелец шлёт FILE_CHUNK бинарными фреймами с окном N
неподтверждённых чанков, получатель подтверждает пачками (FILE_CHUNK_ACK), в конце — FILE_FINISH.
Так resume после обрыва, закрытия приложения и перезагрузки — один и тот же путь кода.

Фреймы файлового соединения: length-prefix + 1 байт типа (0x00 JSON, 0x01 бинарный чанк: 4 байта index + данные).
"""
from __future__ import annotations

import asyncio
import logging
import struct
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..core.events import EventType
from ..core.store import Bitmap
from ..net.protocol import MsgType, ProtocolError, decode_packet, encode_packet, make_packet
from ..net.transport import (FRAME_BINARY, FRAME_JSON, ConnectionClosed, close_writer, read_frame, write_frame)
from .manifest import Manifest, build_manifest, chunk_range, file_sha256, sha256_hex, verify_existing_chunks
from .storage import atomic_move, free_space, prepare_temp, sanitize_filename, storage_usage, unique_path

if TYPE_CHECKING:
    from ..core.node import Node
    from ..net.mesh import PeerConnection

log = logging.getLogger("localclass.files.transfer")
IDX = struct.Struct("!I")
FILE_IO_TIMEOUT = 60.0
BUSY_RETRY = 10.0
MAX_ROUNDS = 12          # запросов недостающих чанков в рамках одного соединения
INCOMPLETE_RETRY = 1.0   # пауза перед новым соединением, если часть чанков так и не доехала


class TransferError(Exception):
    pass


class Progress:
    """Честная оценка времени по фактической скорости (ТЗ 12.5), а не только полоса прогресса."""

    def __init__(self, total: int, done: int = 0):
        self.total = total
        self.done = done
        self._samples: list[tuple[float, int]] = [(time.monotonic(), done)]
        self._last_publish = 0.0

    def add(self, n: int) -> None:
        self.done += n
        now = time.monotonic()
        self._samples.append((now, self.done))
        while len(self._samples) > 2 and now - self._samples[0][0] > 5.0:
            self._samples.pop(0)

    @property
    def speed(self) -> float:
        (t0, b0), (t1, b1) = self._samples[0], self._samples[-1]
        return (b1 - b0) / (t1 - t0) if t1 > t0 else 0.0

    @property
    def eta(self) -> float | None:
        s = self.speed
        return (self.total - self.done) / s if s > 0 else None

    def should_publish(self, every: float = 0.5) -> bool:
        now = time.monotonic()
        if now - self._last_publish >= every:
            self._last_publish = now
            return True
        return False

    def snapshot(self) -> dict[str, Any]:
        return {"done": self.done, "total": self.total, "speed": self.speed, "eta": self.eta,
                "percent": (100.0 * self.done / self.total) if self.total else 100.0}


async def _read(reader: asyncio.StreamReader, max_size: int) -> tuple[bytes, bytes]:
    frame = await asyncio.wait_for(read_frame(reader, max_size), FILE_IO_TIMEOUT)
    if not frame:
        raise ProtocolError("пустой фрейм")
    return frame[:1], frame[1:]


async def _send_json(writer: asyncio.StreamWriter, packet: dict[str, Any]) -> None:
    await write_frame(writer, FRAME_JSON + encode_packet(packet))


class TransferManager:
    def __init__(self, node: "Node"):
        self.node = node
        self.limits = node.limits
        self.progress: dict[str, dict[str, Any]] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._out_sem = asyncio.Semaphore(self.limits.max_concurrent_outgoing)
        self._in_sem = asyncio.Semaphore(self.limits.max_concurrent_incoming)
        self._out_active = 0
        node.mesh.handlers[MsgType.FILE_OFFER] = self.on_file_offer_packet
        node.mesh.handlers[MsgType.FILE_ACCEPT] = self._noop
        node.mesh.handlers[MsgType.FILE_REJECT] = self._on_reject_packet
        node.mesh.on_connected.append(self._on_peer_connected)

    async def _noop(self, conn: "PeerConnection", packet: dict[str, Any]) -> None:
        return None

    def stop(self) -> None:
        for t in self._tasks.values():
            t.cancel()

    # ------------------------------------------------------------------ публикация / раздача
    async def share_file(self, path: str | Path, channel: str = "general") -> str:
        """Опубликовать файл в канал: считаем манифест, сохраняем хеши, создаём событие FILE_OFFERED."""
        p = Path(path)
        if not p.is_file():
            raise TransferError(f"файл не найден: {p}")
        size = p.stat().st_size
        if size > self.limits.max_file_size:
            raise TransferError(f"файл больше лимита MAX_FILE_SIZE ({self.limits.max_file_size} байт)")
        chunk = min(self.limits.chunk_size, self.limits.max_chunk_size)
        log.info("считаем манифест %s (%d байт)", p.name, size)
        manifest = await asyncio.to_thread(build_manifest, p, chunk)
        self.node.store.save_file_chunks(manifest.file_id, manifest.chunk_hashes)
        ev = await self.node.emit(EventType.FILE_OFFERED, {**manifest.to_dict(with_hashes=False), "channel": channel})
        self.node.store.set_file_local_path(manifest.file_id, str(p.resolve()))
        self.node.eventlog.write(EventType.FILE_OFFERED, f"опубликован файл {p.name} ({size} байт) в #{channel}",
                                 category="files", event_id=ev.event_id)
        return manifest.file_id

    async def send_file_to(self, peer_id: str, path: str | Path) -> str:
        """Личная отправка: публикуем с каналом dm и напрямую предлагаем пиру (он автоматически скачает)."""
        conn = self.node.mesh.get(peer_id)
        if conn is None:
            raise TransferError("нет прямого соединения с получателем — передача невозможна (relay не поддерживается)")
        file_id = await self.share_file(path, channel=f"dm:{peer_id}")
        f = self.node.store.shared_file(file_id)
        await conn.send(make_packet(MsgType.FILE_OFFER, self.node.device_id, {
            "file_id": file_id, "filename": f["filename"], "size": f["size"], "chunk_size": f["chunk_size"],
            "chunk_count": f["chunk_count"], "full_sha256": f["full_sha256"]}))
        return file_id

    def estimate_traffic(self, size: int, recipients: int) -> dict[str, Any]:
        """Предупреждение до начала (ТЗ 12.5): «Примерно N ГБ трафика»."""
        total = size * recipients
        return {"bytes": total, "gb": total / 1024 ** 3, "recipients": recipients,
                "warn": total > 512 * 1024 ** 2}

    async def on_file_offer_packet(self, conn: "PeerConnection", packet: dict[str, Any]) -> None:
        p = packet["payload"]
        file_id = str(p.get("file_id", ""))
        f = self.node.store.shared_file(file_id)
        if not f:
            # событие FILE_OFFERED ещё не дошло — попросим sync и подождём
            await self.node.sync.request_sync(conn)
            for _ in range(20):
                await asyncio.sleep(0.25)
                f = self.node.store.shared_file(file_id)
                if f:
                    break
        if not f:
            await conn.send(make_packet(MsgType.FILE_REJECT, self.node.device_id, {"file_id": file_id, "reason": "UNKNOWN_FILE"}))
            return
        self.node.bus.publish("file.offer", {"file_id": file_id, "from": conn.device_id, "filename": f["filename"], "size": f["size"]})
        if self.node.settings.auto_accept_files:
            await conn.send(make_packet(MsgType.FILE_ACCEPT, self.node.device_id, {"file_id": file_id}))
            try:
                await self.download(file_id)
            except TransferError as e:
                await conn.send(make_packet(MsgType.FILE_REJECT, self.node.device_id, {"file_id": file_id, "reason": str(e)}))

    async def _on_reject_packet(self, conn: "PeerConnection", packet: dict[str, Any]) -> None:
        p = packet["payload"]
        self.node.bus.publish("file.rejected", {"file_id": p.get("file_id"), "by": conn.device_id, "reason": p.get("reason")})

    # ------------------------------------------------------------------ приём (pull)
    async def download(self, file_id: str) -> str:
        f = self.node.store.shared_file(file_id)
        if not f or f["deleted"]:
            raise TransferError("файл неизвестен или удалён")
        if f["owner_id"] == self.node.device_id:
            raise TransferError("это ваш собственный файл")
        if f["size"] > self.limits.max_file_size:
            raise TransferError("файл больше лимита MAX_FILE_SIZE")
        existing = self.node.store.find_transfer(file_id, "in")
        if existing and existing["status"] == "done":
            raise TransferError("файл уже получен")
        if existing and existing["transfer_id"] in self._tasks and not self._tasks[existing["transfer_id"]].done():
            return existing["transfer_id"]
        if existing:
            t = existing
        else:
            from ..core.events import new_id
            tid = new_id()
            t = {
                "transfer_id": tid, "file_id": file_id, "peer_id": f["owner_id"], "direction": "in",
                "status": "queued", "filename": sanitize_filename(f["filename"]), "size": f["size"],
                "chunk_size": f["chunk_size"], "chunk_count": f["chunk_count"], "received_chunks": "",
                "full_sha256": f["full_sha256"], "temp_path": str(self.node.paths.incoming / f"{tid}.part"),
                "final_path": None, "session_id": f["session_id"], "error": None,
            }
            self.node.store.upsert_transfer(t)
        self._tasks[t["transfer_id"]] = asyncio.create_task(self._download_task(t["transfer_id"]), name=f"dl-{file_id[:8]}")
        return t["transfer_id"]

    async def _on_peer_connected(self, conn: "PeerConnection") -> None:
        """Возобновление незавершённых приёмов от этого владельца после реконнекта."""
        for t in self.node.store.transfers():
            if t["direction"] == "in" and t["peer_id"] == conn.device_id and t["status"] in ("paused", "queued", "active"):
                if t["transfer_id"] not in self._tasks or self._tasks[t["transfer_id"]].done():
                    try:
                        await self.download(t["file_id"])
                    except TransferError as e:
                        log.info("авто-возобновление %s: %s", t["filename"], e)

    def _fail(self, t: dict[str, Any], reason: str, status: str = "failed") -> None:
        log.warning("приём %s: %s (%s)", t["filename"], reason, status)
        self.node.store.update_transfer(t["transfer_id"], status=status, error=reason)
        self.node.bus.publish("transfer.failed", {"transfer_id": t["transfer_id"], "file_id": t["file_id"],
                                                  "filename": t["filename"], "reason": reason, "status": status})

    def _publish_progress(self, t: dict[str, Any], prog: Progress, direction: str, peer_id: str, force: bool = False) -> None:
        if force or prog.should_publish():
            snap = {**prog.snapshot(), "transfer_id": t["transfer_id"], "file_id": t["file_id"], "filename": t["filename"],
                    "direction": direction, "peer_id": peer_id}
            self.progress[t["transfer_id"]] = snap
            self.node.bus.publish("transfer.progress", snap)

    async def _download_task(self, transfer_id: str) -> None:
        t = self.node.store.get_transfer(transfer_id)
        if not t:
            return
        owner = t["peer_id"]
        for attempt in range(12):
            addr = self.node.mesh.best_address(owner)
            if not addr or not self.node.mesh.get(owner):
                self._fail(t, "нет прямого соединения с владельцем файла — передача невозможна (relay не поддерживается)", "paused")
                return
            async with self._in_sem:
                try:
                    result = await self._download_once(t, addr)
                except (ConnectionClosed, asyncio.TimeoutError, OSError) as e:
                    self._fail(t, f"связь потеряна: {e or 'timeout'} — будет продолжено при восстановлении", "paused")
                    return
                except (ProtocolError, TransferError) as e:
                    self._fail(t, str(e))
                    return
            if result == "busy":
                self.node.store.update_transfer(transfer_id, status="queued", error="владелец занят, ждём очередь")
                self.node.bus.publish("transfer.queued", {"transfer_id": transfer_id, "filename": t["filename"]})
                await asyncio.sleep(BUSY_RETRY)
                continue
            if result == "incomplete":
                self.node.store.update_transfer(transfer_id, status="active", error=None)
                await asyncio.sleep(INCOMPLETE_RETRY)
                continue
            return
        self._fail(t, "не все чанки получены — передача продолжится при следующем подключении", "paused")

    async def _download_once(self, t: dict[str, Any], addr: tuple[str, int]) -> str:
        host, port = addr
        tid = t["transfer_id"]
        max_frame = self.limits.max_chunk_size + 16
        reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), 5)
        try:
            bitmap = Bitmap(t["chunk_count"], t["received_chunks"])
            await _send_json(writer, make_packet(MsgType.FILE_RESUME, self.node.device_id, {
                "file_id": t["file_id"], "transfer_id": tid, "have": bitmap.hex(), "session_id": t["session_id"]}))
            kind, body = await _read(reader, max_frame)
            if kind != FRAME_JSON:
                raise ProtocolError("ожидали JSON-пакет")
            pkt = decode_packet(body)
            if pkt["message_type"] == MsgType.FILE_REJECT:
                reason = str(pkt["payload"].get("reason", "?"))
                if reason == "BUSY":
                    return "busy"
                raise TransferError(f"владелец отказал: {reason}")
            if pkt["message_type"] != MsgType.FILE_OFFER:
                raise ProtocolError(f"неожиданный пакет {pkt['message_type']}")
            manifest = Manifest.from_dict(pkt["payload"])
            if manifest.size != t["size"] or manifest.full_sha256 != t["full_sha256"] or manifest.chunk_size != t["chunk_size"]:
                raise TransferError("манифест не совпадает с объявленным файлом")
            if not manifest.chunk_hashes:
                raise TransferError("манифест без хешей чанков")
            if manifest.chunk_size > self.limits.max_chunk_size:
                raise TransferError("chunk_size больше MAX_CHUNK_SIZE")
            self.node.store.save_chunk_hashes(tid, manifest.chunk_hashes)

            temp = Path(t["temp_path"])
            # проверка места и лимита хранилища ДО приёма
            need = manifest.size - (temp.stat().st_size if temp.exists() else 0)
            if free_space(self.node.paths.incoming) < need + 64 * 1024 ** 2:
                raise TransferError("недостаточно свободного места на диске")
            if storage_usage(self.node.paths.files) + manifest.size > self.limits.max_storage_size:
                raise TransferError("превышен MAX_STORAGE_SIZE хранилища принятых файлов")
            # resume: проверяем целостность уже лежащих чанков (после перезапуска доверяем только хешам)
            if temp.exists() and bitmap.received():
                good = set(await asyncio.to_thread(verify_existing_chunks, temp, manifest))
                bitmap = Bitmap(manifest.chunk_count)
                for i in good:
                    bitmap.set(i)
            await asyncio.to_thread(prepare_temp, temp, manifest.size)
            self.node.store.update_transfer(tid, status="active", error=None, received_chunks=bitmap.hex())

            prog = Progress(manifest.size, sum(chunk_range(i, manifest.size, manifest.chunk_size)[1]
                                               for i in range(manifest.chunk_count) if bitmap.has(i)))
            self._publish_progress(t, prog, "in", t["peer_id"], force=True)
            ack_every = max(1, self.limits.ack_window // 4)

            with open(temp, "r+b") as fh:
                # повторяем, пока раунд приносит новые чанки: при потерях в канале фиксированное
                # число попыток не гарантирует доставку (ТЗ 19.2 — «в итоге доходит всё»)
                stalled = 0
                for _round in range(MAX_ROUNDS):
                    missing = bitmap.missing()
                    if not missing:
                        break
                    before = len(missing)
                    await _send_json(writer, make_packet(MsgType.FILE_ACCEPT, self.node.device_id,
                                                         {"transfer_id": tid, "missing": missing}))
                    got_in_round = 0
                    expected = set(missing)
                    while True:
                        kind, body = await _read(reader, max_frame)
                        if kind == FRAME_BINARY:
                            (idx,) = IDX.unpack(body[:4])
                            data = body[4:]
                            if idx not in expected or idx >= manifest.chunk_count:
                                continue
                            off, ln = chunk_range(idx, manifest.size, manifest.chunk_size)
                            if len(data) != ln or sha256_hex(data) != manifest.chunk_hashes[idx]:
                                log.warning("чанк %d файла %s повреждён — запросим повторно", idx, t["filename"])
                                continue
                            fh.seek(off)
                            fh.write(data)
                            bitmap.set(idx)
                            expected.discard(idx)
                            got_in_round += 1
                            prog.add(ln)
                            if got_in_round % ack_every == 0 or not expected:
                                fh.flush()
                                self.node.store.update_transfer(tid, received_chunks=bitmap.hex())
                                await _send_json(writer, make_packet(MsgType.FILE_CHUNK_ACK, self.node.device_id,
                                                                     {"transfer_id": tid, "received": got_in_round}))
                            self._publish_progress(t, prog, "in", t["peer_id"])
                        else:
                            pkt = decode_packet(body)
                            if pkt["message_type"] == MsgType.FILE_FINISH:
                                break
                            if pkt["message_type"] == MsgType.FILE_ABORT:
                                raise TransferError(f"владелец прервал передачу: {pkt['payload'].get('reason', '')}")
                    fh.flush()
                    self.node.store.update_transfer(tid, received_chunks=bitmap.hex())
                    left = len(bitmap.missing())
                    if left and left >= before:
                        stalled += 1
                        if stalled >= 2:      # два раунда подряд без прогресса — пробуем новое соединение
                            break
                    else:
                        stalled = 0
            if bitmap.missing():
                # связь жива и файл цел — просто не все чанки доехали; докачаем на следующем подключении
                log.info("приём %s: осталось %d чанков — повторим", t["filename"], len(bitmap.missing()))
                return "incomplete"
            # полный SHA-256 — второй уровень проверки
            digest = await asyncio.to_thread(file_sha256, temp)
            if digest != manifest.full_sha256:
                temp.unlink(missing_ok=True)
                self.node.store.update_transfer(tid, received_chunks="")
                raise TransferError("полный SHA-256 не совпал — файл отброшен")
            final = unique_path(self.node.paths.files, t["filename"])
            atomic_move(temp, final)
            self.node.store.update_transfer(tid, status="done", final_path=str(final), error=None)
            self.node.store.set_file_local_path(t["file_id"], str(final))
            self._publish_progress(t, prog, "in", t["peer_id"], force=True)
            await _send_json(writer, make_packet(MsgType.FILE_FINISH, self.node.device_id, {"transfer_id": tid, "ok": True}))
            await self.node.emit(EventType.FILE_COMPLETED, {"file_id": t["file_id"], "from": t["peer_id"], "filename": t["filename"]})
            self.node.eventlog.write(EventType.FILE_COMPLETED, f"получен файл {final.name} ({manifest.size} байт)", category="files")
            self.node.bus.publish("transfer.done", {"transfer_id": tid, "file_id": t["file_id"], "path": str(final),
                                                    "filename": final.name})
            log.info("файл %s получен целиком (%d байт) → %s", t["filename"], manifest.size, final)
            return "done"
        finally:
            await close_writer(writer)

    # ------------------------------------------------------------------ отдача (serve)
    async def serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, first: dict[str, Any]) -> None:
        p = first["payload"]
        file_id = str(p.get("file_id", ""))
        peer_id = str(first["sender_id"])
        f = self.node.store.shared_file(file_id)
        try:
            if not f or not f.get("local_path") or not Path(f["local_path"]).is_file():
                await _send_json(writer, make_packet(MsgType.FILE_REJECT, self.node.device_id, {"reason": "NOT_FOUND"}))
                return
            if self._out_active >= self.limits.max_concurrent_outgoing:
                await _send_json(writer, make_packet(MsgType.FILE_REJECT, self.node.device_id,
                                                     {"reason": "BUSY", "retry_after": BUSY_RETRY}))
                log.info("отдача %s для %s отложена: лимит одновременных исходящих", f["filename"], peer_id[:8])
                return
            self._out_active += 1
            try:
                async with self._out_sem:
                    await self._serve_file(reader, writer, f, peer_id, p)
            finally:
                self._out_active -= 1
        except (ConnectionClosed, asyncio.TimeoutError, ProtocolError, OSError) as e:
            log.info("отдача %s для %s прервана: %s", f["filename"] if f else file_id, peer_id[:8], e or "timeout")
        finally:
            await close_writer(writer)

    async def _serve_file(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, f: dict[str, Any],
                          peer_id: str, req: dict[str, Any]) -> None:
        hashes = self.node.store.file_chunks(f["file_id"])
        manifest = Manifest(file_id=f["file_id"], filename=f["filename"], size=f["size"], chunk_size=f["chunk_size"],
                            chunk_count=f["chunk_count"], full_sha256=f["full_sha256"], chunk_hashes=hashes)
        await _send_json(writer, make_packet(MsgType.FILE_OFFER, self.node.device_id, manifest.to_dict()))
        tid = str(req.get("transfer_id") or f"out-{f['file_id'][:8]}-{peer_id[:8]}")
        t = {"transfer_id": f"out:{tid}", "file_id": f["file_id"], "peer_id": peer_id, "direction": "out", "status": "active",
             "filename": f["filename"], "size": f["size"], "chunk_size": f["chunk_size"], "chunk_count": f["chunk_count"],
             "received_chunks": str(req.get("have", "")), "full_sha256": f["full_sha256"], "temp_path": None,
             "final_path": f["local_path"], "session_id": f["session_id"], "error": None}
        self.node.store.upsert_transfer(t)
        have = Bitmap(f["chunk_count"], t["received_chunks"])
        prog = Progress(f["size"], sum(chunk_range(i, f["size"], f["chunk_size"])[1] for i in range(f["chunk_count"]) if have.has(i)))
        self._publish_progress(t, prog, "out", peer_id, force=True)
        window = self.limits.ack_window
        max_frame = self.limits.max_packet_size

        acked = 0
        ack_event = asyncio.Event()
        finished = asyncio.Event()
        requests: asyncio.Queue[list[int] | None] = asyncio.Queue()

        async def reader_task() -> None:
            nonlocal acked
            try:
                while True:
                    kind, body = await _read(reader, max_frame)
                    if kind != FRAME_JSON:
                        continue
                    pkt = decode_packet(body)
                    mt = pkt["message_type"]
                    if mt == MsgType.FILE_CHUNK_ACK:
                        acked = int(pkt["payload"].get("received", acked))
                        ack_event.set()
                    elif mt == MsgType.FILE_ACCEPT:
                        missing = [int(i) for i in pkt["payload"].get("missing", []) if isinstance(i, int)]
                        await requests.put(missing)
                    elif mt in (MsgType.FILE_FINISH, MsgType.FILE_ABORT):
                        await requests.put(None)
                        finished.set()
                        return
            except (ConnectionClosed, asyncio.TimeoutError, ProtocolError, OSError):
                await requests.put(None)
                finished.set()

        rt = asyncio.create_task(reader_task())
        try:
            with open(f["local_path"], "rb") as fh:
                while True:
                    missing = await asyncio.wait_for(requests.get(), FILE_IO_TIMEOUT)
                    if missing is None:
                        break
                    acked = 0
                    sent = 0
                    for idx in missing:
                        if idx < 0 or idx >= f["chunk_count"]:
                            continue
                        while sent - acked >= window and not finished.is_set():   # окно неподтверждённых чанков
                            ack_event.clear()
                            try:
                                await asyncio.wait_for(ack_event.wait(), FILE_IO_TIMEOUT)
                            except asyncio.TimeoutError:
                                raise ConnectionClosed("получатель не подтверждает чанки") from None
                        if finished.is_set():
                            break
                        off, ln = chunk_range(idx, f["size"], f["chunk_size"])
                        fh.seek(off)
                        data = fh.read(ln)
                        if not await self.node.faults.outbound(self.node.device_id, peer_id):
                            continue   # fault injection: чанк «потерян», получатель запросит повторно
                        await write_frame(writer, FRAME_BINARY + IDX.pack(idx) + data)   # drain() = backpressure
                        sent += 1
                        prog.add(ln)
                        self._publish_progress(t, prog, "out", peer_id)
                    await _send_json(writer, make_packet(MsgType.FILE_FINISH, self.node.device_id, {"transfer_id": tid}))
            self.node.store.update_transfer(t["transfer_id"], status="done")
            self._publish_progress(t, prog, "out", peer_id, force=True)
            log.info("отдача %s для %s завершена", f["filename"], peer_id[:8])
        except Exception as e:
            self.node.store.update_transfer(t["transfer_id"], status="failed", error=str(e))
            raise
        finally:
            rt.cancel()
