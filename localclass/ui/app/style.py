"""Оформление: палитры светлой и тёмной темы + чистый QSS (ТЗ v1.3, раздел 5).

Размеры задаются в px и масштабируются средствами Qt High-DPI: при системном масштабе 125 % и 150 %
Windows сообщает devicePixelRatio, и Qt увеличивает шрифты и отступы сам. Поэтому в коде нет ни одного
жёсткого размера контейнера — только минимумы, растяжения и прокрутка.

Селекторы по objectName (#primaryButton, #cardFrame, #dropZone) соответствуют спецификации; виджеты
проставляют их через widgets.py.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

LIGHT = {
    "bg": "#f6f8fa", "surface": "#ffffff", "surface_alt": "#f3f4f6", "border": "#d0d7de",
    "border_soft": "#e1e4e8", "border_strong": "#b0b8c4", "text": "#24292f", "text_muted": "#57606a",
    "primary": "#0969da", "primary_hover": "#0854ad", "primary_pressed": "#0a4f9c",
    "accent": "#1a7f37", "accent_hover": "#176f30", "danger": "#cf222e", "danger_hover": "#a40e26",
    "warning_bg": "#fff8c5", "warning_border": "#d4a72c", "selection": "#ddf4ff", "disabled": "#8c959f",
    "disabled_bg": "#eaeef2", "drop_bg": "#f8fafc", "on_accent": "#ffffff",
    "control_button": "#dfe5ec", "control_button_border": "#b8c2cd",
}

DARK = {
    "bg": "#0d1117", "surface": "#161b22", "surface_alt": "#21262d", "border": "#30363d",
    "border_soft": "#272c33", "border_strong": "#484f58", "text": "#e6edf3", "text_muted": "#9198a1",
    "primary": "#2f81f7", "primary_hover": "#4a92f8", "primary_pressed": "#1f6feb",
    "accent": "#3fb950", "accent_hover": "#56c766", "danger": "#f85149", "danger_hover": "#ff6a61",
    "warning_bg": "#3b2f12", "warning_border": "#9e6a03", "selection": "#1f3b5c", "disabled": "#6e7681",
    "disabled_bg": "#21262d", "drop_bg": "#12171e", "on_accent": "#ffffff",
    "control_button": "#2d343d", "control_button_border": "#495059",
}

FONT_BASE = 13
FONT_CONTROL = 14
FONT_H2 = 16
FONT_H1 = 20
FONT_CODE = 30


def palette(theme: str) -> dict[str, str]:
    return DARK if theme == "dark" else LIGHT


_ICON_DIR: Path | None = None


def set_icon_dir(directory: str | Path) -> None:
    """Куда складывать нарисованные иконки стрелок (по умолчанию — временный каталог)."""
    global _ICON_DIR
    _ICON_DIR = Path(directory)
    _ICON_DIR.mkdir(parents=True, exist_ok=True)


def arrow_icon(direction: str, theme: str) -> str:
    """Нарисовать стрелку (шеврон) для QComboBox/QSpinBox и вернуть путь к PNG.

    Qt не рисует стрелки средствами QSS, а стандартные на фоне наших кнопок сливались в тонкую
    полоску. Иконка рисуется под текущий масштаб экрана, поэтому остаётся чёткой при 125 % и 150 %.
    """
    from PySide6.QtCore import QPointF, Qt
    from PySide6.QtGui import QColor, QGuiApplication, QPainter, QPen, QPixmap

    app = QGuiApplication.instance()
    ratio = 1.0
    if app is not None and app.primaryScreen() is not None:
        ratio = max(1.0, float(app.primaryScreen().devicePixelRatio()))
    directory = _ICON_DIR or Path(tempfile.gettempdir()) / "localclass-icons"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"arrow-{direction}-{theme}-{ratio:.2f}.png"
    if path.exists():
        return path.as_posix()

    w, h = 14, 9
    pm = QPixmap(int(w * ratio), int(h * ratio))
    pm.setDevicePixelRatio(ratio)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    pen = QPen(QColor(palette(theme)["text"]))
    pen.setWidthF(2.0)
    pen.setCapStyle(Qt.RoundCap)
    pen.setJoinStyle(Qt.RoundJoin)
    p.setPen(pen)
    m = 2.5
    if direction == "down":
        points = [QPointF(m, m + 0.5), QPointF(w / 2, h - m), QPointF(w - m, m + 0.5)]
    else:
        points = [QPointF(m, h - m - 0.5), QPointF(w / 2, m), QPointF(w - m, h - m - 0.5)]
    p.drawPolyline(points)
    p.end()
    pm.save(str(path), "PNG")
    return path.as_posix()


def link_color(theme: str) -> str:
    return palette(theme)["primary"]


def rich_text_css(theme: str) -> str:
    """Стили HTML внутри QTextBrowser: ссылки и код должны читаться в обеих темах."""
    c = palette(theme)
    return (f"a {{ color: {c['primary']}; text-decoration: none; }}"
            f"code {{ background: {c['surface_alt']}; color: {c['text']}; padding: 1px 4px; border-radius: 4px; }}"
            f"table {{ color: {c['text']}; }}")


def qss(theme: str = "light") -> str:
    c = palette(theme)
    try:
        arrow_down = arrow_icon("down", theme)
        arrow_up = arrow_icon("up", theme)
    except Exception:  # noqa: BLE001 — без QApplication иконку не нарисовать, оставляем стандартную
        arrow_down = arrow_up = ""
    return f"""
/* ---------- база ---------- */
QWidget {{
    font-family: "Segoe UI", -apple-system, "Noto Sans", "DejaVu Sans", sans-serif;
    font-size: {FONT_BASE}px;
    color: {c['text']};
    background-color: {c['bg']};
}}
QLabel {{ background: transparent; }}
QLabel#h1 {{ font-size: {FONT_H1}px; font-weight: 600; }}
QLabel#h2 {{ font-size: {FONT_H2}px; font-weight: 600; }}
QLabel#muted {{ color: {c['text_muted']}; }}
QLabel#sessionCode {{ font-size: {FONT_CODE}px; font-weight: 700; letter-spacing: 3px; color: {c['primary']}; }}
QLabel#pill {{
    background-color: {c['surface_alt']}; border: 1px solid {c['border']}; border-radius: 6px;
    padding: 4px 10px; color: {c['text_muted']};
}}
QLabel#sectionDivider {{ color: {c['text_muted']}; }}

/* ---------- кнопки ---------- */
QPushButton {{
    background-color: {c['surface_alt']};
    border: 1px solid {c['border']};
    border-radius: 6px;
    padding: 6px 14px;
    min-height: 28px;
    font-weight: 500;
    font-size: {FONT_CONTROL}px;
}}
QPushButton:hover {{ background-color: {c['selection']}; border-color: {c['border_strong']}; }}
QPushButton:pressed {{ background-color: {c['disabled_bg']}; }}
QPushButton:disabled {{ background-color: {c['disabled_bg']}; color: {c['disabled']}; border-color: {c['disabled_bg']}; }}

QPushButton#primaryButton {{
    background-color: {c['primary']}; border: 1px solid {c['primary']}; color: {c['on_accent']};
    font-weight: 600; padding: 8px 18px; min-height: 32px;
}}
QPushButton#primaryButton:hover {{ background-color: {c['primary_hover']}; border-color: {c['primary_hover']}; }}
QPushButton#primaryButton:pressed {{ background-color: {c['primary_pressed']}; }}
QPushButton#primaryButton:disabled {{
    background-color: {c['disabled_bg']}; border-color: {c['disabled_bg']}; color: {c['disabled']};
}}

QPushButton#dangerButton {{
    background-color: {c['danger']}; border: 1px solid {c['danger']}; color: {c['on_accent']};
    font-weight: 600; padding: 8px 18px; min-height: 32px;
}}
QPushButton#dangerButton:hover {{ background-color: {c['danger_hover']}; border-color: {c['danger_hover']}; }}
QPushButton#dangerButton:pressed {{ background-color: {c['danger_hover']}; }}

/* скрепка — квадрат 40×40, «Отправить» — не меньше 100×40 (ТЗ v1.3, 4.1).
   Размеры заданы в QSS, а не setFixedSize: при масштабе 125–150 % Qt увеличит их вместе со шрифтом. */
QPushButton#iconButton {{ font-size: {FONT_H2}px; padding: 2px; min-width: 40px; min-height: 40px; }}
QPushButton#sendButton {{
    background-color: {c['primary']}; border: 1px solid {c['primary']}; color: {c['on_accent']};
    font-weight: 600; padding: 8px 18px; min-width: 100px; min-height: 40px;
}}
QPushButton#sendButton:hover {{ background-color: {c['primary_hover']}; border-color: {c['primary_hover']}; }}
QPushButton#sendButton:pressed {{ background-color: {c['primary_pressed']}; }}
QPushButton#sendButton:disabled {{
    background-color: {c['disabled_bg']}; border-color: {c['disabled_bg']}; color: {c['disabled']};
}}

QPushButton#roleTab {{
    background-color: transparent; border: 1px solid {c['border']}; border-radius: 6px;
    padding: 10px 20px; font-size: {FONT_H2}px; font-weight: 600; color: {c['text_muted']};
}}
QPushButton#roleTab:hover {{ background-color: {c['surface_alt']}; color: {c['text']}; }}
QPushButton#roleTab:checked {{
    background-color: {c['primary']}; border-color: {c['primary']}; color: {c['on_accent']};
}}

QPushButton#sectionToggle {{
    background-color: {c['surface_alt']}; border: 1px solid {c['border']}; border-radius: 6px;
    padding: 8px 14px; font-weight: 600; text-align: left;
}}
QPushButton#sectionToggle:hover {{ background-color: {c['selection']}; border-color: {c['border_strong']}; }}
QPushButton#sectionToggle:checked {{ border-color: {c['primary']}; }}

/* ---------- поля ввода ---------- */
QLineEdit, QTextEdit, QPlainTextEdit, QSpinBox, QComboBox {{
    background-color: {c['surface']};
    border: 1px solid {c['border']};
    border-radius: 6px;
    padding: 6px 10px;
    min-height: 24px;
    color: {c['text']};
    selection-background-color: {c['selection']};
    selection-color: {c['text']};
}}
QLineEdit:focus, QTextEdit:focus, QPlainTextEdit:focus, QSpinBox:focus, QComboBox:focus {{
    border: 2px solid {c['primary']}; padding: 5px 9px;
}}
QLineEdit:disabled, QTextEdit:disabled, QSpinBox:disabled, QComboBox:disabled {{
    background-color: {c['disabled_bg']}; color: {c['disabled']};
}}
QLineEdit#bigInput {{ font-size: {FONT_H2}px; }}
QLineEdit#pinInput {{ font-size: {FONT_H2}px; font-weight: 700; letter-spacing: 4px; }}
/* ---------- выпадающие списки и счётчики: заметные кнопки с чёткими стрелками ---------- */
QComboBox {{ padding-right: 36px; }}
QComboBox::drop-down {{
    subcontrol-origin: padding; subcontrol-position: center right;
    width: 30px; border-left: 1px solid {c['control_button_border']};
    border-top-right-radius: 6px; border-bottom-right-radius: 6px;
    background-color: {c['control_button']};
}}
QComboBox::drop-down:hover {{ background-color: {c['selection']}; }}
QComboBox::down-arrow {{ image: url("{arrow_down}"); width: 14px; height: 9px; }}

QSpinBox {{ padding-right: 36px; }}
QSpinBox::up-button, QSpinBox::down-button {{
    subcontrol-origin: border; width: 30px; margin: 1px;
    background-color: {c['control_button']}; border-left: 1px solid {c['control_button_border']};
}}
QSpinBox::up-button {{
    subcontrol-position: top right; border-bottom: 1px solid {c['control_button_border']};
    border-top-right-radius: 6px;
}}
QSpinBox::down-button {{ subcontrol-position: bottom right; border-bottom-right-radius: 6px; }}
QSpinBox::up-button:hover, QSpinBox::down-button:hover {{ background-color: {c['selection']}; }}
QSpinBox::up-button:pressed, QSpinBox::down-button:pressed {{ background-color: {c['border']}; }}
QSpinBox::up-arrow {{ image: url("{arrow_up}"); width: 14px; height: 9px; }}
QSpinBox::down-arrow {{ image: url("{arrow_down}"); width: 14px; height: 9px; }}

QCheckBox {{ spacing: 8px; padding: 4px 0; background: transparent; }}
QCheckBox::indicator {{
    width: 18px; height: 18px; border: 1px solid {c['border_strong']}; border-radius: 4px;
    background-color: {c['surface']};
}}
QCheckBox::indicator:checked {{ background-color: {c['primary']}; border-color: {c['primary']}; }}
QCheckBox::indicator:hover {{ border-color: {c['primary']}; }}
QCheckBox:disabled {{ color: {c['disabled']}; }}

/* ---------- вкладки ---------- */
QTabWidget::pane {{ border: 1px solid {c['border_soft']}; border-radius: 8px; background-color: {c['bg']}; top: -1px; }}
QTabBar::tab {{
    background: transparent; color: {c['text_muted']};
    padding: 8px 16px; margin-right: 4px;
    border-top-left-radius: 6px; border-top-right-radius: 6px;
    font-size: {FONT_CONTROL}px;
}}
QTabBar::tab:selected {{
    background-color: {c['surface']}; color: {c['text']}; font-weight: 600;
    border: 1px solid {c['border_soft']}; border-bottom-color: {c['surface']};
}}
QTabBar::tab:hover:!selected {{ background-color: {c['surface_alt']}; color: {c['text']}; }}
QTabBar::tab:disabled {{ color: {c['disabled']}; }}

/* ---------- карточки ---------- */
QFrame#cardFrame {{
    background-color: {c['surface']}; border: 1px solid {c['border_soft']}; border-radius: 8px;
}}
QFrame#clickableCard {{
    background-color: {c['surface']}; border: 1px solid {c['border_soft']}; border-radius: 8px;
}}
QFrame#clickableCard:hover {{ background-color: {c['surface_alt']}; border-color: {c['primary']}; }}
QFrame#warningCard {{
    background-color: {c['warning_bg']}; border: 1px solid {c['warning_border']}; border-radius: 8px;
}}
QFrame#dropZone {{
    border: 2px dashed {c['border_strong']}; border-radius: 8px; background-color: {c['drop_bg']};
}}
QFrame#dropZoneActive {{
    border: 2px dashed {c['primary']}; border-radius: 8px; background-color: {c['selection']};
}}
QFrame#cardFrame QLabel, QFrame#clickableCard QLabel, QFrame#warningCard QLabel, QFrame#dropZone QLabel {{
    background: transparent;
}}

/* ---------- прокрутка вкладок ---------- */
QScrollArea#tabScroll {{ border: none; background-color: {c['bg']}; }}
QScrollArea#tabScroll > QWidget > QWidget {{ background-color: {c['bg']}; }}
QScrollArea#plainScroll {{ border: none; background-color: transparent; }}
QScrollArea#plainScroll > QWidget > QWidget {{ background-color: transparent; }}

/* ---------- списки и таблицы ---------- */
QListWidget, QTableWidget, QTextBrowser {{
    background-color: {c['surface']}; border: 1px solid {c['border_soft']}; border-radius: 8px;
}}
QListWidget::item {{ padding: 6px 8px; border-radius: 4px; }}
QListWidget::item:hover {{ background-color: {c['surface_alt']}; }}
QListWidget::item:selected {{ background-color: {c['selection']}; color: {c['text']}; }}
QTableWidget {{ gridline-color: {c['border_soft']}; }}
QTableWidget::item {{ padding: 4px 8px; }}
QTableWidget::item:selected {{ background-color: {c['selection']}; color: {c['text']}; }}
QTableWidget QPushButton {{ padding: 4px 10px; min-height: 26px; font-size: {FONT_BASE}px; }}
QTableWidget QProgressBar {{ min-height: 18px; }}
QHeaderView::section {{
    background-color: {c['surface_alt']}; color: {c['text_muted']}; border: none;
    border-bottom: 1px solid {c['border_soft']}; padding: 6px 8px; font-weight: 600;
}}
QProgressBar {{
    border: 1px solid {c['border']}; border-radius: 6px; background-color: {c['surface_alt']};
    text-align: center; min-height: 20px; color: {c['text']};
}}
QProgressBar::chunk {{ background-color: {c['primary']}; border-radius: 5px; }}

QScrollBar:vertical {{ background: transparent; width: 12px; margin: 2px; }}
QScrollBar::handle:vertical {{ background-color: {c['border_strong']}; border-radius: 6px; min-height: 30px; }}
QScrollBar::handle:vertical:hover {{ background-color: {c['text_muted']}; }}
QScrollBar:horizontal {{ background: transparent; height: 12px; margin: 2px; }}
QScrollBar::handle:horizontal {{ background-color: {c['border_strong']}; border-radius: 6px; min-width: 30px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}

/* ---------- прочее ---------- */
QGroupBox {{
    background-color: {c['surface']}; border: 1px solid {c['border_soft']}; border-radius: 8px;
    margin-top: 12px; padding: 14px 12px 12px 12px; font-size: {FONT_CONTROL}px; font-weight: 600;
}}
QGroupBox::title {{ subcontrol-origin: margin; left: 12px; padding: 0 6px; color: {c['text_muted']}; }}
QFrame#toast[tone="ok"] {{ background-color: {c['accent']}; border-radius: 8px; }}
QFrame#toast[tone="error"] {{ background-color: {c['danger']}; border-radius: 8px; }}
QFrame#toast[tone="info"] {{ background-color: {c['primary']}; border-radius: 8px; }}
QFrame#toast QLabel {{ color: #ffffff; font-size: {FONT_CONTROL}px; font-weight: 600; background: transparent; }}
QFrame#separator {{ background-color: {c['border_soft']}; border: none; max-height: 1px; }}
QStatusBar {{ background-color: {c['surface']}; border-top: 1px solid {c['border_soft']}; }}
QStatusBar QLabel {{ color: {c['text_muted']}; }}
QSplitter::handle {{ background-color: {c['border_soft']}; width: 2px; }}
QMenu {{ background-color: {c['surface']}; border: 1px solid {c['border']}; border-radius: 6px; padding: 6px; }}
QMenu::item {{ padding: 6px 16px; border-radius: 4px; }}
QMenu::item:selected {{ background-color: {c['selection']}; }}
QDialog {{ background-color: {c['bg']}; }}
QToolTip {{
    background-color: {c['surface']}; color: {c['text']}; border: 1px solid {c['border']}; padding: 6px;
}}
"""
