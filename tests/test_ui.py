"""Тесты интерфейса (Qt в offscreen-режиме): экран входа, роли, лента ссылок, сброс после завершения урока."""
from __future__ import annotations

import os
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# Qt требует системные библиотеки (libEGL, libGL, libxkbcommon). Там, где их нет — например, на голом
# CI-раннере, — тесты интерфейса пропускаются, а не роняют весь прогон.
try:
    from PySide6.QtWidgets import QApplication
except ImportError as e:   # noqa: BLE001
    pytest.skip(f"Qt недоступен в этом окружении: {e}", allow_module_level=True)

from localclass.config import NodeConfig, Settings   # noqa: E402
from localclass.core.node import Node                # noqa: E402
from localclass.testing.harness import free_port     # noqa: E402
from localclass.ui.app.bridge import Bridge          # noqa: E402
from localclass.ui.app.main_window import TAB_CHAT, TAB_FILES, TAB_SESSION, MainWindow   # noqa: E402
from localclass.ui.app.style import qss              # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    app.setStyleSheet(qss("light"))
    yield app


@pytest.fixture
def gui(qapp, tmp_path):
    made = []

    def make(label: str = "A", discovery_port: int | None = None):
        cfg = NodeConfig(data_dir=tmp_path / label, device_label=label, listen_port=free_port(),
                         discovery_port=discovery_port or free_port(), dev_mode=True, enable_discovery=False)
        settings = Settings.load(cfg.data_dir / "config" / "settings.json")
        settings.enable_mdns = False
        settings.display_name = label
        node = Node(cfg, settings)
        bridge = Bridge(node)
        bridge.start()
        win = MainWindow(bridge)
        win.resize(1280, 820)
        win.show()
        made.append((bridge, win))
        return node, bridge, win
    yield make
    for bridge, win in made:
        win.timer.stop()
        win.close()
        bridge.stop()


def pump(app, seconds: float = 0.6) -> None:
    """Прокрутить цикл событий Qt, дав ядру в своём потоке закончить работу."""
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        app.processEvents()
        time.sleep(0.01)


def wait_for(app, cond, timeout: float = 10.0, msg: str = "условие не выполнено") -> None:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        app.processEvents()
        if cond():
            return
        time.sleep(0.02)
    raise AssertionError(f"timeout: {msg}")


def test_login_screen_requires_name_and_shows_cards(qapp, gui):
    node, bridge, win = gui("A")
    pump(qapp)
    assert win.session_stack.currentIndex() == 0
    assert not win.tabs.isTabEnabled(TAB_CHAT) and not win.tabs.isTabEnabled(TAB_FILES)

    win.login_page.name_edit.setText("")
    win.login_page.create_session()
    pump(qapp, 0.4)
    assert node.session_id is None, "без имени урок создаваться не должен"

    win.login_page.name_edit.setText("Иван")      # имя заполнено — подключение разрешено
    node.store.seen_session("sid-x", "Робототехника", "QW45-ZZ", "Пётр", 4)
    win.login_page.refresh()
    pump(qapp, 0.3)
    card = win.login_page._cards["sid-x"]
    assert card.title.text() == "Робототехника"
    assert "Пётр" in card.subtitle.text() and "4" in card.subtitle.text()
    assert card.join_btn.text() == "Присоединиться" and card.join_btn.isEnabled()
    card.join_btn.click()
    assert card.join_btn.text() == "Подключение…" and not card.join_btn.isEnabled(), "защита от повторного клика"


def test_stale_card_disappears(qapp, gui):
    node, bridge, win = gui("A")
    pump(qapp)
    node.store.seen_session("sid-x", "Урок", "QW45-ZZ", "Пётр", 1)
    win.login_page.refresh()
    assert "sid-x" in win.login_page._cards
    node.store.db.execute("UPDATE seen_sessions SET last_seen=last_seen-30")
    win.login_page.refresh()
    assert not win.login_page._cards, "карточка старше TTL должна исчезнуть"


def test_create_lesson_switches_screen_and_roles(qapp, gui):
    node, bridge, win = gui("T")
    pump(qapp)
    win.login_page.name_edit.setText("Мария")
    win.login_page.topic_edit.setText("Алгоритмы")
    win.login_page.create_pin.setText("4242")
    win.login_page.create_session()
    wait_for(qapp, lambda: node.session_id is not None, msg="урок не создался")
    pump(qapp, 0.4)

    assert win.session_stack.currentIndex() == 1
    assert win.tabs.isTabEnabled(TAB_CHAT) and win.tabs.isTabEnabled(TAB_FILES)
    assert node.is_teacher and "преподаватель" in win.active_page.role_pill.text()
    assert win.active_page.code.text() == node.current_session()["code"]
    assert not win.active_page.close_btn.isHidden() and win.active_page.pin_card.isHidden()
    # isHidden() — явное скрытие виджета, не зависит от того, какая вкладка сейчас открыта
    assert not win.announce_cb.isHidden() and not win.btn_new_channel.isHidden(), "объявления и каналы — преподавателю"
    # QR не занимает экран: только кнопка, карточка компактная
    assert not any(w.__class__.__name__ == "QRWidget" for w in win.active_page.findChildren(object))


def test_student_sees_pin_card_and_no_teacher_controls(qapp, gui):
    node, bridge, win = gui("S")
    pump(qapp)
    win.login_page.name_edit.setText("Пётр")
    win.login_page.create_session()
    # ждём, пока применятся оба события создания урока (SESSION_CREATED и USER_JOINED)
    wait_for(qapp, lambda: (node.store.member(node.session_id, node.device_id) or {}).get("role") == "teacher",
             msg="роль преподавателя не применилась")
    # понижаем себя до ученика, как будто подключились к чужому уроку
    node.store.db.execute("UPDATE members SET role='student' WHERE session_id=? AND device_id=?",
                          (node.session_id, node.device_id))
    node.store.db.execute("UPDATE sessions SET teacher_id='', teacher_public_key='' WHERE session_id=?",
                          (node.session_id,))
    win.tabs.setCurrentIndex(TAB_SESSION)
    win.refresh_all()
    pump(qapp, 0.3)
    assert node.role == "student"
    assert not win.active_page.pin_card.isHidden(), "ученику показываем поле PIN для входа как преподаватель"
    assert not win.active_page.leave_btn.isHidden() and win.active_page.close_btn.isHidden()
    win.tabs.setCurrentIndex(TAB_CHAT)
    pump(qapp, 0.2)
    assert win.announce_cb.isHidden() and win.btn_new_channel.isHidden()


def test_links_feed_collects_urls_from_chat(qapp, gui):
    node, bridge, win = gui("A")
    pump(qapp)
    win.login_page.name_edit.setText("Мария")
    win.login_page.create_session()
    wait_for(qapp, lambda: node.session_id is not None)
    win.tabs.setCurrentIndex(TAB_CHAT)
    win.msg_input.setPlainText("Задание тут https://docs.python.org/3/ и ещё https://example.org/x")
    win.send_message()
    wait_for(qapp, lambda: len(win._link_cards) >= 2, msg="ссылки из чата не попали в ленту")
    urls = {c.url for c in win._link_cards}
    assert "https://docs.python.org/3/" in urls and "https://example.org/x" in urls
    assert win.links_empty.isHidden()
    card = win._link_cards[0]
    assert card.url and hasattr(card, "open") and hasattr(card, "copy")


def test_files_tab_has_no_file_id_column_and_hides_transfers(qapp, gui, tmp_path):
    node, bridge, win = gui("A")
    pump(qapp)
    win.login_page.name_edit.setText("Мария")
    win.login_page.create_session()
    wait_for(qapp, lambda: node.session_id is not None)
    win.tabs.setCurrentIndex(TAB_FILES)
    pump(qapp, 0.3)
    headers = [win.files_table.horizontalHeaderItem(i).text() for i in range(win.files_table.columnCount())]
    assert headers[:3] == ["Имя файла", "Размер", "Кто выложил"]
    assert "file_id" not in " ".join(headers).lower()
    assert not win.transfers_box.isVisible(), "очередь скрыта, пока нет активных передач"

    f = tmp_path / "Домашка №3.zip"
    f.write_bytes(b"x" * 100_000)
    win.share_file(str(f))
    wait_for(qapp, lambda: win.files_table.rowCount() == 1, msg="файл не появился в таблице")
    from PySide6.QtCore import Qt
    assert win.files_table.item(0, 0).data(Qt.UserRole), "file_id хранится в данных строки"
    assert win.files_table.item(0, 0).text() == "Домашка №3.zip"


def test_close_lesson_returns_to_login(qapp, gui):
    node, bridge, win = gui("A")
    pump(qapp)
    win.login_page.name_edit.setText("Мария")
    win.login_page.create_session()
    wait_for(qapp, lambda: node.session_id is not None)
    sid = node.session_id
    win.tabs.setCurrentIndex(TAB_CHAT)
    bridge.call(node.close_session())
    wait_for(qapp, lambda: node.session_id is None, msg="урок не завершился")
    pump(qapp, 0.5)
    assert win.session_stack.currentIndex() == 0
    assert win.tabs.currentIndex() == TAB_SESSION
    assert not win.tabs.isTabEnabled(TAB_CHAT) and not win.tabs.isTabEnabled(TAB_FILES)
    assert node.store.is_session_closed(sid)
    assert sid not in win.login_page._cards


def test_theme_switch_applies_stylesheet(qapp, gui):
    node, bridge, win = gui("A")
    pump(qapp)
    win.apply_theme("dark")
    assert node.settings.theme == "dark"
    assert "#171c22" in QApplication.instance().styleSheet()
    win.apply_theme("light")
    assert node.settings.theme == "light"
    assert "#f4f6f9" in QApplication.instance().styleSheet()
