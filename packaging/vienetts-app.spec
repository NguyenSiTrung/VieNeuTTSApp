# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec — one-dir CPU build of the VieNeuTTS desktop app.

Layout contract: the app resolves QML and assets relative to its package
(``QML_DIR = Path(__file__).parent / "ui" / "qml"``, app.py), so the data
trees MUST land inside the frozen ``vienetts_app`` package at the same
relative layout — then no frozen-mode code paths are needed.

Collections:
- ``vieneu`` / ``vieneu_utils``: voice-catalog + sample data files and the
  lazily-imported engine submodules (``factory.py`` imports inside
  functions; ``collect_submodules`` makes them all reachable).
- ``sea_g2p``: Vietnamese/SEA G2P loaded lazily from
  ``vieneu_utils.phonemize_text`` — its Rust engine opens
  ``sea_g2p.bin`` (~60 MB dictionary) relative to ``__file__``, so the
  data MUST ship with the package or every infer dies with ENOENT.
- ``kaldi_native_fbank``: package ``lib/`` dylibs the compiled extension
  links against.
- torch/transformers are EXCLUDED on purpose — the CPU (ONNX int8) build
  is torch-free (NFR-1) and the local torch-free venv proves the CPU
  import graph never touches them.

Build (repo root):
    .venv/bin/pyinstaller packaging/vienetts-app.spec --noconfirm \
        --distpath dist --workpath /tmp/pyi-build

Output: ``dist/VieNeuTTS/`` everywhere, plus ``dist/VieNeuTTS.app`` on
macOS (BUNDLE). Smoke-test the binary with:
    dist/VieNeuTTS/VieNeuTTS[.exe] --smoke "Xin chào" -o smoke.wav
"""

import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_data_files, collect_submodules

REPO = Path(SPECPATH).parent
APP_NAME = "VieNeuTTS"

datas = [
    # (source, dest-inside-bundle) — keep the in-package layout app.py expects
    (str(REPO / "src/vienetts_app/ui/qml"), "vienetts_app/ui/qml"),
    (str(REPO / "src/vienetts_app/ui/assets"), "vienetts_app/ui/assets"),
]
binaries = []
hiddenimports = collect_submodules("vienetts_app")

for package in ("vieneu", "vieneu_utils", "sea_g2p", "kaldi_native_fbank"):
    pkg_datas, pkg_binaries, pkg_hidden = collect_all(package)
    datas += pkg_datas
    binaries += pkg_binaries
    hiddenimports += pkg_hidden
# Belt-and-braces: vieneu's own assets (voice catalogs) via the data hook too.
datas += collect_data_files("vieneu")

icon = None
if sys.platform == "darwin":
    icon = str(REPO / "src/vienetts_app/ui/assets/icon.icns")
elif sys.platform == "win32":
    icon = str(REPO / "src/vienetts_app/ui/assets/icon.ico")

a = Analysis(
    [str(REPO / "src/vienetts_app/__main__.py")],
    pathex=[str(REPO / "src")],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=["torch", "torchaudio", "transformers"],  # GPU-only extra (NFR-1)
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=APP_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,  # GUI app; --smoke still exits 0/1 for CI
    icon=icon,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name=APP_NAME,
)

if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name=f"{APP_NAME}.app",
        icon=icon,
        bundle_identifier="com.vienetts.app",
        info_plist={
            "CFBundleDisplayName": APP_NAME,
            "CFBundleShortVersionString": "0.1.0",
            "NSMicrophoneUsageDescription": (
                "Recording a 3-8s reference clip is required for voice cloning; "
                "audio never leaves the device."
            ),
        },
    )
