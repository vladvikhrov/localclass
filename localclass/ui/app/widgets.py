"""Переиспользуемые виджеты интерфейса: прокрутка вкладок, карточки, тосты, drop-зона, раскрывающийся блок.

Правила компоновки (ТЗ v1.3, раздел 1):
  * никаких setGeometry / setFixedWidth / setFixedHeight — только layout'ы, растяжения и минимумы;
  * скрываемые блоки — обычные QWidget, управляются через setVisible(bool);
  * вкладки оборачиваются в QScrollArea, чтобы при масштабе 125–150 % появлялась прокрутка,
    а интерфейс не сжимался.
Вся логика — только представление и сигналы; команды выполняет Node через Bridge.
"""
from __future__ import annotations

import os
from typing import Callable

from PySide6.QtCore import QEasingCurve, QPropertyAnimation, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QGuiApplication
from PySide6.QtWidgets import (QFileDialog, QFrame, QGraphicsOpacityEffect, QHBoxLayout, QLabel, QPushButton,
                               QScrollArea, QSizePolicy, QVBoxLayout, QWidget)


def set_tone(w: QWidget, tone: str) -> None:
    """Сменить QSS-свойство tone и попросить Qt перерисовать виджет по новым правилам стиля."""
    w.setProperty("tone", tone)
    w.style().unpolish(w)
    w.style().polish(w)
    w.update()


def label(text: str, kind: str = "", *, wrap: bool = False, selectable: bool = False) -> QLabel:
    """kind — objectName для QSS: h1, h2, muted, pill, sessionCode."""
    lb = QLabel(text)
    if kind:
        lb.setObjectName(kind)
    lb.setWordWrap(wrap)
    if wrap:
        lb.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Minimum)
    if selectable:
        lb.setTextInteractionFlags(Qt.TextSelectableByMouse)
    return lb


def button(text: str, kind: str = "", on_click: Callable[[], None] | None = None, tooltip: str = "",
           min_width: int = 0, min_height: int = 0) -> QPushButton:
    """kind — objectName: primaryButton, dangerButton, iconButton, roleTab, sectionToggle."""
    b = QPushButton(text)
    if kind:
        b.setObjectName(kind)
    if on_click:
        b.clicked.connect(lambda: on_click())
    if tooltip:
        b.setToolTip(tooltip)
    if min_width:
        b.setMinimumWidth(min_width)
    if min_height:
        b.setMinimumHeight(min_height)
    b.setCursor(Qt.PointingHandCursor)
    return b


def separator() -> QFrame:
    line = QFrame()
    line.setObjectName("separator")
    line.setFrameShape(QFrame.HLine)
    line.setFrameShadow(QFrame.Plain)
    return line


def divider_with_text(text: str) -> QWidget:
    """Разделитель вида ──── Или подключитесь по коду ────."""
    w = QWidget()
    lay = QHBoxLayout(w)
    lay.setContentsMargins(0, 6, 0, 6)
    lay.setSpacing(10)
    lay.addWidget(separator(), 1)
    lay.addWidget(label(text, "sectionDivider"))
    lay.addWidget(separator(), 1)
    return w


def scrollable(inner: QWidget, *, plain: bool = False, min_width: int = 0, min_height: int = 0) -> QScrollArea:
    """Обернуть содержимое вкладки в прокручиваемую область без рамки.

    На ноутбуке 1366×768 при масштабе 150 % содержимое не сжимается, а получает вертикальную прокрутку.
    """
    area = QScrollArea()
    area.setObjectName("plainScroll" if plain else "tabScroll")
    area.setWidgetResizable(True)
    area.setFrameShape(QFrame.NoFrame)
    area.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
    if min_width:
        inner.setMinimumWidth(min_width)
    if min_height:
        inner.setMinimumHeight(min_height)
    area.setWidget(inner)
    return area


def card(object_name: str = "cardFrame", *, margins: tuple[int, int, int, int] = (16, 14, 16, 14),
         spacing: int = 10, vertical: bool = True) -> tuple[QFrame, QVBoxLayout | QHBoxLayout]:
    """Карточка с готовым layout'ом: (frame, layout)."""
    frame = QFrame()
    frame.setObjectName(object_name)
    lay = QVBoxLayout(frame) if vertical else QHBoxLayout(frame)
    lay.setContentsMargins(*margins)
    lay.setSpacing(spacing)
    return frame, lay


def fit_widget_column(table, col: int, extra: int = 16) -> None:
    """Подогнать ширину колонки под виджеты в ячейках.

    QTableWidget считает ResizeToContents только по тексту ячеек и не учитывает cellWidget, из-за чего
    кнопки обрезались. Ширина берётся из sizeHint виджетов, поэтому растёт вместе с масштабом экрана.
    """
    width = table.horizontalHeader().sectionSizeHint(col)
    for row in range(table.rowCount()):
        w = table.cellWidget(row, col)
        if w is not None:
            width = max(width, w.sizeHint().width())
    table.setColumnWidth(col, width + extra)


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


def cell_widget(inner: QWidget, margin: int = 6) -> QWidget:
    """Оборачивает виджет для ячейки таблицы, чтобы он не выходил за её границы."""
    holder = QWidget()
    lay = QHBoxLayout(holder)
    lay.setContentsMargins(margin, 3, margin, 3)
    lay.setSpacing(0)
    lay.addWidget(inner)
    return holder


class Toast(QFrame):
    """Всплывающее уведомление сверху окна: зелёное — успех, красное — ошибка, синее — информация."""

    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self.setObjectName("toast")
        self.setProperty("tone", "ok")
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Minimum)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(16, 10, 16, 10)
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
        set_tone(self, tone)
        self.reposition()
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

    def reposition(self) -> None:
        """Ширина подбирается под окно, а не задаётся жёстко: корректно при любом масштабе экрана."""
        p = self.parentWidget()
        if not p:
            return
        limit = max(320, int(p.width() * 0.6))
        self.setMaximumWidth(limit)
        self._label.setMaximumWidth(limit - 40)
        self.adjustSize()
        self.move(max(0, (p.width() - self.width()) // 2), 14)


class SessionCard(QFrame):
    """Крупная карточка урока в сети: тема, преподаватель, участники онлайн и кнопка [Присоединиться]."""

    join_requested = Signal(str)   # session_id

    def __init__(self, data: dict, button_text: str = "Присоединиться", parent: QWidget | None = None):
        super().__init__(parent)
        self.session_id = data["session_id"]
        self._button_text = button_text
        self.setObjectName("clickableCard")
        self.setCursor(Qt.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Maximum)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(16, 12, 16, 12)
        lay.setSpacing(12)

        info = QVBoxLayout()
        info.setSpacing(4)
        self.title = label(data.get("name") or "Урок", "h2", wrap=True)
        info.addWidget(self.title)
        self.subtitle = label(self._subtitle(data), "muted", wrap=True)
        info.addWidget(self.subtitle)
        lay.addLayout(info, 1)

        right = QVBoxLayout()
        right.setSpacing(6)
        self.code_pill = label(data.get("code") or "", "pill")
        self.code_pill.setAlignment(Qt.AlignCenter)
        right.addWidget(self.code_pill)
        self.join_btn = button(button_text, "primaryButton", self._on_join, min_width=170, min_height=36)
        right.addWidget(self.join_btn)
        lay.addLayout(right)

    @staticmethod
    def _subtitle(data: dict) -> str:
        teacher = data.get("teacher_name") or "преподаватель не представился"
        members = int(data.get("members") or 0)
        return f"Преподаватель: {teacher}   ·   Участников: {members}" if members else f"Преподаватель: {teacher}"

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
        self.join_btn.setText("Подключение…" if busy else self._button_text)

    def set_enabled_join(self, enabled: bool) -> None:
        self.join_btn.setEnabled(enabled)

    def mouseDoubleClickEvent(self, event) -> None:
        self._on_join()


class LinkCard(QFrame):
    """Карточка ссылки: заголовок открывается в системном браузере в один клик."""

    def __init__(self, url: str, title: str, author: str, parent: QWidget | None = None,
                 color: str = "#0969da"):
        super().__init__(parent)
        self.url = url
        self.setObjectName("clickableCard")
        self.setCursor(Qt.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Maximum)
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
        lay.addWidget(label(f"от {author}", "muted", wrap=True))
        row = QHBoxLayout()
        row.setSpacing(8)
        row.addWidget(button("Открыть", "primaryButton", self.open))
        row.addWidget(button("Копировать", "", self.copy))
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
        self.setObjectName("dropZone")
        self.setAcceptDrops(True)
        self.setMinimumHeight(110)
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Maximum)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 16, 18, 16)
        lay.setSpacing(8)
        self.text = label("Перетащите файлы сюда для отправки классу", "h2", wrap=True)
        self.text.setAlignment(Qt.AlignCenter)
        lay.addWidget(self.text)
        hint = label("или нажмите кнопку ниже — файл увидят все участники урока", "muted", wrap=True)
        hint.setAlignment(Qt.AlignCenter)
        lay.addWidget(hint)
        row = QHBoxLayout()
        row.addStretch()
        self.pick = button("Выбрать файл", "primaryButton", self.pick_requested.emit, min_height=36)
        row.addWidget(self.pick)
        row.addStretch()
        lay.addLayout(row)

    def _highlight(self, on: bool) -> None:
        self.setObjectName("dropZoneActive" if on else "dropZone")
        self.style().unpolish(self)
        self.style().polish(self)
        self.update()

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


class Section(QWidget):
    """Раскрывающийся блок: обычная кнопка + обычный QWidget, видимость через setVisible(bool).

    Никаких кастомных анимаций и ручного пересчёта геометрии — именно они давали наложения текста
    на соседние кнопки при масштабировании экрана.
    """

    def __init__(self, title: str, parent: QWidget | None = None, expanded: bool = False):
        super().__init__(parent)
        self._title = title
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)
        self.toggle = button("", "sectionToggle", self._on_toggle)
        self.toggle.setCheckable(True)
        self.toggle.setChecked(expanded)
        self.toggle.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        lay.addWidget(self.toggle)
        self.content = QFrame()
        self.content.setObjectName("cardFrame")
        self.content_layout = QVBoxLayout(self.content)
        self.content_layout.setContentsMargins(14, 12, 14, 12)
        self.content_layout.setSpacing(10)
        lay.addWidget(self.content)
        self.content.setVisible(expanded)
        self._sync_text()

    def _sync_text(self) -> None:
        self.toggle.setText(("▾  " if self.toggle.isChecked() else "▸  ") + self._title)

    def _on_toggle(self) -> None:
        self.content.setVisible(self.toggle.isChecked())
        self._sync_text()

    def set_expanded(self, expanded: bool) -> None:
        self.toggle.setChecked(expanded)
        self._on_toggle()

    def add_widget(self, w: QWidget) -> None:
        self.content_layout.addWidget(w)

    def add_layout(self, inner) -> None:
        self.content_layout.addLayout(inner)


def ask_files(parent: QWidget, title: str = "Выберите файлы") -> list[str]:
    paths, _ = QFileDialog.getOpenFileNames(parent, title)
    return [p for p in paths if p]
