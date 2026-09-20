# Qwen runtime compatibility matrix (locked)

**Track:** `qwen_multiengine_20260920` · **Task:** 0.3 · **Date:** 2026-09-20
**Input:** [`packaging/qwen-runtime-requirements.json`](../../packaging/qwen-runtime-requirements.json)
**Probe:** [`scripts/spike/qwen_runtime_probe.py`](../../scripts/spike/qwen_runtime_probe.py)

This document locks the supported Qwen dependency and device matrix that
Phase 2 renders into checksum-pinned runtime manifests. It records what is
already proven and, explicitly, which real-device runs are still outstanding.
The matrix may not be silently reduced: dropping a platform requires a spec
revision first (Task 0.3 exit condition).

## 1. Locked matrix

| Platform | Device | Dtype | Attention | Index | torch / torchaudio |
|----------|--------|-------|-----------|-------|--------------------|
| `windows-x64-cpu` | cpu | float32 | sdpa | `torchCpu` | 2.8.0+cpu |
| `windows-x64-cuda` | cuda | bfloat16 | sdpa | `torchCu128` | 2.8.0+cu128 |
| `linux-x64-cpu` | cpu | float32 | sdpa | `torchCpu` | 2.8.0+cpu |
| `linux-x64-cuda` | cuda | bfloat16 | sdpa | `torchCu128` | 2.8.0+cu128 |
| `macos-arm64-cpu` | cpu | float32 | sdpa | `pypi` | 2.8.0 |
| `macos-arm64-mps` | mps | float32 | sdpa | `pypi` | 2.8.0 |

Python tags: `cp310`, `cp311`, `cp312`, `cp313` (`>=3.10,<3.14`), matching the
app's supported range. FlashAttention 2 is **not** part of the locked matrix —
it remains an optional CUDA optimization that the host may select when the
import succeeds; SDPA/eager must always work without it.

Common pins (all platforms): `qwen-tts==0.1.1`, `transformers==4.57.3`,
`accelerate==1.12.0`, `soundfile==0.14.0`, `soxr==1.1.0`.

## 2. Dependency resolution facts

`qwen-tts==0.1.1` (latest on PyPI, `requires_python>=3.9`) declares:

```
transformers==4.57.3, accelerate==1.12.0, gradio, librosa, torchaudio,
soundfile, sox, onnxruntime, einops
```

Consequences recorded in the requirements JSON:

- **Hard conflict with the base app.** The app pins `transformers==4.57.6`;
  `qwen-tts` pins `4.57.3`. Process isolation (model host) is the only
  supported resolution — the base environment is never modified.
- **Full-closure policy.** `qwen-tts` pulls `gradio`, `librosa`, `onnxruntime`
  and `sox` although inference needs none of them. Installing a hand-picked
  subset risks import-time failures inside the runtime, so the lock script
  resolves the declared closure and pins every wheel.
- **`sox` risk.** The `sox` distribution wraps the system `sox` binary. The
  model host must not require it at import or inference time; this is verified
  against the real runtime in Task 3.2. If a used code path needs the binary,
  the runtime manifest must ship it per platform.
- **`onnxruntime`** is metadata-only for the 0.6B safetensors checkpoints but
  stays pinned as part of the closure.

Wheel availability for `torch`/`torchaudio` 2.8.0 was verified for every
platform × Python-tag combination in the matrix (sources listed in the JSON
under `wheelAvailabilityVerified.sources`). macOS arm64 has no `+cpu` local
variant: the PyPI `macosx_11_0_arm64` wheel is the MPS-capable build.

## 3. Pinned model artifacts

| Profile | Repo | Revision | Bytes |
|---------|------|----------|-------|
| CustomVoice | `Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice` | `85e237c12c027371202489a0ec509ded67b5e4b5` | 2 498 388 392 |
| Base | `Qwen/Qwen3-TTS-12Hz-0.6B-Base` | `5d83992436eae1d760afd27aff78a71d676296fc` | 2 516 106 051 |

Both repos were read through the Hugging Face API with `?blobs=true`, which
also yields per-file sizes for the Task 2.3 manifests. 683 056 438 bytes are
**shared** between the profiles (text tokenizer + `speech_tokenizer/`), so an
install of both needs ~4.3 GB, not ~5.0 GB; the model manager must reuse
verified shared files instead of storing two copies.

## 4. Probe commands

Run each platform's two commands from the repository root inside the isolated
runtime environment, then commit the JSON evidence under
`docs/performance/evidence/`:

```bash
# linux-x64-cpu / linux-x64-cuda
.venv-qwen/bin/python scripts/spike/qwen_runtime_probe.py \
  --profile customvoice --model-dir "$QWEN_CUSTOMVOICE_DIR" --speaker Ryan \
  --check-instructions --check-cancel \
  --json-out docs/performance/evidence/qwen-linux-x64-cpu-customvoice.json
.venv-qwen/bin/python scripts/spike/qwen_runtime_probe.py \
  --profile base --model-dir "$QWEN_BASE_DIR" \
  --ref-audio build/spike/ref.wav --ref-text "<transcript>" \
  --json-out docs/performance/evidence/qwen-linux-x64-cpu-base.json
```

Windows and macOS variants are recorded verbatim per platform in
`packaging/qwen-runtime-requirements.json` (`platforms[].evidence.probeCommand`).

Each result records: package revisions, resolved device/dtype/attention,
model-reported languages/speakers with their source, native sample rate, TTFR,
total time, RTF, peak RSS/VRAM, whether a non-empty `instruct` changes the
audio, whether an incremental/stream API exists, cancellation terminal and
latency, and clean shutdown.

## 5. Evidence status

| Platform | Status | Evidence |
|----------|--------|----------|
| `windows-x64-cpu` | **pending** | needs a Windows x64 machine |
| `windows-x64-cuda` | **pending** | needs a Windows x64 + NVIDIA CUDA machine |
| `linux-x64-cpu` | **pending** | needs a Linux x64 machine (this audit host is Linux arm64 — off-matrix) |
| `linux-x64-cuda` | **pending** | needs a Linux x64 + NVIDIA CUDA machine |
| `macos-arm64-cpu` | **pending** | needs Apple Silicon |
| `macos-arm64-mps` | **pending** | needs Apple Silicon |

Flipping a platform to `verified` requires committing its probe JSON and
updating `evidence.status`; `tests/unit/test_qwen_runtime_requirements.py`
then enforces that the file exists, matches the probe schema, has no errors,
produced audio, and shut down cleanly.

Verified **without** a Qwen-capable device (audit host: Linux arm64, no CUDA):

- wheel availability for every matrix cell (PyTorch indexes + PyPI JSON API),
- `qwen-tts==0.1.1` dependency closure (PyPI JSON API),
- pinned model revisions, file inventory and byte sizes (HF API),
- the probe's own contract: 15 unit tests covering CLI validation, JSON schema,
  capability reporting, instruction effect, cancellation and shutdown.

## 6. Outstanding work before Phase 3 integration

1. Run the two probe commands on each matrix platform and commit the evidence.
2. Confirm the 0.6B `instruct` behavior on the real runtime (expected: accepted
   but byte-identical output → the UI must not expose a style control).
3. Confirm no incremental audio is exposed before the final array (expected:
   `incrementalAudio: false` → app-level segmentation stays mandatory).
4. Confirm cancellation cannot interrupt a live `generate_*` call (expected:
   `interruptible: false` → the host must terminate and lazily restart).
5. Record CPU TTFR/RTF per platform so Settings can warn before download.