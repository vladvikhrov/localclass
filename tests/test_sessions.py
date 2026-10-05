"""Жизненный цикл уроков и роли по PIN (требования рефакторинга UI/логики сессий)."""
import time

import pytest

from localclass.core.node import PermissionDenied
from localclass.core.session import DEFAULT_PIN, check_pin, pin_hash


async def test_close_session_resets_all_nodes(cluster):
    """Преподаватель завершает урок → SESSION_CLOSED по mesh → все узлы на экране входа."""
    c = await cluster(["A", "B", "C"])
    await c.nodes["A"].create_session("Урок", 60, "1234")
    c.session_id = c.nodes["A"].session_id
    await c.join_all()
    await c.connect_all()
    await c.wait_connected("A", "B")
    await c.wait_connected("B", "C")
    closed = {}
    for lb in "BC":
        c.nodes[lb].bus.subscribe(lambda t, d, lb=lb: closed.setdefault(lb, d) if t == "session.closed" else None)
    await c.nodes["A"].close_session()
    await c.wait_until(lambda: c.nodes["B"].session_id is None and c.nodes["C"].session_id is None, 15)
    assert c.nodes["A"].session_id is None
    assert closed["B"]["reason"] == "remote" and closed["C"]["reason"] == "remote"
    # закрытый урок исчез из списка и не возвращается
    for lb in "ABC":
        n = c.nodes[lb]
        assert n.store.is_session_closed(c.session_id)
        assert not [s for s in n.sessions_in_network() if s["session_id"] == c.session_id]
    with pytest.raises(PermissionDenied):
        await c.nodes["B"].join_session(c.session_id)


async def test_expired_session_auto_resets(cluster):
    """Истёк срок урока → узлы сами сбрасывают состояние (watchdog), без действий пользователя."""
    c = await cluster(["A", "B"])
    await c.nodes["A"].create_session("Короткий", 60)
    c.session_id = c.nodes["A"].session_id
    await c.join_all()
    await c.connect("A", "B")
    await c.wait_connected("A", "B")
    for n in (c.nodes["A"], c.nodes["B"]):   # имитируем, что урок начался давно
        n.store.db.execute("UPDATE sessions SET created_at=? WHERE session_id=?", (time.time() - 7200, c.session_id))
    reasons = []
    c.nodes["B"].bus.subscribe(lambda t, d: reasons.append(d["reason"]) if t == "session.closed" else None)
    await c.wait_until(lambda: c.nodes["A"].session_id is None and c.nodes["B"].session_id is None, 20)
    assert reasons and reasons[0] in ("expired", "remote", "closed")


async def test_stale_session_card_disappears(cluster):
    """Карточка урока живёт 15 секунд с последнего broadcast."""
    c = await cluster(["A", "B"])
    a, b = c.nodes["A"], c.nodes["B"]
    s = await a.create_session("Урок", 60)
    b.store.seen_session(s["session_id"], s["name"], s["code"], "A", 1)
    assert [x["name"] for x in b.sessions_in_network()] == ["Урок"]
    b.store.db.execute("UPDATE seen_sessions SET last_seen=last_seen-20")
    for p in b.mesh.known.values():
        p.last_seen -= 20
    assert b.sessions_in_network() == []


async def test_student_joins_without_pin_teacher_claims_with_pin(cluster):
    """Ученику пароль не нужен; преподаватель со второго ноутбука получает права по PIN."""
    c = await cluster(["A", "B", "T"])
    s = await c.nodes["A"].create_session("Урок", 60, "4242")
    c.session_id = s["session_id"]
    assert c.nodes["A"].is_teacher
    await c.join_all()           # B и T входят как ученики, без пароля
    await c.connect_all()
    await c.wait_connected("A", "B")
    assert not c.nodes["B"].is_teacher and c.nodes["B"].role == "student"
    t = c.nodes["T"]
    await c.wait_until(lambda: t.pin_is_known(), 10)
    with pytest.raises(PermissionDenied):
        await t.claim_teacher("0000")
    assert not t.is_teacher
    assert await t.claim_teacher("4242")
    assert t.is_teacher and t.role == "teacher"
    await c.wait_consistent()
    assert c.nodes["A"].store.member(c.session_id, t.device_id)["role"] == "teacher"
    # права преподавателя реально работают у второго ноутбука
    await t.create_channel("домашка", "Домашка")
    await c.wait_consistent()
    assert "домашка" in [ch["name"] for ch in c.nodes["B"].store.channels(c.session_id)]
    with pytest.raises(PermissionDenied):
        await c.nodes["B"].create_channel("нельзя")


async def test_pin_entered_before_session_data_arrives(cluster):
    """PIN введён при входе, когда SESSION_CREATED ещё не пришёл — роль присваивается после sync."""
    c = await cluster(["A", "T"])
    s = await c.nodes["A"].create_session("Урок", 60, "7777")
    t = c.nodes["T"]
    await t.join_session(s["session_id"], s["name"], s["code"], teacher_pin="7777")
    assert not t.is_teacher and not t.pin_is_known()   # данных урока пока нет
    await c.connect("T", "A")
    await c.wait_connected("A", "T")
    await c.wait_until(lambda: t.is_teacher, 15)
    assert t.role == "teacher"


async def test_pin_hash_is_session_scoped():
    assert pin_hash("s1", "123") != pin_hash("s2", "123")
    assert check_pin("s1", "123", pin_hash("s1", "123"))
    assert not check_pin("s1", "123", "")
    assert pin_hash("s1", "") == ""
    assert DEFAULT_PIN == "123"
