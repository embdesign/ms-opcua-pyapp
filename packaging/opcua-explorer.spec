# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec — OPC UA Explorer desktop bundle (one folder).

Build from the repository root:

    uv run --with pyinstaller==6.22.3 pyinstaller --noconfirm --clean \
        --distpath dist/pyinstaller --workpath build/pyinstaller \
        packaging/opcua-explorer.spec

Result: ``dist/pyinstaller/opcua-explorer/`` with the windowed
``opcua-explorer`` launcher. The headless exporter is a separate spec
(``opcua-export.spec``) so that it can be built without Qt.

``asyncua`` is collected wholesale: its crypto, transport and client submodules
are imported indirectly at run time, so static analysis alone would miss the
ones needed for secure connections.

The application ships no asset files (the window icon is painted at run time),
so there is nothing else to embed.
"""

from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules, copy_metadata

ROOT = Path(SPECPATH).parent
NAME = "opcua-explorer"

datas = collect_data_files("asyncua")
# importlib.metadata reads this at run time to report the application version;
# without it a frozen build would fall back to "0.0.0+unknown".
datas += copy_metadata("opcua-explorer")
hiddenimports = collect_submodules("asyncua")

# Qt modules this application never imports; only QtCore, QtGui and QtWidgets
# are used. The excludes cannot remove Qt's own shared libraries that arrive as
# binary dependencies (libQt6Quick, libQt6Qml, libQt6Pdf), but they do keep
# unused Python bindings out. Measured Linux one-folder size: 205 MB
# (2026-11-14); the Qt-free exporter bundle is 53 MB.
EXCLUDES = [
    "tkinter",
    "PySide6.QtWebEngineCore",
    "PySide6.QtWebEngineWidgets",
    "PySide6.QtWebEngineQuick",
    "PySide6.QtQuick",
    "PySide6.QtQuickWidgets",
    "PySide6.QtQml",
    "PySide6.Qt3DCore",
    "PySide6.Qt3DRender",
    "PySide6.QtMultimedia",
    "PySide6.QtCharts",
    "PySide6.QtDataVisualization",
    "PySide6.QtPdf",
    "PySide6.QtSql",
    "PySide6.QtTest",
]

a = Analysis(
    [str(ROOT / "packaging" / "launchers" / "opcua_explorer_main.py")],
    pathex=[str(ROOT / "src")],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=EXCLUDES,
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name=NAME,
)
