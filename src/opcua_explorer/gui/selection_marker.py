"""One label that says whether a page's target still matches the address-space selection.

The classic wrong-node mistake in an engineering client is a target field that was
filled minutes ago: the page keeps working, the browser has moved on, and nothing on
screen says so. Each page still owns its target (watching one node while writing
another is legitimate), but the marker next to the field speaks up as soon as the
two disagree — and stays hidden while they agree, so it costs no space.

The comparison is textual and only a **hint**: it never changes a request, and a
page's own validation remains the gate. ``SessionWorkspace`` refreshes every marker
when the browser selection changes; a marker also refreshes itself when its field
is edited.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel, QLineEdit, QWidget

#: Longest selection text shown inline; the tooltip carries the full value.
_MAX_INLINE_CHARS = 48


class SelectionMarker(QLabel):
    """Shows that a target field differs from the current browser selection."""

    def __init__(
        self,
        line_edit: QLineEdit,
        provider: Callable[[], str | None] | None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._line_edit = line_edit
        self._provider = provider
        self.setObjectName("warnLabel")
        self.setWordWrap(False)
        self.setTextInteractionFlags(Qt.TextSelectableByMouse)
        line_edit.textChanged.connect(self._on_text_changed)
        self.refresh()

    # -- state -------------------------------------------------------------
    def selection(self) -> str | None:
        """The address-space selection this marker compares against, if any."""
        if self._provider is None:
            return None
        try:
            value = self._provider()
        except Exception:  # pragma: no cover - a broken provider must not break a page
            return None
        text = "" if value is None else str(value).strip()
        return text or None

    def target(self) -> str:
        return self._line_edit.text().strip()

    @property
    def differs(self) -> bool:
        target, selection = self.target(), self.selection()
        return bool(target and selection and target != selection)

    def refresh(self) -> None:
        """Re-evaluate the marker; hidden whenever there is nothing to warn about."""
        selection = self.selection()
        if not self.differs or selection is None:
            self.clear()
            self.setToolTip("")
            self.setVisible(False)
            return
        inline = selection if len(selection) <= _MAX_INLINE_CHARS else selection[: _MAX_INLINE_CHARS - 1] + "…"
        self.setText(f"⚠ differs from the selection ({inline})")
        self.setToolTip(
            f"This page targets {self.target()}, while the address-space browser has {selection} selected. "
            "Press 'Use browser selection' to take the selection, or type the NodeId you really mean — "
            "the operation always uses this field."
        )
        self.setVisible(True)

    def _on_text_changed(self, _text: Any = "") -> None:
        self.refresh()


def refresh_markers(root: QWidget) -> int:
    """Refresh every :class:`SelectionMarker` below ``root``; returns the count."""
    markers = root.findChildren(SelectionMarker)
    for marker in markers:
        marker.refresh()
    return len(markers)


__all__ = ["SelectionMarker", "refresh_markers"]
