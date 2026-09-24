# Qwen GGUF (qwentts.cpp) compatibility matrix

**Track:** `qwen_gguf_engine_20260923` · **Tasks:** 1.1 (contract audit + probe) and 1.2 (native packs)
**Input:** [`packaging/qwen-gguf-runtime-requirements.json`](../../packaging/qwen-gguf-runtime-requirements.json)
**Probe:** [`scripts/spike/qwen_gguf_probe.py`](../../scripts/spike/qwen_gguf_probe.py)

This document locks the supported `qwentts.cpp` GGUF engine matrix — upstream
commits, ABI version, model file identities, per-platform backends — and records
what the real-model probe has proven on each cell. The matrix may not be
silently reduced: dropping a platform or variant requires a spec revision.

GGUF is a **format variant of the existing Qwen profiles**, not new profiles:
`qwen_base_0_6b` and `qwen_custom_0_6b` each offer *Official full weights*
(PyTorch host) and GGUF `Q8_0` / `Q4_K_M` (`qwentts.cpp` host). **GGUF is the
app's default format for the Qwen family** (`models.Settings.qwen_model_format`
— the managed native pack is a fraction of the full checkpoint's runtime plus
weights footprint); the official weights stay selectable, and the Settings
picker states their cost (more RAM/VRAM, more disk and download time, slower
than GGUF). Vulkan, ROCm, DirectML, Intel macOS, 1.7B checkpoints and
VoiceDesign are out of scope.

## 1. Locked matrix

6 cells × 4 variants (2 profiles × 2 quantizations) = **24 combinations**.

| Cell | OS/arch | Device | ggml backend | Runner |
|------|---------|--------|--------------|--------|
| `windows-x64-cpu` | windows x86_64 | cpu | `CPU` | `windows-latest` |
| `windows-x64-cuda` | windows x86_64 | cuda | `CUDA0` | self-hosted `cuda` |
| `linux-x64-cpu` | linux x86_64 | cpu | `CPU` | `ubuntu-22.04` |
| `linux-x64-cuda` | linux x86_64 | cuda | `CUDA0` | self-hosted `cuda` |
| `macos-arm64-cpu` | macos arm64 | cpu | `CPU` | `macos-latest` |
| `macos-arm64-metal` | macos arm64 | metal | `MTL0` | `macos-latest` |

Device selection is `GGML_BACKEND=<name>`; unset means auto — ggml picks the
best GPU backend, else CPU. ggml names Metal devices `MTL<N>`
(`ggml-metal-device.m`), so Apple Silicon resolves to `MTL0` — the literal
string `Metal` is not a device name and `GGML_BACKEND=Metal` fails to init
(observed on arm64; fixed in `GGUF_DEVICE_BACKENDS`/`DEVICE_ENV`). There is no
per-context device or thread parameter; CPU threads are
`hardware_concurrency()/2` inside the library. Bounding threads further is a
host-environment concern (Phase 3+).

## 2. Upstream pins

| Input | Value |
|-------|-------|
| Runtime repo | `ServeurpersoCom/qwentts.cpp` |
| Commit | `0cbde9b5d21aa2142efa5d62e14b97f197ac6f6d` (2026-09-22) |
| ggml submodule | `ServeurpersoCom/ggml` @ `0af0d7d5f66a6976b259b292cb4e7dc60457aa45` (ggml 0.23.0) — a **fork**, not upstream ggml; both revs are pinned |
| ABI header | `src/qwen.h` — `QT_ABI_VERSION 5`, `QT_ABI_MIN_VERSION 5` |
| Shared lib | `-DQWEN_SHARED=ON` → `libqwen.so` / `qwen.dll` / `libqwen.dylib` |
| Model repo | `Serveurperso/Qwen3-TTS-GGUF` @ `b7ee2e8c7459c3bea99da23e3d178125a7d1713c` |
| Licenses | runtime MIT; models Apache-2.0 (Qwen3-TTS upstream) |

Only `QT_API` symbols are exported; `pipeline_*`/`backend_*` internals stay
hidden. The static `qwen-core` target is for the bundled tools, not ABI
consumers. `qt_synthesize` emits mono float32 PCM at **24 kHz**; the app
resamples to 48 kHz with the existing `StreamingResampler`.

### Build flags per cell

All cells: `-DQWEN_SHARED=ON -DGGML_BACKEND_DL=ON -DGGML_NATIVE=OFF
-DGGML_CPU_ALL_VARIANTS=ON`. CUDA cells add `-DGGML_CUDA=ON`; Metal cells add
`-DGGML_METAL=ON`; the macOS CPU cell adds `-DGGML_METAL=OFF` (ggml auto-enables
Metal on arm64, so an unflagged "cpu" build still emits `libggml-metal.so` and
the variant .so staging picks it up).

`GGML_BACKEND_DL=ON` is required so one pack can ship CPU + CUDA/Metal
dispatch without hard-linking a vendor SDK — see §3.

`GGML_NATIVE=OFF` is **required**, not cosmetic: ggml only compiles the
per-variant feature probe (`ggml-cpu-*-feats` → `ggml_backend_score`) in the
`GGML_CPU_ALL_VARIANTS` + `!GGML_NATIVE` branch. With `GGML_NATIVE=ON` — the
default on Apple Silicon — the `libggml-cpu-*` modules still build but export
no score function, so `ggml_backend_load_best("cpu")` scores nothing and the
CPU backend never registers (`GGML_BACKEND=CPU` → `backend_init failed`;
observed on arm64 before the flag was pinned).

## 3. Backend discovery contract (verified)

With `GGML_BACKEND_DL=ON`, `qt_init` calls `ggml_backend_load_all()`, which
searches, in order:

1. compile-time `GGML_BACKEND_DIR`,
2. the **host executable's** directory,
3. the process **cwd**.

It does **not** search the directory containing `libqwen.so`. Loading the
library from an arbitrary process fails with
`backend_init failed (no GGML backend available)` — observed on
linux-x64-cpu before the cwd fix.

**Contract:** the GGUF host subprocess must be spawned with `cwd=<pack dir>`
so the ISA-scored `libggml-cpu-*` modules resolve. (`GGML_BACKEND_PATH` can
name one explicit backend file, but forfeits CPU-variant dispatch.) The parent
owns this at spawn time.

### Linux pack inventory (verified on x86_64, `packaging/qwen-gguf-pack-manifests.json`)

```
libqwen.so                     (SONAME libqwen.so, 413 KB)
libggml.so.0 -> .0.23.0        (+ libggml.so dev symlink)
libggml-base.so.0 -> .0.23.0   (+ libggml-base.so dev symlink)
libggml-cpu-<isa>.so           x13: x64, sse42, sandybridge, ivybridge,
                               haswell, piledriver, zen4, skylakex,
                               cascadelake, cooperlake, icelake,
                               cannonlake, alderlake, sapphirerapids
libgomp.so.1 -> .1.0.0         (bundled OpenMP runtime, GCC Runtime
                               Library Exception; notice in licenses/)
licenses/                      qwentts.cpp MIT, ggml MIT, gcc runtime
BUILD-INFO.json                cell, commits, ABI, flags, floor
```

Two relocation details are handled at stage time by
`scripts/build_qwen_gguf_runtime.py`:

- `libqwen.so` is built with an **absolute build-tree RUNPATH**; staging
  rewrites it to `$ORIGIN` via `patchelf` (`@loader_path` on macOS, module-dir
  search is native on Windows). Verified: `ldd` resolves every ggml lib and
  `libgomp.so.1` from the pack directory regardless of cwd.
- `libgomp.so.1` is NEEDED by `libggml-base` and every `libggml-cpu-*`, so it
  is **bundled** rather than floored — a minimal target OS has no GCC runtime.

Remaining OS-floor deps (not bundled): `libstdc++.so.6`, `libm.so.6`,
`libgcc_s.so.1`, `libc.so.6`. **The glibc floor is the build host's
toolchain**, not a promise: CI builds on `ubuntu-22.04` (→ glibc 2.35);
`BUILD-INFO.json` records the floor measured from the pack binaries
(`measuredGlibcFloor`, e.g. 2.38 for a 24.04-built pack). Windows/macOS
inventories are recorded per cell as their packs build.

### macOS pack inventory (verified on arm64, Apple M4)

```
libqwen.dylib                  (~1.3 MB)
libggml.dylib -> .0 -> .0.23.0 (+ libggml.0.dylib chain, 4 symlinks)
libggml-base.dylib -> .0 -> .0.23.0
libggml-cpu-apple_<g>.so       x3: m1, m2_m3, m4 — ISA-scored variants;
                               apple_m4 wins on this host
libggml-blas.so                Accelerate framework backend
libggml-metal.so               metal cell only
licenses/                      qwentts.cpp MIT, ggml MIT
BUILD-INFO.json                cell, commits, ABI, flags, floor
```

macOS differs from Linux in two ways:

- Backend modules are CMake MODULE libraries — `libggml-*.so`, **not**
  `.dylib` — while the shared libs (`libqwen`, `libggml`, `libggml-base`) are
  versioned `.dylib` with the usual `libX.dylib → libX.0.dylib →
  libX.0.23.0.dylib` chain. There is no generic `libggml-cpu` module; only the
  per-variant files exist.
- Nothing is bundled: every dependency resolves to an OS framework —
  Foundation, Metal/MetalKit (metal cell), CoreFoundation, Accelerate — or
  system `libc++.1`/`libSystem`/`libobjc`. The floor is
  `CMAKE_OSX_DEPLOYMENT_TARGET=13.0` (macOS 13 Ventura), not a library
  version.

Relocation: staging adds `@loader_path` to each **real** `.dylib`/`.so`
(`install_name_tool -add_rpath`); patching a symlink resolves to the same file
and fails on the duplicate `LC_RPATH`, so the script skips symlinks. A stale
build-tree rpath may remain in module LC_RPATH lists — it is inert on other
hosts because `@loader_path` is searched first and the path does not exist.

## 4. ABI and mode contract (from `src/qwen.h` audit)

- Status codes: `0` ok · `-1` invalid_params · `-2` mode_invalid ·
  `-3` generate_failed · `-4` oom · `-5` cancelled.
- `on_chunk` fires on the library's **internal compute worker thread**, never
  the caller; samples are borrowed — copy before return; returning `false`
  requests cancellation. First flush is one 12.5 Hz frame; chunk width doubles
  up to 8 frames; EOS/`max_new` flush the remainder.
- `qt_cancel_cb` is polled during prompt processing **and** at every Talker
  decode step (~83 ms at 12.5 Hz). Verified: a cancel requested 300 ms into a
  call still inside prefill ended with `cancelled` and 0 chunks; abort latency
  0.2 ms once polled.
- Mode rules enforced by `qt_init`/`qt_synthesize`:
  - `custom_voice` without `speaker` → `MODE_INVALID`; a `speaker` on base →
    `MODE_INVALID`; `speaker` + ref input → `INVALID_PARAMS`.
  - `instruct` on base → `MODE_INVALID`; `ref_text` without ref audio/latents →
    `INVALID_PARAMS`; ref input on non-base → `MODE_INVALID`.
  - Speaker names are lowercase in the GGUF table; the prompt builder
    lowercases input, so `Ryan`/`Uncle_Fu` resolve to `ryan`/`uncle_fu`.
- Model metadata (languages, speakers, model_type, codebook count) is read from
  `qwen3-tts.*` GGUF keys at load — the app must take its capability lists from
  `qt_n_languages`/`qt_n_speakers`, never a hardcoded table.
- `qt_extract_voice_ref` returns `ref_spk_emb` (1024-dim float) + RVQ latents
  (`refT` frames × 16 codebooks); extraction is a one-time per-source cost
  (2.43 s measured for a 17.3 s clip on CPU) and its tensors are reusable
  across `qt_synthesize` calls — the app caches them per clone source.

## 5. Pinned model artifacts

All from `Serveurperso/Qwen3-TTS-GGUF` @ `b7ee2e8c`, verified by ranged GGUF
header reads and full-file SHA-256:

| Variant | File | Bytes | SHA-256 |
|---------|------|-------|---------|
| base Q8_0 | `qwen-talker-0.6b-base-Q8_0.gguf` | 992 615 488 | `d54dbaf1…f226426` |
| base Q4_K_M | `qwen-talker-0.6b-base-Q4_K_M.gguf` | 628 905 056 | `4b468ec7…af04226` |
| customvoice Q8_0 | `qwen-talker-0.6b-customvoice-Q8_0.gguf` | 968 588 544 | `4eb38675…588519` |
| customvoice Q4_K_M | `qwen-talker-0.6b-customvoice-Q4_K_M.gguf` | 604 878 080 | `b3a7e661…868e1e` |
| tokenizer Q8_0 | `qwen-tokenizer-12hz-Q8_0.gguf` | 291 150 624 | `1883beee…dbfd94` |
| tokenizer Q4_K_M | `qwen-tokenizer-12hz-Q4_K_M.gguf` | 254 974 752 | `cf3788b4…658039` |

Pairing rule: talker `qwen3-tts.model_type` must equal the variant's profile
and `general.file_type` must equal its quantization; the tokenizer carries
`qwen3-tts-tokenizer` + `file_type` but no `model_type`, so pairing is by
quantization. GGUF headers: talkers report arch `qwen3-tts`, model size
`0b6`, 16 code groups; `custom_voice` lists 12 language rows (10 languages +
`beijing_dialect`, `sichuan_dialect`) and 9 speakers; `base` lists **10**
language rows (no dialect entries) and no speakers — confirmed by ABI
enumeration, not just headers.

## 6. Evidence status

| Cell | Status | Evidence |
|------|--------|----------|
| `linux-x64-cpu` | **verified** | 4 probe JSONs under `docs/performance/evidence/` |
| `windows-x64-cpu` | pending | needs Windows x64 |
| `windows-x64-cuda` | pending | needs Windows x64 + NVIDIA GPU |
| `linux-x64-cuda` | pending | needs NVIDIA GPU (none on this host) |
| `macos-arm64-cpu` | pending | 1/4 probes pass (customvoice Q4_K_M, Apple M4); base + Q8_0 remain |
| `macos-arm64-metal` | pending | 1/4 probes pass (customvoice Q4_K_M, Apple M4); base + Q8_0 remain |

A cell flips to `verified` only when all four variant probes pass and their
JSONs are committed; `tests/unit/test_qwen_gguf_probe.py` enforces file
existence, schema version, `verdict == "pass"`, and full variant coverage.

### linux-x64-cpu measurements (this audit host, CPU backend)

| Variant | load ms | ref-extract ms | TTFA ms | total ms | audio s | RTF | peak RSS MB | chunks |
|---------|--------:|---------------:|--------:|---------:|--------:|----:|------------:|-------:|
| customvoice Q4_K_M | 1 238 | — | 346 | 13 055 | 5.12 | 2.55 | 2 641 | 11 |
| base Q4_K_M | 1 272 | 2 434 | 19 802 | 34 620 | 5.12 | 6.76 | 3 500 | 11 |
| customvoice Q8_0 | 1 412 | — | 354 | 10 832 | 4.08 | 2.66 | 3 379 | 9 |
| base Q8_0 | 1 381 | 2 589 | 20 165 | 34 001 | 4.80 | 7.08 | 4 284 | 11 |

CPU RTF > 1 means synthesis is slower than realtime on this host — Settings
must carry a throughput warning for CPU cells, and the UI should prefer the
official engine or a GPU cell where available. (GGUF is the app's default
format, so a CPU-only Linux machine starts on this slower pack: the CPU
throughput notice is the mitigation, and the official weights stay one click
away with their own cost stated.) Base is slower than CustomVoice
because the clone path pays prefill over the reference latents; the 2.4 s
ref-extraction is per-source, not per-synthesis.

All runs: streaming on (first-chunk 1 920 samples = 80 ms @ 24 kHz),
`language=auto`, seed unset, cancellation interruptible at ~0.2 ms latency,
clean `qt_free` shutdown, SHA-256-verified model files, no network access
during inference.

### macos-arm64 measurements (Apple M4, macOS 26.6)

| Cell | Variant | load ms | TTFA ms | total ms | audio s | RTF | peak RSS MB | chunks |
|------|---------|--------:|--------:|---------:|--------:|----:|------------:|-------:|
| metal (MTL0) | customvoice Q4_K_M | 993 | 659 | 4 219 | 8.88 | 0.475 | 2 678 | 16 |
| cpu | customvoice Q4_K_M | 1 826 | 143 | 4 824 | 6.00 | 0.804 | 2 727 | 12 |

Both cells: streaming on, `language=english`, speaker `ryan`, cancellation
interruptible (0.0–0.1 ms once polled), clean `qt_free`, model digests match
the pinned SHAs. RTF < 1 — unlike linux-x64 CPU — because Apple Silicon's
i8mm/dotprod kernels keep up; Metal is ~2× CPU on this host.

## 7. Release validation (production path, Task 6.2)

The §6 probe evidence proves the library works; the release gate proves the
**app's own path** works: managed offline install of the verified packs, the
isolated `qwen_gguf_host` subprocess, streaming, cancellation, crash recovery
and shutdown. Runner: [`scripts/qwen_gguf_release_smoke.py`](../../scripts/qwen_gguf_release_smoke.py);
workflow: [`.github/workflows/qwen-gguf-runtime-smoke.yml`](../../.github/workflows/qwen-gguf-runtime-smoke.yml)
(`workflow_dispatch` only — ordinary CI stays on the deterministic fake host).

```bash
python scripts/qwen_gguf_release_smoke.py \
  --profile customvoice --quantization Q8_0 --device cpu --speaker Ryan \
  --packs <packs> --out <out> \
  --json-out <out>/<cell>-customvoice-Q8_0.json
# base needs the clone pair:
#   --profile base --quantization Q4_K_M --ref-audio ref.wav --ref-text "…"
```

`--packs` holds `runtime/` (the cell's pack dir or archive — `linux-x64-cpu`
etc.) and `models/` (the model root: `customvoice-Q8_0/`, `customvoice-Q4_K_M/`,
`base-Q8_0/`, `base-Q4_K_M/`, `shared/`). The runner stages a per-variant view
of the model root and feeds both packs to the app's own installers
(`install_from_offline_pack` / `install_offline`) — checksum-verified against
the shipped manifests, so a tampered or mismatched pack fails before anything
loads. Nothing is downloaded; `HF_HUB_OFFLINE`/`TRANSFORMERS_OFFLINE` are
exported regardless.

Per run it proves, all through `QwenGgufEngine`:

- promoted **identities** equal the manifest pins (runtime cell identity,
  model identity, shared-codec identity) — a cell with no published pack is
  *blocked*, never silently skipped;
- the **device evidence** the host logs (`loaded` event's device/backend)
  matches the requested device — no silent CPU fallback;
- one bounded segment → 48 kHz WAV, gated by `check_smoke_wav.py
  --expect-rate 48000` **and** a finite-sample check (a native buffer bug can
  mint a constant-NaN stream that silence-checking alone would pass);
- an overlong segment is refused before IPC (`MAX_TEXT_CHARS`);
- a second job streams on the resident host (repeated jobs);
- a backend the pack does not ship is refused with a structured, non-fatal
  error, and non-native spellings (`mps`, `tpu`) never reach a spawn;
- cancellation settles the job and the next job works — whether the host
  survived (settled natively) or was terminated and lazily restarted;
- a killed host is restarted by the next job; `close()` reaps the child.

The JSON report (`schemaVersion 1`, `kind: qwen-gguf-release-smoke`) carries
`install`, `identities`, `deviceEvidence`, `capabilities`, `synthesis` (TTFR /
total / RTF / peak RSS), `wav`, `segmentation`, `backendRefusal`,
`repeatedJob`, `cancellation`, `restart`, `shutdown`, and `problems`. A report
can never exit 0 with a `problems` entry, a skipped lifecycle check, or a
missing section — `--no-check-cancel`/`--no-check-restart` exist for debugging
but their skips are recorded as findings, so partial runs cannot pass.

The workflow plans the locked §1 cells through a `plan` job (job-level `if:`
cannot read `matrix`), then each cell job loops **both profiles × both
quantizations** (4 runs per cell → 24 per full dispatch), uploads the JSON +
WAV artifacts, and tabulates TTFR / total / RTF / peak RSS / cancel / restart /
findings into the step summary. A cell flips §6 to `verified` for the release
gate only when all four of its variant reports exist with empty `problems`.

## 8. Probe commands

```bash
# run from the pack dir so backend modules resolve (§3)
cd <pack dir>
python <repo>/scripts/spike/qwen_gguf_probe.py \
  --profile customvoice --quantization Q4_K_M \
  --library ./libqwen.so \
  --talker <models>/qwen-talker-0.6b-customvoice-Q4_K_M.gguf \
  --tokenizer <models>/qwen-tokenizer-12hz-Q4_K_M.gguf \
  --speaker ryan --check-cancel --cell linux-x64-cpu \
  --json-out docs/performance/evidence/qwen-gguf-linux-x64-cpu-customvoice-Q4_K_M.json

python <repo>/scripts/spike/qwen_gguf_probe.py \
  --profile base --quantization Q4_K_M \
  --library ./libqwen.so \
  --talker <models>/qwen-talker-0.6b-base-Q4_K_M.gguf \
  --tokenizer <models>/qwen-tokenizer-12hz-Q4_K_M.gguf \
  --ref-audio <clip>.wav --ref-text "<transcript>" \
  --check-cancel --cell linux-x64-cpu \
  --json-out docs/performance/evidence/qwen-gguf-linux-x64-cpu-base-Q4_K_M.json
```

The upstream repo ships `examples/freeman.wav` (22.05 kHz mono, 17.3 s) with a
matching `freeman.txt` transcript — the canonical Base reference. Base probes
require `--ref-audio` + `--ref-text`; CustomVoice probes require `--speaker`.
The probe never logs source text or reference audio content — only byte counts
and digests.

Each JSON records: ABI version/minimum, runtime version string, model file
identities (bytes + SHA-256) and GGUF metadata, requested/resolved backend,
model-reported languages/speakers/codebook count, native sample rate, stream
chunk stats, load/ref-extract/TTFA/total times, audio duration, RTF, peak RSS,
cancellation terminal + latency, shutdown cleanliness, and an error list.

## 9. Outstanding before Phase 3 integration

1. Windows/CUDA cells still need their hardware; `pending` until proven.
   macOS cells: base profile + Q8_0 quantizations remain to probe (3 more
   talker+tokenizer pairs to download); the customvoice Q4_K_M leg of each is
   proven.
2. Windows DLL inventory + `libgomp`-equivalent check (`libgomp-1.dll` or
   static). macOS `.dylib`/`.so` inventory is now verified (§3).
3. CUDA/Metal backend names beyond `CUDA0`/`MTL0` selection (multi-GPU
   `CUDA1`, `MTL1`…) — only the first device is in scope.
4. Confirm `instruct` on `custom_voice` is honoured (mode-valid there) and
   decide whether the UI exposes it — spec currently hides it.
