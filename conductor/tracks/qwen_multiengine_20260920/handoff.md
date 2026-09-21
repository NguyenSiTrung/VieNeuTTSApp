# Handoff: qwen_multiengine_20260920

Status when this note was written: Phases 1–5 complete (Phases 3, 4 and 5 user manual
verification approved 2026-09-21), Phase 6 in progress (Tasks 6.1 and 6.2 complete, 6.3 next,
6.4 waits for it), Phase 0 partial (Task 0.3 needs release hardware). All commits are **local on
`main`** — nothing has been pushed (AGENTS.md Git Policy).

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
| `411f8d4` | 6.1 shared engine/language controls + Settings Qwen management |
| `198ffac` | 6.2 synthesis surfaces bound to the active engine capabilities |

## Gate (always run before committing)

```bash
.venv/bin/ruff check . && .venv/bin/ruff format --check .
QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest -q \
  --deselect tests/unit/test_stream_playback.py::TestRealQtSmoke::test_real_qaudiosink_offscreen_smoke
```

Baseline: everything passes except
`tests/unit/test_stream_playback.py::TestRealQtSmoke::test_real_qaudiosink_offscreen_smoke`
(device-less host; documented, not a regression — bead `VieNeuTTSApp-3iy`). Latest full run:
**1665 passed**, 1 deselected. Note the nodeid spelling: it is `qaudiosink`
(q-a-u-d-i-o-s-i-n-k); a typo makes `--deselect` match nothing and the failure reappears.

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

## Landed: Phase 6 Task 6.1 — shared engine/language controls and Settings management

Commit `411f8d4` (bead `VieNeuTTSApp-nqx.8.1`, closed). Build on it, do not re-litigate:

- `src/vienetts_app/ui/qml/components/EngineProfilePicker.qml` — the shared engine-profile control.
  `objectName`s: `engineProfilePicker`, `engineProfileCombo`, `engineProfileReadinessBadge`,
  `engineProfileReadinessText`, `engineProfileDeviceLabel`, `engineProfileStatusLabel`. It derives
  its own readiness (`ready` / `busy` / `failed` / `unsupported` / `missing`) from the controller's
  `profileRuntime*` + `profileModel*` properties and shows the reason in place.
- `src/vienetts_app/ui/qml/components/LanguagePicker.qml` — `languagePicker`, `languagePickerCombo`,
  `languagePickerNote`; bound to `profileLanguages` / `synthesisLanguage` /
  `setSynthesisLanguage(code)`. Language labels stay NATIVE (中文, 日本語, Tiếng Việt…) — do not
  translate them.
- Both are registered in `qml/qmldir` **and** `qml/components/qmldir` (6.2/6.3 import them from the
  synthesis tabs; `import "components"` resolves both ways).
- `SettingsTab.qml` now opens with four cards before the CUDA card: Model family
  (`engineProfileCard`), Compute device (`qwenDeviceCard`), Qwen runtime (`qwenRuntimeCard`), Qwen
  model (`qwenModelCard`, Repeater rows `qwenModel*_<key>`). The file's header comment documents the
  full objectName contract — read it before adding names.
- Tested seams on the root item: `pickQwenRuntimePack(url)`, `openQwenModelPackDialog(key)`,
  `pickQwenModelPack(key, url)`. Native `FolderDialog`s stay **closed** offscreen; the smoke test
  asserts the seams and the buttons' `enabled` state, never a real dialog.
- Controller additions are the Qwen device choices + runtime/model management signals, properties
  and slots; `qwenSharedBytes` is manifest-derived; `qwenModels[*].requiredBytes` falls back to the
  pinned manifest totals before install; model removal passes real `in_use` / `remove_shared` flags.
- `qwen_runtime_manifest` gained `host_platform_tag` / `host_platform_key` / `host_devices` /
  `platform_label`, and `_build_default_qwen_runtime_manager` is now device-aware (it previously
  passed the *current platform key* where a Qwen manifest key was expected, so the manager was
  always `None`).

Smoke-test lessons (they cost time once; keep them):

- Repeater delegates are **rebuilt** when the model changes — look rows up fresh via
  `ifind(name_key)` (`row_item()` in the test) instead of caching the item.
- Extend `FakeController` in `tests/smoke/test_ui_tabs.py` with real `Property(..., notify=...)`
  signals; the scenarios are `settings_engine_profiles` and `settings_qwen_states`, and
  `settings_load`'s `required` set lists the objectNames a scenario must have.
- A scenario's workspace is `tmp_path/<scenario_name>/…`.

## Landed: Phase 6 Task 6.2 — synthesis surfaces bound to the active engine capabilities

Commit `198ffac` (bead `VieNeuTTSApp-nqx.8.2`, closed). Build on it, do not re-litigate:

- `src/vienetts_app/ui/qml/components/EngineState.qml` — **new QML singleton** (`pragma Singleton`,
  registered as `singleton EngineState 1.0 EngineState.qml` in **both** `qml/qmldir` and
  `qml/components/qmldir`, the `Theme` precedent). It is the ONE derivation of the active profile's
  capability state: `activeProfile`, `profileId` / `profileLabel`, `voicesSource`
  (`vieneu_catalog` | `pinned` | `enrollment_only`), `needsManagedInstall` (`runtime === "qwen_host"`),
  `voiceGroups`, `hasNoVoices` / `noVoicesReason`, `languageTakesParameter`, `readiness` /
  `statusText`, `blocked` / `blockerReason`, `deviceName(device)` / `deviceLabel`. Read it before
  adding another capability branch anywhere.
- `VoicePicker.qml`: `flatModel` comes from `EngineState.voiceGroups`; `effectiveVoice` is the
  submission voice (the picker's choice, else the profile's own fallback — VieNeu's Settings default
  voice validated against the catalog, a Qwen profile's first offered voice); `unavailableReason`
  disables the picker with the reason in the trigger (new objectName `voicePickerTriggerLabel`) and
  a tooltip; `syncSelection()` re-points a stale selection when the catalog changes (deferred via
  `Qt.callLater` — see the gotcha below); `voiceInfoFor(row)` prefers explicit persona fields on a
  capability row (a pinned speaker's native language rides in the region chip); `rowForId("")` is
  `null` (a group header is not a voice).
- `LanguagePicker.qml`: `takesLanguage` (= `EngineState.languageTakesParameter`) hides the combo for
  an engine that consumes no language argument, and the note says why.
- Text (`TextTab`), Paragraph/Batch (`SynthesisBar` + `ParagraphTab`), Audiobook (`AudiobookTab`) and
  Subtitle (`SubtitleCard`) all submit `effectiveVoice` and gate their primary action
  (Generate / Ctrl+Return / Run all / chapter render / cue render / "Tạo và phát") on
  `EngineState.blockerReason`, carrying it as the `disabledReason`.
- `EngineProfilePicker.qml` now renders `EngineState.readiness` / `statusText` / `deviceLabel`
  (the copy moved to the singleton's context; `EngineProfilePicker` keeps only the readiness words
  "Sẵn sàng" / "Đang chuẩn bị" / "Cần chú ý" / "Không hỗ trợ" / "Chưa sẵn sàng").
- English catalog regenerated: **724 finished, 0 unfinished**; `tests/unit/test_i18n.py` asserts the
  new `EngineState` context (the two readiness sentences moved there from `EngineProfilePicker`).

Gotchas that cost time here (also in `learnings.md`):

- Reading a control's `visible` from Python returns its **effective** value (Qt Quick's READ is
  `isVisible()`), and hiding an ancestor flips every descendant. A smoke assertion must be taken
  while the owning tab is current, and a container gate must be satisfied first — the scenario opens
  the committed `tests/fixtures/sample.epub` through the real controller
  (`engine.rootContext().contextProperty("audiobook")`, synchronous under `bg_runner=run_sync`) so
  the audiobook card is genuinely on screen.
- Inside an `onXChanged` handler the sibling bindings may still hold their PREVIOUS value and a
  ComboBox has not adopted its new model yet: an index assigned there is clamped away and the
  control's own `onCurrentIndexChanged` then clears the selection. Resync deferred
  (`onFlatModelChanged: Qt.callLater(syncSelection)`) and resolve the target against the fresh model
  passed in, never through another bound property.
- A scenario fake must publish the real signal contract: `engineProfiles` is notified by
  `engineProfilesChanged` (the switch emits profile + profiles + catalog + voices), so emitting only
  `profileCatalogChanged` leaves `EngineState.activeProfile` stale.
- The new smoke scenario is `surface_profile_bindings` (test
  `TestSettingsTabSmoke::test_synthesis_surfaces_follow_the_active_profile`); `FakeController` grew
  `profileVoices` / `profileClones` and the `engineProfilesChanged` signal, and the `FakeBatch`
  factory is now also built for this scenario (the paragraph bar re-seeds the run's voice).

## Next: Phase 6 Task 6.3, then 6.4

**6.3 — Cloning and Studio QML** (`CloningTab.qml`, `StudioTab.qml`,
`components/StudioClipRow.qml`, `tests/unit/test_studio_controller.py`): require a transcript for
Base, show profile ownership, disable CustomVoice cloning, expose the Studio provenance +
matching-profile actions (`studioClips[*].profile` / `profileLabel` / `language`,
`studioRegenProfile` / `studioRegenProfileLabel` / `studioSwitchToRegenProfile()`). Known leftover:
`StudioTab.qml`'s `regenVoicePicker` still falls back to `controller.defaultVoice` (line ~143) —
it should adopt `VoicePicker.effectiveVoice` / `EngineState` like the other surfaces, and the
Cloning tab's own picker (`fieldLabel` "Giọng đọc mới") is a `VoicePicker` instance whose catalog now
follows the active profile.

**6.4 — catalogs** waits for 6.3 so `vienetts_en.ts`/`.qm` stay single-owner.

Correction to the earlier i18n guidance: `tests/unit/test_i18n.py::test_english_ts_has_no_unfinished_translations`
fails the moment `scripts/update_i18n.sh` records a new string, so **6.2/6.3 must translate their
own new strings and recompile the `.qm` in the same task** (the full suite is the gate). 6.4 then
does the final consolidated pass and extends the catalog assertions. Keep 6.4 as the single owner of
any *shared* string wording.

The controller surface those QML files bind is already in place from Phases 1–5:
`engineProfiles` / `engineProfile` / `switchEngineProfile(id)` / `engineDevice`,
`profileModel*` + `profileRuntime*` readiness, `synthesisLanguage` / `setSynthesisLanguage(code)`,
the install/import/cancel/repair/remove slots and their status/error strings, `voices` filtered per
profile, and (for 6.3) the Studio `profile`/`profileLabel`/`language` clip rows plus
`studioRegenProfile` / `studioRegenProfileLabel` / `studioSwitchToRegenProfile()`.

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
  2026-09-21), Phase 6 tasks are `.8.x` (`.8.1` and `.8.2` closed 2026-09-21; `.8.3` is next,
  `.8.4` the catalog pass, `.8.5` the phase manual checkpoint). Note: `bd ready` does
  not list a task whose parent phase bead is still open (parent-child blocks) — that is the
  established pattern, so do not close a phase bead early.
  `conductor/tracks/qwen_multiengine_20260920/metadata.json` carries the corrected
  phase→beads mapping.
- Do **not** push, pull, or run `bd dolt push` without an explicit request.