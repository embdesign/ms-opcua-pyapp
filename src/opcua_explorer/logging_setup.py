"""Logging setup with mandatory secret redaction.

Requirement: passwords and private keys must never be logged - not even when a
library or a traceback embeds them in a message. Every handler installed by this
module runs :class:`SecretRedactor` over the *formatted* message and over
formatted exception text, so lazily formatted ``%s`` arguments cannot slip
through either.
"""

from __future__ import annotations

import contextlib
import logging
import re
import sys
import threading
import traceback
from collections import deque
from collections.abc import Callable, Iterable

REDACTED = "***REDACTED***"

_PATTERNS: tuple[re.Pattern[str], ...] = (
    # key=value / key: value / "key": "value"
    re.compile(
        r"(?i)\b(password|passwd|pwd|secret|token|private[_\- ]?key)\b[\"']?\s*[=:]\s*"
        r"(\"[^\"]*\"|'[^']*'|[^\s,;&)\]}]+)"
    ),
    # PEM private key blocks (never log them, not even truncated)
    re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----", re.DOTALL),
)


class SecretRedactor:
    """Replaces known secret values and secret-looking patterns in text."""

    def __init__(self) -> None:
        self._secrets: set[str] = set()
        self._lock = threading.Lock()

    def register(self, secret: str | None) -> None:
        """Register a concrete secret value (password, key password, token)."""
        if not secret:
            return
        with self._lock:
            self._secrets.add(secret)

    def redact(self, text: str) -> str:
        if not text:
            return text
        with self._lock:
            secrets = sorted(self._secrets, key=len, reverse=True)
        result = text
        for secret in secrets:
            if len(secret) >= 4:
                result = result.replace(secret, REDACTED)
            else:
                # Very short secrets would destroy the log; match them as words.
                result = re.sub(rf"\b{re.escape(secret)}\b", REDACTED, result)
        for pattern in _PATTERNS:
            result = pattern.sub(lambda m: f"{m.group(1)}={REDACTED}" if m.lastindex and m.lastindex >= 2 else REDACTED, result)
        return result


class _RedactionFilter(logging.Filter):
    """Formats the record, redacts it, then re-attaches the sanitised result."""

    def __init__(self, redactor: SecretRedactor) -> None:
        super().__init__()
        self._redactor = redactor

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # pragma: no cover - defensive
            message = str(record.msg)
        record.msg = self._redactor.redact(message)
        record.args = ()

        if record.exc_info is not None:
            text = "".join(traceback.format_exception(*record.exc_info))
            record.exc_text = self._redactor.redact(text)
            record.exc_info = None
        elif record.exc_text:
            record.exc_text = self._redactor.redact(record.exc_text)
        return True


class LogBus(logging.Handler):
    """In-memory log ring buffer with thread-safe subscriber callbacks.

    The GUI subscribes and forwards entries to the Qt thread through a queued
    signal; core code never imports Qt.
    """

    def __init__(self, capacity: int = 5000) -> None:
        super().__init__()
        self._records: deque[str] = deque(maxlen=capacity)
        self._subscribers: list[Callable[[int, str, str], None]] = []
        self._lock = threading.Lock()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = self.format(record)
        except Exception:  # pragma: no cover - a broken formatter must not kill logging
            self.handleError(record)
            return
        line = f"{record.levelname:<8} {message}"
        with self._lock:
            self._records.append(line)
            subscribers = list(self._subscribers)
        for callback in subscribers:
            with contextlib.suppress(Exception):  # subscriber errors are not fatal
                callback(record.levelno, record.levelname, message)

    def subscribe(self, callback: Callable[[int, str, str], None]) -> Callable[[], None]:
        with self._lock:
            self._subscribers.append(callback)

        def unsubscribe() -> None:
            with self._lock:
                if callback in self._subscribers:
                    self._subscribers.remove(callback)

        return unsubscribe

    def snapshot(self) -> list[str]:
        with self._lock:
            return list(self._records)

    def clear(self) -> None:
        with self._lock:
            self._records.clear()


REDACTOR = SecretRedactor()
LOG_BUS = LogBus()
_FILTER = _RedactionFilter(REDACTOR)


def register_secret(value: str | None) -> None:
    """Register a value that must never appear in any log record."""
    REDACTOR.register(value)


def setup_logging(level: str | int = "INFO", *, quiet_libraries: Iterable[str] = ("asyncua",)) -> LogBus:
    """Configure root logging once and return the in-memory bus."""
    root = logging.getLogger()
    try:
        root.setLevel(level if isinstance(level, int) else getattr(logging, str(level).upper(), logging.INFO))
    except (TypeError, ValueError):
        root.setLevel(logging.INFO)

    def _already_installed(handler: logging.Handler) -> bool:
        return getattr(handler, "_opcua_explorer_handler", False)

    if not any(_already_installed(handler) for handler in root.handlers):
        formatter = logging.Formatter(
            fmt="%(asctime)s %(name)s: %(message)s",
            datefmt="%H:%M:%S",
        )
        stream = logging.StreamHandler(stream=sys.stderr)
        stream.setFormatter(formatter)
        stream.addFilter(_FILTER)
        stream._opcua_explorer_handler = True  # type: ignore[attr-defined]
        root.addHandler(stream)

        LOG_BUS.setFormatter(logging.Formatter(fmt="%(name)s: %(message)s"))
        LOG_BUS.addFilter(_FILTER)
        LOG_BUS._opcua_explorer_handler = True  # type: ignore[attr-defined]
        root.addHandler(LOG_BUS)

    # Protocol libraries are noisy at INFO and may contain endpoint details.
    for name in quiet_libraries:
        logging.getLogger(name).setLevel(logging.WARNING)

    return LOG_BUS


def get_log_bus() -> LogBus:
    return LOG_BUS
