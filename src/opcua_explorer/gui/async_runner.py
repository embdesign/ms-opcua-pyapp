"""Safe asyncio <-> Qt integration.

One asyncio event loop runs in a dedicated ``QThread``. All OPC UA work happens
there; the GUI thread only ever receives results through queued Qt signals.

``run_coroutine_threadsafe`` is deliberately not used for cancellation: the task
is tracked inside the loop and cancelled with ``loop.call_soon_threadsafe`` so we
never touch an asyncio object from the GUI thread.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import threading
from collections.abc import Awaitable, Callable
from typing import Any

from PySide6.QtCore import QObject, Signal

_logger = logging.getLogger(__name__)


class TaskHandle:
    """Handle for one submitted coroutine."""

    __slots__ = ("_runner", "key")

    def __init__(self, runner: AsyncRunner, key: str) -> None:
        self._runner = runner
        self.key = key

    def cancel(self) -> None:
        self._runner.cancel(self.key)

    @property
    def is_active(self) -> bool:
        return self._runner.is_active(self.key)

    def progress_callback(self) -> Callable[[Any], None]:
        """Thread-safe progress reporter to pass into service coroutines."""
        return self._runner.progress_callback(self.key)


class AsyncRunner(QObject):
    """Runs an asyncio loop in a worker thread and marshals results to Qt."""

    #: key, result
    done = Signal(str, object)
    #: key, error message
    failed = Signal(str, str)
    #: key
    cancelled = Signal(str)
    #: key, payload
    progress = Signal(str, object)

    CANCELLED_MARKER = "\x00cancelled"

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._tasks: dict[str, asyncio.Task[Any]] = {}
        self._callbacks: dict[str, tuple[Callable[[Any], None] | None, Callable[[str], None] | None, Callable[[Any], None] | None]] = {}
        self._listeners: dict[str, Callable[[Any], None]] = {}
        #: Submitted but not yet started on the loop thread.
        self._pending: set[str] = set()
        self._cancelled_before_start: set[str] = set()
        #: Guards the task/callback bookkeeping written on both threads.
        self._lock = threading.Lock()
        self._keys = itertools.count(1)

        # Queued automatically because these connections are made in the GUI thread
        # while the signals are emitted from the asyncio thread.
        self.done.connect(self._dispatch_done)
        self.failed.connect(self._dispatch_failed)
        self.progress.connect(self._dispatch_progress)

    # -- lifecycle ---------------------------------------------------------
    def start(self, timeout: float = 5.0) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._thread_main, name="opcua-asyncio", daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout):
            raise RuntimeError("The asyncio worker thread did not start in time.")

    def _thread_main(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        self._ready.set()
        try:
            loop.run_forever()
        finally:
            pending = asyncio.all_tasks(loop)
            for task in pending:
                task.cancel()
            if pending:
                loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            loop.run_until_complete(loop.shutdown_asyncgens())
            loop.close()
            self._loop = None

    def stop(self, timeout: float = 8.0) -> None:
        """Cancel all tasks, stop the loop and join the thread."""
        loop = self._loop
        if loop is None:
            return

        def _cancel_all() -> None:
            for task in list(self._tasks.values()):
                task.cancel()

        try:
            loop.call_soon_threadsafe(_cancel_all)
            loop.call_soon_threadsafe(loop.stop)
        except RuntimeError:  # pragma: no cover - loop already closed
            pass
        self._listeners.clear()
        with self._lock:
            self._callbacks.clear()
            self._pending.clear()
            self._cancelled_before_start.clear()
        if self._thread is not None:
            self._thread.join(timeout)
            self._thread = None

    # -- submission --------------------------------------------------------
    def new_key(self) -> str:
        return f"task-{next(self._keys)}"

    def run_until_complete(
        self,
        coro: Awaitable[Any] | Callable[[], Awaitable[Any]],
        timeout: float = 5.0,
    ) -> Any:
        """Run one coroutine to completion from a non-loop thread.

        Only intended for orderly shutdown (for example releasing a session), where
        the caller must know the operation finished before the loop stops.
        """
        loop = self._loop
        if loop is None or not loop.is_running():
            raise RuntimeError("The asyncio worker loop is not running.")
        if not asyncio.iscoroutine(coro) and not callable(coro):
            raise TypeError("run_until_complete() expects a coroutine or a callable returning one")

        async def runner() -> Any:
            return await (coro() if callable(coro) else coro)

        return asyncio.run_coroutine_threadsafe(runner(), loop).result(timeout)

    def submit(
        self,
        coro: Awaitable[Any] | Callable[[], Awaitable[Any]],
        *,
        key: str | None = None,
        on_done: Callable[[Any], None] | None = None,
        on_error: Callable[[str], None] | None = None,
        on_progress: Callable[[Any], None] | None = None,
    ) -> TaskHandle:
        """Schedule ``coro`` on the worker loop.

        ``coro`` may be a coroutine object or a zero-argument factory. A factory
        is preferred: it creates the coroutine inside the loop thread, so a
        failure to schedule can never leave an un-awaited coroutine behind.
        """
        loop = self._loop
        if loop is None or not loop.is_running():
            raise RuntimeError("The asyncio worker loop is not running.")

        task_key = key or self.new_key()
        with self._lock:
            self._callbacks[task_key] = (on_done, on_error, on_progress)
            self._pending.add(task_key)

        if asyncio.iscoroutine(coro):
            pending = coro

            async def factory() -> Any:
                return await pending

        elif callable(coro):
            factory = coro  # type: ignore[assignment]
        else:
            raise TypeError("submit() expects a coroutine or a callable returning one")

        loop.call_soon_threadsafe(lambda: loop.create_task(self._track(task_key, factory)))
        return TaskHandle(self, task_key)

    def cancel(self, key: str) -> None:
        loop = self._loop
        if loop is None:
            return

        def _cancel() -> None:
            with self._lock:
                task = self._tasks.get(key)
                # Only a task that has not started yet needs a marker. A cancel for a
                # finished task must not leave an entry behind for the window lifetime.
                if task is None and key in self._pending:
                    self._cancelled_before_start.add(key)
            if task is not None:
                task.cancel()

        loop.call_soon_threadsafe(_cancel)

    def is_active(self, key: str) -> bool:
        with self._lock:
            task = self._tasks.get(key)
        return task is not None and not task.done()

    def progress_callback(self, key: str) -> Callable[[Any], None]:
        """Return a thread-safe reporter emitting :attr:`progress` for ``key``."""

        def report(payload: Any) -> None:
            self.progress.emit(key, payload)

        return report

    def listen(self, key: str, on_progress: Callable[[Any], None]) -> None:
        """Register a progress consumer that outlives the submitting task.

        Streaming sources such as a live subscription keep emitting after the
        coroutine that created them returned, so their reporter cannot live in
        the per-task callback table (which is cleared when the task finishes).
        """
        self._listeners[key] = on_progress

    def unlisten(self, key: str) -> None:
        """Remove a listener registered with :meth:`listen`."""
        self._listeners.pop(key, None)

    # -- loop-thread plumbing ---------------------------------------------
    async def _track(self, key: str, factory: Callable[[], Awaitable[Any]]) -> None:
        with self._lock:
            self._pending.discard(key)
            cancelled_before_start = key in self._cancelled_before_start
            if cancelled_before_start:
                self._cancelled_before_start.discard(key)
                self._callbacks.pop(key, None)
        if cancelled_before_start:
            self.cancelled.emit(key)
            return

        task = asyncio.current_task()
        if task is not None:
            with self._lock:
                self._tasks[key] = task
        try:
            result = await factory()
        except asyncio.CancelledError:
            # A cancelled task never reports done/failed, so release its callbacks here;
            # otherwise the closures (and the widgets they reference) are retained until stop().
            with self._lock:
                self._callbacks.pop(key, None)
            self.cancelled.emit(key)
        except Exception as exc:
            _logger.debug("Async task %s failed", key, exc_info=True)
            self.failed.emit(key, _format_error(exc))
        else:
            self.done.emit(key, result)
        finally:
            with self._lock:
                self._tasks.pop(key, None)

    # -- GUI-thread dispatch ----------------------------------------------
    def _dispatch_done(self, key: str, result: Any) -> None:
        with self._lock:
            callbacks = self._callbacks.pop(key, None)
        if callbacks and callbacks[0] is not None:
            try:
                callbacks[0](result)
            except Exception:  # pragma: no cover - a slot bug must not kill the dispatcher
                _logger.exception("on_done callback for %s raised", key)

    def _dispatch_failed(self, key: str, message: str) -> None:
        with self._lock:
            callbacks = self._callbacks.pop(key, None)
        if callbacks and callbacks[1] is not None:
            try:
                callbacks[1](message)
            except Exception:  # pragma: no cover
                _logger.exception("on_error callback for %s raised", key)

    def _dispatch_progress(self, key: str, payload: Any) -> None:
        with self._lock:
            callbacks = self._callbacks.get(key)
        callback = callbacks[2] if callbacks else None
        if callback is None:
            callback = self._listeners.get(key)
        if callback is not None:
            try:
                callback(payload)
            except Exception:  # pragma: no cover
                _logger.exception("on_progress callback for %s raised", key)


def _format_error(exc: BaseException) -> str:
    message = str(exc).strip()
    if not message:
        message = type(exc).__name__
    return message
