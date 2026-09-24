# Qwen runtime compatibility matrix (locked)

**Track:** `qwen_multiengine_20260920` · **Tasks:** 0.3 (matrix) and 7.3 (release validation)
**Input:** [`packaging/qwen-runtime-requirements.json`](../../packaging/qwen-runtime-requirements.json)
**Probe:** [`scripts/spike/qwen_runtime_probe.py`](../../scripts/spike/qwen_runtime_probe.py)
**Release smoke:** [`scripts/qwen_release_smoke.py`](../../scripts/qwen_release_smoke.py)
**Workflow:** [`qwen-runtime-smoke.yml`](../../.github/workflows/qwen-runtime-smoke.yml)

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
`accelerate==1.12.0`, `soundfile==0.14.0`, `sox==1.4.1`, `soxr==1.1.0`.

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
- **`sox` is required, and is pinned to the last wheel-bearing release.**
  `qwen-tts` imports `sox` at module load
  (`qwen_tts/core/tokenizer_25hz/vq/speech_vq.py`), so a runtime without
  pysox fails *every* profile load with `No module named 'sox'` — the host
  never gets as far as reading a checkpoint. pysox 1.5.0 (the version an
  unpinned `sox` resolves to) publishes only an sdist and the runtime
  installer is wheel-only, which is how the module went missing; the closure
  therefore pins `sox==1.4.1`, whose `py2.py3-none-any` wheel installs on
  every matrix cell. The `sox` *binary* is only needed by the 25Hz xvector
  clone path (never taken by the shipped 12Hz CustomVoice/Base checkpoints);
  a future profile that takes it would need the binary shipped per platform.
- **`onnxruntime`** is metadata-only for the 0.6B safetensors checkpoints but
  stays pinned as part of the closure.
- **A batched `generate_*` NaNs unequal-length items in the SDK's default mode.**
  With `non_streaming_mode=True` (the SDK default) `qwen_tts` 0.1.1 left-pads
  the batched prompts and the sampler fails with ``probability tensor contains
  either inf, nan or element < 0``. Measured on the real 0.6B CustomVoice
  checkpoint (`mps`, float32, sdpa): two unequal segments fail with the default
  and return both wavs with the flag off, equal-length items pass either way,
  and `do_sample=False` does not help. The host therefore passes
  `non_streaming_mode=False` for multi-segment batches only
  (`workers/qwen_host.py::_generate`), leaving the one-segment interactive path
  on the SDK default. `scripts/qwen_release_smoke.py` does not yet exercise a
  multi-segment job — see `VieNeuTTSApp-04jq`.
- **Batch memory is bounded by total characters, not just segment count.** A
  batch generates all of its segments at once — every prompt, KV cache and
  codec activation live together, in this matrix's float32 on MPS. A
  4 × 2000-character batch exhausted a 16 GB Mac mini: the kernel's memory
  manager SIGKILLed the host 25 s into the generate, which reads upstream as a
  bare "closed its output stream" EOF with nothing on stderr (kernel log:
  `memorystatus: killing largest compressed process python3.13 … 22699 MB`).
  `MAX_BATCH_CHARS` (= `MAX_TEXT_CHARS`) in `core/qwen_protocol.py` now caps
  the total text per `synthesize_batch` frame, keeping every batch inside what
  the interactive single-segment path already proves fits;
  `QwenEngineProvider.infer_stream_segments` splits a job's segments across
  batch jobs accordingly. The parent's closed-stream error also names the
  host's exit status now, so a signal death (SIGKILL = the memory manager) is
  self-diagnosing instead of a mystery.
- **The host's machine footprint is governed across jobs** (the char cap above
  bounds one job; this bounds the process that runs hundreds of them):
  - the host releases the MPS/CUDA allocator caches after every settled job
    (`workers/qwen_host.py::serve` → `_release_accelerator`), so a long
    export's resident footprint tracks the working set instead of ratcheting;
  - the parent samples the host's RSS at job boundaries
    (`core/qwen_engine.py::QwenEngine._recycle_if_bloated`) and recycles it —
    clean shutdown, lazy respawn, seconds of model reload — when growth over
    the post-load baseline crosses `RSS_RECYCLE_GROWTH_BYTES` (1.5 GiB), which
    makes the kernel's mid-job SIGKILL unreachable in practice;
  - the batch bounds also scale down with physical RAM
    (`batch_bounds_for_ram`: ≥ 16 GiB full 4/2000, ≥ 8 GiB 2/1024, below that
    one segment) because the protocol ceiling is only proven on the 16 GB
    machine;
  - the host lowers its own CPU priority (`os.nice(5)` /
    `BELOW_NORMAL_PRIORITY_CLASS`), the in-process inference worker runs its
    QThread at `LowPriority`, and generation never starves the UI on an idle
    machine while yielding to it under load.

Wheel availability for `torch`/`torchaudio` 2.8.0 was verified for every
platform × Python-tag combination in the matrix (sources listed in the JSON
under `wheelAvailabilityVerified.sources`). macOS arm64 has no `+cpu` local
variant: the PyPI `macosx_11_0_arm64` wheel is the MPS-capable build.

### Promotion gates: how a broken closure is caught before the user meets it

`QwenRuntimeManager` promotes an install only after two gates:

1. **Archive verification** — every wheel matches the pinned manifest (size and
   SHA-256) while downloading and again as it is extracted, and the promoted
   tree must report the metadata it was installed with.
2. **Import verification** — the model host itself imports the stack a load
   needs (`workers/qwen_host.py::check_runtime_imports`, run through
   `core/qwen_engine.py::host_check_command`). That is a short-lived process
   started exactly like a real load — same interpreter, same sanitized
   environment, runtime on `sys.path` — so the app never imports torch into its
   own process. A closure that installs but cannot import (a missing module, a
   native library built for another interpreter) is rejected with the missing
   module named, the promotion is rolled back, and the previous install is
   kept. A fresh install that fails the gate leaves no active runtime.

The `sox` defect above is exactly what gate 2 exists for: every archive matched
the manifest, so only an import could prove the closure incomplete.

When a load fails *after* an install (files removed under the runtime, a
manifest that no longer matches the closure), the host reports the error code
`runtime_incomplete` (`core/qwen_protocol.py::RUNTIME_INCOMPLETE_CODE`) instead
of the generic `load_failed`, with a message that names the missing module and
the one action that fixes it (Repair, in Settings). The app keys recovery off
that code — never off message text — so the runtime card flips to `failed` with
the same reason and the profile view disables synthesis with "open Settings",
while a model or device failure leaves the runtime card untouched.

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
   **Measured 2026-09-24** on a `linux-x64-cpu` host with the pinned
   `qwen-tts==0.1.1`, `torch==2.8.0+cpu` and the pinned
   `Qwen3-TTS-12Hz-0.6B-CustomVoice` revision: `generate_custom_voice(instruct=…)`
   renders **byte-identical** audio to the same call without it, because
   `qwen_tts/inference/qwen3_tts_model.py:799` nulls `instruct` whenever
   `tts_model_size == "0b6"` — the pinned config's own value. Feeding those
   tokens past the gate does change the render, but a nonsense instruction
   changes it too, and across two seeds a "read slowly" versus "speak as fast as
   you can" pair is not separable from the no-instruction spread (6.4 s between
   the pair against 9.0 s within one instruction) — i.e. a prompt shift, not
   control. Qwen's own model table lists instruction control for the 1.7B
   checkpoints only. Product decision (unchanged, now evidenced): no style
   control on 0.6B — the Text tab hides its VieNeu emotion-tag chips on Qwen
   profiles and states the reason in their place
   (`engine_profiles.supports_emotion_tags` → `EngineState.expressivenessNote`).
3. Confirm no incremental audio is exposed before the final array (expected:
   `incrementalAudio: false` → app-level segmentation stays mandatory).
4. Confirm cancellation cannot interrupt a live `generate_*` call (expected:
   `interruptible: false` → the host must terminate and lazily restart).
5. Record CPU TTFR/RTF per platform so Settings can warn before download.
## 7. Release validation (Task 7.3)

The probe above answers "does this runtime behave as the lock says?" in a
throwaway environment. The release smoke answers the other question: "does the
SHIPPED app produce real 48 kHz speech with it?" It runs the app's own model
host (`core/qwen_engine.py` → `workers/qwen_host.py`) against the app's own
managed installs, and it is **opt-in** — ordinary CI never downloads a model and
stays on the deterministic fake host
([`tests/smoke/test_e2e_flows.py`](../../tests/smoke/test_e2e_flows.py), Task
7.2).

### What a run proves

1. **Verified packs install.** Both packs are imported through the offline
   installers (`QwenRuntimeManager.install_from_offline_pack`,
   `QwenModelManager.install_offline_pack`), so every wheel and weight file is
   checked against the pinned manifest before anything loads. A pack that is not
   the locked artifact fails there, not in the middle of inference.
2. **Real audio at the app's rate.** One bounded segment streams from the host,
   is written as a float WAV, and is checked by
   [`scripts/check_smoke_wav.py`](../../scripts/check_smoke_wav.py) with
   `--expect-rate 48000` — the resample from the model's native 24 kHz is part
   of what is under test, and silence is rejected (RMS/peak floors).
3. **Cancellation** stops the job (`cancelled` terminal, latency recorded). A
   host that ignores the request is terminated; the next job must then lazily
   start a clean one, and the corpse must be reaped.
4. **Crash recovery.** The host is killed mid-job; the next job must recover.
5. **Shutdown** leaves no child behind.

The result is ONE JSON object (`"kind": "qwen-release-smoke"`) recording:
platform, profile/device/dtype/attention, pack install identities (platform key,
model revision, bytes), the host-reported capabilities, `ttfrSeconds`,
`totalSeconds`, `audioSeconds`, `rtf`, `peakRssBytes` (sampled from the host
process; `ps` on POSIX, `GetProcessMemoryInfo` on Windows), the WAV stats,
`cancellation.latencySeconds`, `restart.recoverySeconds` and `shutdown`. A
non-empty `problems` list means the run failed.

### Running it

```bash
python scripts/qwen_release_smoke.py \
  --profile customvoice --device cpu --speaker Ryan --language zh \
  --packs build/qwen-packs --out build/qwen-smoke \
  --json-out build/qwen-smoke/customvoice-cpu.json

python scripts/qwen_release_smoke.py \
  --profile base --device mps \
  --ref-audio build/spike/ref.wav --ref-text "<transcript of the clip>" ...
```

`--packs` holds `runtime/` (the wheel files of the cell's pinned manifest, for
the interpreter you run) and `models/` (the model root tree the app installs
into: `customvoice/`, `base/`, `shared/`). Build them once per cell, publish
them, and point `.github/workflows/qwen-runtime-smoke.yml` at them with
`packs_url`; the workflow additionally needs `qwen-ref.wav` (a 3-8 s clip whose
transcript matches the `ref_text` input) for the Base profile. `qwen-models.zip`
is shared by every cell.

Provisioning is a deliberate operator action, not CI: download the manifest's
wheel URLs into `runtime/`, and fetch each pinned repository revision into
`models/<profile_key>/` plus the shared files into `models/shared/`, then verify
the tree by importing it once (step 1 above does exactly that).

### Runner requirements

| Cell | Runner |
|------|--------|
| `windows-x64-cpu` | `windows-latest` |
| `linux-x64-cpu` | `ubuntu-22.04` |
| `macos-arm64-cpu`, `macos-arm64-mps` | `macos-latest` (Apple Silicon) |
| `windows-x64-cuda`, `linux-x64-cuda` | **self-hosted** runners labelled `cuda` — GitHub-hosted runners have no GPU |

The dispatch input `cells` selects the cells to run (default: the three CPU
cells), so a dispatch never waits for a GPU runner that is not online. Each
cell uploads its metrics JSON and WAVs as the `qwen-runtime-smoke-<cell>`
artifact, and the job summary tabulates TTFR, total time, RTF, peak RSS,
cancellation latency and restart time.

### Relationship to the evidence table

A release-smoke run proves the app-level flows for a cell, but it does **not**
flip `evidence.status` in §5: that status is owned by the probe JSON schema
(§4), which records the runtime's own behavior (instructions, incremental audio,
interruptibility, native rate). Attach a cell's smoke JSON next to its probe
evidence once both exist for the same machine.
