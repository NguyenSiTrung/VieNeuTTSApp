# Handoff: qwen_multiengine_20260920

Status when this note was written: Phases 1–3 complete (Phase 3's user manual
verification was approved 2026-09-21), Phase 4 Tasks 4.1 and 4.2 complete, Phase 0
partial (Task 0.3 needs release hardware). All commits are **local on `main`** — nothing has
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
| `d82c6a4` | 4.2 voice operations routed per engine profile |

## Gate (always run before committing)

```bash
.venv/bin/ruff check . && .venv/bin/ruff format --check .
QT_QPA_PLATFORM=offscreen .venv/bin/pytest -q
```

Baseline: everything passes except
`tests/unit/test_stream_playback.py::TestRealQtSmoke::test_real_qaudiosink_offscreen_smoke`
(device-less host; documented, not a regression). Latest full run: 1499 passed,
1 deselected (that smoke).

Environment note: the real-QtMultimedia smoke cases (`TestRealPlayerSmoke`,
`TestRealProbeSmoke`, `TestRealQtSmoke`) are fragile under `-n auto` in a
device-less sandbox — one run hung inside a `QFFmpeg` demuxer thread under pytest's
fd capture (the GOTCHA already documented in `test_playback.py`). They finish in
under a second when run serially (`-n 0`); if a full run ever stalls, re-run those
three alone and deselect them to get the rest of the signal.

## Next: the Phase 4 manual checkpoint, then Phase 5 Task 5.1

`plan.md` Phase 4 has no implementable task left after 4.2 — the remaining entry is the
Conductor **"User Manual Verification 'Add safe Qwen voice-clone persistence'"** checkpoint
(beads `VieNeuTTSApp-nqx.6.3`), which needs the user. Run it, or skip it explicitly and
continue with **Phase 5 Task 5.1 — add global profile switching and readiness**
(`nqx.7.1`: `src/vienetts_app/core/settings.py`, `src/vienetts_app/ui/controller.py`,
`src/vienetts_app/core/engine.py` + tests; snapshot the active profile, keep VieNeu the
default, and surface readiness truthfully).

What Task 4.2 already gives the next tasks (build on it, do not re-litigate):

- `VoiceOp(op=..., name=..., clip_path=..., denoise=..., profile=<EngineId|None>,
  transcript=..., consent=...)`; `profile=None` means the worker's default engine, so the
  existing controller path is unchanged. Enrollment fields are rejected on remove/denoise.
- `InferenceWorker(engine, providers=...)` + `EngineProviders.provider_for_profile(profile)`
  route voice jobs; `EngineProvider.voice_op(op)` returns the terminal payload
  (`{"op", "name", ...}`, plus `cloneId`/`profile` for Qwen adds/removes).
- Wire a Qwen profile with `QwenEngineProvider(engine, clone_store=CloneStore(<app data dir>
  / "clones"))` — that single argument both enables enrollment/removal and resolves
  `clone_id` contexts for synthesis. `VieNeuProvider(engine)` keeps the SDK registry.
- `denoise` and CustomVoice cloning are rejected with the capability reason; the Phase 6 UI
  work hides/disables those controls rather than letting the job fail.

The Phase 4 manual-verification checkpoint (beads `nqx.6.3`) is open for the user.

## Still blocked (needs the user's machines)

- Task 0.3 real-device probes — the six commands live in
  `packaging/qwen-runtime-requirements.json` (`platforms[].evidence.probeCommand`).
- The Conductor "User Manual Verification" checkpoints for Phases 0–2 (`nqx.2.4`,
  `nqx.3.3`, `nqx.4.4`) — Phase 3's (`nqx.5.5`) was approved 2026-09-21 and is closed.

## Housekeeping

- `bd` epic `VieNeuTTSApp-nqx`; Phase 3 tasks are `.5.x` (all closed, including the manual
  checkpoint), Phase 4 tasks are `.6.x` (`.6.1` and `.6.2` closed; `.6.3` is the Phase 4
  manual checkpoint), Phase 5 tasks are `.7.x`.
  `conductor/tracks/qwen_multiengine_20260920/metadata.json` carries the corrected
  phase→beads mapping.
- Do **not** push, pull, or run `bd dolt push` without an explicit request.