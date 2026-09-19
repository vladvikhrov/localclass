import hashlib
import os

from localclass.files.manifest import build_manifest
from localclass.files.storage import sanitize_filename
from tests.conftest import fast_limits


def test_sanitize():
    assert sanitize_filename("../../Windows/System32/file.exe") == "file.exe"
    assert sanitize_filename("C:\\Windows\\x.txt") == "x.txt"
    assert sanitize_filename("Домашка №3.zip") == "Домашка №3.zip"
    assert sanitize_filename("CON") == "_CON"
    assert sanitize_filename("") == "file"


def test_manifest(tmp_path):
    p = tmp_path / "f.bin"
    data = os.urandom(2500)
    p.write_bytes(data)
    m = build_manifest(p, 1000)
    assert m.chunk_count == 3 and m.full_sha256 == hashlib.sha256(data).hexdigest()
    assert m.chunk_hashes[2] == hashlib.sha256(data[2000:]).hexdigest()


async def _setup(cluster, tmp_path, size, **lim):
    c = await cluster(["A", "B"], limits=fast_limits(chunk_size=64 * 1024, ack_window=4, **lim))
    await c.create_session("A")
    await c.join_all()
    await c.connect("A", "B")
    await c.wait_connected("A", "B")
    src = tmp_path / "Домашка №3.bin"
    src.write_bytes(os.urandom(size))
    return c, src


async def test_transfer_with_loss_and_chat_alive(cluster, tmp_path):
    """Передача при потерях чанков + чат не блокируется во время передачи."""
    c, src = await _setup(cluster, tmp_path, 3 * 1024 * 1024 + 12345)
    a, b = c.nodes["A"], c.nodes["B"]
    c.packet_loss("A", "B", 0.1)
    fid = await a.transfers.share_file(src)
    await c.wait_until(lambda: b.store.shared_file(fid) is not None, 10)
    done = []
    b.bus.subscribe(lambda t, d: done.append(d) if t == "transfer.done" else None)
    await b.transfers.download(fid)
    await a.send_message("чат во время передачи")
    await c.wait_until(lambda: c.messages("B") == ["чат во время передачи"], 10)
    await c.wait_until(lambda: bool(done), 60)
    out = b.paths.files / "Домашка №3.bin"
    assert out.exists() and out.read_bytes() == src.read_bytes()
    t = b.store.find_transfer(fid, "in")
    assert t["status"] == "done"
    await c.wait_until(lambda: any(e.type == "FILE_COMPLETED" for e in a.store.events(a.session_id)), 10)


async def test_resume_after_restart(cluster, tmp_path):
    """Перезапуск процесса в середине передачи: resume из SQLite, целостность чанков."""
    c, src = await _setup(cluster, tmp_path, 4 * 1024 * 1024)
    a, b = c.nodes["A"], c.nodes["B"]
    c.latency("A", "B", 0.05)
    fid = await a.transfers.share_file(src)
    await c.wait_until(lambda: b.store.shared_file(fid) is not None, 10)
    tid = await b.transfers.download(fid)
    await c.wait_until(lambda: (b.store.get_transfer(tid) or {}).get("received_chunks", "").count("f") >= 2, 30)
    # "падение" получателя посреди приёма
    t_mid = b.store.get_transfer(tid)
    assert t_mid["status"] == "active"
    await c.restart("B")
    b = c.nodes["B"]
    c.clear_faults()
    t_after = b.store.get_transfer(tid)
    assert t_after and t_after["received_chunks"] == t_mid["received_chunks"]
    await c.wait_connected("A", "B", 20)
    await c.wait_until(lambda: (b.store.get_transfer(tid) or {}).get("status") == "done", 60)
    out = b.paths.files / "Домашка №3.bin"
    assert out.read_bytes() == src.read_bytes()
    assert not any(b.paths.incoming.iterdir())


async def test_no_direct_path_is_reported(cluster, tmp_path):
    """Файлы не идут через gossip: нет прямого пути — честная причина, а не бесконечные попытки."""
    c = await cluster(["A", "B", "C"], limits=fast_limits(chunk_size=64 * 1024))
    await c.create_session("A")
    await c.join_all()
    c.partition("A", "C")
    await c.connect_chain("A", "B", "C")
    await c.wait_connected("A", "B"); await c.wait_connected("B", "C")
    src = tmp_path / "x.bin"
    src.write_bytes(os.urandom(1000))
    fid = await c.nodes["A"].transfers.share_file(src)
    cn = c.nodes["C"]
    await c.wait_until(lambda: cn.store.shared_file(fid) is not None, 10)
    failed = []
    cn.bus.subscribe(lambda t, d: failed.append(d) if t == "transfer.failed" else None)
    await cn.transfers.download(fid)
    await c.wait_until(lambda: bool(failed), 10)
    assert "нет прямого соединения" in failed[0]["reason"]


async def test_outgoing_limit_queue(cluster, tmp_path):
    """Лимит одновременных исходящих: лишние получатели ждут в очереди, но всё доходит."""
    c = await cluster(["A", "B", "C", "D"], limits=fast_limits(chunk_size=64 * 1024, max_concurrent_outgoing=1))
    await c.create_session("A")
    await c.join_all()
    await c.connect_all()
    for lb in "BCD":
        await c.wait_connected("A", lb)
    c.latency("A", "B", 0.02); c.latency("A", "C", 0.02); c.latency("A", "D", 0.02)
    src = tmp_path / "big.bin"
    src.write_bytes(os.urandom(1024 * 1024))
    fid = await c.nodes["A"].transfers.share_file(src)
    for lb in "BCD":
        await c.wait_until(lambda lb=lb: c.nodes[lb].store.shared_file(fid) is not None, 10)
    for lb in "BCD":
        await c.nodes[lb].transfers.download(fid)
    def all_done():
        return all((c.nodes[lb].store.find_transfer(fid, "in") or {}).get("status") == "done" for lb in "BCD")
    await c.wait_until(all_done, 90)
    for lb in "BCD":
        assert (c.nodes[lb].paths.files / "big.bin").read_bytes() == src.read_bytes()
