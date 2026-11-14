"""Application entry point.

Startup order matters:

1. load settings and configure logging (with redaction) first,
2. create the ``QApplication`` and apply the theme,
3. start the asyncio worker thread,
4. build the window,
5. on exit, cancel UI work, disconnect the OPC UA session *inside* the loop,
   then stop the loop and join the thread.
"""

from __future__ import annotations

import logging
import sys

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import QApplication

from .. import __version__
from ..config import AppSettings, TrustStore
from ..logging_setup import setup_logging
from ..opcua.client_service import OpcUaService
from .async_runner import AsyncRunner
from .main_window import MainWindow
from .theme import ACCENT, apply_theme

_logger = logging.getLogger(__name__)


def _build_icon() -> QIcon:
    """Generate the window icon so the application needs no asset files."""
    size = 64
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    try:
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setBrush(QColor(ACCENT))
        painter.setPen(Qt.NoPen)
        painter.drawRoundedRect(2, 2, size - 4, size - 4, 14, 14)
        painter.setPen(QColor("#ffffff"))
        font = QFont()
        font.setPointSize(22)
        font.setBold(True)
        painter.setFont(font)
        painter.drawText(pixmap.rect(), Qt.AlignCenter, "UA")
    finally:
        painter.end()
    return QIcon(pixmap)


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv if argv is None else argv)

    settings = AppSettings.load()
    setup_logging(settings.log_level)
    _logger.info("Starting OPC UA Explorer %s", __version__)

    app = QApplication(args)
    app.setApplicationName("OPC UA Explorer")
    app.setApplicationDisplayName(f"OPC UA Explorer {__version__}")
    app.setOrganizationName("opcua-explorer")
    app.setApplicationVersion(__version__)
    app.setWindowIcon(_build_icon())
    apply_theme(app)

    runner = AsyncRunner()
    runner.start()

    # One runner (one asyncio loop) and one trust store shared by all sessions.
    trust_store = TrustStore()
    service = OpcUaService(trust_store=trust_store)
    window = MainWindow(runner, service, settings, trust_store=trust_store)
    window.show()

    try:
        return app.exec()
    finally:
        window.shutdown()
        for session_service in window.services:
            try:
                runner.run_until_complete(session_service.shutdown, timeout=6.0)
            except Exception as exc:  # pragma: no cover - best effort on exit
                _logger.warning("Closing an OPC UA session on exit reported: %s", exc)
        runner.stop()
        # Destroy the widget tree before Python finalizes. PySide6 aborts in GC
        # ("bool_dealloc ... refcount error in a C extension") when live widgets are
        # still around during interpreter shutdown.
        window.close()
        window.deleteLater()
        app.processEvents()
        _logger.info("OPC UA Explorer stopped")


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
