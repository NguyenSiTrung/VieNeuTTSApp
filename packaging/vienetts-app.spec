# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec — one-dir build of the VieNeuTTS desktop app.

TWO variants share this spec:

- CPU (default): torch-free, ONNX Runtime int8 only (NFR-1).
- CUDA (``VIENETTS_GPU_BUILD=1``): bundles the torch/torchaudio cu128 stack
  + transformers so users need no Python. ~2 GB larger; the Release
  workflow tags the artifact ``-cuda`` and core.updates matches it.

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
- torch/transformers are EXCLUDED in the CPU build on purpose and
  COLLECTED in the CUDA build (env switch below); the CPU import graph
  never touches them either way.

Build (repo root):
    .venv/bin/pyinstaller packaging/vienetts-app.spec --noconfirm \
        --distpath dist --workpath /tmp/pyi-build
    VIENETTS_GPU_BUILD=1 pyinstaller ...   # CUDA variant

Output: ``dist/VieNeuTTS/`` everywhere, plus ``dist/VieNeuTTS.app`` on
macOS (BUNDLE). Smoke-test the binary with:
    dist/VieNeuTTS/VieNeuTTS[.exe] --smoke "Xin chào" -o smoke.wav
"""

import os
import sys
import tempfile
from pathlib import Path
from PyInstaller.utils.hooks import collect_all, collect_data_files, collect_submodules

REPO = Path(SPECPATH).parent
APP_NAME = "VieNeuTTS"

# CUDA variant switch: the Release workflow sets VIENETTS_GPU_BUILD=1 after
# installing the cu128 wheels. A marker file ships inside the frozen package
# so core.updates can pick the right release asset at runtime (source
# checkouts never have the marker — they report the plain CPU key, which is
# correct: source users install torch themselves and don't consume zips).
GPU_BUILD = os.environ.get("VIENETTS_GPU_BUILD", "") == "1"

# Version stamp: release.yml exports VERSION from the git tag (v0.1.10 → 0.1.10).
# Writing it here — not into the source tree — keeps the working checkout
# clean while the frozen app reports the real tag (the update check compares
# this against the latest GitHub Release). Dev builds fall back to
# vienetts_app.__version__ via _version.get_version.
_version_stamp = Path(os.environ.get("VERSION", "") or "")
_stamp_version = _version_stamp.name if _version_stamp.name else ""
_stamp_path = REPO / "src" / "vienetts_app" / "_version.py"
_stamp_path.write_text(
    '"""Build-stamped version: release.yml writes this file from the git tag.\n'
    "\n"
    "Source checkouts have no stamp — ``get_version`` falls back to the\n"
    "committed ``__version__``. Generated file: never commit it.\n"
    '"""\n'
    "\n"
    "from __future__ import annotations\n"
    "\n"
    f'BUILD_VERSION = "{_stamp_version}"\n'
    "\n"
    "\n"
    "def get_version(package_fallback: str) -> str:\n"
    '    """Stamped version when the build wrote one, else the package fallback."""\n'
    "    stamped = BUILD_VERSION.strip()\n"
    "    return stamped if stamped else package_fallback\n",
    encoding="utf-8",
)

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
# App data files beyond the QML/assets trees above: the compiled i18n
# catalogs (ui/i18n/vienetts_en.qm) load via Path(__file__), so without
# this the English language setting silently no-ops in frozen builds.
datas += collect_data_files("vienetts_app")

# CUDA build: bundle the full torch stack + the transformers runtime the
# SDK's PyTorch engine imports, and ship the variant marker inside the
# package (empty file; core.updates only checks existence). Generated temp
# file — never a repo artifact.
if GPU_BUILD:
    _marker = Path(tempfile.mkstemp(prefix="_cuda_build_")[1])
    _marker.write_text("", encoding="utf-8")
    datas.append((str(_marker), "vienetts_app/_cuda_build"))
    for _gpu_pkg in ("torch", "torchaudio", "transformers", "safetensors"):
        _gpu_datas, _gpu_binaries, _gpu_hidden = collect_all(_gpu_pkg)
        datas += _gpu_datas
        binaries += _gpu_binaries
        hiddenimports += _gpu_hidden

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
    # GPU-only extra (NFR-1): the CPU build stays torch-free; the CUDA build
    # (GPU_BUILD) must NOT exclude them — they are collected above instead.
    excludes=[] if GPU_BUILD else ["torch", "torchaudio", "transformers"],
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
            "CFBundleShortVersionString": _stamp_version or "0.1.10",
            "NSMicrophoneUsageDescription": (
                "Recording a 3-8s reference clip is required for voice cloning; "
                "audio never leaves the device."
            ),
        },
    )
