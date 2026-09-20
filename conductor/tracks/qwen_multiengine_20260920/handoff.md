# Handoff: qwen_multiengine_20260920

Status when this note was written: Phases 1–3 complete (Phase 3's user manual
verification was approved 2026-09-21), Phase 4 Task 4.1 complete, Phase 0 partial
(Task 0.3 needs release hardware). All commits are **local on `main`** — nothing has
been pushed (AGENTS.md Git Policy).

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
| `0dd67f4` | 4.1 profile-scoped clone store (`core/voice_profiles.py`) |

## Gate (always run before committing)

```bash
.venv/bin/ruff check . && .venv/bin/ruff format --check .
QT_QPA_PLATFORM=offscreen .venv/bin/pytest -q
```

Baseline: everything passes except
`tests/unit/test_stream_playback.py::TestRealQtSmoke::test_real_qaudiosink_offscreen_smoke`
(device-less host; documented, not a regression). Latest full run: 1468 passed,
1 deselected (that smoke).

Environment note: the real-QtMultimedia smoke cases (`TestRealPlayerSmoke`,
`TestRealProbeSmoke`, `TestRealQtSmoke`) are fragile under `-n auto` in a
device-less sandbox — one run hung inside a `QFFmpeg` demuxer thread under pytest's
fd capture (the GOTCHA already documented in `test_playback.py`). They finish in
under a second when run serially (`-n 0`); if a full run ever stalls, re-run those
three alone and deselect them to get the rest of the signal.

## Next: Phase 4 Task 4.2 — adapt clone operations to engine capabilities

`plan.md` files: `src/vienetts_app/core/models.py`,
`src/vienetts_app/workers/inference_worker.py`, `src/vienetts_app/core/engine.py`,
`src/vienetts_app/core/qwen_engine.py` + the matching unit tests
(beads `VieNeuTTSApp-nqx.6.2`).

Contract:

- Extend `VoiceOp` with profile/context; preserve VieNeu add/remove behavior.
- For Qwen Base, store reference data through `CloneStore.enroll(...)` and build/cache
  runtime prompts only inside the model host; reject CustomVoice cloning with a
  capability reason.
- Test add/use/remove, restart, profile isolation, and prompt rebuild.

What Task 4.1 already gives it (build on it, do not re-litigate):

- `CloneStore(root)` is the only writer of clone metadata; `enroll(name=..., profile=...,
  reference_clip=..., transcript=..., consent=...)` enforces the profile's
  `clone_requirements` itself (Base: transcript + consent; VieNeu: no transcript;
  CustomVoice: refused). It is idempotent for the same clip content + transcript.
- `store.prompt_for(clone_id)` returns `ClonePrompt(reference_path, transcript)` — pass it
  as `QwenEngineProvider(engine, clone_prompt_for=store.prompt_for)`. It raises
  `CloneStoreError` (actionable, includes "re-enroll it" for a missing clip) rather than
  returning `None`, so decide whether the provider hook or the caller maps that to the
  job's failure message.
- `store.list(profile)` is the profile-scoped catalog for the UI; `store.remove(id)`
  drops the entry and its reference copy.

## Still blocked (needs the user's machines)

- Task 0.3 real-device probes — the six commands live in
  `packaging/qwen-runtime-requirements.json` (`platforms[].evidence.probeCommand`).
- The Conductor "User Manual Verification" checkpoints for Phases 0–2 (`nqx.2.4`,
  `nqx.3.3`, `nqx.4.4`) — Phase 3's (`nqx.5.5`) was approved 2026-09-21 and is closed.

## Housekeeping

- `bd` epic `VieNeuTTSApp-nqx`; Phase 3 tasks are `.5.x` (`.5.1`–`.5.4` closed; `.5.5` is
  the Phase 3 manual checkpoint), Phase 4 tasks are `.6.x` (`.6.1` closed; next `.6.2`).
  `conductor/tracks/qwen_multiengine_20260920/metadata.json` carries the corrected
  phase→beads mapping.
- Do **not** push, pull, or run `bd dolt push` without an explicit request.