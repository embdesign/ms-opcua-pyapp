# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec — headless ``opcua-compare`` bundle (one folder, console).

Build from the repository root:

    uv run --with pyinstaller==6.22.3 pyinstaller --noconfirm --clean \
        --distpath dist/pyinstaller --workpath build/pyinstaller \
        packaging/opcua-compare.spec

Qt is excluded entirely: ``opcua_explorer.cli_compare`` reuses the connection and
traversal helpers of ``cli.py``, calls ``OpcUaService.download()`` and
``compare_address_spaces()``, and never touches PySide6. Two saved JSON exports
can be compared without opening a session at all, which is the point of having
this bundle next to the GUI.

Optional XSD validation is not part of this command; ``lxml`` is only pulled in
when the environment provides it (it is a dev-group dependency).
"""

from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules, copy_metadata

ROOT = Path(SPECPATH).parent
NAME = "opcua-compare"

datas = collect_data_files("asyncua")
# importlib.metadata reads this at run time to report the application version;
# without it a frozen build would fall back to "0.0.0+unknown".
datas += copy_metadata("opcua-explorer")
hiddenimports = collect_submodules("asyncua")

EXCLUDES = ["tkinter", "PySide6"]

a = Analysis(
    [str(ROOT / "packaging" / "launchers" / "opcua_compare_main.py")],
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
