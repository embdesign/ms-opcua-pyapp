"""AsyncRunner bookkeeping across the GUI/loop boundary."""

from __future__ import annotations

import asyncio
import time

from opcua_explorer.gui.async_runner import AsyncRunner


def _wait_for(qapp, predicate, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        qapp.processEvents()
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition was not reached in time")


def test_cancel_after_completion_leaves_no_stale_marker(qapp) -> None:
    """A cancel for a finished task must not be remembered for the window lifetime."""
    runner = AsyncRunner()
    runner.start()
    try:
        results: list[object] = []
        handle = runner.submit(asyncio.sleep(0), key="finished", on_done=results.append)
        _wait_for(qapp, lambda: bool(results))

        handle.cancel()
        runner.run_until_complete(asyncio.sleep(0.01))

        assert runner._cancelled_before_start == set()
        assert runner._pending == set()
    finally:
        runner.stop()


def test_progress_listener_is_removed_on_unlisten(qapp) -> None:
    runner = AsyncRunner()
    runner.start()
    try:
        seen: list[object] = []
        runner.listen("stream", seen.append)
        runner.progress.emit("stream", "payload")
        _wait_for(qapp, lambda: seen == ["payload"])

        runner.unlisten("stream")
        runner.progress.emit("stream", "ignored")
        runner.run_until_complete(asyncio.sleep(0.01))
        assert seen == ["payload"]
    finally:
        runner.stop()
