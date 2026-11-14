"""Architecture guards: layer boundaries that must not regress.

The exporters are documented as Qt-free and asyncua-free (``docs/architecture.md``).
A subprocess keeps its own import state, so this test observes exactly what
``import opcua_explorer.exporters`` pulls in.
"""

from __future__ import annotations

import subprocess
import sys

_PROBE = """
import sys
import opcua_explorer.exporters
loaded = [name for name in ("asyncua", "PySide6") if name in sys.modules]
print(",".join(loaded))
"""


def test_exporters_import_neither_asyncua_nor_qt() -> None:
    result = subprocess.run(
        [sys.executable, "-c", _PROBE],
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == ""
