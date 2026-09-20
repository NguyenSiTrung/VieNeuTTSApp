# Handoff: qwen_multiengine_20260920

Status when this note was written: Phase 0 (partial), Phase 1, Phase 2 and Phase 3
Tasks 3.1–3.3 complete. All commits are **local on `main`** — nothing has been
pushed (AGENTS.md Git Policy).

## Commits

| Commit | Task |
| --- | --- |
| `a13cee1` | 0.1 port audit (`docs/performance/qwen-port-audit.md`) |
| `01de651` | 0.2 runtime probe CLI (`scripts/spike/qwen_runtime_probe.py`) |
| `5cc57a5` | 0.3 dependency/device matrix (`packaging/qwen-runtime-requirements.json`) — probes still pending hardware |
| `88feee2` | 1.1 + 1.2 engine profiles, generation context |
| `6b8845a` | 2.1 shared managed-install primitives |
| `dc12914` | 2.2 Qwen runtime manifests + installer |
| `a03f365` | 2.3 Qwen model manifests + shared-profile installer |
| `cec1b59` | 3.1 framed IPC protocol |
| `7392e2f` | 3.2 isolated model-host executable |
| `c348855` | 3.3 parent Qwen adapter and lifecycle |

## Gate (always run before committing)

```bash
.venv/bin/ruff check . && .venv/bin/ruff format --check .
QT_QPA_PLATFORM=offscreen .venv/bin/pytest -q
```

Baseline: everything passes except
`tests/unit/test_stream_playback.py::TestRealQtSmoke::test_real_qaudiosink_offscreen_smoke`
(device-less host; documented, not a regression). Latest full run: 1357 passed, 1 failed.

Environment note: the real-QtMultimedia smoke cases (`TestRealPlayerSmoke`,
`TestRealProbeSmoke`, `TestRealQtSmoke`) are fragile under `-n auto` in a
device-less sandbox — one run hung inside a `QFFmpeg` demuxer thread under pytest's
fd capture (the GOTCHA already documented in `test_playback.py`). They finish in
under a second when run serially (`-n 0`); if a full run ever stalls, re-run those
three alone and deselect them to get the rest of the signal.

## Next: Phase 3 Task 3.4 — route Qwen through the worker/artifact pipeline

Files (per `plan.md`): `src/vienetts_app/core/text_segmentation.py`,
`src/vienetts_app/workers/inference_worker.py`, `src/vienetts_app/core/engine.py`,
`src/vienetts_app/core/artifacts.py`, plus the matching unit tests.

Contract it must satisfy:

- Add `split_text_for_profile(text, language, max_chars)` in `core/text_segmentation.py`:
  preserve the existing Vietnamese/English behavior, handle CJK punctuation without
  inserting spaces, and bound every Qwen segment **before it crosses IPC**.
  `QwenEngine.infer_stream()` already rejects `len(text) > MAX_TEXT_CHARS` (2000) with
  an actionable "split the text before it crosses IPC" message, so the splitter's
  `max_chars` must be ≤ 2000 (the host's own bound comes from `core/qwen_protocol.py`).
- Introduce the engine-provider protocol and select the provider from the immutable
  generation context without switching mid-job.
- Preserve one terminal per job, partial-artifact cleanup, bounded preview, progress,
  and cancellation semantics.

What Task 3.3 already gives the worker (build on it, do not re-litigate):

- `QwenEngine(profile, model_dir=..., shared_dir=..., device=..., dtype=...,
  attention=..., runtime_dir=...)`; call `initialize()` once, then `capabilities()`
  (`speakers`/`languages`/`supports_clone`/`sample_rate`).
- `infer_stream(text, *, language, speaker="", voice_prompt="", ref_text="",
  job_id="", on_progress=None)` is a generator of 48 kHz mono float32 chunks; pass the
  worker's own job id so `cancel(job_id)` lines up. Cancelling the *iterator* also
  cancels the host job. `QwenEngineCancelled` vs `QwenEngineError` is the terminal
  signal to translate into the worker's own terminal frame.
- `QwenEngineError` messages are user-facing and already actionable; the host's stderr
  tail is appended where it exists.
- Fatal device/OOM failures and protocol violations leave the engine uninitialized;
  the next `infer_stream()` lazily starts a clean host, so the worker only has to report
  the failure (no manual restart bookkeeping).
- Only one job may run per host at a time: a second `infer_stream()` while one is active
  raises instead of interleaving.

## Still blocked (needs the user's machines)

- Task 0.3 real-device probes — the six commands live in
  `packaging/qwen-runtime-requirements.json` (`platforms[].evidence.probeCommand`).
- The Conductor "User Manual Verification" checkpoints for Phases 0–3.

## Housekeeping

- `bd` epic `VieNeuTTSApp-nqx`; Phase 3 tasks are `.5.x` (`.5.1`, `.5.2`, `.5.3` closed;
  next is `.5.4`). `conductor/tracks/qwen_multiengine_20260920/metadata.json` carries the
  corrected phase→beads mapping.
- Do **not** push, pull, or run `bd dolt push` without an explicit request.