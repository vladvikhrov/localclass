"""Главное окно LocalClass (ТЗ v1.3).

Компоновка: только layout'ы, растяжения и минимальные размеры — ни setGeometry, ни фиксированных
размеров контейнеров, поэтому интерфейс не ломается при системном масштабе 125 % и 150 %.
Каждая вкладка верхнего уровня обёрнута в QScrollArea: на экране 1366×768 содержимое не сжимается,
а получает вертикальную прокрутку.

Экран «Урок» разделён переключателем ролей: «Я Ученик» (вход по карточке или коду) и «Я Преподаватель»
(создание урока, подключение к идущему уроку по PIN). Любое действие пользователя превращается в команду
ядра через Bridge, любое изменение состояния приходит событием шины bus.py (ТЗ 3.3).
"""
from __future__ import annotations

import html
import os
import re
import time
from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt, QTimer, QUrl
from PySide6.QtGui import QColor, QDesktopServices, QGuiApplication, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (QApplication, QButtonGroup, QCheckBox, QComboBox, QFileDialog, QFormLayout,
                               QGroupBox, QHBoxLayout, QHeaderView, QInputDialog, QLabel, QLineEdit, QListWidget,
                               QListWidgetItem, QMainWindow, QMenu, QMessageBox, QPlainTextEdit, QProgressBar,
                               QSizePolicy, QSpinBox, QSplitter, QStackedWidget, QSystemTrayIcon, QTableWidget,
                               QTableWidgetItem, QTabWidget, QTextBrowser, QTextEdit, QVBoxLayout, QWidget)

from ... import __version__
from ...core.session import DEFAULT_PIN, remaining_seconds
from ...core.store import Bitmap
from .bridge import Bridge
from .qr import QRDialog
from .style import link_color, qss, rich_text_css
from .widgets import (DropZone, LinkCard, Section, SessionCard, Toast, ask_files, button, card, cell_widget,
                      divider_with_text, fit_widget_column, fmt_eta, fmt_size, fmt_speed, label, scrollable)

STATUS_RU = {"connected": "на связи", "discovered": "обнаружен", "unreachable": "недоступен",
             "blocked": "заблокирован", "conflict": "⚠ ключ изменился", "other_session": "другой урок"}
URL_RE = re.compile(r"(https?://[^\s<>\"']+)")
TAB_SESSION, TAB_CHAT, TAB_FILES, TAB_PEERS, TAB_DIAG, TAB_LOGS, TAB_SETTINGS = range(7)
ROLE_STUDENT, ROLE_TEACHER = 0, 1


def app_icon() -> QIcon:
    pm = QPixmap(64, 64)
    pm.fill(QColor("#0969da"))
    p = QPainter(pm)
    p.setPen(QColor("white"))
    f = p.font()
    f.setPixelSize(34)
    f.setBold(True)
    p.setFont(f)
    p.drawText(pm.rect(), Qt.AlignCenter, "LC")
    p.end()
    return QIcon(pm)


class MessageInput(QTextEdit):
    """Enter — отправить, Shift+Enter — перевод строки. Высота растёт с текстом, но ограничена сверху."""

    def __init__(self, on_send, parent=None):
        super().__init__(parent)
        self.on_send = on_send
        self.setPlaceholderText("Сообщение…  (Enter — отправить, Shift+Enter — новая строка)")
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)
        self.setMinimumHeight(44)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.document().documentLayout().documentSizeChanged.connect(self._fit_height)

    def _fit_height(self) -> None:
        doc = int(self.document().size().height()) + 2 * int(self.frameWidth()) + 10
        self.setMaximumHeight(max(44, min(doc, 150)))

    def keyPressEvent(self, e) -> None:
        if e.key() in (Qt.Key_Return, Qt.Key_Enter) and not (e.modifiers() & Qt.ShiftModifier):
            self.on_send()
            return
        super().keyPressEvent(e)


# ======================================================================== экран входа
class LoginPage(QWidget):
    """Вне урока: переключатель «Я Ученик» / «Я Преподаватель» и соответствующий экран."""

    def __init__(self, win: "MainWindow"):
        super().__init__()
        self.win = win
        self.node = win.node
        self._cards: dict[str, SessionCard] = {}
        self._joining = False

        root = QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 16)
        root.setSpacing(14)

        # ---------- переключатель ролей
        switch_row = QHBoxLayout()
        switch_row.setSpacing(10)
        self.role_group = QButtonGroup(self)
        self.role_group.setExclusive(True)
        self.btn_student = button("Я Ученик", "roleTab", min_height=44)
        self.btn_teacher = button("Я Преподаватель", "roleTab", min_height=44)
        for idx, b in ((ROLE_STUDENT, self.btn_student), (ROLE_TEACHER, self.btn_teacher)):
            b.setCheckable(True)
            b.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            self.role_group.addButton(b, idx)
            switch_row.addWidget(b, 1)
        self.btn_student.setChecked(True)
        self.role_group.idClicked.connect(self._on_role_switch)
        root.addLayout(switch_row)

        # ---------- общее поле имени
        name_card, name_lay = card(margins=(16, 12, 16, 12), spacing=6)
        name_lay.addWidget(label("Ваше имя", "h2"))
        name_lay.addWidget(label("его увидят участники урока — заполните перед входом", "muted", wrap=True))
        self.name_edit = QLineEdit(self.node.display_name)
        self.name_edit.setObjectName("bigInput")
        self.name_edit.setPlaceholderText("Например: Иван Петров")
        self.name_edit.setMinimumHeight(40)
        self.name_edit.editingFinished.connect(self._save_name)
        name_lay.addWidget(self.name_edit)
        root.addWidget(name_card)

        self.stack = QStackedWidget()
        self.stack.addWidget(self._build_student_page())
        self.stack.addWidget(self._build_teacher_page())
        root.addWidget(self.stack, 1)

    # ---------------- экран ученика
    def _build_student_page(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(12)

        lessons, ll = card(spacing=10)
        ll.addWidget(label("Доступные уроки в сети", "h2"))
        self.hint = label("Ищем уроки в сети… Попросите преподавателя начать урок.", "muted", wrap=True)
        ll.addWidget(self.hint)
        holder = QWidget()
        self.cards_layout = QVBoxLayout(holder)
        self.cards_layout.setContentsMargins(0, 0, 0, 0)
        self.cards_layout.setSpacing(10)
        self.cards_layout.addStretch()
        self.cards_area = scrollable(holder, plain=True)
        self.cards_area.setMinimumHeight(170)
        ll.addWidget(self.cards_area, 1)
        lay.addWidget(lessons, 1)

        code_card, cl = card(spacing=10)
        cl.addWidget(divider_with_text("Или подключитесь по коду"))
        code_row = QHBoxLayout()
        code_row.setSpacing(10)
        self.code_edit = QLineEdit()
        self.code_edit.setPlaceholderText("Код урока, например FB2K-9G")
        self.code_edit.setMaxLength(12)
        self.code_edit.setMinimumHeight(40)
        self.code_edit.returnPressed.connect(self.join_by_code)
        code_row.addWidget(self.code_edit, 1)
        self.code_btn = button("Войти по коду", "primaryButton", self.join_by_code,
                               "Запасной путь, если урок не виден в списке", min_width=160, min_height=40)
        code_row.addWidget(self.code_btn)
        cl.addLayout(code_row)

        self.more_btn = button("▸  Другие способы подключения", "sectionToggle", self._toggle_more)
        self.more_btn.setCheckable(True)
        cl.addWidget(self.more_btn)
        self.more_box = QWidget()          # обычный QWidget, видимость только через setVisible
        more_lay = QVBoxLayout(self.more_box)
        more_lay.setContentsMargins(0, 4, 0, 0)
        more_lay.setSpacing(8)
        more_lay.addWidget(label("Текст QR-кода, который показывает преподаватель:", "muted", wrap=True))
        self.qr_edit = QLineEdit()
        self.qr_edit.setPlaceholderText('{"version":1,"session_id":…}')
        self.qr_edit.setMinimumHeight(36)
        more_lay.addWidget(self.qr_edit)
        more_lay.addWidget(button("Войти по QR", "", self.join_by_qr, min_height=36))
        more_lay.addWidget(label("Прямое подключение к компьютеру преподавателя:", "muted", wrap=True))
        self.ip_edit = QLineEdit()
        self.ip_edit.setPlaceholderText("192.168.1.10:45821")
        self.ip_edit.setMinimumHeight(36)
        more_lay.addWidget(self.ip_edit)
        more_lay.addWidget(button("Подключиться по IP", "", self.connect_ip, min_height=36))
        self.more_box.setVisible(False)
        cl.addWidget(self.more_box)
        lay.addWidget(code_card)
        return page

    def _toggle_more(self) -> None:
        self.more_box.setVisible(self.more_btn.isChecked())
        self.more_btn.setText(("▾  " if self.more_btn.isChecked() else "▸  ") + "Другие способы подключения")

    # ---------------- экран преподавателя
    def _build_teacher_page(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(12)

        create, cl = card(spacing=12)
        cl.addWidget(label("Начать новый урок", "h2"))
        cl.addWidget(label("Вы станете преподавателем: сможете создавать каналы, публиковать объявления "
                           "и завершить урок для всех.", "muted", wrap=True))
        form = QFormLayout()
        form.setSpacing(10)
        form.setLabelAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        self.topic_edit = QLineEdit("Урок")
        self.topic_edit.setPlaceholderText("Например: Алгоритмы и структуры данных")
        self.topic_edit.setMinimumHeight(40)
        form.addRow("Тема урока", self.topic_edit)
        self.create_pin = QLineEdit(DEFAULT_PIN)
        self.create_pin.setObjectName("pinInput")
        self.create_pin.setMaxLength(12)
        self.create_pin.setMinimumHeight(40)
        form.addRow("PIN преподавателя", self.create_pin)
        self.duration = QSpinBox()
        self.duration.setRange(5, 24 * 60)
        self.duration.setValue(120)
        self.duration.setSingleStep(15)
        self.duration.setSuffix(" мин")
        self.duration.setMinimumHeight(40)
        form.addRow("Длительность", self.duration)
        cl.addLayout(form)
        cl.addWidget(label("PIN нужен, только чтобы получить права преподавателя с другого ноутбука. "
                           "Ученикам он не требуется.", "muted", wrap=True))
        self.create_btn = button("Начать урок", "primaryButton", self.create_session, min_height=46)
        cl.addWidget(self.create_btn)
        lay.addWidget(create)

        join, jl = card(spacing=10)
        jl.addWidget(divider_with_text("Или подключитесь к уроку, который уже идёт"))
        jl.addWidget(label("Введите код урока и PIN преподавателя — получите права управления "
                           "на этом ноутбуке.", "muted", wrap=True))
        row = QHBoxLayout()
        row.setSpacing(10)
        self.t_code_edit = QLineEdit()
        self.t_code_edit.setPlaceholderText("Код урока")
        self.t_code_edit.setMaxLength(12)
        self.t_code_edit.setMinimumHeight(40)
        row.addWidget(self.t_code_edit, 2)
        self.t_pin_edit = QLineEdit()
        self.t_pin_edit.setObjectName("pinInput")
        self.t_pin_edit.setPlaceholderText("PIN")
        self.t_pin_edit.setMaxLength(12)
        self.t_pin_edit.setMinimumHeight(40)
        self.t_pin_edit.returnPressed.connect(self.join_as_teacher)
        row.addWidget(self.t_pin_edit, 1)
        self.t_join_btn = button("Войти как преподаватель", "", self.join_as_teacher, min_width=200, min_height=40)
        row.addWidget(self.t_join_btn)
        jl.addLayout(row)
        lay.addWidget(join)
        lay.addStretch()
        return page

    def _on_role_switch(self, role: int) -> None:
        self.stack.setCurrentIndex(role)
        self.refresh()

    # ---------------- команды
    def _save_name(self) -> None:
        name = self.name_edit.text().strip()
        if name and name != self.node.display_name:
            self.node.set_display_name(name)
            self.win.refresh_status()

    def _require_name(self) -> bool:
        if self.name_edit.text().strip():
            self._save_name()
            return True
        self.win.toast.show_message("Сначала введите ваше имя", "error")
        self.name_edit.setFocus()
        return False

    def _set_busy(self, busy: bool, session_id: str | None = None) -> None:
        self._joining = busy
        for w in (self.code_btn, self.create_btn, self.t_join_btn):
            w.setEnabled(not busy)
        for sid, c in self._cards.items():
            c.set_busy(busy and sid == session_id)
            if not busy:
                c.set_enabled_join(True)
            elif sid != session_id:
                c.set_enabled_join(False)

    def _joined(self, _result: Any) -> None:
        self._set_busy(False)
        self.code_btn.setText("Войти по коду")
        self.t_join_btn.setText("Войти как преподаватель")
        self.win.on_joined()

    def _failed(self, msg: str) -> None:
        self._set_busy(False)
        self.code_btn.setText("Войти по коду")
        self.t_join_btn.setText("Войти как преподаватель")
        self.create_btn.setText("Начать урок")
        self.win.toast.show_message(msg, "error", 6000)

    def join_session(self, session_id: str) -> None:
        if self._joining or not self._require_name():
            self._set_busy(False)
            return
        c = self._cards.get(session_id)
        name = c.title.text() if c else ""
        code = c.code_pill.text() if c else ""
        self._set_busy(True, session_id)
        self.win.bridge.call(self.node.join_session(session_id, name, code), self._joined, self._failed)

    def join_by_code(self) -> None:
        if self._joining or not self._require_name():
            return
        code = self.code_edit.text().strip()
        if not code:
            self.win.toast.show_message("Введите код урока", "error")
            return
        self._set_busy(True)
        self.code_btn.setText("Подключение…")
        self.win.bridge.call(self.node.join_by_code(code), self._joined, self._failed)

    def join_as_teacher(self) -> None:
        if self._joining or not self._require_name():
            return
        code = self.t_code_edit.text().strip()
        pin = self.t_pin_edit.text().strip()
        if not code or not pin:
            self.win.toast.show_message("Введите код урока и PIN преподавателя", "error")
            return
        self._set_busy(True)
        self.t_join_btn.setText("Подключение…")
        self.win.bridge.call(self.node.join_by_code(code, pin), self._joined, self._failed)

    def join_by_qr(self) -> None:
        if self._joining or not self._require_name():
            return
        text = self.qr_edit.text().strip()
        if not text:
            self.win.toast.show_message("Вставьте текст QR-кода", "error")
            return
        self._set_busy(True)
        self.win.bridge.call(self.node.join_by_qr(text), self._joined, self._failed)

    def connect_ip(self) -> None:
        host, _, port = self.ip_edit.text().strip().rpartition(":")
        if not host or not port.isdigit():
            self.win.toast.show_message("Формат: IP:порт, например 192.168.1.10:45821", "error")
            return
        self.win.bridge.call(self.node.connect_manual(host, int(port)),
                             lambda _: self.win.toast.show_message("Подключаемся — урок появится в списке", "info"),
                             self._failed)

    def create_session(self) -> None:
        if self._joining or not self._require_name():
            return
        self._set_busy(True)
        self.create_btn.setText("Создаём урок…")
        self.win.bridge.call(
            self.node.create_session(self.topic_edit.text(), self.duration.value(), self.create_pin.text().strip()),
            self._created, self._failed)

    def _created(self, session: dict) -> None:
        self._set_busy(False)
        self.create_btn.setText("Начать урок")
        self.win.on_session_created(session)

    # ---------------- обновление списка уроков
    def refresh(self) -> None:
        self.name_edit.setEnabled(not self._joining)
        if self.stack.currentIndex() != ROLE_STUDENT:
            return
        sessions = self.node.sessions_in_network()
        seen = set()
        for data in sessions:
            sid = data["session_id"]
            seen.add(sid)
            c = self._cards.get(sid)
            if c is None:
                c = SessionCard(data)
                c.join_requested.connect(self.join_session)
                self.cards_layout.insertWidget(self.cards_layout.count() - 1, c)
                self._cards[sid] = c
            else:
                c.update_data(data)
        for sid in list(self._cards):          # урок закрыт или пропал из сети — карточка уходит
            if sid not in seen:
                self._cards.pop(sid).deleteLater()
        if sessions:
            self.hint.setText(f"Найдено уроков: {len(sessions)}. Нажмите [Присоединиться] у нужного.")
        else:
            self.hint.setText("Ищем уроки в сети… Попросите преподавателя начать урок "
                              "или введите код урока ниже.")


# ======================================================================== экран активного урока
class ActiveLessonPage(QWidget):
    """В уроке: компактная карточка с кодом, QR по кнопке, завершение урока, подтверждение роли по PIN."""

    def __init__(self, win: "MainWindow"):
        super().__init__()
        self.win = win
        self.node = win.node
        root = QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 16)
        root.setSpacing(12)

        info_card, cl = card(margins=(20, 16, 20, 16), spacing=18, vertical=False)
        left = QVBoxLayout()
        left.setSpacing(6)
        self.topic = label("—", "h1", wrap=True)
        left.addWidget(self.topic)
        self.meta = label("", "muted", wrap=True)
        left.addWidget(self.meta)
        self.role_pill = label("", "pill")
        left.addWidget(self.role_pill, 0, Qt.AlignLeft)
        left.addStretch()
        cl.addLayout(left, 1)

        right = QVBoxLayout()
        right.setSpacing(8)
        right.addWidget(label("Код урока", "muted"), 0, Qt.AlignHCenter)
        self.code = label("—", "sessionCode", selectable=True)
        self.code.setAlignment(Qt.AlignCenter)
        right.addWidget(self.code)
        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)
        btn_row.addWidget(button("Показать QR-код", "primaryButton", self.show_qr,
                                 "Открыть QR на весь экран — удобно выводить на проектор", min_height=38))
        btn_row.addWidget(button("Копировать код", "", self.copy_code, min_height=38))
        right.addLayout(btn_row)
        cl.addLayout(right)
        root.addWidget(info_card)

        self.pin_card, pl = card("warningCard", margins=(18, 12, 18, 12), spacing=12, vertical=False)
        pl.addWidget(label("Вы преподаватель? Введите PIN, чтобы получить права управления уроком:",
                           "muted", wrap=True), 1)
        self.claim_pin = QLineEdit()
        self.claim_pin.setObjectName("pinInput")
        self.claim_pin.setPlaceholderText("PIN")
        self.claim_pin.setMaxLength(12)
        self.claim_pin.setMinimumHeight(38)
        self.claim_pin.setMinimumWidth(120)
        self.claim_pin.returnPressed.connect(self.claim_teacher)
        pl.addWidget(self.claim_pin)
        pl.addWidget(button("Подтвердить", "", self.claim_teacher, min_height=38))
        root.addWidget(self.pin_card)

        actions, al = card(margins=(20, 14, 20, 14), spacing=10, vertical=False)
        al.addWidget(button("Перейти в чат", "primaryButton", lambda: self.win.tabs.setCurrentIndex(TAB_CHAT),
                            min_height=38))
        al.addWidget(button("Передать файл", "", lambda: self.win.tabs.setCurrentIndex(TAB_FILES), min_height=38))
        al.addStretch()
        self.leave_btn = button("Выйти из урока", "", self.leave, min_height=38)
        al.addWidget(self.leave_btn)
        self.close_btn = button("Завершить урок", "dangerButton", self.close_session,
                                "Урок завершится у всех участников", min_height=38)
        al.addWidget(self.close_btn)
        root.addWidget(actions)

        people, ppl = card(spacing=8)
        ppl.addWidget(label("Участники урока", "h2"))
        self.members = QListWidget()
        self.members.setSelectionMode(QListWidget.NoSelection)
        self.members.setMinimumHeight(140)
        ppl.addWidget(self.members)
        root.addWidget(people, 1)

    # ---------------- команды
    def show_qr(self) -> None:
        s = self.node.current_session()
        if not s:
            return
        try:
            text = self.node.qr_text()
        except Exception as e:  # noqa: BLE001
            self.win.toast.show_message(str(e), "error")
            return
        QRDialog(text, s["code"], s["name"], self.win).exec_fullscreen()

    def copy_code(self) -> None:
        s = self.node.current_session()
        if s:
            QGuiApplication.clipboard().setText(s["code"])
            self.win.toast.show_message("Код урока скопирован", "ok", 2000)

    def claim_teacher(self) -> None:
        pin = self.claim_pin.text().strip()
        if not pin:
            self.win.toast.show_message("Введите PIN преподавателя", "error")
            return
        self.claim_pin.clear()

        def done(ok: bool) -> None:
            if ok:
                self.win.toast.show_message("Права преподавателя подтверждены", "ok")
            else:
                self.win.toast.show_message("Проверяем PIN — ждём данные урока от преподавателя…", "info")
            self.win.refresh_all()
        self.win.bridge.call(self.node.claim_teacher(pin), done,
                             lambda m: self.win.toast.show_message(m, "error"))

    def leave(self) -> None:
        if QMessageBox.question(self.win, "Выйти из урока",
                                "Выйти из урока? Вы сможете вернуться по коду.") == QMessageBox.Yes:
            self.win.bridge.call(self.node.leave_session(), lambda _: self.win.refresh_all(),
                                 lambda m: self.win.toast.show_message(m, "error"))

    def close_session(self) -> None:
        s = self.node.current_session()
        box = QMessageBox(self.win)
        box.setIcon(QMessageBox.Warning)
        box.setWindowTitle("Завершить урок")
        box.setText(f"Завершить урок «{s['name'] if s else ''}» для всех участников?")
        box.setInformativeText("Все ноутбуки выйдут на экран входа. Переписка и файлы останутся на компьютерах.")
        yes = box.addButton("Завершить урок", QMessageBox.YesRole)
        yes.setObjectName("dangerButton")
        box.addButton("Отмена", QMessageBox.RejectRole)
        box.exec()
        if box.clickedButton() is yes:
            self.win.bridge.call(self.node.close_session(), lambda _: self.win.refresh_all(),
                                 lambda m: self.win.toast.show_message(m, "error"))

    # ---------------- обновление
    def refresh(self) -> None:
        s = self.node.current_session()
        if not s:
            return
        st = self.node.status()
        self.topic.setText(s["name"])
        rem = remaining_seconds(s)
        teacher = st["teacher_name"] or "—"
        self.meta.setText(f"Преподаватель: {teacher}   ·   Участников онлайн: {st['members']}   ·   "
                          f"Осталось: {fmt_eta(rem) if rem is not None else 'без ограничения'}")
        self.code.setText(s["code"])
        is_teacher = self.node.is_teacher
        self.role_pill.setText("Ваша роль: преподаватель" if is_teacher else "Ваша роль: ученик")
        self.close_btn.setVisible(is_teacher)
        self.leave_btn.setVisible(not is_teacher)
        self.pin_card.setVisible(not is_teacher)
        self.members.clear()
        connected = set(self.node.mesh.connected_ids())
        for m in self.node.store.members(s["session_id"]):
            if m["left"]:
                continue
            online = m["device_id"] in connected or m["device_id"] == self.node.device_id
            role = " · преподаватель" if m["role"] == "teacher" else ""
            blocked = " · доступ к чату ограничен" if m["blocked"] else ""
            it = QListWidgetItem(f"{'●' if online else '○'}  {m['display_name'] or m['device_id'][:8]}{role}{blocked}")
            it.setForeground(QColor("#1a7f37") if online else QColor("#8c959f"))
            self.members.addItem(it)


# ======================================================================== главное окно
class MainWindow(QMainWindow):
    def __init__(self, bridge: Bridge):
        super().__init__()
        self.bridge = bridge
        self.node = bridge.node
        self.channel = "general"
        self.dm_peer: str | None = None
        self._log_app: list[dict] = list(self.node.app_log.records)
        self._log_events: list[dict] = list(self.node.eventlog.records)
        self._diag: dict[str, Any] | None = None
        self._link_cards: list[LinkCard] = []

        self.setWindowTitle(f"LocalClass {__version__}")
        self.setWindowIcon(app_icon())
        self.resize(1180, 780)
        self.setMinimumSize(900, 560)
        self.setAcceptDrops(True)

        self.tabs = QTabWidget()
        self.setCentralWidget(self.tabs)
        # каждая вкладка — в прокручиваемой области: при масштабе 125–150 % появляется скролл,
        # а содержимое не сжимается и не накладывается
        self.tabs.addTab(scrollable(self._build_session_tab(), min_height=620), "Урок")
        self.tabs.addTab(scrollable(self._build_chat_tab(), min_width=860, min_height=520), "Чат")
        self.tabs.addTab(scrollable(self._build_files_tab(), min_height=560), "Файлы")
        self.tabs.addTab(scrollable(self._build_peers_tab(), min_height=420), "Компьютеры")
        self.tabs.addTab(scrollable(self._build_diag_tab(), min_height=420), "Диагностика")
        self.tabs.addTab(scrollable(self._build_logs_tab(), min_height=420), "Логи")
        self.tabs.addTab(scrollable(self._build_settings_tab(), min_height=520), "Настройки")
        self.tabs.currentChanged.connect(self._on_tab_changed)

        self.status_label = QLabel()
        self.status_label.setWordWrap(False)
        self.statusBar().addPermanentWidget(self.status_label, 1)
        self.toast = Toast(self)

        self.tray = QSystemTrayIcon(app_icon(), self)
        menu = QMenu()
        menu.addAction("Показать окно", self.showNormal)
        menu.addAction("Выход", QApplication.instance().quit)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(lambda r: self.showNormal() if r == QSystemTrayIcon.Trigger else None)
        self.tray.show()

        bridge.bus.connect(self.on_bus)
        self.apply_theme_assets(self.node.settings.theme)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh_periodic)
        self.timer.start(2000)
        self.refresh_all()

    # ---------------- вкладка «Урок»
    def _build_session_tab(self) -> QWidget:
        self.session_stack = QStackedWidget()
        self.login_page = LoginPage(self)
        self.active_page = ActiveLessonPage(self)
        self.session_stack.addWidget(self.login_page)
        self.session_stack.addWidget(self.active_page)
        return self.session_stack

    def on_joined(self) -> None:
        """Успешный вход: сразу в чат, с зелёным уведомлением."""
        self.refresh_all()
        self.tabs.setCurrentIndex(TAB_CHAT)
        s = self.node.current_session()
        self.toast.show_message(f"Вы подключились к уроку «{s['name'] if s else ''}»", "ok")
        self.msg_input.setFocus()

    def on_session_created(self, session: dict) -> None:
        self.refresh_all()
        self.toast.show_message(f"Урок начат. Код для учеников: {session.get('code', '')}", "ok", 6000)

    # ---------------- вкладка «Чат»
    def _build_chat_tab(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(12, 12, 12, 12)
        split = QSplitter(Qt.Horizontal)
        split.setChildrenCollapsible(False)
        lay.addWidget(split)

        # --- левая колонка: каналы и участники
        left = QWidget()
        left.setMinimumWidth(200)
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 8, 0)
        ll.setSpacing(8)
        ll.addWidget(label("Каналы", "h2"))
        self.channel_list = QListWidget()
        self.channel_list.setMinimumHeight(90)
        self.channel_list.currentItemChanged.connect(self.on_channel_selected)
        ll.addWidget(self.channel_list, 1)
        ch_row = QHBoxLayout()
        ch_row.setSpacing(8)
        self.btn_new_channel = button("+ канал", "", self.new_channel)
        self.btn_close_channel = button("закрыть", "", self.close_channel)
        ch_row.addWidget(self.btn_new_channel)
        ch_row.addWidget(self.btn_close_channel)
        ll.addLayout(ch_row)
        ll.addWidget(label("Участники", "h2"))
        ll.addWidget(label("двойной клик — личное сообщение", "muted", wrap=True))
        self.member_list = QListWidget()
        self.member_list.setMinimumHeight(120)
        self.member_list.itemDoubleClicked.connect(self.open_dm)
        self.member_list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.member_list.customContextMenuRequested.connect(self.member_menu)
        ll.addWidget(self.member_list, 2)
        split.addWidget(left)

        # --- центр: переписка и ввод
        center = QWidget()
        center.setMinimumWidth(380)
        cl = QVBoxLayout(center)
        cl.setContentsMargins(8, 0, 8, 0)
        cl.setSpacing(8)
        title_row = QHBoxLayout()
        title_row.setSpacing(10)
        self.chat_title = label("#general", "h2")
        title_row.addWidget(self.chat_title)
        title_row.addStretch()
        self.search_box = QLineEdit()
        self.search_box.setPlaceholderText("Поиск по сообщениям…")
        self.search_box.setMinimumWidth(180)
        self.search_box.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        self.search_box.textChanged.connect(self.refresh_messages)
        title_row.addWidget(self.search_box, 1)
        cl.addLayout(title_row)
        self.messages_view = QTextBrowser()
        self.messages_view.setOpenExternalLinks(True)
        self.messages_view.setMinimumHeight(200)
        self.messages_view.setContextMenuPolicy(Qt.CustomContextMenu)
        self.messages_view.customContextMenuRequested.connect(self.message_menu)
        cl.addWidget(self.messages_view, 1)
        self.announce_cb = QCheckBox("Отправить как объявление для всего класса")
        cl.addWidget(self.announce_cb)

        input_row = QHBoxLayout()
        input_row.setSpacing(8)
        self.attach_btn = button("📎", "iconButton", self.attach_file, "Прикрепить файл",
                                 min_width=40, min_height=40)
        self.attach_btn.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        input_row.addWidget(self.attach_btn, 0, Qt.AlignBottom)
        self.msg_input = MessageInput(self.send_message)
        input_row.addWidget(self.msg_input, 1)
        self.send_btn = button("Отправить", "sendButton", self.send_message, min_width=100, min_height=40)
        input_row.addWidget(self.send_btn, 0, Qt.AlignBottom)
        cl.addLayout(input_row)
        split.addWidget(center)

        # --- правая колонка: лента ссылок
        right = QWidget()
        right.setMinimumWidth(220)
        rl = QVBoxLayout(right)
        rl.setContentsMargins(8, 0, 0, 0)
        rl.setSpacing(8)
        rl.addWidget(label("Лента ссылок", "h2"))
        rl.addWidget(label("ссылки из чата открываются одним кликом", "muted", wrap=True))
        holder = QWidget()
        self.links_layout = QVBoxLayout(holder)
        self.links_layout.setContentsMargins(0, 0, 0, 0)
        self.links_layout.setSpacing(8)
        self.links_empty = label("Ссылок пока нет. Отправьте ссылку в чат — она появится здесь.", "muted", wrap=True)
        self.links_layout.addWidget(self.links_empty)
        self.links_layout.addStretch()
        self.links_area = scrollable(holder, plain=True)
        self.links_area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        rl.addWidget(self.links_area, 1)
        split.addWidget(right)

        split.setStretchFactor(0, 0)
        split.setStretchFactor(1, 1)
        split.setStretchFactor(2, 0)
        split.setSizes([240, 640, 260])
        return w

    def on_channel_selected(self, item: QListWidgetItem | None, _prev=None) -> None:
        if item is None:
            return
        self.channel = item.data(Qt.UserRole)
        self.dm_peer = None
        self.chat_title.setText(f"#{item.text()}")
        self.refresh_messages()

    def open_dm(self, item: QListWidgetItem) -> None:
        did = item.data(Qt.UserRole)
        if did == self.node.device_id:
            return
        self.dm_peer = did
        self.channel = f"dm:{':'.join(sorted([self.node.device_id, did]))}"
        self.chat_title.setText(f"Личные сообщения · {item.text().lstrip('●○ ')}")
        self.refresh_messages()

    def member_menu(self, pos) -> None:
        it = self.member_list.itemAt(pos)
        if not it:
            return
        did = it.data(Qt.UserRole)
        menu = QMenu(self)
        menu.addAction("Личное сообщение", lambda: self.open_dm(it))
        menu.addAction("Отправить файл…", lambda: self.send_file_to(did))
        if self.node.is_teacher and did != self.node.device_id:
            m = self.node.store.member(self.node.session_id, did) or {}
            menu.addSeparator()
            menu.addAction("Разрешить писать в чат" if m.get("blocked") else "Запретить писать в чат",
                           lambda: self.bridge.call(self.node.set_blocked(did, not m.get("blocked")),
                                                    lambda _: self.refresh_members(),
                                                    lambda msg: self.toast.show_message(msg, "error")))
        menu.exec(self.member_list.mapToGlobal(pos))

    def message_menu(self, pos) -> None:
        anchor = self.messages_view.anchorAt(pos)
        menu = self.messages_view.createStandardContextMenu()
        if anchor.startswith("msg:"):
            mid = anchor[4:]
            menu.addSeparator()
            menu.addAction("Удалить сообщение",
                           lambda: self.bridge.call(self.node.delete_message(mid), lambda _: self.refresh_messages(),
                                                    lambda m: self.toast.show_message(m, "error")))
        menu.exec(self.messages_view.mapToGlobal(pos))

    def new_channel(self) -> None:
        name, ok = QInputDialog.getText(self, "Новый канал", "Название канала (латиница, цифры, дефис):")
        if ok and name:
            self.bridge.call(self.node.create_channel(name, name), lambda _: self.refresh_channels(),
                             lambda m: self.toast.show_message(m, "error"))

    def close_channel(self) -> None:
        if self.channel and not self.channel.startswith("dm:"):
            self.bridge.call(self.node.close_channel(self.channel), lambda _: self.refresh_channels(),
                             lambda m: self.toast.show_message(m, "error"))

    def send_message(self) -> None:
        text = self.msg_input.toPlainText().strip()
        if not text:
            return
        kind = "announcement" if (self.announce_cb.isChecked() and self.node.is_teacher) else "message"
        self.bridge.call(self.node.send_message(text, self.channel, kind=kind, to=self.dm_peer),
                         lambda _: (self.msg_input.clear(), self.announce_cb.setChecked(False)),
                         lambda m: self.toast.show_message(m, "error"))

    def attach_file(self) -> None:
        for path in ask_files(self, "Прикрепить файл"):
            self.share_file(path, peer=self.dm_peer)

    def refresh_channels(self) -> None:
        if not self.node.session_id:
            self.channel_list.clear()
            return
        cur = self.channel
        self.channel_list.blockSignals(True)
        self.channel_list.clear()
        for ch in self.node.store.channels(self.node.session_id):
            it = QListWidgetItem(ch["name"])
            it.setData(Qt.UserRole, ch["name"])
            self.channel_list.addItem(it)
            if ch["name"] == cur:
                self.channel_list.setCurrentItem(it)
        if self.channel_list.currentItem() is None and self.channel_list.count():
            self.channel_list.setCurrentRow(0)
            self.channel = self.channel_list.item(0).data(Qt.UserRole)
        self.channel_list.blockSignals(False)
        teacher = self.node.is_teacher
        self.btn_new_channel.setVisible(teacher)
        self.btn_close_channel.setVisible(teacher)
        self.announce_cb.setVisible(teacher)     # объявления — только преподавателю
        if not teacher:
            self.announce_cb.setChecked(False)

    def refresh_members(self) -> None:
        self.member_list.clear()
        if not self.node.session_id:
            return
        connected = set(self.node.mesh.connected_ids())
        for m in self.node.store.members(self.node.session_id):
            if m["left"]:
                continue
            online = m["device_id"] in connected or m["device_id"] == self.node.device_id
            role = " · преп." if m["role"] == "teacher" else ""
            blocked = " 🚫" if m["blocked"] else ""
            it = QListWidgetItem(f"{'●' if online else '○'}  {m['display_name'] or m['device_id'][:8]}{role}{blocked}")
            it.setData(Qt.UserRole, m["device_id"])
            it.setForeground(QColor("#1a7f37") if online else QColor("#8c959f"))
            self.member_list.addItem(it)

    @staticmethod
    def _render_text(text: str) -> str:
        """Минимальный Markdown: **жирный**, *курсив*, `код`, ссылки, переводы строк."""
        t = html.escape(text)
        t = URL_RE.sub(r'<a href="\1">\1</a>', t)
        t = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", t)
        t = re.sub(r"(?<!\*)\*(?!\*)(.+?)\*", r"<i>\1</i>", t)
        t = re.sub(r"`(.+?)`", r"<code>\1</code>", t)
        return t.replace("\n", "<br>")

    def refresh_messages(self) -> None:
        if not self.node.session_id:
            self.messages_view.setHtml("<i>Нет активного урока</i>")
            return
        q = self.search_box.text().strip()
        msgs = (self.node.store.search_messages(self.node.session_id, q) if q
                else self.node.store.messages(self.node.session_id, self.channel))
        parts = []
        for m in msgs:
            ts = time.strftime("%H:%M", time.localtime(m["timestamp"]))
            who = html.escape(m.get("display_name") or self.node._name_of(m["device_id"]))
            body = self._render_text(m["text"])
            style = ' style="background:#fff8c5;padding:6px;border-radius:6px"' if m["kind"] == "announcement" else ""
            ch = f" <span style='color:#8c959f'>#{html.escape(m['channel'])}</span>" if q else ""
            parts.append(f'<div{style}><a name="msg:{m["message_id"]}" href="msg:{m["message_id"]}" '
                         f'style="text-decoration:none;color:#8c959f">{ts}</a> <b>{who}</b>{ch}'
                         f'{" 📣" if m["kind"] == "announcement" else ""}: {body}</div><div style="height:6px"></div>')
        self.messages_view.setHtml("".join(parts) or "<i>Сообщений пока нет — напишите первым</i>")
        self.messages_view.verticalScrollBar().setValue(self.messages_view.verticalScrollBar().maximum())

    def refresh_links(self) -> None:
        """Лента ссылок: события LINK_CREATED плюс все ссылки, найденные в сообщениях чата."""
        for c in self._link_cards:
            c.deleteLater()
        self._link_cards.clear()
        if not self.node.session_id:
            self.links_empty.setVisible(True)
            return
        items: list[tuple[str, str, str]] = []     # (url, title, author)
        seen: set[str] = set()
        for l in self.node.store.links(self.node.session_id):
            if l["url"] in seen:
                continue
            seen.add(l["url"])
            items.append((l["url"], l["title"] or l["url"], l.get("display_name") or self.node._name_of(l["device_id"])))
        for m in self.node.store.search_messages(self.node.session_id, "http"):
            for url in URL_RE.findall(m["text"]):
                if url in seen:
                    continue
                seen.add(url)
                items.append((url, url, self.node._name_of(m["device_id"])))
        self.links_empty.setVisible(not items)
        color = link_color(self.node.settings.theme)
        for url, title, author in items[:100]:
            c = LinkCard(url, title, author, color=color)
            self.links_layout.insertWidget(self.links_layout.count() - 1, c)
            self._link_cards.append(c)

    # ---------------- вкладка «Файлы»
    def _build_files_tab(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(12, 12, 12, 12)
        lay.setSpacing(12)
        self.drop_zone = DropZone()
        self.drop_zone.files_dropped.connect(lambda paths: [self.share_file(p) for p in paths])
        self.drop_zone.pick_requested.connect(lambda: [self.share_file(p) for p in ask_files(self, "Отправить классу")])
        lay.addWidget(self.drop_zone)

        lay.addWidget(label("Файлы урока", "h2"))
        self.files_table = QTableWidget(0, 4)
        self.files_table.setHorizontalHeaderLabels(["Имя файла", "Размер", "Кто выложил", "Действие"])
        head = self.files_table.horizontalHeader()
        head.setSectionResizeMode(0, QHeaderView.Stretch)
        head.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        head.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        head.setSectionResizeMode(3, QHeaderView.Fixed)     # ширину подгоняем под кнопки действия
        self.files_table.setWordWrap(False)
        self.files_table.setMinimumHeight(180)
        self.files_table.verticalHeader().setVisible(False)
        self.files_table.verticalHeader().setDefaultSectionSize(46)
        self.files_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.files_table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.files_table.customContextMenuRequested.connect(self.file_menu)
        lay.addWidget(self.files_table, 1)

        row = QHBoxLayout()
        row.setSpacing(8)
        self.downloads_label = label("", "muted", wrap=True)
        row.addWidget(self.downloads_label, 1)
        row.addWidget(button("Открыть папку загрузок", "", self.open_downloads_folder))
        lay.addLayout(row)

        self.transfers_box = QGroupBox("Активные передачи")
        tl = QVBoxLayout(self.transfers_box)
        self.transfers_table = QTableWidget(0, 5)
        self.transfers_table.setHorizontalHeaderLabels(["Файл", "Направление", "С кем", "Прогресс",
                                                        "Скорость / осталось"])
        th = self.transfers_table.horizontalHeader()
        th.setSectionResizeMode(0, QHeaderView.Stretch)
        th.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        th.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        th.setSectionResizeMode(3, QHeaderView.Fixed)
        th.setSectionResizeMode(4, QHeaderView.ResizeToContents)
        self.transfers_table.verticalHeader().setVisible(False)
        self.transfers_table.verticalHeader().setDefaultSectionSize(42)
        self.transfers_table.setMinimumHeight(110)
        tl.addWidget(self.transfers_table)
        lay.addWidget(self.transfers_box)
        self.transfers_box.setVisible(False)       # скрыта, пока нет активных передач
        return w

    def open_downloads_folder(self) -> None:
        self.node.paths.files.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.node.paths.files)))

    def share_file(self, path: str, peer: str | None = None) -> None:
        if not self.node.session_id:
            self.toast.show_message("Сначала подключитесь к уроку", "error")
            return
        try:
            size = os.path.getsize(path)
        except OSError as e:
            self.toast.show_message(f"Файл недоступен: {e}", "error")
            return
        recipients = 1 if peer else max(1, len(self.node.mesh.connected_ids()))
        est = self.node.transfers.estimate_traffic(size, recipients)
        if est["warn"]:
            r = QMessageBox.question(self, "Большой файл",
                                     f"Файл {fmt_size(size)}. Если его скачают все {recipients} участ., это примерно "
                                     f"{est['gb']:.1f} ГБ трафика по Wi-Fi — передача займёт значительное время.\n\n"
                                     "Продолжить?")
            if r != QMessageBox.Yes:
                return
        ch = self.channel if not self.channel.startswith("dm:") else "general"
        coro = self.node.transfers.send_file_to(peer, path) if peer else self.node.transfers.share_file(path, ch)
        name = Path(path).name
        self.toast.show_message(f"Готовим «{name}» к отправке…", "info", 2500)
        self.bridge.call(coro, lambda _: (self.refresh_files(),
                                          self.toast.show_message(f"Файл «{name}» доступен классу", "ok")),
                         lambda m: self.toast.show_message(m, "error", 6000))

    def send_file_to(self, peer: str) -> None:
        for path in ask_files(self, "Отправить файл участнику"):
            self.share_file(path, peer)

    def _selected_file_id(self) -> str | None:
        r = self.files_table.currentRow()
        it = self.files_table.item(r, 0) if r >= 0 else None
        return it.data(Qt.UserRole) if it else None

    def download_file(self, file_id: str) -> None:
        self.bridge.call(self.node.transfers.download(file_id),
                         lambda _: (self.refresh_files(), self.toast.show_message("Загрузка началась", "info", 2500)),
                         lambda m: self.toast.show_message(m, "error", 6000))

    def open_file_location(self, path: str) -> None:
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(Path(path).parent)))   # папку, не файл (ТЗ 12.8)

    def file_menu(self, pos) -> None:
        fid = self._selected_file_id()
        if not fid:
            return
        f = self.node.store.shared_file(fid) or {}
        menu = QMenu(self)
        if f.get("local_path") and Path(f["local_path"]).exists():
            menu.addAction("📂 Открыть папку", lambda: self.open_file_location(f["local_path"]))
        else:
            menu.addAction("⬇ Скачать", lambda: self.download_file(fid))
        menu.addSeparator()
        menu.addAction("Удалить из урока",
                       lambda: self.bridge.call(self.node.delete_file(fid), lambda _: self.refresh_files(),
                                                lambda m: self.toast.show_message(m, "error")))
        menu.exec(self.files_table.mapToGlobal(pos))

    def _file_action_widget(self, f: dict, transfer: dict | None) -> QWidget:
        """Двухпозиционная кнопка: [⬇ Скачать] → прогресс → [📂 Открыть] (ТЗ v1.3, 3.1)."""
        mine = f["owner_id"] == self.node.device_id
        local = bool(f.get("local_path")) and Path(f["local_path"]).exists()
        if local or mine:
            btn = button("📂 Открыть", "", lambda p=f.get("local_path"): self.open_file_location(p),
                         "Показать файл в папке")
            btn.setEnabled(local)
            if not local:
                btn.setText("файл не найден")
            return cell_widget(btn)
        if transfer and transfer["status"] in ("active", "queued"):
            bar = QProgressBar()
            bar.setRange(0, 100)
            prog = self.node.transfers.progress.get(transfer["transfer_id"])
            if prog:
                bar.setValue(int(prog["percent"]))
                bar.setFormat(f"%p%  ·  {fmt_speed(prog['speed'])}")
            else:
                bm = Bitmap(transfer["chunk_count"], transfer["received_chunks"])
                bar.setValue(int(100 * bm.received() / max(1, transfer["chunk_count"])))
                bar.setFormat("%p%  ·  в очереди" if transfer["status"] == "queued" else "%p%")
            bar.setMinimumWidth(160)
            return cell_widget(bar)
        online = bool(self.node.mesh.get(f["owner_id"]))
        resume = bool(transfer and transfer["status"] == "paused")
        btn = button("⬇ Продолжить" if resume else "⬇ Скачать", "primaryButton",
                     lambda fid=f["file_id"]: self.download_file(fid))
        btn.setEnabled(online)
        if not online:
            btn.setText("нет в сети")
            btn.setToolTip("Компьютер, выложивший файл, сейчас недоступен — файлы передаются только напрямую")
        elif resume:
            btn.setToolTip(transfer["error"] or "продолжить с места обрыва")
        return cell_widget(btn)

    def refresh_files(self) -> None:
        self.downloads_label.setText(f"Принятые файлы сохраняются в {self.node.paths.files}")
        self.files_table.setRowCount(0)
        if not self.node.session_id:
            self.refresh_transfers()
            return
        transfers = {t["file_id"]: t for t in self.node.store.transfers(self.node.session_id) if t["direction"] == "in"}
        for f in self.node.store.shared_files(self.node.session_id):
            r = self.files_table.rowCount()
            self.files_table.insertRow(r)
            name_item = QTableWidgetItem(f["filename"])
            name_item.setData(Qt.UserRole, f["file_id"])     # file_id хранится в данных строки, не в колонке
            name_item.setToolTip(f["filename"])
            self.files_table.setItem(r, 0, name_item)
            self.files_table.setItem(r, 1, QTableWidgetItem(fmt_size(f["size"])))
            owner = "вы" if f["owner_id"] == self.node.device_id else (f.get("owner_name") or f["owner_id"][:8])
            self.files_table.setItem(r, 2, QTableWidgetItem(owner))
            self.files_table.setCellWidget(r, 3, self._file_action_widget(f, transfers.get(f["file_id"])))
        fit_widget_column(self.files_table, 3)
        self.refresh_transfers()

    def refresh_transfers(self) -> None:
        active = [t for t in (self.node.store.transfers(self.node.session_id) if self.node.session_id else [])
                  if t["status"] in ("active", "queued", "paused")]
        self.transfers_box.setVisible(bool(active))
        self.transfers_table.setRowCount(0)
        for t in active:
            r = self.transfers_table.rowCount()
            self.transfers_table.insertRow(r)
            prog = self.node.transfers.progress.get(t["transfer_id"])
            bar = QProgressBar()
            bar.setRange(0, 100)
            bar.setMinimumWidth(180)
            if prog:
                bar.setValue(int(prog["percent"]))
            else:
                bm = Bitmap(t["chunk_count"], t["received_chunks"])
                bar.setValue(int(100 * bm.received() / max(1, t["chunk_count"])))
            self.transfers_table.setItem(r, 0, QTableWidgetItem(t["filename"]))
            self.transfers_table.setItem(r, 1, QTableWidgetItem("приём" if t["direction"] == "in" else "отдача"))
            self.transfers_table.setItem(r, 2, QTableWidgetItem(self.node._name_of(t["peer_id"])))
            self.transfers_table.setCellWidget(r, 3, cell_widget(bar))
            if t["status"] == "active" and prog:
                detail = f"{fmt_speed(prog['speed'])} · осталось {fmt_eta(prog['eta'])}"
            elif t["status"] == "queued":
                detail = "в очереди — отправитель занят"
            elif t["status"] == "paused":
                detail = f"пауза: {t['error'] or 'ждём связь'}"
            else:
                detail = ""
            self.transfers_table.setItem(r, 4, QTableWidgetItem(detail))
        fit_widget_column(self.transfers_table, 3)

    def dragEnterEvent(self, e) -> None:
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e) -> None:
        for u in e.mimeData().urls():
            if u.isLocalFile() and os.path.isfile(u.toLocalFile()):
                self.share_file(u.toLocalFile())

    # ---------------- вкладка «Компьютеры»
    def _build_peers_tab(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(12, 12, 12, 12)
        lay.setSpacing(10)
        self.peers_table = QTableWidget(0, 6)
        self.peers_table.setHorizontalHeaderLabels(["Имя", "Состояние", "Адрес", "Задержка", "Версия", "Примечание"])
        ph = self.peers_table.horizontalHeader()
        ph.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        ph.setSectionResizeMode(5, QHeaderView.Stretch)
        self.peers_table.verticalHeader().setVisible(False)
        self.peers_table.verticalHeader().setDefaultSectionSize(40)
        self.peers_table.setMinimumHeight(200)
        self.peers_table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.peers_table.customContextMenuRequested.connect(self.peer_menu)
        lay.addWidget(self.peers_table, 1)
        row = QHBoxLayout()
        row.setSpacing(8)
        self.manual_host = QLineEdit()
        self.manual_host.setPlaceholderText("IP:порт, например 192.168.1.10:45821")
        self.manual_host.setMinimumHeight(38)
        row.addWidget(self.manual_host, 1)
        row.addWidget(button("Подключиться вручную", "", self.connect_manual, min_height=38))
        lay.addLayout(row)
        lay.addWidget(label("«обнаружен» — компьютер виден в сети, но соединение не установлено; «недоступен» — "
                            "TCP-соединение не проходит (брандмауэр или изоляция клиентов на точке доступа). "
                            "⚠ — у компьютера изменился ключ: правый клик, чтобы решить.", "muted", wrap=True))
        return w

    def connect_manual(self) -> None:
        host, _, port = self.manual_host.text().strip().rpartition(":")
        if not host or not port.isdigit():
            self.toast.show_message("Формат: IP:порт", "error")
            return
        self.bridge.call(self.node.connect_manual(host, int(port)),
                         lambda _: self.toast.show_message("Подключаемся…", "info"),
                         lambda m: self.toast.show_message(m, "error"))

    def peer_menu(self, pos) -> None:
        r = self.peers_table.currentRow()
        if r < 0:
            return
        did = self.peers_table.item(r, 0).data(Qt.UserRole)
        menu = QMenu(self)
        if self.node.mesh.peer_status(did) == "conflict":
            menu.addAction("Принять новый ключ (доверять)",
                           lambda: self.bridge.call(self.node.trust_peer(did, True), lambda _: self.refresh_peers()))
            menu.addAction("Отклонить и заблокировать",
                           lambda: self.bridge.call(self.node.trust_peer(did, False), lambda _: self.refresh_peers()))
            menu.addSeparator()
        menu.addAction("Отправить файл…", lambda: self.send_file_to(did))
        menu.exec(self.peers_table.mapToGlobal(pos))

    def refresh_peers(self) -> None:
        self.peers_table.setRowCount(0)
        for p in self.node.peers_view():
            r = self.peers_table.rowCount()
            self.peers_table.insertRow(r)
            addr = f"{p['address'] or (p['addresses'][0] if p['addresses'] else '?')}:{p['port']}"
            vals = [p["display_name"] or p["device_id"][:8], STATUS_RU.get(p["status"], p["status"]), addr,
                    f"{p['rtt'] * 1000:.0f} мс" if p["rtt"] else "", p["app_version"], p["error"] or ""]
            for c, v in enumerate(vals):
                it = QTableWidgetItem(v)
                if c == 0:
                    it.setData(Qt.UserRole, p["device_id"])
                if c == 1:
                    it.setForeground(QColor({"connected": "#1a7f37", "unreachable": "#cf222e",
                                             "conflict": "#cf222e"}.get(p["status"], "#57606a")))
                self.peers_table.setItem(r, c, it)

    # ---------------- вкладка «Диагностика»
    def _build_diag_tab(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(12, 12, 12, 12)
        lay.setSpacing(10)
        self.diag_view = QTextBrowser()
        self.diag_view.setMinimumHeight(260)
        lay.addWidget(self.diag_view, 1)
        row = QHBoxLayout()
        row.setSpacing(8)
        row.addWidget(button("Проверить ещё раз", "primaryButton", self.run_diag, min_height=38))
        row.addWidget(button("Подключиться вручную", "", lambda: self.tabs.setCurrentIndex(TAB_PEERS), min_height=38))
        row.addStretch()
        lay.addLayout(row)
        return w

    def run_diag(self) -> None:
        self.bridge.call(self.node.run_diagnostics(), self.show_diag,
                         lambda m: self.toast.show_message(m, "error"))

    def show_diag(self, r: dict[str, Any]) -> None:
        self._diag = r
        rows = "".join(
            f"<tr><td>{html.escape(c['name'])}</td>"
            f"<td style='color:{'#1a7f37' if c['ok'] else '#cf222e'}'><b>{'✓' if c['ok'] else '✗'}</b></td>"
            f"<td style='color:#57606a'>{html.escape(str(c['detail']))}</td></tr>" for c in r["checks"])
        reasons = "".join(f"<li>{html.escape(x)}</li>" for x in r["reasons"]) or "<li>проблем не обнаружено</li>"
        peers = "".join(f"<li>{html.escape(p['display_name'] or p['device_id'][:8])} — "
                        f"{STATUS_RU.get(p['status'], p['status'])} {html.escape(p['error'] or '')}</li>"
                        for p in r["peers"])
        self.diag_view.setHtml(
            f"<h2>Диагностика сети</h2><table cellpadding=6>{rows}</table>"
            f"<p><b>Обнаружено компьютеров:</b> {r['discovered']} &nbsp; <b>На связи:</b> {r['reachable']} &nbsp; "
            f"<b>Недоступно:</b> {r['unreachable']} &nbsp; <b>В других уроках:</b> {r['other_sessions']}</p>"
            f"<p><b>Возможные причины:</b></p><ul>{reasons}</ul>"
            f"<p><b>Участники урока:</b></p><ul>{peers or '<li>—</li>'}</ul>"
            f"<p style='color:#8c959f'>обновлено {time.strftime('%H:%M:%S', time.localtime(r['ts']))}</p>")

    # ---------------- вкладка «Логи»
    def _build_logs_tab(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(12, 12, 12, 12)
        lay.setSpacing(10)
        row = QHBoxLayout()
        row.setSpacing(8)
        self.log_kind = QComboBox()
        self.log_kind.addItems(["Журнал событий", "Технический лог"])
        self.log_level = QComboBox()
        self.log_level.addItems(["Все", "INFO", "WARNING", "ERROR", "DEBUG"])
        self.log_cat = QComboBox()
        self.log_cat.addItems(["Все", "Сеть", "Файлы", "Чат", "Урок", "Безопасность"])
        for x in (self.log_kind, self.log_level, self.log_cat):
            x.setMinimumHeight(36)
            x.currentIndexChanged.connect(self.refresh_logs)
            row.addWidget(x)
        self.log_filter = QLineEdit()
        self.log_filter.setPlaceholderText("фильтр по тексту или компьютеру")
        self.log_filter.setMinimumHeight(36)
        self.log_filter.textChanged.connect(self.refresh_logs)
        row.addWidget(self.log_filter, 1)
        row.addWidget(button("Папка логов", "",
                             lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.node.paths.logs))),
                             min_height=36))
        lay.addLayout(row)
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMinimumHeight(260)
        self.log_view.setMaximumBlockCount(5000)
        lay.addWidget(self.log_view, 1)
        return w

    def refresh_logs(self) -> None:
        cat_map = {"Сеть": "net", "Файлы": "files", "Чат": "chat", "Урок": "session", "Безопасность": "security"}
        cat = cat_map.get(self.log_cat.currentText())
        level = self.log_level.currentText()
        text = self.log_filter.text().lower()
        lines = []
        if self.log_kind.currentIndex() == 0:
            self.log_level.setEnabled(False)
            for rec in self._log_events[-2000:]:
                if cat and rec["category"] != cat:
                    continue
                s = f"{time.strftime('%H:%M:%S', time.localtime(rec['ts']))} {rec['type']:20} {rec['text']}"
                if text and text not in s.lower() and text not in (rec.get("device_id") or ""):
                    continue
                lines.append(s)
        else:
            self.log_level.setEnabled(True)
            for rec in self._log_app[-3000:]:
                if level != "Все" and rec["level"] != level:
                    continue
                if cat and rec["category"] != cat:
                    continue
                s = (f"{time.strftime('%H:%M:%S', time.localtime(rec['ts']))} {rec['level']:7} "
                     f"{rec['category']:8} {rec['message']}")
                if text and text not in s.lower():
                    continue
                lines.append(s)
        self.log_view.setPlainText("\n".join(lines))
        self.log_view.verticalScrollBar().setValue(self.log_view.verticalScrollBar().maximum())

    # ---------------- вкладка «Настройки»
    def _build_settings_tab(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(14, 14, 14, 14)
        lay.setSpacing(14)

        user = QGroupBox("Основные настройки")
        uf = QFormLayout(user)
        uf.setSpacing(10)
        uf.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        self.set_name = QLineEdit(self.node.display_name)
        self.set_name.setMinimumHeight(38)
        self.set_name.editingFinished.connect(self._apply_name)
        uf.addRow("Ваше имя", self.set_name)

        dl_row = QHBoxLayout()
        dl_row.setSpacing(8)
        self.dl_path = QLineEdit(str(self.node.paths.files))
        self.dl_path.setReadOnly(True)
        self.dl_path.setMinimumHeight(38)
        dl_row.addWidget(self.dl_path, 1)
        dl_row.addWidget(button("Выбрать…", "", self.pick_download_dir, min_height=38))
        dl_row.addWidget(button("По умолчанию", "", lambda: self.apply_download_dir(""), min_height=38))
        uf.addRow("Папка для принятых файлов", dl_row)

        self.theme_box = QComboBox()
        self.theme_box.addItem("Светлая", "light")
        self.theme_box.addItem("Тёмная", "dark")
        self.theme_box.setCurrentIndex(1 if self.node.settings.theme == "dark" else 0)
        self.theme_box.setMinimumHeight(38)
        self.theme_box.currentIndexChanged.connect(lambda: self.apply_theme(self.theme_box.currentData()))
        uf.addRow("Тема оформления", self.theme_box)

        self.set_auto = QCheckBox("Автоматически принимать файлы, отправленные лично мне")
        self.set_auto.setChecked(self.node.settings.auto_accept_files)
        self.set_auto.toggled.connect(lambda v: self._set_setting("auto_accept_files", v))
        uf.addRow(self.set_auto)
        lay.addWidget(user)

        self.adv = Section("Для системных администраторов")
        self.adv.add_widget(label("Менять эти настройки нужно только при проблемах с сетью. "
                                  "Порты и discovery применяются после перезапуска приложения.", "muted", wrap=True))
        af = QFormLayout()
        af.setSpacing(10)
        af.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        self.set_port = QSpinBox()
        self.set_port.setRange(1024, 65535)
        self.set_port.setValue(self.node.settings.listen_port)
        self.set_port.setMinimumHeight(36)
        self.set_port.valueChanged.connect(lambda v: self._set_setting("listen_port", v))
        af.addRow("TCP-порт", self.set_port)
        self.set_dport = QSpinBox()
        self.set_dport.setRange(1024, 65535)
        self.set_dport.setValue(self.node.settings.discovery_port)
        self.set_dport.setMinimumHeight(36)
        self.set_dport.valueChanged.connect(lambda v: self._set_setting("discovery_port", v))
        af.addRow("UDP-порт discovery", self.set_dport)
        self.set_bc = QCheckBox("UDP broadcast discovery")
        self.set_bc.setChecked(self.node.settings.enable_broadcast)
        self.set_bc.toggled.connect(lambda v: self._set_setting("enable_broadcast", v))
        af.addRow(self.set_bc)
        self.set_mdns = QCheckBox("mDNS discovery (резервный способ обнаружения)")
        self.set_mdns.setChecked(self.node.settings.enable_mdns)
        self.set_mdns.toggled.connect(lambda v: self._set_setting("enable_mdns", v))
        af.addRow(self.set_mdns)
        self.adv.add_layout(af)

        self.adv.add_widget(label("Сетевые интерфейсы", "h2"))
        self.adv.add_widget(label("По умолчанию используется интерфейс со шлюзом по умолчанию; виртуальные "
                                  "адаптеры (VirtualBox, Docker, Hyper-V, VPN) отключаются автоматически.",
                                  "muted", wrap=True))
        self.iface_list = QListWidget()
        self.iface_list.setMinimumHeight(120)
        self.iface_list.itemChanged.connect(self.iface_toggled)
        self.adv.add_widget(self.iface_list)

        lim = self.node.limits
        self.adv.add_widget(label("Лимиты и пути", "h2"))
        self.adv.add_widget(label(
            f"События: {lim.max_events_per_sec}/с на компьютер, размер события ≤ {fmt_size(lim.max_event_size)}. "
            f"Файлы: ≤ {fmt_size(lim.max_file_size)}, одновременных отдач {lim.max_concurrent_outgoing}, "
            f"чанк {fmt_size(lim.chunk_size)}, окно ACK {lim.ack_window}, "
            f"хранилище ≤ {fmt_size(lim.max_storage_size)}. Хранение истории: {lim.retention_days} дн. "
            f"Значения меняются в config/settings.json → limits.", "muted", wrap=True))
        self.paths_label = QLabel()
        self.paths_label.setOpenExternalLinks(True)
        self.paths_label.setWordWrap(True)
        self.adv.add_widget(self.paths_label)
        lay.addWidget(self.adv)
        lay.addStretch()
        return w

    def _apply_name(self) -> None:
        self.node.set_display_name(self.set_name.text())
        self.login_page.name_edit.setText(self.node.display_name)
        self.refresh_status()
        self.toast.show_message("Имя сохранено", "ok", 2000)

    def _set_setting(self, key: str, value: Any) -> None:
        setattr(self.node.settings, key, value)
        self.node.save_settings()

    def pick_download_dir(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Папка для принятых файлов", str(self.node.paths.files))
        if path:
            self.apply_download_dir(path)

    def apply_download_dir(self, path: str) -> None:
        self.node.set_download_dir(path)
        self.dl_path.setText(str(self.node.paths.files))
        self.refresh_files()
        self.toast.show_message(f"Файлы будут сохраняться в {self.node.paths.files}", "ok", 4000)

    def apply_theme(self, theme: str) -> None:
        self.node.set_theme(theme)
        QApplication.instance().setStyleSheet(qss(theme))
        self.apply_theme_assets(theme)

    def apply_theme_assets(self, theme: str) -> None:
        """Цвета HTML-содержимого (ссылки, код) не задаются QSS — обновляем их отдельно."""
        css = rich_text_css(theme)
        for view in (self.messages_view, self.diag_view):
            view.document().setDefaultStyleSheet(css)
        self.refresh_messages()
        self.refresh_links()
        if self._diag:
            self.show_diag(self._diag)

    def refresh_interfaces(self) -> None:
        self.iface_list.blockSignals(True)
        self.iface_list.clear()
        for i in self.node.interfaces():
            marks = []
            if i["preferred"]:
                marks.append("шлюз по умолчанию")
            if i["hint"]:
                marks.append(i["hint"])
            suffix = f"  —  {', '.join(marks)}" if marks else ""
            it = QListWidgetItem(f"{i['ip']}   {i['name']}{suffix}")
            it.setData(Qt.UserRole, i["key"])
            it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
            it.setCheckState(Qt.Checked if i["enabled"] else Qt.Unchecked)
            self.iface_list.addItem(it)
        self.iface_list.blockSignals(False)
        self.paths_label.setText(
            f"Данные приложения: <a href='{self.node.paths.root.as_uri()}'>{self.node.paths.root}</a><br>"
            f"Принятые файлы: <a href='{self.node.paths.files.as_uri()}'>{self.node.paths.files}</a><br>"
            f"База: {self.node.paths.db_file}")

    def iface_toggled(self, it: QListWidgetItem) -> None:
        self.node.set_interface_enabled(it.data(Qt.UserRole), it.checkState() == Qt.Checked)

    # ---------------- события шины bus.py
    def on_bus(self, topic: str, data: dict) -> None:
        if topic == "log.app":
            self._log_app.append(data)
            if self.tabs.currentIndex() == TAB_LOGS and self.log_kind.currentIndex() == 1:
                self.refresh_logs()
        elif topic == "log.event":
            self._log_events.append(data)
            if self.tabs.currentIndex() == TAB_LOGS and self.log_kind.currentIndex() == 0:
                self.refresh_logs()
        elif topic == "event":
            self._on_core_event(data)
        elif topic in ("peer.connected", "peer.disconnected", "peer.discovered", "peer.unreachable"):
            self.refresh_peers()
            self.refresh_members()
            self.refresh_status()
            if self.session_stack.currentIndex() == 0:
                self.login_page.refresh()
            else:
                self.active_page.refresh()
            if topic == "peer.unreachable":
                self.toast.show_message("Компьютер найден, но соединение не проходит — откройте «Диагностика»",
                                        "error", 6000)
        elif topic == "security.warning":
            self.refresh_peers()
            QMessageBox.warning(self, "Предупреждение безопасности",
                                f"У компьютера {data.get('display_name') or data['device_id'][:8]} изменился ключ.\n"
                                f"Было: {data['old_fingerprint'][:16]}…\nСтало: {data['new_fingerprint'][:16]}…\n\n"
                                "Соединение отклонено. Решение — вкладка «Компьютеры» (правый клик).")
        elif topic in ("transfer.progress", "transfer.done", "transfer.failed", "transfer.queued"):
            if topic == "transfer.done":
                self.toast.show_message(f"Файл «{data['filename']}» получен", "ok")
                self.notify("Файл получен", data["filename"])
            elif topic == "transfer.failed":
                self.toast.show_message(f"«{data['filename']}»: {data['reason']}", "error", 7000)
            if self.tabs.currentIndex() == TAB_FILES:
                self.refresh_files()
        elif topic == "session.changed":
            self.refresh_all()
        elif topic == "session.closed":
            reason = data.get("reason")
            self.refresh_all()
            self.tabs.setCurrentIndex(TAB_SESSION)
            if reason == "remote":
                self.toast.show_message("Преподаватель завершил урок", "info", 6000)
            elif reason == "expired":
                self.toast.show_message("Время урока истекло — урок завершён", "info", 6000)
            elif reason == "teacher":
                self.toast.show_message("Урок завершён для всех участников", "ok")
        elif topic == "role.changed":
            self.refresh_all()
            self.toast.show_message("Вы вошли как преподаватель", "ok")
        elif topic == "role.rejected":
            self.toast.show_message(data.get("reason", "PIN не подошёл"), "error", 6000)
        elif topic == "role.pending":
            self.toast.show_message("Ждём данные урока от преподавателя, чтобы проверить PIN…", "info")
        elif topic == "settings.theme":
            QApplication.instance().setStyleSheet(qss(data["theme"]))
            self.apply_theme_assets(data["theme"])
        elif topic == "protocol.mismatch":
            self.toast.show_message("Версия LocalClass на этом компьютере устарела — обновите приложение",
                                    "error", 8000)
        elif topic == "peer.manual_failed":
            self.toast.show_message(f"{data['address']}: {data['error']}", "error", 6000)
        elif topic == "diagnostics.result":
            self.show_diag(data)

    def _on_core_event(self, data: dict) -> None:
        ev = data["event"]
        t = ev["type"]
        if t in ("MESSAGE_CREATED", "MESSAGE_DELETED"):
            self.refresh_messages()
            self.refresh_links()
            if t == "MESSAGE_CREATED" and not data["local"]:
                who = self.node._name_of(ev["device_id"])
                text = str(ev["payload"].get("text", ""))[:120]
                self.notify(who, text)
                if self.tabs.currentIndex() != TAB_CHAT:
                    self.toast.show_message(f"{who}: {text[:60]}", "info", 2500)
        elif t in ("LINK_CREATED", "LINK_DELETED"):
            self.refresh_links()
        elif t.startswith("FILE_"):
            self.refresh_files()
            if t == "FILE_OFFERED" and not data["local"]:
                self.toast.show_message(f"{self.node._name_of(ev['device_id'])} выложил файл "
                                        f"«{ev['payload'].get('filename', '')}»", "info", 4000)
        elif t.startswith("CHANNEL_"):
            self.refresh_channels()
        elif t in ("USER_JOINED", "USER_LEFT", "PERMISSIONS_UPDATED"):
            self.refresh_members()
            self.refresh_channels()
            self.active_page.refresh()
            if t == "USER_JOINED" and not data["local"]:
                self.toast.show_message(f"{ev['payload'].get('display_name') or 'Участник'} присоединился к уроку",
                                        "info", 2500)
        elif t.startswith("SESSION_"):
            self.refresh_all()

    def notify(self, title: str, text: str) -> None:
        if not self.isActiveWindow():
            self.tray.showMessage(title, text, QSystemTrayIcon.Information, 4000)

    # ---------------- обновление
    def _on_tab_changed(self, idx: int) -> None:
        if idx == TAB_PEERS:
            self.refresh_peers()
        elif idx == TAB_FILES:
            self.refresh_files()
        elif idx == TAB_DIAG:
            self.run_diag()
        elif idx == TAB_LOGS:
            self.refresh_logs()
        elif idx == TAB_SETTINGS:
            self.refresh_interfaces()
        elif idx == TAB_CHAT:
            self.refresh_messages()
            self.refresh_links()

    def refresh_periodic(self) -> None:
        self.refresh_status()
        if self.session_stack.currentIndex() == 0:
            self.login_page.refresh()       # TTL карточек: пропавшие уроки исчезают сами
        else:
            self.active_page.refresh()
        idx = self.tabs.currentIndex()
        if idx == TAB_PEERS:
            self.refresh_peers()
        elif idx == TAB_FILES:
            self.refresh_transfers()

    def refresh_status(self) -> None:
        st = self.node.status()
        s = st["session"]
        in_session = bool(s)
        self.tabs.setTabEnabled(TAB_CHAT, in_session)
        self.tabs.setTabEnabled(TAB_FILES, in_session)
        where = f"урок «{s['name']}» · код {s['code']}" if in_session else "вы не в уроке"
        role = " · преподаватель" if in_session and self.node.is_teacher else ""
        self.status_label.setText(
            f"{st['display_name']}{role} · {where} · на связи {st['connected']} из {st['known']} · "
            f"порт {st['port']}")
        self.status_label.setToolTip("Адреса: " + ", ".join(st["addresses"]))
        self.setWindowTitle(f"LocalClass {__version__} — {st['display_name']}"
                            + (f" — {s['name']}" if in_session else ""))

    def refresh_all(self) -> None:
        in_session = bool(self.node.session_id)
        self.session_stack.setCurrentIndex(1 if in_session else 0)
        if in_session:
            self.active_page.refresh()
        else:
            self.login_page.refresh()
            if self.tabs.currentIndex() in (TAB_CHAT, TAB_FILES):
                self.tabs.setCurrentIndex(TAB_SESSION)
        self.refresh_status()
        self.refresh_channels()
        self.refresh_members()
        self.refresh_messages()
        self.refresh_links()
        self.refresh_files()
        self.refresh_peers()
        self.refresh_interfaces()
        self.refresh_logs()
        self.run_diag()

    def resizeEvent(self, e) -> None:
        super().resizeEvent(e)
        self.toast.reposition()

    def closeEvent(self, e) -> None:
        self.timer.stop()     # иначе таймер успеет обратиться к уже закрытой базе
        self.bridge.stop()
        super().closeEvent(e)
