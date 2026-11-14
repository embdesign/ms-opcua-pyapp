# packaging — standalone bundles

This directory turns the repository into archives that run **without Python**
and without `uv` on the target machine.

| File | Purpose |
|---|---|
| `opcua-explorer.spec` | windowed desktop bundle (`opcua-explorer`, `opcua-explorer.exe`) |
| `opcua-export.spec` | console bundle (`opcua-export`, `opcua-export.exe`) — Qt-free |
| `launchers/*.py` | tiny entry scripts PyInstaller analyses as plain scripts |
| `../.github/actions/build-bundles/action.yml` | shared build step used by the workflows |
| `../.github/workflows/package.yml` | build on push/PR, upload artefacts |
| `../.github/workflows/release.yml` | build on a `v*` tag, attach to the GitHub Release |

Nothing here is committed as a binary: builds are produced by GitHub Actions or
locally, and `dist/` and `build/` are already ignored by `.gitignore`.

`opcua-demo-server` is **not** bundled. It is a development aid that starts an
in-process demo server; use `uv run opcua-demo-server` from a checkout.

## Build locally

```bash
uv sync
uv run --with pyinstaller==6.22.3 pyinstaller --noconfirm --clean \
    --distpath dist/pyinstaller --workpath build/pyinstaller \
    packaging/opcua-explorer.spec

uv run --with pyinstaller==6.22.3 pyinstaller --noconfirm --clean \
    --distpath dist/pyinstaller --workpath build/pyinstaller \
    packaging/opcua-export.spec
```

Result:

```
dist/pyinstaller/opcua-explorer/   opcua-explorer[.exe] + _internal/
dist/pyinstaller/opcua-export/     opcua-export[.exe]   + _internal/
```

Measured Linux one-folder sizes (2026-11-14, Python 3.11, PySide6 6.12.0,
PyInstaller 6.22.3): **opcua-explorer 205 MB, opcua-export 53 MB**. Qt's own
shared libraries dominate the GUI bundle; the Python-level `excludes` in the
spec cannot remove them, but they do keep the exporter Qt-free.

A wheel (`uv build`) is **not** equivalent: it still needs Python plus a
dependency install on the target.

## Cross-building is not possible

A Windows `.exe` must be produced on Windows. CI covers both platforms:

* `ubuntu-22.04` — pinned on purpose. Bundles built there need glibc 2.35, which
  covers Debian 12, Ubuntu 22.04+ and RHEL/Rocky 9. Building on `ubuntu-latest`
  (24.04, glibc 2.39) would silently exclude Debian 12.
* `windows-latest` — a Windows Server x64 runner; the output runs on Windows
  10/11.

## What the bundle does *not* contain

* No credentials, certificates, trust store or project files. The trust store and
  settings are created per user on first start (`AppSettings`, `TrustStore`), in
  the same locations the `uv`-run build uses, so a bundled install and a source
  install on the same machine share their configuration.
* No Python. Nothing else is required on the target besides the Qt runtime
  libraries that ship inside the bundle — on Linux the usual X11/Wayland client
  libraries of the distribution are still needed.
* Optional XSD validation needs `lxml`, which a normal `uv sync` build includes.

Windows builds are unsigned, so SmartScreen warns on first launch. Signing needs
an Authenticode certificate kept outside this repository.

## Artefact names

```
opcua-explorer-windows-x64.zip   opcua-explorer-linux-x64.zip
opcua-export-windows-x64.zip     opcua-export-linux-x64.zip
```

Each archive contains the bundle folder (`opcua-explorer/` or `opcua-export/`);
unpack it and run the executable inside.

## Verifying a build

```bash
./dist/pyinstaller/opcua-export/opcua-export --version
./dist/pyinstaller/opcua-export/opcua-export --help

QT_QPA_PLATFORM=offscreen ./dist/pyinstaller/opcua-explorer/opcua-explorer &   # starts, then kill
```

The workflows run exactly these checks, plus a 15-second "the GUI did not exit
during start-up" check on Linux.
