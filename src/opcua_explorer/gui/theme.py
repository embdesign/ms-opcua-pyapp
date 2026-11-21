"""Application theme (Qt Fusion + a single stylesheet).

Keeping all styling in one place makes the GUI consistent and avoids per-widget
inline styles. Blocks that need dynamic colours (the connection status chip) use
Qt dynamic properties and :func:`set_chip_state`.
"""

from __future__ import annotations

from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtWidgets import QApplication, QLabel

ACCENT = "#14639e"
ACCENT_DARK = "#0f5286"
ERROR = "#b3261e"
WARNING = "#8a5a00"
SUCCESS = "#17603a"

STYLESHEET = """
QWidget {
    color: #1b2533;
    font-size: 10pt;
}
QMainWindow, QDialog {
    background: #f2f4f7;
}
QMenuBar {
    background: #f2f4f7;
    border-bottom: 1px solid #e0e5ec;
}
QMenuBar::item { padding: 4px 10px; background: transparent; }
QMenuBar::item:selected { background: #e3ebf5; border-radius: 4px; }
QMenu { background: #ffffff; border: 1px solid #d9dfe7; padding: 4px; }
QMenu::item { padding: 6px 22px; border-radius: 4px; }
QMenu::item:selected { background: #e3ebf5; }

QGroupBox {
    background: #ffffff;
    border: 1px solid #d9dfe7;
    border-radius: 8px;
    margin-top: 16px;
    padding: 14px 12px 12px 12px;
    font-weight: 600;
}
QGroupBox::title {
    subcontrol-origin: margin;
    left: 12px;
    padding: 0 5px;
    color: #3b4d63;
}

QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox {
    background: #ffffff;
    border: 1px solid #c8d1dc;
    border-radius: 6px;
    padding: 6px 8px;
    min-height: 18px;
    selection-background-color: #14639e;
    selection-color: #ffffff;
}
QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus {
    border: 1px solid #14639e;
}
QLineEdit:disabled, QSpinBox:disabled, QDoubleSpinBox:disabled, QComboBox:disabled {
    background: #eef1f5;
    color: #8b97a5;
}
QComboBox::drop-down { border: none; width: 22px; }
QComboBox QAbstractItemView {
    background: #ffffff;
    border: 1px solid #c8d1dc;
    selection-background-color: #dcebf8;
    selection-color: #12314a;
    padding: 4px;
}

QPushButton {
    background: #ffffff;
    border: 1px solid #c8d1dc;
    border-radius: 6px;
    padding: 7px 16px;
    font-weight: 600;
    color: #22303f;
    min-height: 18px;
}
QPushButton:hover { background: #f4f7fb; border-color: #9fb0c4; }
QPushButton:pressed { background: #e8eef6; }
QPushButton:disabled { color: #9aa6b4; background: #eef1f5; border-color: #dde3ea; }
QPushButton#primary {
    background: #14639e;
    border-color: #14639e;
    color: #ffffff;
}
QPushButton#primary:hover { background: #0f5286; border-color: #0f5286; }
QPushButton#primary:disabled { background: #a9c3d8; border-color: #a9c3d8; color: #f2f6fa; }
QPushButton#danger { color: #b3261e; border-color: #e2b8b4; }
QPushButton#danger:hover { background: #fdeceb; }
QPushButton#flat { border: none; background: transparent; color: #14639e; }

QTabWidget::pane {
    border: 1px solid #d9dfe7;
    border-radius: 8px;
    background: #ffffff;
    top: -1px;
}
QTabBar::tab {
    background: transparent;
    padding: 9px 18px;
    margin-right: 3px;
    border-radius: 6px 6px 0 0;
    color: #51637a;
    font-weight: 600;
}
QTabBar::tab:selected {
    background: #ffffff;
    color: #14639e;
    border: 1px solid #d9dfe7;
    border-bottom-color: #ffffff;
}
QTabBar::tab:hover { color: #14639e; }

QTreeWidget, QTableWidget, QListWidget {
    background: #ffffff;
    border: 1px solid #d9dfe7;
    border-radius: 8px;
    alternate-background-color: #f7f9fc;
    gridline-color: #e6ebf1;
    selection-background-color: #dcebf8;
    selection-color: #12314a;
}
QTreeWidget::item, QListWidget::item { padding: 4px 2px; }
QTreeWidget::item:selected, QListWidget::item:selected { border-radius: 4px; }
QHeaderView::section {
    background: #eef2f7;
    border: none;
    border-bottom: 1px solid #d9dfe7;
    padding: 6px 8px;
    font-weight: 600;
    color: #42536a;
}
QTableCornerButton::section { background: #eef2f7; border: none; }

QProgressBar {
    border: 1px solid #d9dfe7;
    border-radius: 6px;
    background: #eef1f5;
    max-height: 14px;
    text-align: center;
    font-size: 8pt;
    color: #38495c;
}
QProgressBar::chunk { background: #14639e; border-radius: 5px; }

QPlainTextEdit, QTextEdit {
    background: #ffffff;
    border: 1px solid #d9dfe7;
    border-radius: 8px;
    padding: 6px;
}
QPlainTextEdit#console {
    background: #11161d;
    color: #d8e2ec;
    border: 1px solid #1f2a36;
    selection-background-color: #27496d;
}
QPlainTextEdit#summary { background: #fbfcfe; }
QPlainTextEdit#fingerprint {
    background: #f4f6f9;
    border: 1px solid #d9dfe7;
    border-radius: 6px;
    padding: 8px;
}

QLabel#hintLabel { color: #5b6a7d; }
QLabel#warnLabel {
    color: #8a5a00;
    border: 1px solid #f0d9a6;
    border-radius: 6px;
    padding: 2px 6px;
}
QLabel#warningLabel {
    color: #8a5a00;
    background: #fff6e0;
    border: 1px solid #f0d9a6;
    border-radius: 6px;
    padding: 6px 9px;
}
QLabel#errorLabel {
    color: #8c1d18;
    background: #fdeceb;
    border: 1px solid #f2c4c1;
    border-radius: 6px;
    padding: 6px 9px;
}
QLabel#sectionTitle {
    font-size: 15pt;
    font-weight: 700;
    color: #16222f;
}
QLabel#subTitle { color: #5b6a7d; }

QLabel#chip {
    border-radius: 10px;
    padding: 4px 12px;
    font-weight: 700;
    background: #e8ecf2;
    color: #4a586b;
    border: 1px solid #d5dce5;
}
QLabel#chip[state="connected"] {
    background: #e2f4ea;
    color: #17603a;
    border: 1px solid #b6e0c8;
}
QLabel#chip[state="connecting"] {
    background: #fff3d6;
    color: #7a5200;
    border: 1px solid #f0d9a6;
}
QLabel#chip[state="error"] {
    background: #fdeceb;
    color: #8c1d18;
    border: 1px solid #f2c4c1;
}

QSplitter::handle { background: #dfe5ec; }
QScrollArea { border: none; background: transparent; }
QCheckBox, QRadioButton { spacing: 8px; }
QStatusBar { background: #e9edf3; color: #3d4c5f; }
QStatusBar::item { border: none; }
QToolTip {
    background: #1e2a36;
    color: #ffffff;
    border: none;
    padding: 5px 7px;
}
"""


def _pick_family(candidates: list[str], fallback: str) -> str:
    available = set(QFontDatabase.families())
    for name in candidates:
        if name in available:
            return name
    return fallback


def apply_theme(app: QApplication) -> None:
    app.setStyle("Fusion")
    family = _pick_family(
        ["Inter", "Segoe UI Variable Text", "Segoe UI", "Noto Sans", "DejaVu Sans"],
        "Sans Serif",
    )
    font = QFont(family)
    font.setPointSize(10)
    app.setFont(font)
    app.setStyleSheet(STYLESHEET)


def mono_font(point_size: int = 9) -> QFont:
    family = _pick_family(
        ["JetBrains Mono", "Cascadia Mono", "Consolas", "DejaVu Sans Mono", "Liberation Mono"],
        "Monospace",
    )
    font = QFont(family)
    font.setStyleHint(QFont.Monospace)
    font.setPointSize(point_size)
    return font


def set_chip_state(label: QLabel, state: str) -> None:
    """Repolish a chip label after changing its ``state`` dynamic property."""
    label.setProperty("state", state)
    style = label.style()
    style.unpolish(label)
    style.polish(label)
    label.update()
