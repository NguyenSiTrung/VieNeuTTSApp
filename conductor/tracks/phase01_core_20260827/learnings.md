# Track Learnings: phase01_core_20260827

Patterns, gotchas, and context discovered during implementation.

## Codebase Patterns (Inherited)

No patterns inherited — this is the first track and `conductor/patterns.md`
is still empty. Discoveries from this track will seed it (especially: vieneu
SDK API realities vs the documented contract, and per-OS install quirks).

---

<!-- Learnings from implementation will be appended below -->

## [2026-08-27] - Phase 0 Tasks 7-8: cross-OS gaps + report finalized
- **Implemented:** ubuntu docker validation script (committed, not runnable locally — docker network broken); spike report finalized with §0 API contract.
- **Files changed:** scripts/spike/phase0_ubuntu_docker.sh, docs/spike-report.md
- **Commits:** 7be29a1, e7fa754
- **Learnings:**
  - Gotchas: Docker Desktop image pulls can hang with no error output — check `docker images` vs `docker ps` to distinguish pull-hang from container-run; kill early rather than waiting.
  - Context: Phase 0 verdict — all SDK assumptions held EXCEPT: codec repo needed (86MB, +plan estimate), fp32 subfolder is `onnx_update/` not `onnx/`, long-workload RSS breach (u5c), PDF dep swapped to pypdf.
---


## [2026-08-27] - Phase 0 Task 5: offline bundling validation
- **Implemented:** `scripts/spike/phase0_offline.py` (3 strategies), `scripts/fetch_models.py` (minimal download + SHA256 manifest + verify); report §6.
- **Files changed:** scripts/spike/phase0_offline.py, scripts/fetch_models.py, docs/spike-report.md
- **Commit:** 864c978
- **Learnings:**
  - Patterns: recommended bundle = portable `HF_HOME` + `HF_HUB_OFFLINE=1` (self-contained, works with zero network).
  - Gotchas: codec repo (`MOSS-Audio-Tokenizer-Nano-ONNX`) is NOT dir-loadable via public API — always resolved through HF cache; `onnx_dir` only covers backbone graphs.
  - Gotchas: fp32 ONNX graphs live in `onnx_update/` subfolder (NOT `onnx/` as plan §3 said); precision→subfolder map = {"int8": "onnx_int8", "fp32": "onnx_update"}.
  - Context: `snapshot_download(..., local_dir=..., allow_patterns=[...])` writes `.cache/huggingface` metadata inside the bundle — exclude from manifests.
---


## [2026-08-27] - Phase 0 Task 4: performance & memory measurements
- **Implemented:** `scripts/spike/phase0_perf.py`, `scripts/spike/phase0_rss_current.py`; spike report §5 filled.
- **Files changed:** scripts/spike/phase0_perf.py, scripts/spike/phase0_rss_current.py, docs/spike-report.md
- **Commit:** 98fa39e
- **Learnings:**
  - Patterns: measure BOTH `ru_maxrss` (peak, monotonic) and current RSS via `ps -o rss= -p PID` — peak alone can't distinguish leak vs arena.
  - Gotchas: ONNX arena grows with largest workload and never returns memory to OS — long synthesis → ~2.5GB steady on M4 (§18 <2GB breach for long workloads only; interactive use 766MB). Follow-up: VieNeuTTSApp-u5c.
  - Context: cold start 5.17s, warm short 0.13s, long RTF 0.14 on M4 — all other §18 budgets pass with wide margins.
---


## [2026-08-27] - Phase 0 Task 3: infer_stream chunk format + latency
- **Implemented:** `scripts/spike/phase0_stream.py`; spike report §4 filled.
- **Files changed:** scripts/spike/phase0_stream.py, docs/spike-report.md
- **Commit:** efad783
- **Learnings:**
  - Patterns: stream chunks = float32 1-D mono, variable 15360..96000 samples; concatenate for full audio.
  - Patterns: first-chunk 0.153s / RTF 0.13 on M4 — §18 budgets pass with big margin.
  - Gotchas: SDK 3.3.0 streaming is NOT ONNX-only (both engines expose `infer_stream`) — plan §3 corrected in report.
  - Gotchas: `Vieneu(backend="torch")` on torch-free install → `ModuleNotFoundError: No module named 'torch'` — engine.py must catch and rephrase.
  - Context: `infer_stream` has its own sampling defaults (temperature=0.8, top_k=25, top_p=0.95) different from `infer` (temperature=0.4, top_k=50).
---


## [2026-08-27] - Phase 0 Task 2: CPU/ONNX synthesis contract (macOS)
- **Implemented:** `scripts/spike/phase0_contract.py` (16 checks, `--with-cloning` opt-in); spike report §3 filled.
- **Files changed:** scripts/spike/phase0_contract.py, docs/spike-report.md
- **Commit:** 6b6ce4c
- **Learnings:**
  - Patterns: `list_preset_voices()` → `[(label, voice_id)]`; **voice_id (element [1])** is the key for `infer(voice=...)`; label encodes gender·region·style (Bắc/Trung/Nam).
  - Patterns: warm init 5.2s; first-run download ~35s (~240MB, cached in ~/.cache/huggingface).
  - Gotchas: `denoise()` returns **44.1 kHz** not 48 kHz — don't feed denoiser output straight into 48k pipelines un-resampled.
  - Gotchas: `add_voice(save=True)` persists into **site-packages** `vieneu/assets/voices_v3_turbo.json` — packaged app must redirect via `save_voices(path=...)`.
  - Context: `infer` supports `show_progress=False` to keep logs clean; `remove_voice(name, save=True)` is the cleanup counterpart.
---

## [2026-08-27] - Phase 0 Task 1: Scaffold repo toolchain + CPU install
- **Implemented:** pyproject (hatchling, pinned deps, [gpu]/[dev] extras, ruff+pytest+coverage config), src/tests skeletons, uv venv on Python 3.13.13, torch-free vieneu 3.3.0 verified, docs/spike-report.md started.
- **Files changed:** pyproject.toml, .gitignore, README.md, src/vienetts_app/**, tests/**, docs/spike-report.md
- **Commit:** ca99a73
- **Learnings:**
  - Patterns: uv venv + `uv pip install -p .venv/bin/python -e ".[dev]"` is the dev loop; gates via `.venv/bin/{ruff,pytest}`.
  - Patterns: `Vieneu` is a factory FUNCTION (`Vieneu(mode=..., **kwargs)`), not a class — plan §3 API sketch still works since kwargs pass through.
  - Gotchas: system Homebrew python3 is 3.14 (out of SDK range) — always use `uv venv --python 3.13`.
  - Gotchas: ruff 0.16 `format --check` formats Python inside `*.md` → excluded `*.md` and `.agents/`, `conductor/` in ruff config (tooling dirs are not app code).
  - Context: vieneu pulls gradio (92 packages total); soundfile on macOS has MP3; SDK exposes `infer(temperature=0.4, top_k=50)`; cloned voices persist via `save_voices()` JSON.
---

