# Handoff: qwen_multiengine_20260920

Status when this note was written: Phase 0 (partial), Phase 1, Phase 2 and Phase 3
Tasks 3.1–3.4 complete. All commits are **local on `main`** — nothing has been
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
| `c62759b` | 3.4 profile-aware text segmentation (`core/text_segmentation.py`) |
| `6356be5` | 3.4 engine-provider seam + worker routing |

## Gate (always run before committing)

```bash
.venv/bin/ruff check . && .venv/bin/ruff format --check .
QT_QPA_PLATFORM=offscreen .venv/bin/pytest -q
```

Baseline: everything passes except
`tests/unit/test_stream_playback.py::TestRealQtSmoke::test_real_qaudiosink_offscreen_smoke`
(device-less host; documented, not a regression). Latest full run: 1424 passed,
1 deselected (that smoke).

Environment note: the real-QtMultimedia smoke cases (`TestRealPlayerSmoke`,
`TestRealProbeSmoke`, `TestRealQtSmoke`) are fragile under `-n auto` in a
device-less sandbox — one run hung inside a `QFFmpeg` demuxer thread under pytest's
fd capture (the GOTCHA already documented in `test_playback.py`). They finish in
under a second when run serially (`-n 0`); if a full run ever stalls, re-run those
three alone and deselect them to get the rest of the signal.

## Next: Phase 3 manual verification, then Phase 4 Task 4.1 (clone store)

`plan.md` Phase 3 has no task left after 3.4 — the remaining entry is the Conductor
**"User Manual Verification 'Implement the isolated Qwen model host'"** checkpoint
(beads `VieNeuTTSApp-nqx.5.5`), which needs the user and a machine with the Qwen
runtime. Run it, or skip it explicitly and continue with:

**Task 4.1 — create the profile-scoped clone store** (`VieNeuTTSApp-nqx.6.1`):
`src/vienetts_app/core/voice_profiles.py` + `tests/unit/test_voice_profiles.py`.
Persist stable id, display name, engine profile, copied reference WAV, required
transcript, content hash and timestamps as atomic JSON plus app-owned audio; never
persist pickle/torch objects. Test duration/codec/transcript validation, restart,
collisions, corruption, deduplication, removal, and atomic failures.

Task 4.2 (`nqx.6.2`) then wires `VoiceOp` to profiles and consumes
`QwenEngineProvider(engine, clone_prompt_for=...)` — that hook already exists and is
the only supported way to give the host a Base clone prompt.

What Task 3.4 already gives the next tasks (build on it, do not re-litigate):

- `EngineProviders(by_profile={...}, default=provider)` is frozen; build a NEW set to
  switch profiles. `InferenceWorker(engine, providers=...)` resolves the provider once
  per job from `job.context` and never switches mid-job; `engine` may be `None` when
  every job carries a context.
- `QwenEngineProvider(engine, *, clone_prompt_for=callable)` maps a context to host
  kwargs (`language` → model name, `voice_id`/`voice` → speaker, `clone_id` → resolved
  `ClonePrompt(reference_path, transcript)`), gives each segment its own protocol job id
  and forwards `cancel(worker_job_id)` to the running segment.
- `split_text_for_profile(text, language, max_chars)` + `segment_limit_for(profile)` are
  the only supported way to bound text before IPC; the worker already uses them.
- Voice ops (`VoiceOp`) are VieNeu-only today and fail with
  "voice management is only available on the VieNeu-TTS profile" when the worker has no
  VieNeu engine — Task 4.2 replaces that guard with capability-driven behavior.

## Still blocked (needs the user's machines)

- Task 0.3 real-device probes — the six commands live in
  `packaging/qwen-runtime-requirements.json` (`platforms[].evidence.probeCommand`).
- The Conductor "User Manual Verification" checkpoints for Phases 0–3.

## Housekeeping

- `bd` epic `VieNeuTTSApp-nqx`; Phase 3 tasks are `.5.x` (`.5.1`–`.5.4` closed; next is
  `.5.5`, the Phase 3 manual checkpoint; Phase 4 tasks are `.6.x`).
  `conductor/tracks/qwen_multiengine_20260920/metadata.json` carries the corrected
  phase→beads mapping.
- Do **not** push, pull, or run `bd dolt push` without an explicit request.