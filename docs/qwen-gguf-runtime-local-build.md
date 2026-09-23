# Building a Qwen GGUF runtime pack locally

The app installs Qwen GGUF runtime packs in one click for the four
published cells:

| Cell | Status |
|---|---|
| `windows-x64-cpu` | Published — Install works |
| `linux-x64-cpu` | Published — Install works |
| `macos-arm64-cpu` | Published — Install works |
| `macos-arm64-metal` | Published — Install works |
| `windows-x64-cuda` | **Not published** — build locally (this guide) |
| `linux-x64-cuda` | **Not published** — build locally (this guide) |

The CUDA cells are unpublished because the project's CI has no GPU runner —
packs are only ever built and probed on real matching hardware, and nothing
unverified gets published. If you have an NVIDIA GPU you can build the pack
yourself and import it; the same pipeline CI runs — build → lock → render →
import — works locally.

## Who this is for

- You run the app **from a source checkout** (`git clone` + `uv pip install -e .`).
  The install manifest lives inside the installed package, so a packaged
  release (`.exe`, `.dmg`, `.zip`) cannot accept a locally built cell.
- Linux x64 **or** Windows x64 with an NVIDIA GPU whose driver supports
  CUDA 12.x (`nvidia-smi` reports `CUDA Version` ≥ 12.0).
- The same recipe works for any other pending cell on matching hardware.

## How it works

The app never loads a pack it cannot verify byte-for-byte. Your build
produces its own lock, so verification is exactly as strong as for the
published packs:

1. `scripts/build_qwen_gguf_runtime.py` compiles `qwentts.cpp` + ggml at the
   pinned commits and stages a flat pack dir (runtime lib + backend modules
   + licenses + `BUILD-INFO.json`).
2. `scripts/lock_qwen_gguf_runtime.py` records every file's size and
   SHA-256 into `packaging/qwen-gguf-pack-manifests.json`.
3. `scripts/render_qwen_gguf_runtime_manifests.py` writes the cell's install
   recipe into `src/vienetts_app/core/qwen_gguf_runtime_manifests.json` —
   the manifest the app's installer verifies against.
4. In the app, **Settings → Qwen → GGUF → Import offline runtime bundle**
   hashes your pack against that lock and promotes it.

## Prerequisites

### Linux (`linux-x64-cuda`)

- NVIDIA driver with CUDA 12.x support
- CUDA toolkit 12.x — `nvcc` must be on `PATH` (CMake finds it via
  `CUDAToolkit`)
- `cmake`, `gcc`/`g++`, `patchelf`, `git`, `python3`
- Debian/Ubuntu example:

  ```bash
  sudo apt install build-essential cmake patchelf git
  # CUDA toolkit from NVIDIA's repo, or: sudo apt install nvidia-cuda-toolkit
  ```

### Windows (`windows-x64-cuda`)

- NVIDIA driver with CUDA 12.x support
- CUDA toolkit 12.x — include the **Visual Studio integration** component
- Visual Studio 2022 with the *Desktop development with C++* workload
- `cmake` and `git` on `PATH`
- A normal PowerShell window works — CMake picks the Visual Studio
  generator itself, and the script already passes `-A x64`.

## Build → lock → render

Run from the repository root. `build/` is gitignored — use it for outputs.
The compile takes a few minutes; the upstream clone lands in
`build/qwen-gguf-src/qwentts.cpp` at the pinned commits.

### Linux

```bash
python3 scripts/build_qwen_gguf_runtime.py \
  --cell linux-x64-cuda \
  --clone-dir build/qwen-gguf-src \
  --out build/linux-x64-cuda

python3 scripts/lock_qwen_gguf_runtime.py \
  --pack build/linux-x64-cuda --cell linux-x64-cuda

python3 scripts/render_qwen_gguf_runtime_manifests.py \
  --packs-dir build --cells linux-x64-cuda
```

### Windows (PowerShell)

```powershell
python scripts\build_qwen_gguf_runtime.py `
  --cell windows-x64-cuda `
  --clone-dir build\qwen-gguf-src `
  --out build\windows-x64-cuda

python scripts\lock_qwen_gguf_runtime.py `
  --pack build\windows-x64-cuda --cell windows-x64-cuda

python scripts\render_qwen_gguf_runtime_manifests.py `
  --packs-dir build --cells windows-x64-cuda
```

`--packs-dir build` works because the pack lands at `build/<cell>` — the
layout the renderer expects.

Quick symbol smoke before touching the app — run **from inside the pack
dir** (ggml discovers backend modules via the process cwd):

```bash
cd build/linux-x64-cuda
python3 -c "import ctypes; l=ctypes.CDLL('./libqwen.so'); l.qt_version.restype=ctypes.c_char_p; print(l.qt_version())"
```

Windows: `ctypes.CDLL('qwen.dll')` from `build\windows-x64-cuda`. It should
print the pinned commit, e.g. `0cbde9b (2026-09-22)`.

## Import into the app

1. Launch the app → **Settings → Qwen** → choose the **GGUF** format.
2. The runtime card now lists **cuda** as an installable device (it appears
   because your rendered manifest entry exists).
3. Click **Import offline runtime bundle** and select `build/linux-x64-cuda`
   (or `build\windows-x64-cuda`).
4. The installer hashes every file against your lock and promotes the pack
   atomically — a mismatched or incomplete pack is refused.
5. Install the GGUF models through the model card as usual — those download
   from Hugging Face and are platform-independent.

The **Install** button fetches from the project's Pages host — your cell is
not published there, so it reports "no published runtime pack". Offline
import is the path for local builds.

## Optional: verify with the real-model probe

The probe runs an actual synthesis through the pack — the same evidence the
release gate requires. You need the talker + tokenizer GGUF files (pinned
names, sizes and digests are in the `variants` section of
`packaging/qwen-gguf-runtime-requirements.json` and in
`src/vienetts_app/core/qwen_gguf_model_manifests.json`; they download from
`Serveurperso/Qwen3-TTS-GGUF` on Hugging Face, same as the app's model
card):

```bash
cd build/linux-x64-cuda   # backend modules resolve via cwd
python3 <repo>/scripts/spike/qwen_gguf_probe.py \
  --profile customvoice --quantization Q4_K_M \
  --library ./libqwen.so \
  --talker <models>/qwen-talker-0.6b-customvoice-Q4_K_M.gguf \
  --tokenizer <models>/qwen-tokenizer-12hz-Q4_K_M.gguf \
  --speaker ryan --check-cancel \
  --cell linux-x64-cuda \
  --json-out <repo>/docs/performance/evidence/qwen-gguf-linux-x64-cuda-customvoice-Q4_K_M.json
```

A `pass` verdict means the pack is proven end-to-end on your hardware.

## Caveats

- **Build where it runs.** The pack is native code for your OS + arch +
  CUDA architecture — do not copy it to a different machine.
- **Pin bumps.** When `packaging/qwen-gguf-runtime-requirements.json` moves
  to a newer upstream commit, re-run the same three commands; the rendered
  entry supersedes the old one and re-import installs the new bytes.
- **Not the official pack.** Checksum-locked CI artifacts on the project's
  Pages host are the canonical releases — your local pack is for your own
  install.
- **License notices travel with the pack** (`licenses/` inside it):
  `qwentts.cpp` + ggml are MIT; the GGUF models are Apache-2.0; linux packs
  also bundle `libgomp` under the GCC Runtime Library Exception.
