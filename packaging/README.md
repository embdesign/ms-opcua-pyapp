# packaging — standalone bundles

This directory turns the repository into archives that run **without Python**
and without `uv` on the target machine.

| File | Purpose |
|---|---|
| `opcua-explorer.spec` | windowed desktop bundle (`opcua-explorer`, `opcua-explorer.exe`) |
| `opcua-export.spec` | console bundle (`opcua-export`, `opcua-export.exe`) — Qt-free |
| `opcua-compare.spec` | console bundle (`opcua-compare`, `opcua-compare.exe`) — Qt-free |
| `launchers/*.py` | tiny entry scripts PyInstaller analyses as plain scripts |
| `../.github/actions/build-bundles/action.yml` | shared build step used by the workflows |
| `../.github/workflows/package.yml` | build on push/PR, upload artefacts |
| `../.github/workflows/release.yml` | build on a `v*` tag, attach to the GitHub Release |

Nothing here is committed as a binary: builds are produced by GitHub Actions or
locally, and `dist/` and `build/` are already ignored by `.gitignore`.

`opcua-demo-server` is **not** bundled. It is a development aid that starts an
in-process demo server; use `uv run opcua-demo-server` from a checkout.

`opcua-compare` **is** bundled (console, Qt-free): it can compare two saved JSON
exports on a machine with no display server and no Python.

## Build locally

```bash
uv sync
uv run --with pyinstaller==6.22.3 pyinstaller --noconfirm --clean \
    --distpath dist/pyinstaller --workpath build/pyinstaller \
    packaging/opcua-explorer.spec

uv run --with pyinstaller==6.22.3 pyinstaller --noconfirm --clean \
    --distpath dist/pyinstaller --workpath build/pyinstaller \
    packaging/opcua-export.spec

uv run --with pyinstaller==6.22.3 pyinstaller --noconfirm --clean \
    --distpath dist/pyinstaller --workpath build/pyinstaller \
    packaging/opcua-compare.spec
```

Result:

```
dist/pyinstaller/opcua-explorer/   opcua-explorer[.exe] + _internal/
dist/pyinstaller/opcua-export/     opcua-export[.exe]   + _internal/
dist/pyinstaller/opcua-compare/    opcua-compare[.exe]  + _internal/
```

Measured Linux one-folder sizes (2026-11-14, Python 3.11, PySide6 6.12.0,
PyInstaller 6.22.3): **opcua-explorer 205 MB, opcua-export 53 MB,
opcua-compare 53 MB**. Qt's own shared libraries dominate the GUI bundle; the
Python-level `excludes` in the spec cannot remove them, but they do keep both
command-line bundles Qt-free.

A wheel (`uv build`) is **not** equivalent: it still needs Python plus a
dependency install on the target.

## Cross-building is not possible

A Windows `.exe` must be produced on Windows. CI covers both platforms:

* `ubuntu-22.04` — pinned on purpose. Bundles built there need glibc 2.35, which
  covers Debian 12, Ubuntu 22.04+ and RHEL/Rocky 9. Building on `ubuntu-latest`
  (24.04, glibc 2.39) would silently exclude Debian 12.
* `windows-latest` — a Windows Server x64 runner; the output runs on Windows
  10/11.

## Linux runtime libraries

The bundle carries Qt but **not** the system graphics stack and **not** a GL
driver. Qt6Gui links `libEGL.so.1` and `libGL.so.1` directly — the offscreen
platform plugin included — so even a headless start fails without them:

```
ImportError: libEGL.so.1: cannot open shared object file: No such file or directory
```

Every desktop installation already has these. On a minimal or server system:

```bash
# Debian / Ubuntu
sudo apt-get install -y --no-install-recommends \
  libegl1 libgl1 libglib2.0-0 libfontconfig1 libfreetype6 libdbus-1-3 \
  libx11-6 libx11-xcb1 libxcb1 libxcb-cursor0 libxcb-icccm4 libxcb-image0 \
  libxcb-keysyms1 libxcb-randr0 libxcb-render-util0 libxcb-shape0 \
  libxcb-xinerama0 libxcb-xkb1 libxkbcommon0 libxkbcommon-x11-0 libxi6 \
  libxrender1 libsm6 libice6

# RHEL / Fedora
sudo dnf install -y mesa-libEGL mesa-libGL libxkbcommon libxkbcommon-x11 \
  fontconfig freetype dbus-libs glib2 libX11 libxcb xcb-util-cursor \
  xcb-util-wm xcb-util-image xcb-util-keysyms xcb-util-renderutil \
  libXi libXrender libSM libICE
```

CI installs these *after* the build on purpose: bundling the GL/EGL stack would
pin one vendor dispatch layout and break on machines with a different driver.
`libxcb-cursor0` matters in practice — Qt 6.5+ refuses to start on X11 without
it and Ubuntu 22.04 does not pull it in by default.

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
opcua-compare-windows-x64.zip    opcua-compare-linux-x64.zip
```

Each archive contains the bundle folder (`opcua-explorer/`, `opcua-export/` or
`opcua-compare/`); unpack it and run the executable inside.

## Verifying a build

```bash
./dist/pyinstaller/opcua-export/opcua-export --version
./dist/pyinstaller/opcua-export/opcua-export --help

./dist/pyinstaller/opcua-compare/opcua-compare --version
./dist/pyinstaller/opcua-compare/opcua-compare --help
# offline smoke test: the same document against itself must compare equal (exit 0)
./dist/pyinstaller/opcua-compare/opcua-compare \
    --reference-file examples/demo-address-space.json \
    --candidate-file examples/demo-address-space.json --quiet > /dev/null

QT_QPA_PLATFORM=offscreen ./dist/pyinstaller/opcua-explorer/opcua-explorer &   # starts, then kill
```

The workflows run exactly these checks, plus a 15-second "the GUI did not exit
during start-up" check on Linux.
