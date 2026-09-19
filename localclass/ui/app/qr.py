from __future__ import annotations

import qrcode
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import QWidget


class QRWidget(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._matrix: list[list[bool]] = []
        self.setMinimumSize(220, 220)

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
            p.drawText(self.rect(), Qt.AlignCenter, "QR появится после создания сессии")
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
