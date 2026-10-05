"""QR-код сессии: компактный виджет и модальное окно на весь экран (для вывода через проектор)."""
from __future__ import annotations

import qrcode
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import QDialog, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget


class QRWidget(QWidget):
    def __init__(self, parent=None, placeholder: str = "QR появится после создания урока"):
        super().__init__(parent)
        self._matrix: list[list[bool]] = []
        self._placeholder = placeholder
        self.setMinimumSize(180, 180)

    def set_text(self, text: str) -> None:
        if not text:
            self._matrix = []
        else:
            qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, border=2)
            qr.add_data(text)
            qr.make(fit=True)
            self._matrix = qr.get_matrix()
        self.update()

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.fillRect(self.rect(), QColor("white"))
        if not self._matrix:
            p.setPen(QColor("gray"))
            p.drawText(self.rect(), Qt.AlignCenter | Qt.TextWordWrap, self._placeholder)
            return
        n = len(self._matrix)
        side = min(self.width(), self.height())
        cell = side / n
        ox = (self.width() - side) / 2
        oy = (self.height() - side) / 2
        p.setPen(Qt.NoPen)
        p.setBrush(QColor("black"))
        for y, row in enumerate(self._matrix):
            for x, v in enumerate(row):
                if v:
                    p.drawRect(int(ox + x * cell), int(oy + y * cell), int(cell) + 1, int(cell) + 1)


class QRDialog(QDialog):
    """QR во весь экран: ученики сканируют с проектора или с доски. Esc — закрыть."""

    def __init__(self, text: str, code: str, session_name: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("QR-код урока")
        self.setModal(True)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 24, 24, 24)
        lay.setSpacing(12)
        title = QLabel(session_name or "Урок")
        title.setProperty("role", "h1")
        title.setAlignment(Qt.AlignCenter)
        lay.addWidget(title)
        hint = QLabel("Отсканируйте код в приложении LocalClass или введите код вручную")
        hint.setProperty("role", "muted")
        hint.setAlignment(Qt.AlignCenter)
        lay.addWidget(hint)
        self.qr = QRWidget(placeholder="нет активного урока")
        self.qr.set_text(text)
        lay.addWidget(self.qr, 1)
        code_label = QLabel(code)
        code_label.setProperty("role", "code")
        code_label.setAlignment(Qt.AlignCenter)
        lay.addWidget(code_label)
        row = QHBoxLayout()
        row.addStretch()
        close = QPushButton("Закрыть  (Esc)")
        close.setProperty("kind", "primary")
        close.clicked.connect(self.accept)
        row.addWidget(close)
        row.addStretch()
        lay.addLayout(row)

    def exec_fullscreen(self) -> int:
        self.showFullScreen()
        return self.exec()
