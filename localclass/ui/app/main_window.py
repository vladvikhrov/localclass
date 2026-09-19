"""Полноценный GUI (Sprint 6B). Все действия — команды ядра через Bridge; всё состояние — из событий шины."""
from __future__ import annotations

import html
import os
import re
import time
from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt, QTimer, QUrl
from PySide6.QtGui import QColor, QDesktopServices, QGuiApplication, QIcon, QPixmap, QPainter
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QFileDialog, QFormLayout, QGroupBox, QHBoxLayout,
                               QHeaderView, QInputDialog, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMainWindow,
                               QMenu, QMessageBox, QPlainTextEdit, QProgressBar, QPushButton, QSpinBox, QSplitter,
                               QSystemTrayIcon, QTableWidget, QTableWidgetItem, QTabWidget, QTextBrowser, QTextEdit,
                               QVBoxLayout, QWidget)

from ... import __version__
from ...core.session import remaining_seconds
from .bridge import Bridge
from .qr import QRWidget

STATUS_RU = {"connected": "доступен", "discovered": "обнаружен", "unreachable": "недоступен", "blocked": "заблокирован",
             "conflict": "⚠ ключ изменился", "other_session": "другая сессия"}
URL_RE = re.compile(r"(https?://[^\s<>\"]+)")


def fmt_size(n: float) -> str:
    for unit in ("Б", "КБ", "МБ", "ГБ", "ТБ"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "Б" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} ПБ"


def fmt_eta(sec: float | None) -> str:
    if sec is None:
        return "—"
    sec = int(sec)
    if sec < 60:
        return f"{sec} с"
    if sec < 3600:
        return f"{sec // 60} мин {sec % 60} с"
    return f"{sec // 3600} ч {(sec % 3600) // 60} мин"


def app_icon() -> QIcon:
    pm = QPixmap(64, 64)
    pm.fill(QColor("#2b6cb0"))
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
    """Enter — отправить, Shift+Enter — перевод строки."""

    def __init__(self, on_send, parent=None):
        super().__init__(parent)
        self.on_send = on_send
        self.setMaximumHeight(80)
        self.setPlaceholderText("Сообщение… (Enter — отправить, Shift+Enter — новая строка, поддерживается Markdown)")

    def keyPressEvent(self, e) -> None:
        if e.key() in (Qt.Key_Return, Qt.Key_Enter) and not (e.modifiers() & Qt.ShiftModifier):
            self.on_send()
            return
        super().keyPressEvent(e)


class MainWindow(QMainWindow):
    def __init__(self, bridge: Bridge):
        super().__init__()
        self.bridge = bridge
        self.node = bridge.node
        self.channel = "general"
        self.dm_peer: str | None = None
        self.setWindowTitle(f"LocalClass {__version__} — {self.node.display_name}")
        self.setWindowIcon(app_icon())
        self.resize(1100, 720)
        self.setAcceptDrops(True)
        self._log_app: list[dict] = list(self.node.app_log.records)
        self._log_events: list[dict] = list(self.node.eventlog.records)
        self._diag: dict[str, Any] | None = None

        self.tabs = QTabWidget()
        self.setCentralWidget(self.tabs)
        self.tabs.addTab(self._build_session_tab(), "Сессия")
        self.tabs.addTab(self._build_chat_tab(), "Чат")
        self.tabs.addTab(self._build_files_tab(), "Файлы")
        self.tabs.addTab(self._build_peers_tab(), "Пиры")
        self.tabs.addTab(self._build_diag_tab(), "Диагностика")
        self.tabs.addTab(self._build_logs_tab(), "Логи")
        self.tabs.addTab(self._build_settings_tab(), "Настройки")
        self.status_label = QLabel()
        self.statusBar().addPermanentWidget(self.status_label, 1)

        self.tray = QSystemTrayIcon(app_icon(), self)
        menu = QMenu()
        menu.addAction("Показать", self.showNormal)
        menu.addAction("Выход", QApplication.instance().quit)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(lambda r: self.showNormal() if r == QSystemTrayIcon.Trigger else None)
        self.tray.show()

        bridge.bus.connect(self.on_bus)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh_periodic)
        self.timer.start(3000)
        self.refresh_all()

    # ------------------------------------------------------------------ helpers
    def call(self, coro, on_done=None) -> None:
        self.bridge.call(coro, on_done, self.show_error)

    def show_error(self, msg: str) -> None:
        self.statusBar().showMessage(f"Ошибка: {msg}", 8000)
        QMessageBox.warning(self, "LocalClass", msg)

    def notify(self, title: str, text: str) -> None:
        if not self.isActiveWindow():
            self.tray.showMessage(title, text, QSystemTrayIcon.Information, 4000)

    # ------------------------------------------------------------------ session tab
    def _build_session_tab(self) -> QWidget:
        w = QWidget()
        lay = QHBoxLayout(w)
        left = QVBoxLayout()
        self.session_info = QLabel()
        self.session_info.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.session_info.setWordWrap(True)
        left.addWidget(self.session_info)

        g1 = QGroupBox("Создать сессию (преподаватель)")
        f1 = QFormLayout(g1)
        self.new_session_name = QLineEdit("Python Evening")
        self.new_session_duration = QSpinBox()
        self.new_session_duration.setRange(5, 24 * 60)
        self.new_session_duration.setValue(120)
        self.new_session_duration.setSuffix(" мин")
        f1.addRow("Название", self.new_session_name)
        f1.addRow("Длительность", self.new_session_duration)
        b = QPushButton("Создать")
        b.clicked.connect(self.create_session)
        f1.addRow(b)
        left.addWidget(g1)

        g2 = QGroupBox("Подключиться (ученик)")
        f2 = QFormLayout(g2)
        self.join_code = QLineEdit()
        self.join_code.setPlaceholderText("K7F2-X9")
        bj = QPushButton("По коду")
        bj.clicked.connect(self.join_by_code)
        f2.addRow("Код сессии", self.join_code)
        f2.addRow(bj)
        self.seen_sessions = QListWidget()
        self.seen_sessions.itemDoubleClicked.connect(lambda it: self.call(self.node.join_session(it.data(Qt.UserRole), it.text().split("  ")[1] if "  " in it.text() else "", it.text().split("  ")[0]), lambda _: self.refresh_all()))
        f2.addRow("Сессии в сети\n(двойной клик)", self.seen_sessions)
        self.join_qr_text = QLineEdit()
        self.join_qr_text.setPlaceholderText('вставьте содержимое QR: {"version":1,"session_id":...}')
        bq = QPushButton("По QR (текст)")
        bq.clicked.connect(lambda: self.call(self.node.join_by_qr(self.join_qr_text.text()), lambda _: self.refresh_all()))
        f2.addRow("QR", self.join_qr_text)
        f2.addRow(bq)
        left.addWidget(g2)

        row = QHBoxLayout()
        bl = QPushButton("Выйти из сессии")
        bl.clicked.connect(lambda: self.call(self.node.leave_session(), lambda _: self.refresh_all()))
        self.btn_close_session = QPushButton("Закрыть сессию (преподаватель)")
        self.btn_close_session.clicked.connect(self.close_session)
        row.addWidget(bl)
        row.addWidget(self.btn_close_session)
        left.addLayout(row)
        left.addStretch()
        lay.addLayout(left, 1)

        right = QVBoxLayout()
        self.qr = QRWidget()
        right.addWidget(self.qr, 1)
        self.code_label = QLabel()
        self.code_label.setAlignment(Qt.AlignCenter)
        self.code_label.setStyleSheet("font-size: 28px; font-weight: bold; letter-spacing: 3px;")
        right.addWidget(self.code_label)
        bc = QPushButton("Копировать текст QR")
        bc.clicked.connect(self.copy_qr)
        right.addWidget(bc)
        lay.addLayout(right, 1)
        return w

    def create_session(self) -> None:
        self.call(self.node.create_session(self.new_session_name.text(), self.new_session_duration.value()),
                  lambda s: (self.refresh_all(), self.statusBar().showMessage(f"Сессия создана, код {s['code']}", 5000)))

    def join_by_code(self) -> None:
        self.call(self.node.join_by_code(self.join_code.text()), lambda _: self.refresh_all())

    def close_session(self) -> None:
        if QMessageBox.question(self, "LocalClass", "Закрыть сессию для всех участников?") == QMessageBox.Yes:
            self.call(self.node.close_session(), lambda _: self.refresh_all())

    def copy_qr(self) -> None:
        try:
            QGuiApplication.clipboard().setText(self.node.qr_text())
            self.statusBar().showMessage("Скопировано", 3000)
        except Exception as e:  # noqa: BLE001
            self.show_error(str(e))

    def refresh_session(self) -> None:
        s = self.node.current_session()
        st = self.node.status()
        if s:
            rem = remaining_seconds(s)
            self.session_info.setText(
                f"<b>Сессия:</b> {html.escape(s['name'])} &nbsp; <b>код:</b> {s['code']}<br>"
                f"<b>session_id:</b> <code>{s['session_id']}</code><br>"
                f"<b>Роль:</b> {'преподаватель' if self.node.is_teacher else 'ученик'} &nbsp; "
                f"<b>Осталось:</b> {fmt_eta(rem) if rem is not None else '—'}<br>"
                f"<b>Участников подключено:</b> {st['connected']} &nbsp; <b>событий:</b> {st['events']}")
            try:
                self.qr.set_text(self.node.qr_text())
            except Exception:  # noqa: BLE001
                self.qr.set_text("")
            self.code_label.setText(s["code"])
        else:
            self.session_info.setText("<b>Вы не в сессии.</b> Создайте сессию или подключитесь по коду / QR.")
            self.qr.set_text("")
            self.code_label.setText("")
        self.btn_close_session.setEnabled(self.node.is_teacher)
        self.seen_sessions.clear()
        for ss in self.node.sessions_in_network():
            it = QListWidgetItem(f"{ss['code']}  {ss['name']}")
            it.setData(Qt.UserRole, ss["session_id"])
            self.seen_sessions.addItem(it)
        self.status_label.setText(
            f"{self.node.display_name} · id {self.node.device_id[:8]} · порт {st['port']} · "
            f"{', '.join(st['addresses'][:3])} · пиров {st['connected']}/{st['known']} · lamport {st['lamport']}")

    # ------------------------------------------------------------------ chat tab
    def _build_chat_tab(self) -> QWidget:
        w = QWidget()
        lay = QHBoxLayout(w)
        split = QSplitter()
        lay.addWidget(split)
        left = QWidget()
        ll = QVBoxLayout(left)
        ll.addWidget(QLabel("<b>Каналы</b>"))
        self.channel_list = QListWidget()
        self.channel_list.currentItemChanged.connect(self.on_channel_selected)
        ll.addWidget(self.channel_list, 2)
        row = QHBoxLayout()
        self.btn_new_channel = QPushButton("+ канал")
        self.btn_new_channel.clicked.connect(self.new_channel)
        self.btn_close_channel = QPushButton("закрыть")
        self.btn_close_channel.clicked.connect(self.close_channel)
        row.addWidget(self.btn_new_channel)
        row.addWidget(self.btn_close_channel)
        ll.addLayout(row)
        ll.addWidget(QLabel("<b>Участники</b> (двойной клик — личное сообщение)"))
        self.member_list = QListWidget()
        self.member_list.itemDoubleClicked.connect(self.open_dm)
        self.member_list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.member_list.customContextMenuRequested.connect(self.member_menu)
        ll.addWidget(self.member_list, 2)
        split.addWidget(left)

        center = QWidget()
        cl = QVBoxLayout(center)
        self.chat_title = QLabel("<b>#general</b>")
        cl.addWidget(self.chat_title)
        self.search_box = QLineEdit()
        self.search_box.setPlaceholderText("Поиск по сообщениям…")
        self.search_box.textChanged.connect(self.refresh_messages)
        cl.addWidget(self.search_box)
        self.messages_view = QTextBrowser()
        self.messages_view.setOpenExternalLinks(True)
        self.messages_view.setContextMenuPolicy(Qt.CustomContextMenu)
        self.messages_view.customContextMenuRequested.connect(self.message_menu)
        cl.addWidget(self.messages_view, 1)
        self.announce_cb = QCheckBox("Объявление (преподаватель)")
        cl.addWidget(self.announce_cb)
        self.msg_input = MessageInput(self.send_message)
        cl.addWidget(self.msg_input)
        row2 = QHBoxLayout()
        bs = QPushButton("Отправить")
        bs.clicked.connect(self.send_message)
        bf = QPushButton("Файл…")
        bf.clicked.connect(self.share_file_dialog)
        bl = QPushButton("Ссылка…")
        bl.clicked.connect(self.share_link_dialog)
        bcl = QPushButton("Из буфера")
        bcl.clicked.connect(self.share_clipboard)
        for b in (bs, bf, bl, bcl):
            row2.addWidget(b)
        cl.addLayout(row2)
        split.addWidget(center)

        right = QWidget()
        rl = QVBoxLayout(right)
        rl.addWidget(QLabel("<b>Лента ссылок</b>"))
        self.links_view = QTextBrowser()
        self.links_view.setOpenExternalLinks(True)
        rl.addWidget(self.links_view)
        split.addWidget(right)
        split.setSizes([220, 620, 260])
        return w

    def on_channel_selected(self, item: QListWidgetItem | None, _prev=None) -> None:
        if item is None:
            return
        self.channel = item.data(Qt.UserRole)
        self.dm_peer = None
        self.chat_title.setText(f"<b>#{html.escape(item.text())}</b>")
        self.refresh_messages()

    def open_dm(self, item: QListWidgetItem) -> None:
        did = item.data(Qt.UserRole)
        if did == self.node.device_id:
            return
        self.dm_peer = did
        self.channel = f"dm:{':'.join(sorted([self.node.device_id, did]))}"
        self.chat_title.setText(f"<b>Личные сообщения: {html.escape(item.text())}</b>")
        self.refresh_messages()

    def member_menu(self, pos) -> None:
        it = self.member_list.itemAt(pos)
        if not it or not self.node.is_teacher:
            return
        did = it.data(Qt.UserRole)
        m = self.node.store.member(self.node.session_id, did) or {}
        menu = QMenu(self)
        menu.addAction("Разблокировать" if m.get("blocked") else "Заблокировать в чате",
                       lambda: self.call(self.node.set_blocked(did, not m.get("blocked")), lambda _: self.refresh_members()))
        menu.addAction("Отправить файл…", lambda: self.send_file_to(did))
        menu.exec(self.member_list.mapToGlobal(pos))

    def message_menu(self, pos) -> None:
        anchor = self.messages_view.anchorAt(pos)
        menu = self.messages_view.createStandardContextMenu()
        if anchor.startswith("msg:"):
            mid = anchor[4:]
            menu.addSeparator()
            menu.addAction("Удалить сообщение", lambda: self.call(self.node.delete_message(mid), lambda _: self.refresh_messages()))
        menu.exec(self.messages_view.mapToGlobal(pos))

    def new_channel(self) -> None:
        name, ok = QInputDialog.getText(self, "Новый канал", "Имя канала (латиница, цифры, -):")
        if ok and name:
            self.call(self.node.create_channel(name, name), lambda _: self.refresh_channels())

    def close_channel(self) -> None:
        if self.channel and not self.channel.startswith("dm:"):
            self.call(self.node.close_channel(self.channel), lambda _: self.refresh_channels())

    def send_message(self) -> None:
        text = self.msg_input.toPlainText().strip()
        if not text:
            return
        kind = "announcement" if self.announce_cb.isChecked() else "message"
        self.call(self.node.send_message(text, self.channel, kind=kind, to=self.dm_peer), lambda _: self.msg_input.clear())

    def share_link_dialog(self) -> None:
        url, ok = QInputDialog.getText(self, "Ссылка", "URL:")
        if ok and url:
            ch = self.channel if not self.channel.startswith("dm:") else "general"
            self.call(self.node.send_link(url, "", ch), lambda _: self.refresh_links())

    def share_clipboard(self) -> None:
        cb = QGuiApplication.clipboard()
        md = cb.mimeData()
        if md.hasImage():
            img = cb.image()
            p = self.node.paths.root / "storage" / "clipboard"
            p.mkdir(parents=True, exist_ok=True)
            path = p / f"screenshot-{time.strftime('%Y%m%d-%H%M%S')}.png"
            img.save(str(path))
            self.share_file(str(path))
        elif md.hasUrls() and md.urls()[0].isLocalFile():
            self.share_file(md.urls()[0].toLocalFile())
        elif md.hasText():
            t = md.text().strip()
            if URL_RE.fullmatch(t):
                self.call(self.node.send_link(t, "", "general"), lambda _: self.refresh_links())
            else:
                self.msg_input.setPlainText(t)

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
        self.channel_list.blockSignals(False)
        teacher = self.node.is_teacher
        self.btn_new_channel.setEnabled(teacher)
        self.btn_close_channel.setEnabled(teacher)
        self.announce_cb.setVisible(teacher)

    def refresh_members(self) -> None:
        self.member_list.clear()
        if not self.node.session_id:
            return
        connected = set(self.node.mesh.connected_ids())
        for m in self.node.store.members(self.node.session_id):
            if m["left"]:
                continue
            online = m["device_id"] in connected or m["device_id"] == self.node.device_id
            mark = "● " if online else "○ "
            role = " (преп.)" if m["role"] == "teacher" else ""
            blocked = " [заблокирован]" if m["blocked"] else ""
            it = QListWidgetItem(f"{mark}{m['display_name'] or m['device_id'][:8]}{role}{blocked}")
            it.setData(Qt.UserRole, m["device_id"])
            it.setForeground(QColor("#1a7f37") if online else QColor("gray"))
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
            self.messages_view.setHtml("<i>Нет активной сессии</i>")
            return
        q = self.search_box.text().strip()
        msgs = self.node.store.search_messages(self.node.session_id, q) if q else self.node.store.messages(self.node.session_id, self.channel)
        parts = []
        for m in msgs:
            ts = time.strftime("%H:%M", time.localtime(m["timestamp"]))
            who = html.escape(m.get("display_name") or self.node._name_of(m["device_id"]))
            body = self._render_text(m["text"])
            style = ' style="background:#fff3cd;padding:4px"' if m["kind"] == "announcement" else ""
            ch = f" <span style='color:#888'>#{html.escape(m['channel'])}</span>" if q else ""
            parts.append(f'<div{style}><a name="msg:{m["message_id"]}" href="msg:{m["message_id"]}" style="text-decoration:none;color:#888">{ts}</a> '
                         f'<b>{who}</b>{ch}{" 📣" if m["kind"] == "announcement" else ""}: {body}</div>')
        self.messages_view.setHtml("".join(parts) or "<i>Сообщений пока нет</i>")
        self.messages_view.verticalScrollBar().setValue(self.messages_view.verticalScrollBar().maximum())

    def refresh_links(self) -> None:
        if not self.node.session_id:
            self.links_view.clear()
            return
        parts = []
        for l in self.node.store.links(self.node.session_id):
            who = html.escape(l.get("display_name") or l["device_id"][:8])
            url = html.escape(l["url"])
            parts.append(f'<div><b>{who}</b>: <a href="{url}">{html.escape(l["title"] or l["url"])}</a></div>')
        self.links_view.setHtml("".join(parts) or "<i>Ссылок пока нет</i>")

    # ------------------------------------------------------------------ files tab
    def _build_files_tab(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.addWidget(QLabel("<b>Файлы сессии</b> — перетащите файл в окно или нажмите «Опубликовать файл». "
                             "Двойной клик — скачать / открыть папку."))
        self.files_table = QTableWidget(0, 5)
        self.files_table.setHorizontalHeaderLabels(["Файл", "Размер", "Владелец", "Статус", "file_id"])
        self.files_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.files_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.files_table.itemDoubleClicked.connect(self.file_double_clicked)
        self.files_table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.files_table.customContextMenuRequested.connect(self.file_menu)
        lay.addWidget(self.files_table, 2)
        row = QHBoxLayout()
        b1 = QPushButton("Опубликовать файл…")
        b1.clicked.connect(self.share_file_dialog)
        b2 = QPushButton("Скачать выбранный")
        b2.clicked.connect(self.download_selected)
        b3 = QPushButton("Открыть папку с файлами")
        b3.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.node.paths.files))))
        for b in (b1, b2, b3):
            row.addWidget(b)
        row.addStretch()
        lay.addLayout(row)
        lay.addWidget(QLabel("<b>Передачи</b>"))
        self.transfers_table = QTableWidget(0, 6)
        self.transfers_table.setHorizontalHeaderLabels(["Файл", "Направление", "Пир", "Прогресс", "Скорость / осталось", "Статус"])
        self.transfers_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        lay.addWidget(self.transfers_table, 1)
        return w

    def share_file_dialog(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Опубликовать файл")
        if path:
            self.share_file(path)

    def share_file(self, path: str, peer: str | None = None) -> None:
        size = os.path.getsize(path)
        recipients = 1 if peer else max(1, len(self.node.mesh.connected_ids()))
        est = self.node.transfers.estimate_traffic(size, recipients)
        if est["warn"]:
            r = QMessageBox.question(self, "Большой файл", f"Файл {fmt_size(size)}. Если его скачают все {recipients} участ., "
                                     f"это примерно {est['gb']:.1f} ГБ трафика по Wi-Fi — передача займёт значительное время.\n\nПродолжить?")
            if r != QMessageBox.Yes:
                return
        ch = self.channel if not self.channel.startswith("dm:") else "general"
        coro = self.node.transfers.send_file_to(peer, path) if peer else self.node.transfers.share_file(path, ch)
        self.call(coro, lambda _: (self.refresh_files(), self.statusBar().showMessage("Файл опубликован", 4000)))

    def send_file_to(self, peer: str) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Отправить файл участнику")
        if path:
            self.share_file(path, peer)

    def _selected_file_id(self) -> str | None:
        r = self.files_table.currentRow()
        return self.files_table.item(r, 4).text() if r >= 0 else None

    def download_selected(self) -> None:
        fid = self._selected_file_id()
        if fid:
            self.call(self.node.transfers.download(fid), lambda _: self.refresh_files())

    def file_double_clicked(self, item: QTableWidgetItem) -> None:
        r = item.row()
        fid = self.files_table.item(r, 4).text()
        f = self.node.store.shared_file(fid)
        if f and f.get("local_path") and Path(f["local_path"]).exists():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(Path(f["local_path"]).parent)))   # папку, не файл (ТЗ 12.8)
        else:
            self.call(self.node.transfers.download(fid), lambda _: self.refresh_files())

    def file_menu(self, pos) -> None:
        fid = self._selected_file_id()
        if not fid:
            return
        menu = QMenu(self)
        menu.addAction("Скачать", self.download_selected)
        menu.addAction("Удалить (владелец/преподаватель)", lambda: self.call(self.node.delete_file(fid), lambda _: self.refresh_files()))
        menu.exec(self.files_table.mapToGlobal(pos))

    def refresh_files(self) -> None:
        self.files_table.setRowCount(0)
        if not self.node.session_id:
            return
        transfers = {t["file_id"]: t for t in self.node.store.transfers(self.node.session_id) if t["direction"] == "in"}
        for f in self.node.store.shared_files(self.node.session_id):
            r = self.files_table.rowCount()
            self.files_table.insertRow(r)
            if f["owner_id"] == self.node.device_id:
                status = "мой файл"
            elif f.get("local_path"):
                status = "получен"
            elif f["file_id"] in transfers:
                t = transfers[f["file_id"]]
                status = {"active": "загружается", "queued": "в очереди", "paused": "пауза: " + (t["error"] or ""), "failed": "ошибка: " + (t["error"] or ""), "done": "получен"}.get(t["status"], t["status"])
            else:
                status = "доступен" if self.node.mesh.get(f["owner_id"]) else "владелец не в сети"
            for c, v in enumerate([f["filename"], fmt_size(f["size"]), f.get("owner_name") or f["owner_id"][:8], status, f["file_id"]]):
                self.files_table.setItem(r, c, QTableWidgetItem(v))
        self.refresh_transfers()

    def refresh_transfers(self) -> None:
        self.transfers_table.setRowCount(0)
        for t in self.node.store.transfers(self.node.session_id)[:50] if self.node.session_id else []:
            r = self.transfers_table.rowCount()
            self.transfers_table.insertRow(r)
            prog = self.node.transfers.progress.get(t["transfer_id"])
            bar = QProgressBar()
            bar.setRange(0, 100)
            if t["status"] == "done":
                bar.setValue(100)
            elif prog:
                bar.setValue(int(prog["percent"]))
            else:
                from ...core.store import Bitmap
                bm = Bitmap(t["chunk_count"], t["received_chunks"])
                bar.setValue(int(100 * bm.received() / max(1, t["chunk_count"])))
            speed = f"{fmt_size(prog['speed'])}/с · {fmt_eta(prog['eta'])}" if prog and t["status"] == "active" else ""
            self.transfers_table.setItem(r, 0, QTableWidgetItem(t["filename"]))
            self.transfers_table.setItem(r, 1, QTableWidgetItem("приём" if t["direction"] == "in" else "отдача"))
            self.transfers_table.setItem(r, 2, QTableWidgetItem(self.node._name_of(t["peer_id"])))
            self.transfers_table.setCellWidget(r, 3, bar)
            self.transfers_table.setItem(r, 4, QTableWidgetItem(speed))
            self.transfers_table.setItem(r, 5, QTableWidgetItem(f"{t['status']} {t['error'] or ''}".strip()))

    def dragEnterEvent(self, e) -> None:
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e) -> None:
        for u in e.mimeData().urls():
            if u.isLocalFile() and os.path.isfile(u.toLocalFile()):
                self.share_file(u.toLocalFile())

    # ------------------------------------------------------------------ peers tab
    def _build_peers_tab(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        self.peers_table = QTableWidget(0, 7)
        self.peers_table.setHorizontalHeaderLabels(["Имя", "device_id", "Статус", "Адрес", "RTT", "Версия", "Ошибка"])
        self.peers_table.horizontalHeader().setSectionResizeMode(6, QHeaderView.Stretch)
        self.peers_table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.peers_table.customContextMenuRequested.connect(self.peer_menu)
        lay.addWidget(self.peers_table)
        row = QHBoxLayout()
        self.manual_host = QLineEdit()
        self.manual_host.setPlaceholderText("IP:порт, например 192.168.1.10:45821")
        b = QPushButton("Подключиться вручную")
        b.clicked.connect(self.connect_manual)
        row.addWidget(self.manual_host, 1)
        row.addWidget(b)
        lay.addLayout(row)
        lay.addWidget(QLabel("«обнаружен» — виден в discovery, но TCP-соединения нет; «недоступен» — попытки TCP не удались "
                             "(Firewall / изоляция клиентов). ⚠ — у узла изменился ключ: правый клик, чтобы решить."))
        return w

    def connect_manual(self) -> None:
        host, _, port = self.manual_host.text().strip().rpartition(":")
        if not host or not port.isdigit():
            self.show_error("формат: IP:порт")
            return
        self.call(self.node.connect_manual(host, int(port)), lambda _: self.statusBar().showMessage("Подключаемся…", 3000))

    def peer_menu(self, pos) -> None:
        r = self.peers_table.currentRow()
        if r < 0:
            return
        did = self.peers_table.item(r, 1).data(Qt.UserRole)
        menu = QMenu(self)
        st = self.node.mesh.peer_status(did)
        if st == "conflict":
            menu.addAction("Принять новый ключ (доверять)", lambda: self.call(self.node.trust_peer(did, True), lambda _: self.refresh_peers()))
            menu.addAction("Отклонить (блокировать)", lambda: self.call(self.node.trust_peer(did, False), lambda _: self.refresh_peers()))
        menu.addAction("Отправить файл…", lambda: self.send_file_to(did))
        menu.exec(self.peers_table.mapToGlobal(pos))

    def refresh_peers(self) -> None:
        self.peers_table.setRowCount(0)
        for p in self.node.peers_view():
            r = self.peers_table.rowCount()
            self.peers_table.insertRow(r)
            vals = [p["display_name"], p["device_id"][:16], STATUS_RU.get(p["status"], p["status"]),
                    f"{p['address'] or (p['addresses'][0] if p['addresses'] else '?')}:{p['port']}",
                    f"{p['rtt'] * 1000:.0f} мс" if p["rtt"] else "", p["app_version"], p["error"] or ""]
            for c, v in enumerate(vals):
                it = QTableWidgetItem(v)
                if c == 1:
                    it.setData(Qt.UserRole, p["device_id"])
                if c == 2:
                    it.setForeground(QColor({"connected": "#1a7f37", "unreachable": "#c62828", "conflict": "#c62828"}.get(p["status"], "#555")))
                self.peers_table.setItem(r, c, it)

    # ------------------------------------------------------------------ diagnostics tab
    def _build_diag_tab(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        self.diag_view = QTextBrowser()
        lay.addWidget(self.diag_view, 1)
        row = QHBoxLayout()
        b1 = QPushButton("Повторить")
        b1.clicked.connect(self.run_diag)
        b2 = QPushButton("Подключиться вручную…")
        b2.clicked.connect(lambda: self.tabs.setCurrentIndex(3))
        row.addWidget(b1)
        row.addWidget(b2)
        row.addStretch()
        lay.addLayout(row)
        return w

    def run_diag(self) -> None:
        self.call(self.node.run_diagnostics(), self.show_diag)

    def show_diag(self, r: dict[str, Any]) -> None:
        self._diag = r
        rows = "".join(f"<tr><td>{html.escape(c['name'])}</td><td style='color:{'#1a7f37' if c['ok'] else '#c62828'}'><b>{'✓' if c['ok'] else '✗'}</b></td>"
                       f"<td style='color:#666'>{html.escape(str(c['detail']))}</td></tr>" for c in r["checks"])
        reasons = "".join(f"<li>{html.escape(x)}</li>" for x in r["reasons"]) or "<li>проблем не обнаружено</li>"
        peers = "".join(f"<li>{html.escape(p['display_name'] or p['device_id'][:8])} — {STATUS_RU.get(p['status'], p['status'])} {html.escape(p['error'] or '')}</li>" for p in r["peers"])
        self.diag_view.setHtml(
            f"<h3>Диагностика сети</h3><table cellpadding=4>{rows}</table>"
            f"<p><b>Обнаружено узлов:</b> {r['discovered']} &nbsp; <b>Доступно:</b> {r['reachable']} &nbsp; "
            f"<b>Недоступно:</b> {r['unreachable']} &nbsp; <b>В других сессиях:</b> {r['other_sessions']}</p>"
            f"<p><b>Возможные причины:</b></p><ul>{reasons}</ul><p><b>Узлы сессии:</b></p><ul>{peers or '<li>—</li>'}</ul>"
            f"<p style='color:#888'>{time.strftime('%H:%M:%S', time.localtime(r['ts']))}</p>")

    # ------------------------------------------------------------------ logs tab
    def _build_logs_tab(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        row = QHBoxLayout()
        self.log_kind = QComboBox()
        self.log_kind.addItems(["Журнал событий", "Технический лог"])
        self.log_level = QComboBox()
        self.log_level.addItems(["Все", "INFO", "WARNING", "ERROR", "DEBUG"])
        self.log_cat = QComboBox()
        self.log_cat.addItems(["Все", "Сеть", "Файлы", "Чат", "Сессия", "Безопасность"])
        self.log_filter = QLineEdit()
        self.log_filter.setPlaceholderText("фильтр по тексту / узлу")
        for x in (self.log_kind, self.log_level, self.log_cat):
            x.currentIndexChanged.connect(self.refresh_logs)
            row.addWidget(x)
        self.log_filter.textChanged.connect(self.refresh_logs)
        row.addWidget(self.log_filter, 1)
        bo = QPushButton("Папка логов")
        bo.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.node.paths.logs))))
        row.addWidget(bo)
        lay.addLayout(row)
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(5000)
        lay.addWidget(self.log_view)
        return w

    def refresh_logs(self) -> None:
        cat_map = {"Сеть": "net", "Файлы": "files", "Чат": "chat", "Сессия": "session", "Безопасность": "security"}
        cat = cat_map.get(self.log_cat.currentText())
        level = self.log_level.currentText()
        text = self.log_filter.text().lower()
        lines = []
        if self.log_kind.currentIndex() == 0:
            for r in self._log_events[-2000:]:
                if cat and r["category"] != cat:
                    continue
                s = f"{time.strftime('%H:%M:%S', time.localtime(r['ts']))} {r['type']:20} {r['text']}"
                if text and text not in s.lower() and text not in (r.get("device_id") or ""):
                    continue
                lines.append(s)
        else:
            for r in self._log_app[-3000:]:
                if level != "Все" and r["level"] != level:
                    continue
                if cat and r["category"] != cat:
                    continue
                s = f"{time.strftime('%H:%M:%S', time.localtime(r['ts']))} {r['level']:7} {r['category']:8} {r['message']}"
                if text and text not in s.lower():
                    continue
                lines.append(s)
        self.log_view.setPlainText("\n".join(lines))
        self.log_view.verticalScrollBar().setValue(self.log_view.verticalScrollBar().maximum())

    # ------------------------------------------------------------------ settings tab
    def _build_settings_tab(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        f = QFormLayout()
        self.set_name = QLineEdit(self.node.display_name)
        self.set_name.editingFinished.connect(lambda: (self.node.set_display_name(self.set_name.text()), self.refresh_session()))
        f.addRow("Отображаемое имя", self.set_name)
        self.set_port = QSpinBox()
        self.set_port.setRange(1024, 65535)
        self.set_port.setValue(self.node.settings.listen_port)
        self.set_port.valueChanged.connect(lambda v: setattr(self.node.settings, "listen_port", v))
        f.addRow("TCP-порт (после перезапуска)", self.set_port)
        self.set_dport = QSpinBox()
        self.set_dport.setRange(1024, 65535)
        self.set_dport.setValue(self.node.settings.discovery_port)
        self.set_dport.valueChanged.connect(lambda v: setattr(self.node.settings, "discovery_port", v))
        f.addRow("UDP-порт discovery (после перезапуска)", self.set_dport)
        self.set_auto = QCheckBox("автоматически принимать файлы, отправленные лично")
        self.set_auto.setChecked(self.node.settings.auto_accept_files)
        self.set_auto.toggled.connect(lambda v: setattr(self.node.settings, "auto_accept_files", v))
        f.addRow(self.set_auto)
        self.set_bc = QCheckBox("UDP broadcast discovery (после перезапуска)")
        self.set_bc.setChecked(self.node.settings.enable_broadcast)
        self.set_bc.toggled.connect(lambda v: setattr(self.node.settings, "enable_broadcast", v))
        f.addRow(self.set_bc)
        self.set_mdns = QCheckBox("mDNS discovery (после перезапуска)")
        self.set_mdns.setChecked(self.node.settings.enable_mdns)
        self.set_mdns.toggled.connect(lambda v: setattr(self.node.settings, "enable_mdns", v))
        f.addRow(self.set_mdns)
        lay.addLayout(f)
        lay.addWidget(QLabel("<b>Сетевые интерфейсы</b> (рабочий адрес определяется реальным соединением; отключите лишние — VirtualBox, Docker, VPN):"))
        self.iface_list = QListWidget()
        self.iface_list.itemChanged.connect(self.iface_toggled)
        lay.addWidget(self.iface_list)
        lim = self.node.limits
        lay.addWidget(QLabel(f"<b>Лимиты</b> (config/settings.json → limits): событий/с {lim.max_events_per_sec}, событие ≤ {fmt_size(lim.max_event_size)}, "
                             f"файл ≤ {fmt_size(lim.max_file_size)}, исходящих передач ≤ {lim.max_concurrent_outgoing}, чанк {fmt_size(lim.chunk_size)}, "
                             f"окно ACK {lim.ack_window}, хранилище ≤ {fmt_size(lim.max_storage_size)}, retention {lim.retention_days} дн."))
        p = QLabel(f"Данные: <a href='{self.node.paths.root.as_uri()}'>{self.node.paths.root}</a>")
        p.setOpenExternalLinks(True)
        lay.addWidget(p)
        bs = QPushButton("Сохранить настройки")
        bs.clicked.connect(lambda: (self.node.save_settings(), self.statusBar().showMessage("Сохранено", 3000)))
        lay.addWidget(bs)
        lay.addStretch()
        return w

    def refresh_interfaces(self) -> None:
        self.iface_list.blockSignals(True)
        self.iface_list.clear()
        for i in self.node.interfaces():
            it = QListWidgetItem(f"{i['ip']}  —  {i['name']}{('  [' + i['hint'] + ']') if i['hint'] else ''}")
            it.setData(Qt.UserRole, i["key"])
            it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
            it.setCheckState(Qt.Checked if i["enabled"] else Qt.Unchecked)
            self.iface_list.addItem(it)
        self.iface_list.blockSignals(False)

    def iface_toggled(self, it: QListWidgetItem) -> None:
        self.node.set_interface_enabled(it.data(Qt.UserRole), it.checkState() == Qt.Checked)

    # ------------------------------------------------------------------ bus events
    def on_bus(self, topic: str, data: dict) -> None:
        if topic == "log.app":
            self._log_app.append(data)
            if self.tabs.currentIndex() == 5 and self.log_kind.currentIndex() == 1:
                self.refresh_logs()
        elif topic == "log.event":
            self._log_events.append(data)
            if self.tabs.currentIndex() == 5 and self.log_kind.currentIndex() == 0:
                self.refresh_logs()
        elif topic == "event":
            ev = data["event"]
            t = ev["type"]
            if t in ("MESSAGE_CREATED", "MESSAGE_DELETED"):
                self.refresh_messages()
                if t == "MESSAGE_CREATED" and not data["local"]:
                    self.notify(self.node._name_of(ev["device_id"]), str(ev["payload"].get("text", ""))[:120])
            elif t in ("LINK_CREATED", "LINK_DELETED"):
                self.refresh_links()
            elif t.startswith("FILE_"):
                self.refresh_files()
            elif t.startswith("CHANNEL_"):
                self.refresh_channels()
            elif t in ("USER_JOINED", "USER_LEFT", "PERMISSIONS_UPDATED"):
                self.refresh_members()
            elif t.startswith("SESSION_"):
                self.refresh_all()
        elif topic in ("peer.connected", "peer.disconnected", "peer.discovered", "peer.unreachable"):
            self.refresh_peers()
            self.refresh_members()
            self.refresh_session()
            if topic == "peer.unreachable":
                self.statusBar().showMessage(f"Узел {data['device_id'][:8]} обнаружен, но недоступен по TCP — см. Диагностику", 8000)
        elif topic == "security.warning":
            self.refresh_peers()
            QMessageBox.warning(self, "SECURITY WARNING",
                                f"У узла {data.get('display_name') or data['device_id'][:8]} изменился отпечаток ключа.\n"
                                f"Было: {data['old_fingerprint'][:16]}…\nСтало: {data['new_fingerprint'][:16]}…\n\n"
                                "Соединение отклонено. Решите на вкладке «Пиры» (правый клик).")
        elif topic in ("transfer.progress", "transfer.done", "transfer.failed", "transfer.queued"):
            if topic == "transfer.done":
                self.notify("Файл получен", data["filename"])
            elif topic == "transfer.failed":
                self.statusBar().showMessage(f"Передача {data['filename']}: {data['reason']}", 10000)
            if self.tabs.currentIndex() == 2:
                self.refresh_files()
        elif topic == "session.changed" or topic == "session.closed":
            if topic == "session.closed":
                QMessageBox.information(self, "LocalClass", "Преподаватель закрыл сессию.")
            self.refresh_all()
        elif topic == "protocol.mismatch":
            QMessageBox.warning(self, "LocalClass", "Версия LocalClass на этом компьютере устарела — обновите приложение.")
        elif topic == "peer.manual_failed":
            self.statusBar().showMessage(f"Ручное подключение {data['address']}: {data['error']}", 8000)

    def refresh_periodic(self) -> None:
        self.refresh_session()
        idx = self.tabs.currentIndex()
        if idx == 3:
            self.refresh_peers()
        elif idx == 2:
            self.refresh_transfers()

    def refresh_all(self) -> None:
        self.refresh_session()
        self.refresh_channels()
        self.refresh_members()
        self.refresh_messages()
        self.refresh_links()
        self.refresh_files()
        self.refresh_peers()
        self.refresh_interfaces()
        self.refresh_logs()
        self.run_diag()

    def closeEvent(self, e) -> None:
        self.bridge.stop()
        super().closeEvent(e)
