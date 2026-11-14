# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec — headless ``opcua-export`` bundle (one folder, console).

Build from the repository root:

    uv run --with pyinstaller==6.22.3 pyinstaller --noconfirm --clean \
        --distpath dist/pyinstaller --workpath build/pyinstaller \
        packaging/opcua-export.spec

Qt is excluded entirely: ``opcua_explorer.cli`` is a second front end over
``OpcUaService`` and ``export_nodeset2``, not a second implementation, and it
never touches PySide6. That is the point of having this bundle — an address
space can be exported on a machine with no display server.

Optional XSD validation uses ``lxml``. It is a dev-group dependency of this
project, so a normal ``uv sync`` build includes it; a build without ``lxml``
still exports and simply reports that schema validation was skipped.
"""

from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ROOT = Path(SPECPATH).parent
NAME = "opcua-export"

datas = collect_data_files("asyncua")
hiddenimports = collect_submodules("asyncua")

EXCLUDES = ["tkinter", "PySide6"]

a = Analysis(
    [str(ROOT / "packaging" / "launchers" / "opcua_export_main.py")],
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
    console=True,
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
