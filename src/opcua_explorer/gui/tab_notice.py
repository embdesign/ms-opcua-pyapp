"""Space-saving furniture for a tab: one-line notifications and hidden option rows.

An engineering tool is judged on the working area, not on the prose above it, but
the prose has to stay true and reachable. Two small widgets live here so every
page solves that the same way:

* :class:`TabNotice` shows **one line** of description by default and the full
  explanation when the operator turns "Show tab descriptions" on (View menu) or
  hovers it. The collapsed line is a strict subset of the full text, never a
  different claim.
* :class:`OptionsRow` keeps secondary controls out of the working area until they
  are asked for. A collapsed row must never shape a request silently, so the
  toggle **names the non-default options it is hiding** (`Options (average) ▸`).

Both are layout-only: nothing here sends, reads or decides.
"""

from __future__ import annotations

from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QWidget

#: Arrow glyphs of the options toggle (collapsed / expanded).
_COLLAPSED = "▸"
_EXPANDED = "▾"


class TabNotice(QLabel):
    """The description of one tab: one line now, the full text on request."""

    def __init__(self, summary: str, detail: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("subTitle")
        self.setWordWrap(True)
        self._summary = " ".join(summary.split())
        self._detail = " ".join(detail.split())
        self._verbose = False
        self._apply()

    # -- state -------------------------------------------------------------
    @property
    def verbose(self) -> bool:
        return self._verbose

    @property
    def summary(self) -> str:
        return self._summary

    @property
    def detail(self) -> str:
        return self._detail

    def set_verbose(self, verbose: bool) -> None:
        """Show the full explanation instead of the one-line summary."""
        verbose = bool(verbose)
        if verbose == self._verbose:
            return
        self._verbose = verbose
        self._apply()

    def _apply(self) -> None:
        self.setText(self._detail if self._verbose else self._summary)
        # Collapsed, the tooltip carries what the single line leaves out.
        self.setToolTip("" if self._verbose else self._detail)


class OptionsRow:
    """A control row that stays hidden until the operator asks for it.

    ``toggle`` goes into the always-visible row; ``widget`` holds the secondary
    controls and is added to the page layout (hidden). :meth:`set_active` names
    the non-default options the collapsed row is carrying, so a hidden row can
    never be forgotten: the button reads ``Options (average) ▸``.
    """

    def __init__(self, label: str = "Options", parent: QWidget | None = None) -> None:
        self._label = label
        self._summary = ""
        self.widget = QWidget(parent)
        self.layout = QHBoxLayout(self.widget)
        self.layout.setContentsMargins(0, 0, 0, 0)
        self.layout.setSpacing(8)
        self.widget.setVisible(False)
        self.toggle = QPushButton(parent)
        self.toggle.setObjectName("flat")
        self.toggle.setCheckable(True)
        self.toggle.setChecked(False)
        self.toggle.setToolTip(f"Show or hide the {label.lower()} of this page.")
        self.toggle.toggled.connect(self._on_toggled)
        self._refresh()

    # -- content -----------------------------------------------------------
    def add_widget(self, widget: QWidget, stretch: int = 0) -> None:
        self.layout.addWidget(widget, stretch)

    def add_spacing(self, width: int) -> None:
        self.layout.addSpacing(width)

    def add_stretch(self, stretch: int = 1) -> None:
        self.layout.addStretch(stretch)

    # -- state -------------------------------------------------------------
    def set_visible(self, visible: bool) -> None:
        """Open or close the row without going through the toggle button."""
        self.toggle.setChecked(bool(visible))

    def set_active(self, summary: str) -> None:
        """Name the non-default options in this row; ``""`` means "all defaults"."""
        cleaned = " ".join(str(summary).split())
        if cleaned == self._summary:
            return
        self._summary = cleaned
        self._refresh()

    def toggle_text(self) -> str:
        arrow = _EXPANDED if self.toggle.isChecked() else _COLLAPSED
        marker = f" ({self._summary})" if self._summary and not self.toggle.isChecked() else ""
        return f"{self._label}{marker} {arrow}"

    def _on_toggled(self, shown: bool) -> None:
        self.widget.setVisible(bool(shown))
        self._refresh()

    def _refresh(self) -> None:
        self.toggle.setText(self.toggle_text())
        if self._summary and not self.toggle.isChecked():
            self.toggle.setToolTip(f"{self._label} in effect: {self._summary}.")
        else:
            self.toggle.setToolTip(f"Show or hide the {self._label.lower()} of this page.")


def apply_notices(widget: QWidget, verbose: bool) -> int:
    """Expand or collapse every :class:`TabNotice` below ``widget``; returns the count."""
    notices = widget.findChildren(TabNotice)
    for notice in notices:
        notice.set_verbose(verbose)
    return len(notices)


def notices(widget: QWidget) -> list[TabNotice]:
    """Every tab description below ``widget``, in widget order."""
    return list(widget.findChildren(TabNotice))


__all__ = ["OptionsRow", "TabNotice", "apply_notices", "notices"]
