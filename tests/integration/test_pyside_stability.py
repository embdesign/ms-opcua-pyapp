"""Guard against the PySide6 6.12.0 QTreeWidgetItem refcount regression.

The application browses an address space by creating and discarding thousands of
``QTreeWidgetItem`` rows. With PySide6 6.12.0 that aborts the whole process after a few
hundred rows:

    Fatal Python error: none_dealloc: deallocating None: bug likely caused by a refcount
    error in a C extension

The crash is a Shiboken regression, not application code: the script below uses nothing but
public Qt API, reproduces it within a second on 6.12.0 and passes on the pinned 6.11.3. It runs
in a subprocess on purpose - an in-process reproduction would abort pytest instead of failing
a test.
"""

from __future__ import annotations

import os
import subprocess
import sys

CHURN_SCRIPT = r"""
import sys
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import QApplication, QTreeWidget, QTreeWidgetItem

app = QApplication(["stability", "-platform", "offscreen"])
tree = QTreeWidget()
for round_index in range(1000):
    parent = QTreeWidgetItem(tree, [f"p{round_index}", "Object", f"ns=2;i={round_index}"])
    for child_index in range(20):
        child = QTreeWidgetItem(
            parent, [f"c{child_index}", "Variable", f"ns=2;i={round_index}{child_index}"]
        )
        font = child.font(0)
        font.setBold(child_index % 2 == 0)
        child.setFont(0, font)
        child.setForeground(0, QBrush(QColor("#0a66c2")) if child_index % 2 else QBrush())
        child.setToolTip(0, "tip")
    parent.takeChildren()
    tree.takeTopLevelItem(0)
print("survived")
"""


def test_tree_item_churn_does_not_abort_the_interpreter() -> None:
    environment = dict(os.environ)
    environment.setdefault("QT_QPA_PLATFORM", "offscreen")
    result = subprocess.run(  # noqa: S603 - fixed interpreter and inline script
        [sys.executable, "-c", CHURN_SCRIPT],
        capture_output=True,
        text=True,
        timeout=120,
        env=environment,
    )

    assert result.returncode == 0, (
        "QTreeWidgetItem churn aborted the interpreter (PySide6 refcount regression). "
        "Keep PySide6 pinned to the version in pyproject.toml.\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert "survived" in result.stdout
