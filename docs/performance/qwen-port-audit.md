# Qwen Port Audit — `origin/feature/qwen-support` → current `main`

**Track:** `qwen_multiengine_20260920` · **Task:** 0.1 · **Date:** 2026-09-20
**Verdict:** reference-only. Nothing from the branch is merged, cherry-picked or
copied wholesale. Individual pure contracts and tests are re-implemented in this
track's own architecture (subprocess model host, managed installs, tagged jobs).

## 1. Method and evidence

| Item | Value |
|------|-------|
| Merge base (`main` ∩ branch) | `a52d12f807638cb22e4c8f83feae406a4bde33fd` |
| Audited range | `279574d~1..f154725` (26 commits) |
| Branch tip | range end (§2 row 26) |
| Files changed vs merge base | 70 (`A` 45, `M` 25, `D` 0) |
| Diff volume | 8 280 insertions / 425 deletions |

**Coverage rule (mechanically checkable):** every commit in the audited range
appears exactly once as a row key in §2, and every changed file appears exactly
once as a row key in §3. References to other rows inside a decision cell are not
row keys.

Evidence commands (re-runnable):

```bash
git merge-base main origin/feature/qwen-support
git log --oneline --reverse 279574d~1..f154725
git diff --name-status a52d12f f154725
git show f154725:<path>            # per-file reference read
```

Verdicts used below: **port** (logic is architecture-independent and moves with
light edits), **adapt** (idea/contract is kept, implementation is rewritten for
the current architecture), **reject** (superseded, stale, or unsafe — with the
reason recorded so it is not re-attempted).

The branch predates: the immutable tagged-job pipeline (`core/jobs.py`,
`workers/job_queue.py`), artifact-first writing (`core/artifacts.py`,
`core/pcm_transport.py`), the managed installer primitives
(`core/model_manager.py`, `core/cuda_runtime.py`), the Subtitle/Studio/batch
controllers, and the consolidated test layout. Every worker/controller/QML
change in the branch targets APIs that no longer exist in that shape.

## 2. Commit matrix (26/26)

| # | Commit | Subject | Verdict | Decision and target |
|---|--------|---------|---------|---------------------|
| 1 | `279574d` | backend capability model with engine IDs and validation | adapt | Keep immutable capability descriptors + actionable validation messages. Rewrite in `core/engine_profiles.py` (Task 1.1): canonical IDs `vieneu`/`qwen_custom_0_6b`/`qwen_base_0_6b`, model-reported languages incl. `Auto`, pinned revision, device/runtime availability, streaming granularity. Drop `recommend_engine` (spec forbids engine guessing) and the `supports_instruction` flag (0.6B ignores `instruct`). |
| 2 | `0cc4c56` | engine selection on requests/settings + backend streaming contract | adapt | Keep the settings-migration posture (old JSON → VieNeu, no field loss) in `core/settings.py` (Task 1.1) and the "request snapshots its engine" idea as `core/synthesis_context.py` (Task 1.2). The in-process `TtsBackend` registry is replaced by the engine-provider protocol over the model host (Task 3.4). |
| 3 | `52ba03d` | stateful streaming resampler to 48 kHz | port | Pure NumPy, no stale dependencies. Re-home inside `workers/qwen_host.py` (Task 3.2) — resampling now happens in the host, which emits bounded 48 kHz frames — with its parity tests in `tests/unit/test_qwen_host.py`. Keep the carry-state contract and the `floor((N-1)*dst/src)+1` length assertion. |
| 4 | `8dfd9a7` | route worker synthesis through normalized backend chunks | adapt | Keep "one validated normalized path; every chunk finite mono float32" — already satisfied by `IncrementalArtifactWriter`/`BoundedPcmTransport` on `main`. The worker-side backend switch itself is superseded by provider selection from the immutable context (Task 3.4). |
| 5 | `706111f` | CJK-aware text segmentation with faithful joining | port | Pure text logic. Move to `core/text_segmentation.py` as `split_text_for_profile(text, language, max_chars)` (Task 3.4). Keep: unconditional CJK-terminator split, ASCII `.?!` whitespace rule, separator belongs to the previous unit, clause-preferring hard split. |
| 6 | `888bbff` | model revision + chapter render identity in cache keys | adapt | Keep "identity sidecar; invalidate only incompatible renders; legacy-without-sidecar stays fresh" in `core/audiobook.py` (Task 5.3), re-keyed on `SynthesisContext.fingerprint_payload()` (profile, revision, language, voice/clone, generation). |
| 7 | `c5796b2` | optional Qwen runtime boundary with lazy imports | reject | An in-process import boundary cannot isolate the `transformers==4.57.3` vs `4.57.6` conflict and cannot contain a model-host crash. Replaced by the framed subprocess host (Tasks 3.1–3.3) plus the checksum-pinned managed runtime (Task 2.2). Only the discipline survives: never import torch/qwen at module load; report absence as data, not exceptions. |
| 8 | `8940234` | Qwen CustomVoice/Base backends with injectable loader | adapt | Salvage the verified official API knowledge: `generate_custom_voice(text, language, speaker, instruct)`, `generate_voice_clone(text, language, ref_audio, ref_text)` → `(wavs, sr)`, `LANGUAGE_NAMES` (10 codes), sample-rate drift check, injectable loader for torch-free tests. Re-implement in `workers/qwen_host.py` + `core/qwen_engine.py` (Tasks 3.2–3.3) with local-path-only loading, `trust_remote_code=False`, and no `instruct` parameter. The 12 000-sample whole-utterance slicing is dropped — segmentation happens before generation. |
| 9 | `f97401b` | Qwen model install/readiness via HF cache probe | reject | `snapshot_download(local_files_only=True)` cannot satisfy the spec: no pinned revision/file manifest, no per-file size+SHA-256 verification, no staging-only extraction/atomic promotion, no repair, no free-space preflight, no offline-pack import. Replaced by `core/qwen_model_manifest.py` + `core/qwen_model_manager.py` (Task 2.3). |
| 10 | `bd5384d` | worker-owned backend switch lifecycle | adapt | Keep "refuse switching while busy; shut the current owner down before loading another; never two large models resident" in `ui/controller.py` (Task 5.1) and the host lifecycle (Task 3.3). Reject in-process residency and the worker-owned backend registry. |
| 11 | `05f87d2` | generic voice descriptors, Qwen catalog, speaker validation | adapt | Keep the 9-speaker table with native languages, the region-parsing move out of the controller, and speaker validation at the door. Re-home descriptors as immutable `VoiceOption`/`LanguageOption` capability data (Task 1.1) and validate at job admission (Tasks 4.2/5.2). |
| 12 | `6a554ee` | Qwen Base reference-voice cloning adapter | adapt | Keep: 3–8 s enrollment window, mandatory reference transcript, explicit consent gate, engine-isolated store, atomic part-file writes, corrupt-store errors. Re-implement in `core/voice_profiles.py` (Task 4.1) with a stable clone ID, content hash, timestamps, and an app-owned reference copy — and no name-keyed directories. |
| 13 | `ddd6167` | engine selection/recommendation/validation UI | reject | Targets the pre-Studio SettingsTab/CloningTab and an auto-recommendation model the spec forbids. Replaced by `EngineProfilePicker.qml`/`LanguagePicker.qml` + separate Settings cards (Task 6.1). Keep only the controller discipline: real NOTIFY properties, and every new QProperty mirrored in the smoke fake. |
| 14 | `e1ee2d9` | wire Qwen interactive synthesis, audition, Base enrollment | adapt | Keep submit-time capability validation and per-job engine tagging; re-implement through the immutable `SynthesisContext` and the shared listener seam (Tasks 4.2/5.1/5.2). Reject the engine-keyed audition cache filename (must key on profile + model revision) and the in-process backend registry. |
| 15 | `5fd522e` | route all surfaces through one backend contract + engine-aware audiobook cache | adapt | Keep "every surface goes through one seam" and listener routing (already the `main` pattern). Adapt the audiobook cache identity to `SynthesisContext` (Task 5.3). Reject the shipped `docs/qwen-setup.md` + HF-cache fetch script as the install story (Task 2.2/2.3 replace it). |
| 16 | `6127225` | opt-in real-model smoke with observation records | adapt | Keep env-gated, skip-clean, structural-only assertions and the observation fields (TTFR, total, RTF, peak RSS/VRAM, cancellation, shutdown). Re-implement as `.github/workflows/qwen-runtime-smoke.yml` + `scripts/check_smoke_wav.py` (Task 7.3) over pre-provisioned verified packs on the three-platform matrix. |
| 17 | `86cb9f1` | tolerate broken torch stubs in backend close | reject | No in-process torch exists in the new architecture (the host owns it), so the fix has no code target. The lesson is retained as a pattern: optional-dependency touchpoints degrade on `Exception`, not `ImportError`. |
| 18 | `df97e5b` | expect engine-prefixed audition cache filename | reject | Test for a removed artifact shape. Superseded by the profile+revision-keyed audition cache assertions in Task 5.1. |
| 19 | `12bc460` | close prior Phase 5 with manual verification | reject | Track bookkeeping for the archived track. Its learnings were read as input to this audit; no code target. |
| 20 | `4be8fb4` | archive `qwen-multiengine_20260908` | reject | Bookkeeping. The archived `spec.md`/`plan.md`/`learnings.md` on the branch are reference inputs only (superseded by this track's spec). |
| 21 | `a3895a7` | sync beads issue exports | reject | Bookkeeping; superseded by epic `VieNeuTTSApp-nqx`. |
| 22 | `330bd66` | conductor refresh: sync context with codebase | reject | Bookkeeping; stale against current `main` (Studio/Subtitle/managed-CUDA era). |
| 23 | `1879288` | fix synthesis-language dropdown blanks / English leftovers | adapt | Keep the lessons (native language names in the picker; disambiguated catalog copy; English UI must not show Vietnamese catalog metadata). Re-implement in `LanguagePicker.qml` + catalogs (Tasks 6.2/6.4) against the current SettingsTab. |
| 24 | `e85bbbd` | show native language names in the language dropdown | adapt | Same target as #23 — folds into `LanguagePicker.qml` (Task 6.2). |
| 25 | `9fbad9b` | Qwen Yes/No setup wizard on Settings card | reject | Interactive wizard + copyable `pip install` guidance contradicts the spec (managed runtime/model cards with install/import/cancel/repair/remove, no shell guidance). Replaced by Task 6.1. |
| 26 | `f154725` | redesign adaptive setup wizard + overhaul setup guide | reject | Same as #25. Salvage only: the "explain CPU slowness before download/use" requirement (already spec'd as NFR) and the prose in `docs/qwen-setup.md` as raw material for release-facing setup text (Task 7.4). |

## 3. File matrix (70/70)

`A` = added on the branch, `M` = modified vs merge base.

### 3.1 Core code

| File | St. | Verdict | Reason → target |
|------|-----|---------|-----------------|
| `src/vienetts_app/core/backends.py` | A | adapt | Capability data model is sound; IDs, language source (model-reported + `Auto`), instruction capability and auto-recommendation are not. → `core/engine_profiles.py` (Task 1.1) |
| `src/vienetts_app/core/tts_backend.py` | A | reject | In-process backend ABC/registry assumes same-process torch and a worker-owned switch; the new contract is the host provider protocol. → `core/engine.py` provider protocol (Task 3.4) |
| `src/vienetts_app/core/resample.py` | A | port | Pure NumPy stateful resampler; only its home changes. → `workers/qwen_host.py` (Task 3.2) |
| `src/vienetts_app/core/voices.py` | A | adapt | Speaker table + validation kept; descriptors become immutable capability options. → `core/engine_profiles.py` (Task 1.1), `core/voice_profiles.py` (Task 4.1) |
| `src/vienetts_app/core/qwen_backend.py` | A | adapt | Official API knowledge kept; same-process load, `instruct` and post-hoc slicing rejected. → `workers/qwen_host.py`, `core/qwen_engine.py` (Tasks 3.2–3.3) |
| `src/vienetts_app/core/qwen_runtime.py` | A | reject | Lazy in-process import boundary + HF-cache probes; superseded. The module *name* is reused by Task 2.2 for the managed-runtime manager (different content, different contract). |
| `src/vienetts_app/core/qwen_models.py` | A | reject | HF-cache-based install cannot verify integrity, resume atomically, repair, or import offline packs. → `core/qwen_model_manifest.py` + `core/qwen_model_manager.py` (Task 2.3) |
| `src/vienetts_app/core/qwen_voices.py` | A | adapt | Enrollment rules (3–8 s, transcript, consent, isolation, atomic writes) kept; store schema replaced by stable IDs + content hash + timestamps. → `core/voice_profiles.py` (Task 4.1) |
| `src/vienetts_app/core/engine_selection.py` | A | adapt | Strict pre-job validation and labels kept; recommendation removed and display strings move to the capability model + i18n. → `core/engine_profiles.py` (Task 1.1), `ui/controller.py` (Task 5.1) |
| `src/vienetts_app/core/engine.py` | M | port | CJK-aware boundary detection and faithful joining are architecture-independent. → `core/text_segmentation.py` (Task 3.4) |
| `src/vienetts_app/core/models.py` | M | adapt | Keep validated request-side engine/language fields; re-shape as immutable `SynthesisContext` on the job. → `core/synthesis_context.py` (Task 1.2) |
| `src/vienetts_app/core/settings.py` | M | adapt | Keep the two-pass clamp/migration discipline; add `engine_profile` + `qwen_device`. → `core/settings.py` (Task 1.1) |
| `src/vienetts_app/core/audiobook.py` | M | adapt | Keep the render-identity sidecar design; re-key on `SynthesisContext`. → `core/audiobook.py` (Task 5.3) |
| `src/vienetts_app/workers/inference_worker.py` | M | adapt | Keep one-worker ownership, cancel discipline, per-request engine tagging; the backend switch is replaced by provider selection. → `workers/inference_worker.py` (Tasks 3.4, 4.2) |
| `src/vienetts_app/app.py` | M | adapt | Keep "register/bind the optional engine at bootstrap, not at import"; the host is now started lazily on first Qwen job. → `app.py` (Task 5.1) |
| `src/vienetts_app/ui/controller.py` | M | adapt | Keep guarded switching and truthful readiness properties; rewrite against the current controller (Studio/Subtitle/batch). → `ui/controller.py` (Tasks 5.1–5.4) |
| `src/vienetts_app/ui/audiobook_controller.py` | M | adapt | Keep engine-aware freshness; re-key on the synthesis context fingerprint. → `ui/audiobook_controller.py` (Task 5.3) |
| `pyproject.toml` | M | reject | The `qwen` extra pulled Qwen/PyTorch into the base lock; the spec forbids runtime packages in the root project. → `packaging/qwen-runtime-requirements.json` (Task 2.2) |

### 3.2 UI, QML, i18n

| File | St. | Verdict | Reason → target |
|------|-----|---------|-----------------|
| `src/vienetts_app/ui/qml/QwenSetupWizard.qml` | A | reject | Multi-step wizard with shell commands; replaced by managed Settings cards (install/import/cancel/repair/remove). → `SettingsTab.qml` (Task 6.1) |
| `src/vienetts_app/ui/qml/QwenStatusList.qml` | A | reject | Status list bound to HF-cache probing; readiness now comes from runtime/model managers. → `SettingsTab.qml` (Task 6.1) |
| `src/vienetts_app/ui/qml/SettingsTab.qml` | M | reject | Additions target the pre-refactor Settings card layout (current `main` already split Engine/CUDA/Model cards). → new cards in `SettingsTab.qml` (Task 6.1) |
| `src/vienetts_app/ui/qml/CloningTab.qml` | M | reject | Capability gate targets the old cloning surface; the new surface must also require a transcript for Base. → `CloningTab.qml` (Task 6.3) |
| `src/vienetts_app/ui/qml/components/AppCombo.qml` | M | adapt | The native-language-name fix is a real UX lesson; re-apply to the new `LanguagePicker.qml`. → `components/LanguagePicker.qml` (Task 6.2) |
| `src/vienetts_app/ui/qml/qmldir` | M | reject | Registers removed components; new components register in Task 6.1. |
| `src/vienetts_app/ui/i18n/vienetts_en.ts` | M | reject | Catalog edits belong to strings that no longer exist; Task 6.4 translates the new strings and recompiles. |
| `src/vienetts_app/ui/i18n/vienetts_en.qm` | M | reject | Compiled artifact of the rejected catalog; regenerated in Task 6.4. |

### 3.3 Scripts and docs

| File | St. | Verdict | Reason → target |
|------|-----|---------|-----------------|
| `scripts/fetch_qwen_models.py` | A | adapt | Keep a maintainer/offline fetch entry point, but drive it from the pinned manifest (revision, per-file size+SHA-256, staging/atomic promotion) instead of an unpinned `snapshot_download`. → `scripts/fetch_qwen_models.py` (Task 2.3) |
| `docs/qwen-setup.md` | A | reject | Documents `pip install` + HF-cache setup that the managed installer replaces; prose retained as source material for release-facing setup text. → `docs/performance/qwen-runtime-compatibility.md` (Tasks 0.3, 7.4) |
| `docs/superpowers/plans/2026-09-08-qwen-setup-wizard-redesign.md` | A | reject | Plan for the rejected wizard; no code target. |
| `docs/superpowers/specs/2026-09-08-qwen-setup-wizard-redesign.md` | A | reject | Spec for the rejected wizard; no code target. |
| `README.md` | M | reject | Setup prose for the rejected install story; release-facing text is rewritten in Task 7.4. |

### 3.4 Tests

| File | St. | Verdict | Reason → target |
|------|-----|---------|-----------------|
| `tests/unit/test_backends.py` | A | adapt | Capability assertions are valuable; rewritten against the new IDs/fields and model-reported language list. → `tests/unit/test_engine_profiles.py` (Task 1.1) |
| `tests/unit/test_tts_backend.py` | A | reject | Tests the removed in-process ABC/registry; replaced by provider-protocol coverage. → `tests/unit/test_engine.py` (Task 3.4) |
| `tests/unit/test_resample.py` | A | port | Resampler math, carry-state and length assertions carry over verbatim in intent. → `tests/unit/test_qwen_host.py` (Task 3.2) |
| `tests/unit/test_segmentation_cjk.py` | A | port | CJK split/join cases carry over. → `tests/unit/test_text_segmentation.py` (Task 3.4) |
| `tests/unit/test_cache_identity.py` | A | adapt | Chapter-identity scenarios kept, re-keyed on the synthesis-context fingerprint. → `tests/unit/test_audiobook.py` (Task 5.3) |
| `tests/unit/test_qwen_backend.py` | A | adapt | Speaker validation + API-shape cases kept, re-targeted at the host. → `tests/unit/test_qwen_host.py` (Task 3.2) |
| `tests/unit/test_qwen_models.py` | A | reject | HF-cache install tests; replaced by manifest/manager coverage. → `tests/unit/test_qwen_model_manager.py` (Task 2.3) |
| `tests/unit/test_qwen_runtime.py` | A | reject | In-process import-boundary tests; replaced by managed-runtime tests. → `tests/unit/test_qwen_runtime_manager.py` (Task 2.2) |
| `tests/unit/test_qwen_voices.py` | A | adapt | Enrollment validation/consent/isolation cases kept; store-schema cases rewritten. → `tests/unit/test_voice_profiles.py` (Task 4.1) |
| `tests/unit/test_voices.py` | A | adapt | Catalog/region/sort cases kept, re-homed on capability options. → `tests/unit/test_engine_profiles.py` (Task 1.1) |
| `tests/unit/test_engine_selection.py` | A | adapt | Pre-job validation cases kept (minus recommendation). → `tests/unit/test_engine_profiles.py`, `tests/unit/test_controller.py` (Tasks 1.1, 5.1) |
| `tests/unit/test_normalized_stream.py` | A | adapt | Chunk validation/normalization cases kept as worker/provider parity. → `tests/unit/test_inference_worker.py` (Task 3.4) |
| `tests/unit/test_backend_switch.py` | A | adapt | Switch-refusal and single-owner cases kept against the new lifecycle. → `tests/unit/test_controller.py` (Task 5.1) |
| `tests/unit/test_controller_engine.py` | A | adapt | Controller engine-state cases kept, rewritten against the current controller. → `tests/unit/test_controller.py` (Task 5.1) |
| `tests/unit/test_qwen_interactive.py` | A | adapt | Interactive/audition/enrollment scenarios kept as end-to-end intent. → `tests/unit/test_controller.py`, `tests/unit/test_qwen_engine.py` (Tasks 3.3, 5.1–5.2) |
| `tests/unit/test_audiobook_engine.py` | A | adapt | Engine-aware cache freshness cases kept. → `tests/unit/test_audiobook.py` (Task 5.3) |
| `tests/unit/test_controller.py` | M | reject | Edits pin the removed audition-cache filename. |
| `tests/unit/test_inference_worker.py` | M | adapt | Engine-tagging assertions carry into the provider-selection coverage. → `tests/unit/test_inference_worker.py` (Task 3.4) |
| `tests/unit/test_models.py` | M | adapt | Request-shape assertions carry into `SynthesisContext` coverage. → `tests/unit/test_synthesis_context.py` (Task 1.2) |
| `tests/unit/test_settings.py` | M | adapt | Migration round-trip cases carry over with the new fields. → `tests/unit/test_settings.py` (Task 1.1) |
| `tests/unit/test_app_entry.py` | M | reject | Asserts bootstrap registration of the removed in-process backends. |
| `tests/unit/test_audiobook_controller.py` | M | reject | Targets the pre-Subtitle audiobook controller shape. |
| `tests/unit/test_theme.py` | M | reject | Asserts the removed wizard/catalog strings. |
| `tests/smoke/test_qwen_realmodel.py` | A | adapt | Opt-in/env-gated structure and observation fields kept; re-implemented over verified packs + 48 kHz WAV check. → `.github/workflows/qwen-runtime-smoke.yml`, `scripts/check_smoke_wav.py` (Task 7.3) |
| `tests/smoke/test_ui_tabs.py` | M | reject | Wizard scenarios target removed QML object names; new scenarios land in Tasks 6.1–6.4/7.2. |
| `tests/smoke/test_ui_shell.py` | M | reject | Same as above. |
| `tests/smoke/test_main_cli.py` | M | reject | Asserts the removed backend registration path. |

### 3.5 Track bookkeeping (no code target)

| File | St. | Verdict | Reason |
|------|-----|---------|--------|
| `conductor/archive/qwen-multiengine_20260908/spec.md` | A | reject | Superseded spec; read as audit input only. |
| `conductor/archive/qwen-multiengine_20260908/plan.md` | A | reject | Superseded plan; read as audit input only. |
| `conductor/archive/qwen-multiengine_20260908/learnings.md` | A | reject | Superseded track notes; their durable lessons are folded into this track's `learnings.md`. |
| `conductor/archive/qwen-multiengine_20260908/metadata.json` | A | reject | Prior track metadata. |
| `conductor/archive/qwen-multiengine_20260908/implement_state.json` | A | reject | Prior track state. |
| `conductor/tracks.md` | M | reject | Prior track registration. |
| `conductor/patterns.md` | M | reject | Stale pattern snapshot; current `patterns.md` is newer. |
| `conductor/product.md` | M | reject | Stale product snapshot. |
| `conductor/tech-stack.md` | M | reject | Stale stack snapshot (still described the in-process Qwen extra). |
| `conductor/refresh_state.json` | M | reject | Stale refresh bookkeeping. |
| `.beads/issues.jsonl` | M | reject | Passive export for the prior epic; new work is tracked under `VieNeuTTSApp-nqx`. |
| `.beads/interactions.jsonl` | M | reject | Same as above. |

## 4. Stale assumptions explicitly rejected

1. **Same-process dependency loading.** `qwen-tts==0.1.1` pins
   `transformers==4.57.3` while the app pins `4.57.6`; a lazy import boundary
   cannot resolve that, and an in-process model crash takes the GUI down.
2. **Instruction/style prompting on 0.6B.** The 0.6B CustomVoice implementation
   ignores `instruct`; the branch exposed it as a real control.
3. **Unmanaged package and model setup.** `pip install "vienetts-app[qwen]"` and
   unpinned HF snapshots give no integrity verification, resume, repair,
   offline-pack import, free-space preflight, or atomic promotion.
4. **Pre-v0.1.16 UI/test structure.** SettingsTab, CloningTab, controller and
   smoke drivers have all been re-architected (Studio deck, Subtitle studio,
   batch queue, consolidated smoke drivers); branch QML edits do not apply.
5. **Worker-owned backend switching with resident models.** Superseded by one
   global profile with a single model owner and a restartable subprocess host.
6. **Engine recommendation / silent routing.** The spec requires an explicit
   global profile that is never guessed from text or language.
7. **Whole-utterance generation with post-hoc slicing.** Segment-bounded
   generation is required for memory bounds, progress and artifact-first
   output; the host generates one bounded segment at a time.

## 5. Salvage summary carried into Phase 1+

- **Contracts:** capability descriptors, pre-job validation with actionable
  messages, request/job-snapshotted engine identity, settings migration.
- **Pure logic:** stateful 24→48 kHz resampler, CJK-aware segmentation.
- **Cache identity:** render-identity sidecars with legacy-tolerant freshness.
- **Qwen domain knowledge:** official API signatures, 10-language mapping,
  9-speaker catalog with native languages, 24 kHz native rate + drift check,
  3–8 s reference-clip window, mandatory transcript and consent gate.
- **Discipline:** torch-free tests via injectable loaders, optional-dependency
  degradation on `Exception`, opt-in real-model smoke with observation records.