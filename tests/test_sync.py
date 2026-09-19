"""Обязательные сценарии ТЗ 15.3."""
import asyncio


from tests.conftest import fast_limits


async def test_basic_chat_and_dedup(cluster):
    c = await cluster(["A", "B", "C"])
    await c.create_session("A")
    await c.join_all()
    await c.connect_all()
    await c.wait_connected("A", "B")
    await c.wait_connected("B", "C")
    await c.nodes["A"].send_message("привет\nс переводом строки")
    await c.wait_until(lambda: c.messages("C") == ["привет\nс переводом строки"], 5)
    await c.wait_consistent()
    # серия реконнектов — дубликатов нет
    for _ in range(3):
        c.drop_connection("A", "B")
        await asyncio.sleep(0.3)
    await c.wait_connected("A", "B", 15)
    await c.nodes["B"].send_message("после реконнекта")
    await c.wait_consistent()
    for lb in "ABC":
        assert c.messages(lb).count("привет\nс переводом строки") == 1
        assert c.messages(lb).count("после реконнекта") == 1


async def test_offline_then_return(cluster):
    """Узел офлайн, затем возврат: восстановление пропущенных событий, отсутствие дубликатов."""
    c = await cluster(["A", "B"])
    await c.create_session("A")
    await c.join_all()
    await c.connect("A", "B")
    await c.wait_connected("A", "B")
    c.partition("A", "B")
    for i in range(30):
        await c.nodes["A"].send_message(f"offline {i}")
    await asyncio.sleep(0.5)
    assert c.messages("B") == []
    c.partition("A", "B", on=False)
    await c.wait_connected("A", "B", 15)
    t0 = asyncio.get_event_loop().time()
    await c.wait_consistent(timeout=10)
    assert asyncio.get_event_loop().time() - t0 < 10
    assert len(c.messages("B")) == 30 and len(set(c.messages("B"))) == 30


async def test_packet_loss_anti_entropy(cluster):
    """20 % потерь при живом соединении — anti-entropy доставляет всё."""
    c = await cluster(["A", "B"])
    await c.create_session("A")
    await c.join_all()
    await c.connect("A", "B")
    await c.wait_connected("A", "B")
    c.packet_loss("A", "B", 0.2)
    for i in range(40):
        await c.nodes["A"].send_message(f"lossy {i}")
    await c.wait_consistent(timeout=30)
    assert len(c.messages("B")) == 40


async def test_partition_relay_through_middle(cluster):
    """A ✗ C при живых A—B—C: событие от A доходит до C через B."""
    c = await cluster(["A", "B", "C"])
    await c.create_session("A")
    await c.join_all()
    c.partition("A", "C")
    await c.connect_chain("A", "B", "C")
    await c.wait_connected("A", "B")
    await c.wait_connected("B", "C")
    await asyncio.sleep(1)
    assert not c.connected("A", "C")
    await c.nodes["A"].send_message("через B")
    await c.wait_until(lambda: c.messages("C") == ["через B"], 10)
    await c.nodes["C"].send_message("обратно через B")
    await c.wait_until(lambda: "обратно через B" in c.messages("A"), 10)
    assert not c.connected("A", "C")


async def test_restart_keeps_counters(cluster):
    """Перезапуск узла с существующей базой: lamport и device_seq персистентны, история не перемешана."""
    c = await cluster(["A", "B"])
    await c.create_session("A")
    await c.join_all()
    await c.connect("A", "B")
    await c.wait_connected("A", "B")
    for i in range(5):
        await c.nodes["A"].send_message(f"до {i}")
    await c.wait_consistent()
    seq_before = c.nodes["A"].store.device_seq(c.session_id)
    lam_before = c.nodes["A"].store.lamport()
    await c.restart("A")
    a = c.nodes["A"]
    assert a.session_id == c.session_id
    assert a.store.device_seq(c.session_id) == seq_before and a.store.lamport() == lam_before
    await c.wait_connected("A", "B", 20)
    await a.send_message("после")
    await c.wait_consistent()
    assert c.messages("B") == [f"до {i}" for i in range(5)] + ["после"]
    assert c.messages("A") == c.messages("B")


async def test_deleted_before_created_over_network(cluster):
    """DELETED раньше CREATED: tombstone; сообщение не воскресает."""
    from localclass.core.events import Event
    c = await cluster(["A", "B"])
    await c.create_session("A")
    await c.join_all()
    await c.connect("A", "B")
    await c.wait_connected("A", "B")
    a = c.nodes["A"]
    created = await a.send_message("будет удалено")
    deleted = await a.delete_message(created.payload["message_id"])
    await c.wait_consistent()
    # третий узел получает события в обратном порядке
    await c.start("C")
    cn = c.nodes["C"]
    await cn.join_session(c.session_id)
    assert await cn.apply_remote_event(Event.from_dict(deleted.to_dict()), None)
    assert await cn.apply_remote_event(Event.from_dict(created.to_dict()), None)
    assert c.messages("C") == []


async def test_rate_limit_and_recovery(cluster):
    """Пир превышает лимит событий: отбрасывание, отключение, восстановление через anti-entropy."""
    lim = fast_limits(max_events_per_sec=20, rate_limit_cooldown=2.0)
    c = await cluster(["A", "B"], limits=lim)
    await c.create_session("A")
    await c.join_all()
    await c.connect("A", "B")
    await c.wait_connected("A", "B")
    for i in range(300):
        await c.nodes["A"].send_message(f"flood {i}")
    await asyncio.sleep(0.5)
    b = c.nodes["B"]
    assert b.gossip.dropped_rate > 0
    await c.wait_consistent(timeout=60)
    assert len(c.messages("B")) == 300


async def test_v1_v2_compat(cluster):
    """Узел с protocol_version=2 подключается к v1: неизвестные типы и поля игнорируются."""
    from localclass.net import protocol
    from localclass.net.protocol import make_packet
    c = await cluster(["A", "B"])
    await c.create_session("A")
    await c.join_all()
    old = protocol.PROTOCOL_VERSION
    protocol.PROTOCOL_VERSION = 2
    try:
        await c.connect("B", "A")
        await c.wait_connected("A", "B")
        conn = c.nodes["B"].mesh.get(c.nodes["A"].device_id)
        await conn.send(make_packet("FUTURE_TYPE", c.nodes["B"].device_id, {"weird": [1, 2, 3]}))
        pkt = make_packet("EVENT", c.nodes["B"].device_id, {"event": {"nope": 1}})
        pkt["extra_top_level"] = True
        await conn.send(pkt)
        await c.nodes["B"].send_message("v2 → v1")
        await c.wait_until(lambda: c.messages("A") == ["v2 → v1"], 5)
        assert c.connected("A", "B")
    finally:
        protocol.PROTOCOL_VERSION = old


async def test_tofu_conflict(cluster, tmp_path):
    """Смена ключа при том же device_id невозможна (id = отпечаток), а новый ключ под старым именем — SECURITY WARNING."""
    c = await cluster(["A", "B"])
    await c.create_session("A")
    await c.join_all()
    await c.connect("A", "B")
    await c.wait_connected("A", "B")
    b_id = c.nodes["B"].device_id
    # подменяем отпечаток B в базе A — как если бы у B сменился ключ
    c.nodes["A"].store.db.execute("UPDATE peers SET fingerprint='deadbeef' WHERE device_id=?", (b_id,))
    warnings = []
    c.nodes["A"].bus.subscribe(lambda t, d: warnings.append(d) if t == "security.warning" else None)
    c.drop_connection("A", "B")
    await c.wait_until(lambda: bool(warnings), 15)
    assert c.nodes["A"].mesh.peer_status(b_id) == "conflict"
    await c.nodes["A"].trust_peer(b_id, accept=True)
    await c.wait_connected("A", "B", 20)
