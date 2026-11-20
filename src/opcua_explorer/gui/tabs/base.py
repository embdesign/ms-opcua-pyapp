"""Shared plumbing for the session pages.

Every tab used to carry its own copy of the same six helpers (status text,
stylesheet re-polish, task bookkeeping, cancellation, shutdown guard). They
live here once, so a new page only implements what is specific to it.

:class:`TabRuntime` is a plain mixin (no Qt base) and can therefore be combined
with a ``QWidget`` page (:class:`SessionTab`) or a ``QDialog``.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from PySide6.QtWidgets import QWidget

_logger = logging.getLogger(__name__)


class TabRuntime:
    """Task registry, closed flag and status helpers used by every session page.

    A host page provides ``self._runner`` (an
    :class:`~opcua_explorer.gui.async_runner.AsyncRunner`) and usually a
    ``self.status_label``. ``_closed`` is set by :meth:`shutdown` so a result
    that arrives after the page was torn down is dropped instead of touching
    deleted widgets.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        #: In-flight tasks by key, so shutdown can cancel them.
        self._tasks: dict[str, Any] = {}
        #: Set when the page is being torn down; late results must be ignored.
        self._closed = False

    # -- status ------------------------------------------------------------
    def _set_status(self, message: str, *, error: bool = False) -> None:
        label = getattr(self, "status_label", None)
        if label is None:  # pragma: no cover - defensive for pages without one
            _logger.debug("status message without a label: %s", message)
            return
        label.setText(("⚠ " if error else "") + message)
        label.setObjectName("errorLabel" if error else "hintLabel")
        self._restyle(label)

    @staticmethod
    def _restyle(widget: Any) -> None:
        """Re-apply the stylesheet after an ``objectName`` change."""
        style = widget.style()
        style.unpolish(widget)
        style.polish(widget)

    # -- background work ---------------------------------------------------
    def _submit(
        self,
        coro: Awaitable[Any] | Callable[[], Awaitable[Any]],
        *,
        on_done: Callable[[Any], None] | None = None,
        on_error: Callable[[str], None] | None = None,
        on_progress: Callable[[Any], None] | None = None,
        key: str | None = None,
    ) -> Any:
        """Submit one operation and remember its handle for :meth:`shutdown`."""
        task_key = key or self._runner.new_key()

        def done(result: Any) -> None:
            self._tasks.pop(task_key, None)
            if not self._closed and on_done is not None:
                on_done(result)

        def failed(message: str) -> None:
            self._tasks.pop(task_key, None)
            if not self._closed and on_error is not None:
                on_error(message)

        handle = self._runner.submit(
            coro, key=task_key, on_done=done, on_error=failed, on_progress=on_progress
        )
        self._tasks[task_key] = handle
        return handle

    def _cancel_tasks(self) -> None:
        """Cancel every tracked task; safe to call more than once."""
        pending = list(self._tasks.values())
        self._tasks.clear()
        for handle in pending:
            handle.cancel()

    # -- lifecycle ---------------------------------------------------------
    def shutdown(self) -> None:
        """Stop accepting results and cancel in-flight work."""
        self._closed = True
        self._cancel_tasks()


class SessionTab(TabRuntime, QWidget):
    """A session page: a ``QWidget`` with the shared tab plumbing."""
