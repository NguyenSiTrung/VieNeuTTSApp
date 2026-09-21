# Handoff: qwen_multiengine_20260920

Status when this note was written: Phases 1–3 complete (Phase 3's user manual
verification was approved 2026-09-21), Phase 4 Tasks 4.1 and 4.2 complete, Phase 5
Tasks 5.1 and 5.2 complete, Phase 0 partial (Task 0.3 needs release hardware). All commits are
**local on `main`** — nothing has been pushed (AGENTS.md Git Policy).

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
| `d82c6a4` | 4.2 voice operations routed per engine profile |
| `ce9d74b` | 5.1 engine profiles exposed + guarded switching |
| `173c435` | 5.2 profile context snapshotted at every submission |

## Gate (always run before committing)

```bash
.venv/bin/ruff check . && .venv/bin/ruff format --check .
QT_QPA_PLATFORM=offscreen .venv/bin/pytest -q
```

Baseline: everything passes except
`tests/unit/test_stream_playback.py::TestRealQtSmoke::test_real_qaudiosink_offscreen_smoke`
(device-less host; documented, not a regression). Latest full run: 1561 passed,
1 deselected (that smoke).

Environment note: the real-QtMultimedia smoke cases (`TestRealPlayerSmoke`,
`TestRealProbeSmoke`, `TestRealQtSmoke`) are fragile under `-n auto` in a
device-less sandbox — one run hung inside a `QFFmpeg` demuxer thread under pytest's
fd capture (the GOTCHA already documented in `test_playback.py`). They finish in
under a second when run serially (`-n 0`); if a full run ever stalls, re-run those
three alone and deselect them to get the rest of the signal.

## Next: the Phase 4/Phase 5 manual checkpoints, then Phase 5 Task 5.3

`plan.md` Phase 4 has no implementable task left after 4.2 — the remaining entry is the
Conductor **"User Manual Verification 'Add safe Qwen voice-clone persistence'"** checkpoint
(beads `VieNeuTTSApp-nqx.6.3`), which needs the user. Phase 5's checkpoint (`nqx.7.5`) is
likewise open for the user. Skip or approve them explicitly, then continue with
**Phase 5 Task 5.3 — make Audiobook and Subtitle caches engine-safe**
(`nqx.7.3`: `src/vienetts_app/core/audiobook.py`, `src/vienetts_app/ui/audiobook_controller.py`,
`src/vienetts_app/core/subtitle_project.py`, `src/vienetts_app/ui/subtitle_controller.py` + tests;
include `SynthesisContext.fingerprint_payload()` in persisted render provenance and cache
fingerprints, invalidating only incompatible renders, and migrate old projects without cache
loss). Task 5.4 (Studio provenance + truthful re-synthesis) follows, then the Phase 5 checkpoint.

What Task 5.2 already gives the next tasks (build on it, do not re-litigate):

- `controller.submission_context_for(voice) -> SynthesisContext | None` is the ONE gate: it builds
  the context through `context_for(...)`, which validates profile/language/speaker/clone against
  the capability table, and on refusal sets `errorText` from the capability table's own message
  and returns `None` (nothing is queued, no engine starts, no listener registers). Every
  submission path now carries the frozen context on its `TTSRequest`.
- `controller.synthesisLanguage` (the RESOLVED code: `""` for VieNeu, `auto` for Qwen when unset)
  and `controller.setSynthesisLanguage(code) -> bool` (validates against the active profile,
  persists `Settings.synthesis_language`, refuses with a Vietnamese `tr()` message listing the
  supported codes). Switching profiles drops a code the incoming profile cannot serve.
- `submit_stream_for_listener(text, voice, listener, *, kind=..., context=None)` — callers may pass
  a persisted snapshot (that is what the Paragraph queue does); omitting it builds and validates
  one at submission time. Audiobook/Subtitle currently omit it, so they already require a
  compatible combination; their caches become engine-safe in Task 5.3.
- `BatchItem.context` + one `runAll` snapshot (`_snapshot_context(voice)`) and the `profile`/
  `language` keys on `batchController.items`; the Paragraph run refuses before touching the queue.
- The Qwen owner assembly: `_build_qwen_engine()` (verified install locations only, actionable
  refusals) + `_providers_for()` (one-provider `EngineProviders`, `None` = VieNeu single-engine
  default), with the new injectable `qwen_engine_factory` seam for tests.
- The audition cache is `auditions/<profile>/<voice>_<language>_<speed>.wav` — the pattern Task 5.3
  should mirror for render caches (profile + language + voice/clone + generation in the key).

## Still blocked (needs the user's machines)

- Task 0.3 real-device probes — the six commands live in
  `packaging/qwen-runtime-requirements.json` (`platforms[].evidence.probeCommand`).
- The Conductor "User Manual Verification" checkpoints for Phases 0–2 (`nqx.2.4`,
  `nqx.3.3`, `nqx.4.4`) — Phase 3's (`nqx.5.5`) was approved 2026-09-21 and is closed.

## Housekeeping

- `bd` epic `VieNeuTTSApp-nqx`; Phase 3 tasks are `.5.x` (all closed, including the manual
  checkpoint), Phase 4 tasks are `.6.x` (`.6.1` and `.6.2` closed; `.6.3` is the Phase 4
  manual checkpoint), Phase 5 tasks are `.7.x` (`.7.1` and `.7.2` closed; `.7.5` is the Phase 5
  manual checkpoint). Note: `bd ready` does not list a task whose parent phase bead is still open
  (parent-child blocks) — that is the established pattern, so do not close a phase bead early.
  `conductor/tracks/qwen_multiengine_20260920/metadata.json` carries the corrected
  phase→beads mapping.
- Do **not** push, pull, or run `bd dolt push` without an explicit request.