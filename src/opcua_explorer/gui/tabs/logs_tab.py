"""Log and report viewer.

Log records arrive from any thread (asyncio worker, asyncua internals). They are
forwarded through a Qt signal so the widget is only ever touched in the GUI
thread. Records are already redacted by :mod:`opcua_explorer.logging_setup`.
"""

from __future__ import annotations

import contextlib
import logging
from pathlib import Path

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ...logging_setup import get_log_bus
from ..async_runner import AsyncRunner
from ..tab_notice import TabNotice
from ..theme import mono_font

_LEVELS = [
    ("Debug", logging.DEBUG),
    ("Info", logging.INFO),
    ("Warning", logging.WARNING),
    ("Error", logging.ERROR),
]


class _LogBridge(QObject):
    """Marshals log callbacks (any thread) into the GUI thread."""

    message = Signal(int, str, str)


class LogsTab(QWidget):
    def __init__(self, runner: AsyncRunner | None = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        #: Optional worker loop for the log export; without it the write is synchronous.
        self._runner = runner
        self._min_level = logging.INFO
        self._bridge = _LogBridge(self)
        self._bridge.message.connect(self._on_message)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(10)

        title = QLabel("Logs & reports")
        title.setObjectName("sectionTitle")
        layout.addWidget(title)
        layout.addWidget(
            TabNotice(
                "Passwords, private-key passwords and PEM key material are redacted before they reach this view.",
                "Passwords, private-key passwords and PEM key material are redacted before a record reaches "
                "this view. The Reports page lists what the write, test and sequence paths wrote, with the path "
                "of each file.",
            )
        )

        tabs = QTabWidget()
        tabs.addTab(self._build_log_page(), "Application log")
        tabs.addTab(self._build_report_page(), "Reports")
        layout.addWidget(tabs, 1)

        self._unsubscribe = get_log_bus().subscribe(
            lambda level, name, message: self._bridge.message.emit(level, name, message)
        )
        for line in get_log_bus().snapshot():
            self.console.appendPlainText(line)

    def _build_log_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(8, 8, 8, 8)

        toolbar = QHBoxLayout()
        toolbar.addWidget(QLabel("Minimum level:"))
        self.level_combo = QComboBox()
        for label, level in _LEVELS:
            self.level_combo.addItem(label, level)
        self.level_combo.setCurrentIndex(1)
        self.level_combo.currentIndexChanged.connect(self._on_level_changed)
        toolbar.addWidget(self.level_combo)

        self.autoscroll_check = QCheckBox("Auto-scroll")
        self.autoscroll_check.setChecked(True)
        toolbar.addWidget(self.autoscroll_check)
        toolbar.addStretch(1)

        clear_btn = QPushButton("Clear")
        clear_btn.clicked.connect(self._on_clear)
        copy_btn = QPushButton("Copy")
        copy_btn.clicked.connect(lambda: QApplication.clipboard().setText(self.console.toPlainText()))
        save_btn = QPushButton("Save log…")
        save_btn.clicked.connect(self._on_save)
        toolbar.addWidget(clear_btn)
        toolbar.addWidget(copy_btn)
        toolbar.addWidget(save_btn)
        layout.addLayout(toolbar)

        self.console = QPlainTextEdit()
        self.console.setObjectName("console")
        self.console.setReadOnly(True)
        self.console.setFont(mono_font(9))
        self.console.setMaximumBlockCount(10_000)
        layout.addWidget(self.console, 1)
        return page

    def _build_report_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(8, 8, 8, 8)

        self.report_view = QPlainTextEdit()
        self.report_view.setObjectName("summary")
        self.report_view.setReadOnly(True)
        self.report_view.setFont(mono_font(9))
        self.report_view.setPlainText(
            "Functional test reports are written from the Tests tab (JSON and HTML).\n"
            "This page records operational summaries and the paths of written reports.\n\n"
        )
        layout.addWidget(self.report_view, 1)

        copy_btn = QPushButton("Copy")
        copy_btn.clicked.connect(lambda: QApplication.clipboard().setText(self.report_view.toPlainText()))
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(copy_btn)
        layout.addLayout(row)
        return page

    # -- slots -------------------------------------------------------------
    def _on_level_changed(self, index: int) -> None:
        self._min_level = int(self.level_combo.itemData(index))

    def _on_message(self, level: int, name: str, message: str) -> None:
        if level < self._min_level:
            return
        self.console.appendPlainText(f"{logging.getLevelName(level):<8} {name}: {message}")
        if self.autoscroll_check.isChecked():
            scrollbar = self.console.verticalScrollBar()
            scrollbar.setValue(scrollbar.maximum())

    def _on_clear(self) -> None:
        self.console.clear()

    def _on_save(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self, "Save log", str(Path.home() / "opcua-explorer.log"), "Log files (*.log *.txt);;All files (*)"
        )
        if not path:
            return
        text = self.console.toPlainText()
        if self._runner is None:
            self._write_log(path, text)
            return
        try:
            self._runner.submit_call(
                Path(path).write_text,
                text,
                encoding="utf-8",
                on_done=lambda _result: self.console.appendPlainText(f"Log saved to {path}"),
                on_error=lambda message: self.console.appendPlainText(f"Could not save the log: {message}"),
            )
        except RuntimeError:
            # The worker loop is already stopping (application exit); write directly.
            self._write_log(path, text)

    def _write_log(self, path: str, text: str) -> None:
        try:
            Path(path).write_text(text, encoding="utf-8")
        except OSError as exc:
            self.console.appendPlainText(f"Could not save the log: {exc}")

    def append_report(self, text: str) -> None:
        self.report_view.appendPlainText(f"{text}")

    def save_log(self) -> None:
        """Public entry point used by the File menu."""
        self._on_save()

    def shutdown(self) -> None:
        with contextlib.suppress(Exception):
            self._unsubscribe()
