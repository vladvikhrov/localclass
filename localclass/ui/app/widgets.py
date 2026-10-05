"""Переиспользуемые виджеты интерфейса: тосты, карточки урока и ссылки, drop-зона, спойлер.

Вся логика — только представление и сигналы; команды выполняет Node через Bridge.
"""
from __future__ import annotations

import os
from typing import Callable

from PySide6.QtCore import QEasingCurve, QPropertyAnimation, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QGuiApplication
from PySide6.QtWidgets import (QFileDialog, QFrame, QGraphicsOpacityEffect, QHBoxLayout, QLabel, QPushButton,
                               QSizePolicy, QToolButton, QVBoxLayout, QWidget)


def set_prop(w: QWidget, name: str, value: str) -> None:
    """Сменить QSS-свойство и попросить Qt перерисовать виджет по новым правилам стиля."""
    w.setProperty(name, value)
    w.style().unpolish(w)
    w.style().polish(w)
    w.update()


def label(text: str, role: str = "", *, wrap: bool = False, selectable: bool = False) -> QLabel:
    lb = QLabel(text)
    if role:
        lb.setProperty("role", role)
    lb.setWordWrap(wrap)
    if selectable:
        lb.setTextInteractionFlags(Qt.TextSelectableByMouse)
    return lb


def button(text: str, kind: str = "", on_click: Callable[[], None] | None = None, tooltip: str = "") -> QPushButton:
    b = QPushButton(text)
    if kind:
        b.setProperty("kind", kind)
    if on_click:
        b.clicked.connect(lambda: on_click())
    if tooltip:
        b.setToolTip(tooltip)
    b.setCursor(Qt.PointingHandCursor)
    return b


def cell_widget(inner: QWidget, margin: int = 6) -> QWidget:
    """Оборачивает виджет для ячейки таблицы, чтобы он не выходил за её границы."""
    holder = QWidget()
    lay = QHBoxLayout(holder)
    lay.setContentsMargins(margin, 4, margin, 4)
    lay.setSpacing(0)
    lay.addWidget(inner)
    return holder


def fmt_size(n: float) -> str:
    for unit in ("Б", "КБ", "МБ", "ГБ", "ТБ"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "Б" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} ПБ"


def fmt_speed(bytes_per_sec: float) -> str:
    return f"{bytes_per_sec / 1024 ** 2:.1f} МБ/с" if bytes_per_sec > 0 else "—"


def fmt_eta(sec: float | None) -> str:
    if sec is None:
        return "—"
    sec = int(sec)
    if sec < 60:
        return f"{sec} с"
    if sec < 3600:
        return f"{sec // 60} мин {sec % 60} с"
    return f"{sec // 3600} ч {(sec % 3600) // 60} мин"


class Toast(QFrame):
    """Всплывающее уведомление сверху окна: зелёное — успех, красное — ошибка, синее — информация."""

    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self.setProperty("role", "toast")
        self.setProperty("tone", "ok")
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(18, 12, 18, 12)
        self._label = QLabel("")
        self._label.setWordWrap(True)
        lay.addWidget(self._label)
        self._effect = QGraphicsOpacityEffect(self)
        self.setGraphicsEffect(self._effect)
        self._anim = QPropertyAnimation(self._effect, b"opacity", self)
        self._anim.setDuration(220)
        self._anim.setEasingCurve(QEasingCurve.InOutQuad)
        self._fading_out = False
        self._anim.finished.connect(self._on_anim_finished)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._fade_out)
        self.hide()

    def show_message(self, text: str, tone: str = "ok", msec: int = 3500) -> None:
        self._label.setText(text)
        set_prop(self, "tone", tone)
        self.adjustSize()
        self._reposition()
        self.show()
        self.raise_()
        self._anim.stop()
        self._fading_out = False
        self._anim.setStartValue(self._effect.opacity() if self.isVisible() else 0.0)
        self._anim.setEndValue(1.0)
        self._anim.start()
        self._timer.start(msec)

    def _on_anim_finished(self) -> None:
        if self._fading_out:
            self.hide()

    def _fade_out(self) -> None:
        self._anim.stop()
        self._fading_out = True
        self._anim.setStartValue(self._effect.opacity())
        self._anim.setEndValue(0.0)
        self._anim.start()

    def _reposition(self) -> None:
        p = self.parentWidget()
        if p:
            w = min(max(self.sizeHint().width(), 280), max(320, p.width() - 80))
            self.setFixedWidth(w)
            self.move((p.width() - w) // 2, 16)


class SessionCard(QFrame):
    """Крупная карточка урока в сети: тема, преподаватель, участники онлайн и кнопка [Присоединиться]."""

    join_requested = Signal(str)   # session_id

    def __init__(self, data: dict, parent: QWidget | None = None):
        super().__init__(parent)
        self.session_id = data["session_id"]
        self.setProperty("card", "clickable")
        self.setCursor(Qt.PointingHandCursor)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(16, 14, 16, 14)
        lay.setSpacing(14)

        info = QVBoxLayout()
        info.setSpacing(4)
        self.title = label(data.get("name") or "Урок", "h2", wrap=True)
        info.addWidget(self.title)
        self.subtitle = label(self._subtitle(data), "muted", wrap=True)
        info.addWidget(self.subtitle)
        lay.addLayout(info, 1)

        self.code_pill = label(data.get("code") or "", "pill")
        self.code_pill.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Preferred)
        lay.addWidget(self.code_pill, 0, Qt.AlignVCenter)
        self.join_btn = button("Присоединиться", "primary", self._on_join)
        self.join_btn.setMinimumWidth(190)      # ширина не скачет при смене текста на «Подключение…»
        self.join_btn.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Preferred)
        lay.addWidget(self.join_btn, 0, Qt.AlignVCenter)
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)

    @staticmethod
    def _subtitle(data: dict) -> str:
        teacher = data.get("teacher_name") or "преподаватель не представился"
        members = int(data.get("members") or 0)
        return f"Преподаватель: {teacher}   ·   Участников онлайн: {members}" if members else f"Преподаватель: {teacher}"

    def update_data(self, data: dict) -> None:
        self.title.setText(data.get("name") or "Урок")
        self.subtitle.setText(self._subtitle(data))
        self.code_pill.setText(data.get("code") or "")

    def _on_join(self) -> None:
        self.set_busy(True)
        self.join_requested.emit(self.session_id)

    def set_busy(self, busy: bool) -> None:
        """Защита от повторного клика: кнопка блокируется и показывает «Подключение…»."""
        self.join_btn.setEnabled(not busy)
        self.join_btn.setText("Подключение…" if busy else "Присоединиться")

    def mouseDoubleClickEvent(self, event) -> None:
        self._on_join()

    def set_enabled_join(self, enabled: bool) -> None:
        self.join_btn.setEnabled(enabled)


class LinkCard(QFrame):
    """Карточка ссылки в ленте: заголовок-ссылка в один клик, автор и кнопки «Открыть» / «Копировать»."""

    def __init__(self, url: str, title: str, author: str, parent: QWidget | None = None,
                 color: str = "#2b6cb0"):
        super().__init__(parent)
        self.url = url
        self.setProperty("card", "clickable")
        self.setCursor(Qt.PointingHandCursor)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 10, 12, 10)
        lay.setSpacing(6)
        text = title or url
        link = QLabel(f'<a href="{url}" style="text-decoration:none;color:{color}">{text}</a>')
        link.setOpenExternalLinks(True)
        link.setWordWrap(True)
        link.setToolTip(url)
        link.setCursor(Qt.PointingHandCursor)
        lay.addWidget(link)
        lay.addWidget(label(f"от {author}", "muted"))
        row = QHBoxLayout()
        row.setSpacing(8)
        row.addWidget(button("Открыть", "primary", self.open))
        row.addWidget(button("Копировать", "ghost", self.copy))
        row.addStretch()
        lay.addLayout(row)

    def open(self) -> None:
        QDesktopServices.openUrl(QUrl(self.url))

    def copy(self) -> None:
        QGuiApplication.clipboard().setText(self.url)

    def mouseDoubleClickEvent(self, event) -> None:
        self.open()


class DropZone(QFrame):
    """Область Drag & Drop для отправки файлов классу."""

    files_dropped = Signal(list)
    pick_requested = Signal()

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setProperty("card", "drop")
        self.setAcceptDrops(True)
        self.setMinimumHeight(120)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 18, 20, 18)
        lay.setSpacing(10)
        self.text = label("Перетащите файлы сюда для отправки классу", "h2", wrap=True)
        self.text.setAlignment(Qt.AlignCenter)
        lay.addWidget(self.text)
        hint = label("или нажмите кнопку ниже — файл увидят все участники урока", "muted", wrap=True)
        hint.setAlignment(Qt.AlignCenter)
        lay.addWidget(hint)
        row = QHBoxLayout()
        row.addStretch()
        self.pick = button("Выбрать файл", "primary", self.pick_requested.emit)
        row.addWidget(self.pick)
        row.addStretch()
        lay.addLayout(row)

    def _highlight(self, on: bool) -> None:
        set_prop(self, "card", "dropActive" if on else "drop")

    def dragEnterEvent(self, e) -> None:
        if e.mimeData().hasUrls():
            e.acceptProposedAction()
            self._highlight(True)

    def dragLeaveEvent(self, e) -> None:
        self._highlight(False)

    def dropEvent(self, e) -> None:
        self._highlight(False)
        paths = [u.toLocalFile() for u in e.mimeData().urls() if u.isLocalFile() and os.path.isfile(u.toLocalFile())]
        if paths:
            self.files_dropped.emit(paths)
            e.acceptProposedAction()


class CollapsibleBox(QWidget):
    """Спойлер для расширенных настроек: по умолчанию свёрнут."""

    def __init__(self, title: str, parent: QWidget | None = None, expanded: bool = False):
        super().__init__(parent)
        self._title = title
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)
        self.toggle = QToolButton()
        self.toggle.setProperty("kind", "collapse")
        self.toggle.setCheckable(True)
        self.toggle.setChecked(expanded)
        self.toggle.setCursor(Qt.PointingHandCursor)
        self.toggle.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.toggle.clicked.connect(self._on_toggle)
        lay.addWidget(self.toggle)
        self.content = QFrame()
        self.content.setProperty("card", "true")
        self.content_layout = QVBoxLayout(self.content)
        self.content_layout.setContentsMargins(16, 14, 16, 14)
        self.content_layout.setSpacing(10)
        lay.addWidget(self.content)
        self.content.setVisible(expanded)
        self._sync_text()

    def _sync_text(self) -> None:
        self.toggle.setText(("▾  " if self.toggle.isChecked() else "▸  ") + self._title)

    def _on_toggle(self) -> None:
        self.content.setVisible(self.toggle.isChecked())
        self._sync_text()

    def add_widget(self, w: QWidget) -> None:
        self.content_layout.addWidget(w)

    def add_layout(self, l) -> None:
        self.content_layout.addLayout(l)


def ask_files(parent: QWidget, title: str = "Выберите файлы") -> list[str]:
    paths, _ = QFileDialog.getOpenFileNames(parent, title)
    return [p for p in paths if p]
