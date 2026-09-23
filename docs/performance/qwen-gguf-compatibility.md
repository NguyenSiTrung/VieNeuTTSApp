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
(PyTorch host) and GGUF `Q8_0` / `Q4_K_M` (`qwentts.cpp` host). Vulkan, ROCm,
DirectML, Intel macOS, 1.7B checkpoints and VoiceDesign are out of scope.

## 1. Locked matrix

6 cells × 4 variants (2 profiles × 2 quantizations) = **24 combinations**.

| Cell | OS/arch | Device | ggml backend | Runner |
|------|---------|--------|--------------|--------|
| `windows-x64-cpu` | windows x86_64 | cpu | `CPU` | `windows-latest` |
| `windows-x64-cuda` | windows x86_64 | cuda | `CUDA0` | self-hosted `cuda` |
| `linux-x64-cpu` | linux x86_64 | cpu | `CPU` | `ubuntu-22.04` |
| `linux-x64-cuda` | linux x86_64 | cuda | `CUDA0` | self-hosted `cuda` |
| `macos-arm64-cpu` | macos arm64 | cpu | `CPU` | `macos-latest` |
| `macos-arm64-metal` | macos arm64 | metal | `Metal` | `macos-latest` |

Device selection is `GGML_BACKEND=<name>`; unset means auto — ggml picks the
best GPU backend, else CPU. There is no per-context device or thread parameter;
CPU threads are `hardware_concurrency()/2` inside the library. Bounding threads
further is a host-environment concern (Phase 3+).

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

All cells: `-DQWEN_SHARED=ON -DGGML_BACKEND_DL=ON -DGGML_CPU_ALL_VARIANTS=ON`.
CUDA cells add `-DGGML_CUDA=ON`; Metal cells add `-DGGML_METAL=ON`.
`GGML_BACKEND_DL=ON` is required so one pack can ship CPU + CUDA/Metal
dispatch without hard-linking a vendor SDK — see §3.

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

### Linux pack inventory (verified on x86_64)

```
libqwen.so                     (SONAME libqwen.so, 413 KB)
libggml.so.0 -> .0.23.0        (+ libggml.so dev symlink)
libggml-base.so.0 -> .0.23.0   (+ libggml-base.so dev symlink)
libggml-cpu-<isa>.so           x13: x64, sse42, sandybridge, ivybridge,
                               haswell, piledriver, zen4, skylakex,
                               cascadelake, cooperlake, icelake,
                               cannonlake, alderlake, sapphirerapids
```

Runtime deps beyond the pack: `libgomp.so.1` (OpenMP — needed by
`libggml-base` and every `libggml-cpu-*`; GCC runtime, must be shipped or
declared an OS floor), `libstdc++.so.6`, `libm.so.6`, `libgcc_s.so.1`,
`libc.so.6`. Windows/macOS equivalents are recorded per cell in the
requirements JSON as packs are built (Task 1.2).

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
| `macos-arm64-cpu` | pending | needs Apple Silicon |
| `macos-arm64-metal` | pending | needs Apple Silicon |

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
official engine or a GPU cell where available. Base is slower than CustomVoice
because the clone path pays prefill over the reference latents; the 2.4 s
ref-extraction is per-source, not per-synthesis.

All runs: streaming on (first-chunk 1 920 samples = 80 ms @ 24 kHz),
`language=auto`, seed unset, cancellation interruptible at ~0.2 ms latency,
clean `qt_free` shutdown, SHA-256-verified model files, no network access
during inference.

## 7. Probe commands

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

## 8. Outstanding before Phase 3 integration

1. Windows/macOS/CUDA cells need their hardware; `pending` until proven.
2. Windows DLL inventory + `libgomp`-equivalent check (`libgomp-1.dll` or
   static), macOS `.dylib` inventory — recorded per cell as packs build.
3. CUDA/Metal backend names beyond `CUDA0`/`Metal` selection (multi-GPU
   `CUDA1`…) — only `CUDA0` is in scope.
4. Confirm `instruct` on `custom_voice` is honoured (mode-valid there) and
   decide whether the UI exposes it — spec currently hides it.
