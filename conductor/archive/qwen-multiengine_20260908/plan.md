# Implementation Plan: Qwen Multiengine TTS Support

## Phase 1: Contracts and capability model

- [x] Task 1: Add engine/profile identifiers and immutable capability descriptors. (279574d)
  - Define stable IDs for VieNeu, Qwen CustomVoice, and Qwen Base.
  - Define supported languages, cloning, preset voice, instruction, runtime, and native-rate metadata.
  - Add TDD coverage for capability lookup and compatibility validation.
- [x] Task 2: Extend synthesis requests and settings with language, instruction, engine, and voice-source metadata. (0cc4c56)
  - Preserve backward-compatible VieNeu defaults.
  - Validate persisted settings on restore.
  - Add tests for defaults, invalid combinations, and cache-relevant fields.
- [x] Task 3: Define the backend-neutral streaming contract and factory. (0cc4c56)
  - Adapt current VieNeu implementation behind the contract without changing behavior.
  - Keep worker ownership and lifecycle semantics unchanged.
  - Add fake-backend contract tests.
- [x] Task 4: Conductor - User Manual Verification 'Contracts and capability model' (approved 2026-09-08)
## Phase 2: Audio normalization and shared worker path

- [x] Task 1: Implement stateful Qwen-native-rate to 48 kHz audio normalization. (52ba03d)
  - Cover chunk boundaries, duration preservation, mono float32 output, cancellation, and invalid input.
- [x] Task 2: Route normalized backend chunks through artifact writing and bounded live transport. (8dfd9a7)
  - Preserve existing 48 kHz artifact and playback invariants.
  - Add tests for streaming and artifact metadata.
- [x] Task 3: Make text segmentation language-aware for CJK punctuation and joining. (706111f)
  - Preserve existing Vietnamese/English behavior and memory caps.
  - Add tests for Chinese/Korean/English boundary cases.
- [x] Task 4: Update cache identity to include backend/profile/model revision/language/instruction/voice source. (888bbff)
  - Add collision-prevention tests for engines, speakers, instructions, and revisions.
- [x] Task 5: Conductor - User Manual Verification 'Audio normalization and shared worker path' (approved 2026-09-08)

## Phase 3: Qwen runtime and model management

- [x] Task 1: Add optional Qwen runtime dependency and lazy import boundary. (c5796b2)
  - Keep the default VieNeu installation torch-free.
  - Add actionable missing-runtime diagnostics.
- [x] Task 2: Implement Qwen model profiles and loaders for CustomVoice and Base. (8940234)
  - Use official `qwen-tts` API.
  - Support CUDA when available and explicit CPU fallback.
  - Add injectable loaders for tests.
- [x] Task 3: Add separate Qwen model install/readiness state. (f97401b)
  - Support on-demand download, offline cache, disk-space preflight, progress, cancellation, and retry.
  - Do not reuse the VieNeu file manifest for Qwen repositories.
- [x] Task 4: Implement backend-switch lifecycle and resource cleanup. (bd5384d)
  - Stop active work, close current backend on its owner thread, release references, then initialize the new backend.
  - Add lifecycle and failure-path tests.
- [x] Task 5: Conductor - User Manual Verification 'Qwen runtime and model management' (approved 2026-09-08)
## Phase 4: Voices, cloning, and interactive UI

- [x] Task 1: Introduce generic voice descriptors and capability-aware voice catalogs. (05f87d2)
  - Preserve VieNeu regional grouping.
  - Add Qwen speaker metadata and language filtering.
- [x] Task 2: Implement CustomVoice speaker/instruction request mapping. (05f87d2)
  - Hide cloning controls for CustomVoice.
  - Add English, Chinese, and Korean fake-backend coverage.
- [x] Task 3: Implement Qwen Base reference-audio cloning adapter. (6a554ee)
  - Reuse compatible clip validation.
  - Persist engine-isolated references and enforce consent.
  - Add invalid/missing reference tests.
- [x] Task 4: Add explicit engine/language selection, recommendation, and targeted validation UI. (ddd6167)
  - Persist valid selections.
  - Show capability-aware controls and runtime/model readiness.
- [x] Task 5: Add interactive text synthesis, playback, export, and audition integration. (e1ee2d9)
  - Validate CustomVoice fixed-speaker audition and Base reference audition semantics.
- [x] Task 6: Conductor - User Manual Verification 'Voices, cloning, and interactive UI' (approved 2026-09-08)

## Phase 5: Documents, batch, audiobook, and packaging

- [x] Task 1: Route paragraph, imported-file, SRT, and batch flows through the common backend contract. (5fd522e)
- [x] Task 2: Integrate backend selection and cache identity into audiobook chapter rendering/resume. (5fd522e)
- [x] Task 3: Add optional Qwen runtime/model setup to settings and packaged deployment paths. (5fd522e)
- [x] Task 4: Add representative opt-in real-model smoke coverage and benchmark records. (6127225)
  - Measure load time, first chunk, RSS, and CPU/GPU behavior separately.
  - Do not assert machine-specific exact timings.
- [x] Task 5: Run full quality gates and audit existing VieNeu regressions. (86cb9f1, df97e5b)
  - 1089 passed, 17 skipped; ruff check + format clean.
  - Found + fixed: backend close() crashed on broken torch stubs (leaked fake
    site-packages from cuda_runtime_activation tests under xdist scheduling).
- [x] Task 6: Conductor - User Manual Verification 'Documents, batch, audiobook, and packaging'. (user-accepted 2026-09-08; automated gates green)

- [ ] Track: Evaluate GGUF/quantized Qwen runtime after official backend is stable.
- [ ] Track: Add mixed-language per-paragraph engine routing if real user workflows require it.
- [ ] Track: Evaluate user-selectable output sample rates.
