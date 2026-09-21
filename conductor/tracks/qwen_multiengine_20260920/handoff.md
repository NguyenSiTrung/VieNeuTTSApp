# Handoff: qwen_multiengine_20260920

Status when this note was written: Phases 1–6 complete (Phases 3, 4, 5 and 6 user manual
verification approved 2026-09-21), Phase 7 in progress (Tasks 7.1 and 7.2 landed; Task 7.3 next),
Phase 0 partial (Task 0.3 needs release hardware). All commits are **local on `main`** — nothing has
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
| `ce9d74b` | 5.1 engine profiles exposed + guarded switching |
| `173c435` | 5.2 profile context snapshotted at every submission |
| `5a49a2d` | 5.3 engine-safe audiobook and subtitle caches |
| `24561d5` | 5.4 Studio clip provenance + matching-engine re-synthesis |
| `411f8d4` | 6.1 shared engine/language controls + Settings Qwen management |
| `198ffac` | 6.2 synthesis surfaces bound to the active engine capabilities |
| `c9442d9` | 6.3 cloning and studio surfaces bound to the same capabilities |
| `d912b07` | 6.4 consolidated smoke groups + self-checking English catalog |
| `b38df01` / `c64a2b5` | 6.4 conductor + beads bookkeeping |
| (bookkeeping) | 6.5 Phase 6 checkpoint approved 2026-09-21 |
| `e92cc0f` | 7.1 frozen host packaging without the Qwen stack |
| `328baf5` | 7.2 deterministic fake-host end-to-end coverage (+ a SIGPIPE fix in `core/qwen_engine.py`) |

## Gate (always run before committing)

```bash
.venv/bin/ruff check . && .venv/bin/ruff format --check .
QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest -q \
  --deselect tests/unit/test_stream_playback.py::TestRealQtSmoke::test_real_qaudiosink_offscreen_smoke
```

Baseline: everything passes except
`tests/unit/test_stream_playback.py::TestRealQtSmoke::test_real_qaudiosink_offscreen_smoke`
(device-less host; documented, not a regression — bead `VieNeuTTSApp-3iy`). Latest full run:
**1694 passed**, 1 deselected. Note the nodeid spelling: it is `qaudiosink`
(q-a-u-d-i-o-s-i-n-k); a typo makes `--deselect` match nothing and the failure reappears.

Environment note: the real-QtMultimedia smoke cases (`TestRealPlayerSmoke`,
`TestRealProbeSmoke`, `TestRealQtSmoke`) are fragile under `-n auto` in a
device-less sandbox — one run hung inside a `QFFmpeg` demuxer thread under pytest's
fd capture (the GOTCHA already documented in `test_playback.py`). They finish in
under a second when run serially (`-n 0`); if a full run ever stalls, re-run those
three alone and deselect them to get the rest of the signal.

## Checkpoint closed: Phase 6 manual verification approved 2026-09-21

The user ran the Phase 6 manual verification (model-family switching and the surfaces following it,
the Qwen runtime/model install cards and their states, the cloning gate/transcript/profile-ownership
branches, and the Studio provenance + mismatch switch action) and approved it; checkpoint bead
`nqx.8.5` is closed and Phase 6 is complete. Its implementation commits are 6.1–6.4 in the table
above.

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
  `TestSettingsTabSmoke::test_engine_profiles_install_and_synthesis_bindings` — renamed in 6.4 when
  the scenario joined the settings group); `FakeController` grew
  `profileVoices` / `profileClones` and the `engineProfilesChanged` signal, and the `FakeBatch`
  factory is now also built for this scenario (the paragraph bar re-seeds the run's voice).

## Landed: Phase 6 Task 6.3 — cloning and studio surfaces on the same capabilities

Commit `c9442d9` (bead `VieNeuTTSApp-nqx.8.3`, closed). Build on it, do not re-litigate:

- **Controller** (`src/vienetts_app/ui/controller.py`): `addVoice` is now a pair of stacked overloads
  — `@Slot(str, str, bool)` (the original, still what an older host calls) and
  `@Slot(str, str, bool, str)` carrying the reference **transcript**. Every enrollment is stamped
  with `profile` (the active profile), `transcript` and `consent`; `removeVoice` stamps the profile
  too, so a clone stays attributable after a switch. `refreshVoices()` now **also emits
  `profileCatalogChanged`** — without it the profile-scoped `profileVoices`/`profileClones` catalogs
  would keep rendering the pre-enrollment list.
- **`EngineState.qml`** gained the cloning vocabulary: `supportsCloning`, `cloneRequirements`,
  `requiresTranscript`, `supportsReferenceCleanup` (= the profile is **not** a managed install, i.e.
  the bundled engine whose reference denoise exists), `cloningBlockedReason`, `hasCloneRequirement()`.
- **`CloningTab.qml`**: `cloneCapabilityNotice` + `cloneCapabilityReason` replace the consent
  checkbox and the workspace field for a profile that cannot clone (Qwen CustomVoice);
  `cloneTranscriptLabel` / `cloneTranscriptField` / `cloneTranscriptHint` appear only where the
  engine needs the transcript, and Enroll stays disabled with the reason until it is filled; the
  denoise row plus `referenceCleanupNote` are hidden where the engine rejects a denoise pass; the
  clone list renders `controller.profileClones` (the ACTIVE profile's own clones) and each row names
  its owner through `clonedVoiceProfile` ("Hồ sơ: %1"). The dead `clonedVoices()` helper is gone.
- **`StudioTab.qml`**: `studioRegenProfileBanner` / `studioRegenProfileLabel` /
  `studioSwitchToRegenProfileButton` surface the armed mismatch, and `regenVoice()` now submits
  `regenVoicePicker.effectiveVoice` — the `controller.defaultVoice` fallback is **gone** (it was the
  last surface that could silently render with a voice the active profile does not own).
- **`StudioClipRow.qml`**: `studioClipProfile` / `studioClipLanguage` state each clip's provenance;
  a clip with no recorded identity reads "VieNeu-TTS (bản cũ)".
- **`VoicePicker.qml`**: `genderLabel` / `styleLabel` return `token || ""`, which silences the
  "Unable to assign [undefined] to QString" warnings for capability rows that carry no gender/style.

Coverage (note the plan's file list named `tests/unit/test_studio_controller.py`, but that file
already owns the 5.4 controller seams and needed no change):

- `tests/smoke/test_ui_tabs.py` — new `clone_capability` scenario (Qwen CustomVoice *before* consent
  → VieNeu → Qwen Base) and the cloning/studio test assertions
  (`TestCloningStudioTabSmoke::test_cloning_and_studio_surfaces` since 6.4): the notice names the
  profile and the consent/workspace blocks are absent, VieNeu enrolls without a transcript and lists
  `Hồ sơ: VieNeu-TTS v3 Turbo`, Base lists **none** of VieNeu's clones, shows the transcript hint
  naming its profile, hides the denoise row with the reason, refuses Enroll without the transcript
  and passes the 4-argument `addVoice` (transcript included) once filled, after which the row reads
  `Hồ sơ: Qwen3-TTS Base 0.6B`. `studio_load` asserts the clip provenance line and the mismatch
  banner + switch button (the switch itself is unit-tested from 5.4).
- `tests/unit/test_controller.py` `TestVoiceOps`: five cases — enrollment stamps
  profile/transcript/consent, the 3-argument form still works, removal carries the profile,
  `refreshVoices` emits `profileCatalogChanged`, and **both `addVoice` signatures are registered on
  the metaobject** (QML resolves a slot by argument count, so the 4-argument form must exist or a
  capability-aware call silently binds to the old one).
- `tests/unit/test_i18n.py` covers the new CloningTab/EngineState/StudioTab/StudioClipRow strings;
  English catalog regenerated (**737 finished, 0 unfinished**).

Smoke-test lessons worth keeping:

- A fake must publish the same *catalog* contract the real controller does: the 4-argument
  `addVoice` has to append to `_profile_clones` **and** emit `profileCatalogChanged`, or the row
  never appears and the ownership assertion reads a stale list.
- `item_walk` returns Repeater delegates in reverse order — sort by `mapToScene().y()` before
  asserting an ordered list of rows.
- The pre-existing "Cannot read property 'X' of null" warnings come from the first paint, when the
  root context properties are not yet set (`app.py` sets them after loading the QML); they are noise,
  unlike the real "assign undefined to QString" class that the `|| ""` fix removed.

## Landed: Phase 6 Task 6.4 — consolidated smoke groups and a self-checking catalog

Commit `d912b07` (bead `VieNeuTTSApp-nqx.8.4`, closed). Build on it, do not re-litigate:

- **Smoke consolidation** (`tests/smoke/test_ui_tabs.py`): the last single-scenario subprocesses
  joined their surface families, so each family still runs exactly one `QGuiApplication`:
  - SRT studio → `TestTextParagraphTabSmoke::test_text_paragraph_and_subtitle_surface_flows`
    (`load`, `voice_picker_popup`, `generate_flow`, `export_flow`, `error_flow`, `para_import`,
    `para_import_guard`, `para_batch`, `srt_surface`);
  - Studio → `TestCloningStudioTabSmoke::test_cloning_and_studio_surfaces` (`clone_gate`,
    `clone_flow`, `clone_denoise`, `clone_remove`, `clone_disabled`, `clone_capability`,
    `studio_load`) — the class that used to be `TestCloningTabSmoke` plus `TestStudioTabSmoke`;
  - capability bindings → `TestSettingsTabSmoke::test_engine_profiles_install_and_synthesis_bindings`
    (`settings_engine_profiles`, `settings_qwen_states`, `surface_profile_bindings`) — the 6.2 test
    method was renamed into this one.
  10 smoke tests became 7; the group's wall time went 16.4s → 11.0s with no assertion dropped.
- **Coverage completion** — all 66 objectNames introduced by 6.1/6.2/6.3 are now asserted. The
  closes: the Text tab's own language control is read through `textLanguagePicker` (the
  `paraLanguagePicker` precedent) rather than a bare `languagePickerCombo` lookup; the Qwen model row
  now asserts its manifest label (`qwenModelLabel_<key>`) and its state word — which needed a new
  documented `qwenModelStateLabel_<key>` on the pill's inner Label in `SettingsTab.qml` (asserted in
  all four states: `Chưa cài đặt` / `Đang tải` / `Sẵn sàng` / `Cần chú ý`); and the runtime card's
  `qwenRuntimeOpenDirButton` → `openQwenRuntimeDir` plus `qwenRuntimeImportHint` are asserted (the
  model card's twin was already covered, so `qwen_open_dir_calls` now reads `["runtime", "models"]`).
- **Documented exclusions**: the module docstring names the three kinds that must NOT be asserted —
  native dialogs (they stay closed offscreen; the contract is the `onAccepted` seam), dynamic
  per-key names (reached via `row_item(name, key)`), and style-only elements
  (`qwenModelStateBadge_<key>`, the pill colour). Do not "fix" these gaps by opening a dialog.
- **Catalog** (`scripts/update_i18n.sh`, `vienetts_en.ts`/`.qm`): lupdate now runs `-noobsolete`,
  which dropped **148 stale `vanished` entries + 1 obsolete** that had accumulated across the
  redesigns; the file is now exactly the 737 live sources (lrelease: 737 finished, 0 unfinished).
  Two shared sentences that had drifted were unified: SubtitleController's playback-unavailable
  notice now matches the other three contexts, and its invalid-audio refusal matches
  BatchFileController's.
- **Catalog gates** (`tests/unit/test_i18n.py`, +4 tests, each mutation-verified): no empty
  translations (numerus-aware), no obsolete/vanished entries, every `.ts` entry round-trips through
  the compiled `.qm` (plurals through each numerus form — this is what catches a stale `.qm`), and
  one translation per identical source outside the two documented ambiguous cases (`Xóa`
  Delete/Clear, `Văn bản` Transcript/Text).

## Landed: Phase 7 Task 7.1 — the frozen host ships the lightweight half only

Commit `e92cc0f` (bead `VieNeuTTSApp-nqx.9.1`, closed). Build on it, do not re-litigate:

- **Frozen re-dispatch** (`core/qwen_engine.py`, `__main__.py`): `host_command()` returns
  `[sys.executable, "-m", HOST_MODULE]` in a source checkout and `[sys.executable, HOST_FLAG]`
  (`--qwen-host`) when `sys.frozen` — a frozen build has no second interpreter to hand a module name
  to. `main()` routes the flag to `workers.qwen_host.main()` **before** the GUI import and before
  `ensure_windowed_stdio()` (stdout is the frame channel; the windowed-exe stdio net must not touch
  it). `is_frozen()`, `HOST_FLAG` and `RUNTIME_ENV` are public in `core/qwen_engine.py`.
- **Host import path** (`workers/qwen_host.py`): `configure_import_path()` inserts
  `$VIENETTS_QWEN_RUNTIME` at `sys.path[0]` before the first heavy import, because PyInstaller's
  importer ignores `PYTHONPATH`; `host_environment()` sets that variable alongside the existing
  PYTHONPATH entry. The host's heavy imports stay deferred (`torch`, `qwen_tts`) so nothing of the
  stack is bundled.
- **Windowless Windows**: `core/qwen_engine.IS_WINDOWS` is the (patchable) platform seam for
  `CREATE_NO_WINDOW` in `_spawn`; the spec's `console=False` is the other half. Do not "simplify" it
  back to an inline `os.name` check — tests cannot patch `os.name` without breaking `pathlib`.
- **Spec**: excludes = `torch`, `torchaudio`, `transformers`, `qwen_tts`, `accelerate`, `einops`,
  `safetensors`, `sox`; `vienetts_app.workers.qwen_host` + `vienetts_app.core.qwen_protocol` are
  explicit hidden imports; the QML/asset trees stay inside `vienetts_app/`. `tests/unit/test_package.py`
  DERIVES the exclude list from the host's deferred imports, so a new heavy import fails the suite
  until the spec excludes it.
- **Release pipeline**: two post-build steps — "Assert the bundle carries no Qwen/PyTorch stack"
  (find over `dist/`, fails loudly if `dist/` is missing) and "Assert the frozen host re-dispatch"
  (runs `<binary> --qwen-host` with closed stdin: exit 0, `starting` log, hello frame read through
  the real protocol reader). Neither needs a runtime or weights, so both run in all three jobs.

Verification seams to reuse: `tests/unit/test_package.py` (frozen command, CLI routing, runtime env,
a real host start from a path with spaces + Vietnamese characters, the windowless spawn flags, the
spec/workflow contracts), `tests/smoke/test_main_cli.py::TestQwenHostEntry` (the CLI end to end from
a non-ASCII cwd, asserting one hello frame, `starting`/`peer_closed`, and no PySide6 import), and
`tests/unit/test_linux_packaging.py::TestReleaseWorkflowLinuxLayout` (integration staged before the
zip; install-script/matrix path agreement).

## Landed: Phase 7 Task 7.2 — deterministic fake-host end-to-end coverage

Commit `328baf5` (bead `VieNeuTTSApp-nqx.9.2`, closed). Build on it, do not re-litigate:

- **`tests/smoke/test_e2e_flows.py`** — the Qwen harness lives inside the shared driver and reuses
  its conventions (one scenario per `run_driver` call, workspace `tmp_path/<scenario>`, `RESULT:<json>`
  on stdout, repo root appended to `sys.path` by the driver itself, so the suite needs no
  `PYTHONPATH`). Injectable seams: `qwen_model_manager_factory(data_dir, key)` and
  `qwen_runtime_manager_factory(data_dir)` return ready fakes rooted like the real managers
  (`<data>/qwen/{models,runtime}`); `qwen_engine_factory(**kwargs)` spawns
  `tests/unit/qwen_host_fake.py` once per engine build, with the mode read from a mutable
  `qwen_mode["value"]` and a per-build frame log (`qwen_host_<n>_<mode>.log`); `hardware_probe` is
  pinned to CPU; `worker_factory(eng, providers=None)` takes the provider set when one exists.
- **Two scenarios:** `qwen_e2e` (ready install → profile switch via `engineProfileCombo`/badge/device
  → CustomVoice QML-click synthesis with language `zh` → replay/export → fixed-speaker enrollment
  refusal → Base consent + enrollment + clone synthesis → Studio provenance guard incl. the banner
  switch → Studio gain/preview/export → shutdown + restart persistence) and `qwen_recovery_e2e`
  (cancel → switch refused mid-job → `crash_after_pcm` host crash → reaping → fresh-host recovery →
  shutdown). Tests: `TestQwenProfilesE2E`, `TestQwenRecoveryE2E`.
- **`tests/smoke/test_ui_tabs.py` needed no change** — 6.1–6.4 already assert the Qwen install cards,
  the profile/language controls, the cloning notice and the Studio banner at stub level. Do not
  duplicate those assertions in the e2e scenarios.
- **Real bug fixed in `src/vienetts_app/core/qwen_engine.py`:** `_write_frame_safely()` now guards
  `QwenEngine._send`. `_restore_default_sigpipe()` sets `SIGPIPE` to `SIG_DFL` process-wide when the
  app's stdout is a pipe, so the cleanup `cancel` frame written to a host that had died mid-job
  raised EPIPE → kernel SIGPIPE → the app died (driver exit `-13`; pytest captures stdout with a pipe,
  which is why the smoke suite sees it and a file-backed stdout never did). The guard blocks SIGPIPE
  for the writing thread and consumes the pending signal, so the caller gets the actionable
  `QwenEngineError`. POSIX-only (`HAS_SIGPIPE`).
- Regression test: `tests/unit/test_qwen_engine.py::TestInferStream::test_a_dead_host_pipe_never_kills_the_process`
  (sets `SIG_DFL` explicitly; was a process kill before the fix).

Assertion gotchas: the fake host logs **only frames it received**, so assert the ordered received
list per build; a crashed host's pid still answers `os.kill(pid, 0)` (zombie) until the engine
`close()` reaps it, so check reaping after the close.

## Next: Phase 7 Task 7.3 — opt-in real-model release validation

7.3 is now unblocked (it waited for 7.1 + 7.2). Deliverables per the plan: `.github/workflows/qwen-runtime-smoke.yml`,
`scripts/check_smoke_wav.py`, `docs/performance/qwen-runtime-compatibility.md` — consume
pre-provisioned verified packs and validate 48 kHz WAV output on Windows CPU/CUDA, Linux CPU/CUDA and
Apple Silicon CPU/MPS; record TTFR, total time, RTF, peak memory, cancellation latency and host
restart; ordinary CI downloads nothing. Then 7.4 (final gates, docs, track close) and the Phase 7
manual checkpoint (`nqx.9.5`), which the user must approve — present it as a checklist, never close it
yourself.

## The capability seams Phase 7 builds on

Everything below is in place from Phases 1–6; read `EngineState.qml` and
`core/engine_profiles.py`'s capability table before adding another branch:

- The capability table is the single source of truth (`clone_requirements`,
  `supports_cloning`, `supports_language`, `runtime`); `EngineState.qml` is its one UI derivation
  (voices/language/readiness/cloning), and the controller's `submission_context_for()` is the one
  submission gate.
- `AppController` surfaces: `engineProfiles`/`engineProfile`/`switchEngineProfile(id)`,
  `profileVoices`/`profileClones`/`profileLanguages` + `profileCatalogChanged`,
  `synthesisLanguage`/`setSynthesisLanguage(code)`, the Qwen runtime/model install slots and their
  status/error strings, `addVoice(name, clip, denoise[, transcript])`, `studioRegenProfile*` and
  `studioSwitchToRegenProfile()`, and the Studio clip `profile`/`profileLabel`/`language` rows.
- The Qwen owner assembly is `_build_qwen_engine()` (verified install locations only, actionable
  refusals) + `_providers_for()` (one-provider `EngineProviders`, `None` = the VieNeu single-engine
  default), with the injectable `qwen_engine_factory` seam for tests.
- The English catalog is now self-checking: any new copy must be translated and the `.qm` recompiled
  in the same commit (`scripts/update_i18n.sh` runs `lupdate -noobsolete`; four completeness gates
  live in `tests/unit/test_i18n.py`).

The controller surface those QML files bind is already in place from Phases 1–5:
`engineProfiles` / `engineProfile` / `switchEngineProfile(id)` / `engineDevice`,
`profileModel*` + `profileRuntime*` readiness, `synthesisLanguage` / `setSynthesisLanguage(code)`,
the install/import/cancel/repair/remove slots and their status/error strings, `voices` filtered per
profile, and the Studio `profile`/`profileLabel`/`language` clip rows plus
`studioRegenProfile` / `studioRegenProfileLabel` / `studioSwitchToRegenProfile()` (6.3 binds them).

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
  2026-09-21), Phase 6 tasks are `.8.x` (`.8.1`–`.8.5` all closed, the checkpoint approved
  2026-09-21), Phase 7 tasks are `.9.x` (`.9.1` and `.9.2` closed 2026-09-21; `.9.3` is next, `.9.4`
  the final gate, `.9.5` the phase checkpoint). Note: `bd ready` does
  not list a task whose parent phase bead is still open (parent-child blocks) — that is the
  established pattern, so do not close a phase bead early.
  `conductor/tracks/qwen_multiengine_20260920/metadata.json` carries the corrected
  phase→beads mapping.
- Do **not** push, pull, or run `bd dolt push` without an explicit request.