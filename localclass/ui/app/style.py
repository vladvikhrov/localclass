"""Оформление: палитры светлой и тёмной темы + чистый QSS.

Масштаб интерфейса рассчитан на ноутбуки с масштабированием экрана 125–150 %: основной текст 13 pt,
заголовки и кнопки 15–16 pt, минимальная высота кнопок и полей 38 px, радиус 6 px, явные состояния
:hover / :pressed / :disabled.
"""
from __future__ import annotations

BASE_PT = 13
HEADING_PT = 16
CONTROL_PT = 14

LIGHT = {
    "bg": "#f4f6f9", "surface": "#ffffff", "surface_alt": "#eef2f7", "border": "#d3dae3",
    "text": "#1c2430", "text_muted": "#5b6b7f", "primary": "#2b6cb0", "primary_hover": "#2559901",
    "primary_pressed": "#1f4a79", "accent": "#1a7f37", "danger": "#c62828", "danger_hover": "#a81f1f",
    "warning_bg": "#fff8e1", "warning_border": "#f0cf7a", "selection": "#dce9f8", "disabled": "#9aa7b6",
    "disabled_bg": "#e7ebf0", "toast_text": "#ffffff",
}
LIGHT["primary_hover"] = "#255990"

DARK = {
    "bg": "#171c22", "surface": "#1f262e", "surface_alt": "#262f39", "border": "#36414e",
    "text": "#e8edf3", "text_muted": "#9fb0c3", "primary": "#4c93d8", "primary_hover": "#5ea4e9",
    "primary_pressed": "#3b7cbb", "accent": "#4ec97a", "danger": "#e5675f", "danger_hover": "#f07a72",
    "warning_bg": "#3a3321", "warning_border": "#6b5a2d", "selection": "#2c4762", "disabled": "#6a7988",
    "disabled_bg": "#2a323b", "toast_text": "#0f1317",
}


def palette(theme: str) -> dict[str, str]:
    return DARK if theme == "dark" else LIGHT


def link_color(theme: str) -> str:
    return palette(theme)["primary"]


def rich_text_css(theme: str) -> str:
    """Стили для HTML внутри QTextBrowser: ссылки и код должны читаться в обеих темах."""
    c = palette(theme)
    return (f"a {{ color: {c['primary']}; text-decoration: none; }}"
            f"code {{ background: {c['surface_alt']}; color: {c['text']}; padding: 1px 4px; border-radius: 4px; }}"
            f"table {{ color: {c['text']}; }}")


def qss(theme: str = "light") -> str:
    c = palette(theme)
    return f"""
/* ---------- база ---------- */
QWidget {{
    background: {c['bg']};
    color: {c['text']};
    font-family: "Segoe UI", "Noto Sans", "DejaVu Sans", sans-serif;
    font-size: {BASE_PT}pt;
}}
QLabel {{ background: transparent; }}
QLabel[role="h1"] {{ font-size: {HEADING_PT + 4}pt; font-weight: 600; }}
QLabel[role="h2"] {{ font-size: {HEADING_PT}pt; font-weight: 600; }}
QLabel[role="muted"] {{ color: {c['text_muted']}; }}
QLabel[role="code"] {{ font-size: 30pt; font-weight: 700; letter-spacing: 4px; color: {c['primary']}; }}
QLabel[role="pill"] {{
    background: {c['surface_alt']}; border: 1px solid {c['border']}; border-radius: 6px;
    padding: 4px 10px; color: {c['text_muted']};
}}

/* ---------- кнопки ---------- */
QPushButton {{
    background: {c['surface']};
    border: 1px solid {c['border']};
    border-radius: 6px;
    padding: 8px 16px;
    min-height: 38px;
    font-size: {CONTROL_PT}pt;
}}
QPushButton:hover {{ background: {c['surface_alt']}; border-color: {c['primary']}; }}
QPushButton:pressed {{ background: {c['selection']}; }}
QPushButton:disabled {{ background: {c['disabled_bg']}; color: {c['disabled']}; border-color: {c['disabled_bg']}; }}
QPushButton[kind="primary"] {{
    background: {c['primary']}; color: #ffffff; border: none; font-weight: 600; padding: 10px 22px; min-height: 40px;
}}
QPushButton[kind="primary"]:hover {{ background: {c['primary_hover']}; }}
QPushButton[kind="primary"]:pressed {{ background: {c['primary_pressed']}; }}
QPushButton[kind="primary"]:disabled {{ background: {c['disabled_bg']}; color: {c['disabled']}; }}
QPushButton[kind="danger"] {{
    background: {c['danger']}; color: #ffffff; border: none; font-weight: 600; min-height: 40px;
}}
QPushButton[kind="danger"]:hover {{ background: {c['danger_hover']}; }}
QPushButton[kind="danger"]:pressed {{ background: {c['danger_hover']}; }}
QPushButton[kind="ghost"] {{ background: transparent; border: 1px solid {c['border']}; }}
QPushButton[kind="ghost"]:hover {{ background: {c['surface_alt']}; }}
QPushButton[kind="icon"] {{ min-width: 44px; min-height: 40px; font-size: {HEADING_PT}pt; padding: 4px; }}

/* ---------- поля ввода ---------- */
QLineEdit, QTextEdit, QPlainTextEdit, QSpinBox, QComboBox {{
    background: {c['surface']};
    border: 1px solid {c['border']};
    border-radius: 6px;
    padding: 8px 10px;
    min-height: 36px;
    selection-background-color: {c['selection']};
    selection-color: {c['text']};
}}
QLineEdit[role="big"] {{ font-size: {HEADING_PT}pt; min-height: 44px; }}
QLineEdit[role="pin"] {{ font-size: {HEADING_PT}pt; font-weight: 700; letter-spacing: 6px; max-width: 160px; }}
QLineEdit:focus, QTextEdit:focus, QPlainTextEdit:focus, QSpinBox:focus, QComboBox:focus {{
    border: 2px solid {c['primary']}; padding: 7px 9px;
}}
QLineEdit:disabled, QTextEdit:disabled, QSpinBox:disabled {{ background: {c['disabled_bg']}; color: {c['disabled']}; }}
QComboBox {{ padding-right: 28px; }}
QSpinBox {{ padding-right: 26px; }}
QCheckBox {{ spacing: 8px; padding: 4px 0; background: transparent; }}
QCheckBox::indicator {{ width: 20px; height: 20px; border: 1px solid {c['border']}; border-radius: 4px; background: {c['surface']}; }}
QCheckBox::indicator:checked {{ background: {c['primary']}; border-color: {c['primary']}; }}
QCheckBox::indicator:hover {{ border-color: {c['primary']}; }}
QCheckBox:disabled {{ color: {c['disabled']}; }}

/* ---------- вкладки ---------- */
QTabWidget::pane {{ border: 1px solid {c['border']}; border-radius: 6px; background: {c['bg']}; top: -1px; }}
QTabBar::tab {{
    background: transparent; color: {c['text_muted']};
    padding: 10px 18px; margin-right: 4px; min-height: 34px;
    border-top-left-radius: 6px; border-top-right-radius: 6px;
    font-size: {CONTROL_PT}pt;
}}
QTabBar::tab:selected {{ background: {c['surface']}; color: {c['text']}; font-weight: 600; border: 1px solid {c['border']}; border-bottom-color: {c['surface']}; }}
QTabBar::tab:hover:!selected {{ background: {c['surface_alt']}; color: {c['text']}; }}
QTabBar::tab:disabled {{ color: {c['disabled']}; }}

/* ---------- карточки ---------- */
QFrame[card="true"] {{ background: {c['surface']}; border: 1px solid {c['border']}; border-radius: 6px; }}
QFrame[card="clickable"] {{ background: {c['surface']}; border: 1px solid {c['border']}; border-radius: 6px; }}
QFrame[card="clickable"]:hover {{ background: {c['surface_alt']}; border: 1px solid {c['primary']}; }}
QFrame[card="drop"] {{
    background: {c['surface_alt']}; border: 2px dashed {c['border']}; border-radius: 6px;
}}
QFrame[card="dropActive"] {{
    background: {c['selection']}; border: 2px dashed {c['primary']}; border-radius: 6px;
}}
QFrame[card="warning"] {{ background: {c['warning_bg']}; border: 1px solid {c['warning_border']}; border-radius: 6px; }}

/* ---------- списки и таблицы ---------- */
QListWidget, QTableWidget, QTextBrowser {{
    background: {c['surface']}; border: 1px solid {c['border']}; border-radius: 6px;
}}
QListWidget::item {{ padding: 8px 10px; border-radius: 4px; }}
QListWidget::item:hover {{ background: {c['surface_alt']}; }}
QListWidget::item:selected {{ background: {c['selection']}; color: {c['text']}; }}
QTableWidget {{ gridline-color: {c['border']}; }}
QTableWidget::item {{ padding: 6px 8px; }}
QTableWidget::item:selected {{ background: {c['selection']}; color: {c['text']}; }}
QTableWidget QPushButton {{ min-height: 32px; padding: 4px 12px; font-size: {BASE_PT}pt; }}
QTableWidget QProgressBar {{ min-height: 20px; }}
QHeaderView::section {{
    background: {c['surface_alt']}; color: {c['text_muted']}; border: none;
    border-bottom: 1px solid {c['border']}; padding: 8px; font-weight: 600;
}}
QProgressBar {{
    border: 1px solid {c['border']}; border-radius: 6px; background: {c['surface_alt']};
    text-align: center; min-height: 22px;
}}
QProgressBar::chunk {{ background: {c['primary']}; border-radius: 5px; }}
QScrollBar:vertical {{ background: transparent; width: 12px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: {c['border']}; border-radius: 6px; min-height: 30px; }}
QScrollBar::handle:vertical:hover {{ background: {c['text_muted']}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
QScrollBar:horizontal {{ background: transparent; height: 12px; margin: 2px; }}
QScrollBar::handle:horizontal {{ background: {c['border']}; border-radius: 6px; min-width: 30px; }}

/* ---------- прочее ---------- */
QGroupBox {{
    background: {c['surface']}; border: 1px solid {c['border']}; border-radius: 6px;
    margin-top: 14px; padding: 14px 12px 12px 12px; font-size: {CONTROL_PT}pt; font-weight: 600;
}}
QGroupBox::title {{ subcontrol-origin: margin; left: 12px; padding: 0 6px; color: {c['text_muted']}; }}
QToolButton[kind="collapse"] {{
    background: {c['surface_alt']}; border: 1px solid {c['border']}; border-radius: 6px;
    padding: 10px 14px; font-size: {CONTROL_PT}pt; font-weight: 600; text-align: left; min-height: 38px;
}}
QToolButton[kind="collapse"]:hover {{ background: {c['selection']}; border-color: {c['primary']}; }}
QFrame[role="toast"][tone="ok"] {{ background: {c['accent']}; border-radius: 6px; }}
QFrame[role="toast"][tone="error"] {{ background: {c['danger']}; border-radius: 6px; }}
QFrame[role="toast"][tone="info"] {{ background: {c['primary']}; border-radius: 6px; }}
QFrame[role="toast"] QLabel {{ color: {c['toast_text']}; font-size: {CONTROL_PT}pt; font-weight: 600; }}
QStatusBar {{ background: {c['surface']}; border-top: 1px solid {c['border']}; }}
QStatusBar QLabel {{ color: {c['text_muted']}; }}
QSplitter::handle {{ background: {c['border']}; width: 2px; }}
QMenu {{ background: {c['surface']}; border: 1px solid {c['border']}; border-radius: 6px; padding: 6px; }}
QMenu::item {{ padding: 8px 18px; border-radius: 4px; }}
QMenu::item:selected {{ background: {c['selection']}; }}
QDialog {{ background: {c['bg']}; }}
QToolTip {{ background: {c['surface']}; color: {c['text']}; border: 1px solid {c['border']}; padding: 6px; }}
"""
