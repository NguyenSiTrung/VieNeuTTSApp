# Handoff: qwen_multiengine_20260920

Status when this note was written: Phases 1–5 complete (Phases 3, 4 and 5 user manual
verification approved 2026-09-21), Phase 6 not started, Phase 0 partial (Task 0.3 needs release
hardware). All commits are **local on `main`** — nothing has been pushed (AGENTS.md Git Policy).

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
| `5a49a2d` | 5.3 engine-safe audiobook and subtitle caches |
| `24561d5` | 5.4 Studio clip provenance + matching-engine re-synthesis |

## Gate (always run before committing)

```bash
.venv/bin/ruff check . && .venv/bin/ruff format --check .
QT_QPA_PLATFORM=offscreen .venv/bin/pytest -q
```

Baseline: everything passes except
`tests/unit/test_stream_playback.py::TestRealQtSmoke::test_real_qaudiosink_offscreen_smoke`
(device-less host; documented, not a regression). Latest full run: 1624 passed,
1 deselected (that smoke).

Environment note: the real-QtMultimedia smoke cases (`TestRealPlayerSmoke`,
`TestRealProbeSmoke`, `TestRealQtSmoke`) are fragile under `-n auto` in a
device-less sandbox — one run hung inside a `QFFmpeg` demuxer thread under pytest's
fd capture (the GOTCHA already documented in `test_playback.py`). They finish in
under a second when run serially (`-n 0`); if a full run ever stalls, re-run those
three alone and deselect them to get the rest of the signal.

## Checkpoint closed: Phase 5 manual verification approved 2026-09-21

The user ran the Phase 5 manual verification (Studio provenance, refusal on a profile mismatch,
the switch action, and engine-independent editing/export) and approved it; checkpoint bead
`nqx.7.5` is closed. Phase 5 is complete — its implementation commits are 5.1–5.4 in the table
above.

## Next: Phase 6 Task 6.1 — shared engine/language controls and Settings management

Phase 6 is the first UI phase (`<!-- execution: parallel -->`). Task 6.1 is the prerequisite the
other three wait on: `EngineProfilePicker.qml` + `LanguagePicker.qml` components (registered in
`qmldir`) and the Settings management cards (Model family, Compute device, Qwen runtime, Qwen
model) in `SettingsTab.qml`, with stable `objectName`s and consolidated offscreen smoke scenarios in
`tests/smoke/test_ui_tabs.py`. After it lands, 6.2 (synthesis surfaces) and 6.3 (Cloning + Studio
QML) run concurrently, and 6.4 (catalog completion) waits for both.

The controller surface those QML files bind is already in place from Phases 1–5:
`engineProfiles` / `engineProfile` / `switchEngineProfile(id)` / `engineDevice`,
`profileModel*` + `profileRuntime*` readiness, `synthesisLanguage` / `setSynthesisLanguage(code)`,
the install/import/cancel/repair/remove slots and their status/error strings, `voices` filtered per
profile, and (for 6.3) the Studio `profile`/`profileLabel`/`language` clip rows plus
`studioRegenProfile` / `studioRegenProfileLabel` / `studioSwitchToRegenProfile()`. Task 6.4 is the
single owner of `vienetts_en.ts`/`.qm` at the end, so do not re-run `scripts/update_i18n.sh` for
every new string in 6.1–6.3 unless the focused test needs it.

What Task 5.4 added (build on it, do not re-litigate):

- `synthesis_context.same_engine(stored, requested)` — the engine-only rule for an EXPLICIT
  re-synthesis: same `profile` wins; `stored is None` (audio predating provenance) means VieNeu.
  Caches keep using the stricter `context_matches`.
- `StudioClip.context` (default `None` = unknown/legacy), stamped by `load_project_from_artifact(
  path, text, context=)` and `load_project_from_chapters(..., contexts=state.contexts)`, and
  replaced by `splice_clip_audio(..., context=)` when new audio lands.
- `controller.studioClips` rows now carry `profile`, `profileLabel` and `language` ("" when the
  audio predates provenance) — Phase 6's QML binds these.
- `studioRegenClip` refuses a mismatch with the required profile in `errorText` and arms
  `studioRegenProfile` / `studioRegenProfileLabel` (signal `studioRegenProfileChanged`);
  `studioSwitchToRegenProfile()` performs the switch and consumes the offer.
- The armed splice is disarmed on all three non-success paths (not admitted, cancelled, failed) —
  including `_studio_regen_context`, so a later synthesis can never be spliced into a clip that did
  not ask for it.
- `_current_artifact_context` is the identity of the committed foreground artifact; Studio reads it
  in `openInStudio`, so provenance always names the engine that actually produced the audio.

What Tasks 5.2/5.3 gave Task 5.4 (the seams it builds on):

- `controller.submission_context_for(voice, *, report=True) -> SynthesisContext | None` is the ONE
  gate: it builds the context through `context_for(...)`, which validates
  profile/language/speaker/clone against the capability table, and on refusal sets `errorText` from
  the capability table's own message and returns `None` (nothing is queued, no engine starts, no
  listener registers). Every submission path now carries the frozen context on its `TTSRequest`.
  `report=False` is the read-only probe a cache/Studio comparison uses — it never touches
  `errorText`.
- `core/synthesis_context.py` owns the shared comparison helpers Task 5.4 should reuse:
  `context_from_payload(payload)` (fail-soft inverse of `fingerprint_payload`), `context_matches(
  stored, requested)` (the conservative truth table) and `legacy_render_compatible(requested)`
  (a render with no recorded identity is reusable for VieNeu only — every pre-multi-engine render
  was VieNeu's).
- `controller.synthesisLanguage` (the RESOLVED code: `""` for VieNeu, `auto` for Qwen when unset)
  and `controller.setSynthesisLanguage(code) -> bool` (validates against the active profile,
  persists `Settings.synthesis_language`, refuses with a Vietnamese `tr()` message listing the
  supported codes). Switching profiles drops a code the incoming profile cannot serve.
- `submit_stream_for_listener(text, voice, listener, *, kind=..., context=None)` — callers may pass
  a persisted snapshot; omitting it builds and validates one at submission time.
- `BatchItem.context` + one `runAll` snapshot (`_snapshot_context(voice)`) and the `profile`/
  `language` keys on `batchController.items`; the Paragraph run refuses before touching the queue.
- The Qwen owner assembly: `_build_qwen_engine()` (verified install locations only, actionable
  refusals) + `_providers_for()` (one-provider `EngineProviders`, `None` = VieNeu single-engine
  default), with the injectable `qwen_engine_factory` seam for tests.
- The audiobook's provenance pattern is the reference for Studio clips: a payload map in the
  workspace state, identity re-reconciled on load against the requested context, replacement as one
  locked transaction that drops the sidecars describing the replaced audio, and a render
  snapshotted at submission (never re-derived at terminal time).

## Still blocked (needs the user's machines)

- Task 0.3 real-device probes — the six commands live in
  `packaging/qwen-runtime-requirements.json` (`platforms[].evidence.probeCommand`).
- The Conductor "User Manual Verification" checkpoints for Phases 0–2 (`nqx.2.4`,
  `nqx.3.3`, `nqx.4.4`) — Phase 3's (`nqx.5.5`), Phase 4's (`nqx.6.3`) and Phase 5's
  (`nqx.7.5`) were approved 2026-09-21 and are closed.

## Housekeeping

- `bd` epic `VieNeuTTSApp-nqx`; Phase 3 tasks are `.5.x` (all closed, including the manual
  checkpoint), Phase 4 tasks are `.6.x` (all closed, including the manual checkpoint `.6.3`),
  Phase 5 tasks are `.7.x` (all closed, including the manual checkpoint `.7.5`, approved
  2026-09-21). Note: `bd ready` does not list a task whose parent phase bead is still open
  (parent-child blocks) — that is the established pattern, so do not close a phase bead early.
  `conductor/tracks/qwen_multiengine_20260920/metadata.json` carries the corrected
  phase→beads mapping.
- Do **not** push, pull, or run `bd dolt push` without an explicit request.