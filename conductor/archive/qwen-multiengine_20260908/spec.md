# Qwen Multiengine TTS Support

## Overview

Add selectable TTS backends while preserving the existing VieNeu-TTS v3 Turbo path. The application will support:

- VieNeu-TTS v3 Turbo for Vietnamese-first synthesis and existing voice cloning.
- Qwen3-TTS 0.6B CustomVoice for multilingual synthesis with fixed speakers and natural-language style instructions.
- Qwen3-TTS 0.6B Base for multilingual reference-audio voice cloning.

Qwen support is an optional engine pack. VieNeu remains the lightweight default installation and remains the recommended engine for Vietnamese. Qwen models are used for their supported non-Vietnamese languages.

## Functional Requirements

### Engine and language selection

1. Users can explicitly select VieNeu, Qwen CustomVoice, or Qwen Base.
2. Users can select or confirm the synthesis language.
3. The app recommends VieNeu for Vietnamese and Qwen for languages supported by Qwen, without silently changing the selected engine.
4. The app validates engine/language/voice combinations before a job starts and returns actionable errors.
5. The selected engine and language persist in settings and are revalidated on restore.
6. Per-paragraph or per-document mixed-engine routing is out of scope for this track; one backend is selected per synthesis job.

### Qwen CustomVoice

1. Install and load `Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice` through the official `qwen-tts` PyTorch runtime.
2. Expose the model's fixed speakers through the generic voice catalog.
3. Support Qwen's language selection and natural-language style/emotion instruction field.
4. Disable or hide arbitrary voice cloning for CustomVoice.
5. Support CustomVoice across all existing synthesis surfaces after shared backend integration: free text, paragraph/file/batch flows, audiobook generation, playback, and export.

### Qwen Base

1. Install and load `Qwen/Qwen3-TTS-12Hz-0.6B-Base` through the same official runtime family.
2. Support reference-audio voice cloning for supported workflows.
3. Reuse existing reference-clip validation where compatible, with a backend-specific adapter where required.
4. Persist Qwen Base reference voices in app-owned storage.
5. Keep VieNeu voices and Qwen Base voices isolated by engine and model variant.
6. Expose cloning controls only when the selected backend supports cloning.
7. Require explicit consent before reference audio is enrolled.

### Shared backend and synthesis pipeline

1. Introduce a backend-neutral TTS contract consumed by the existing worker/job pipeline.
2. Preserve single-threaded engine ownership, cancellation, progress, terminal job signaling, artifact-first writing, and bounded live transport.
3. Normalize all backend output to mono float32 at the application's existing 48 kHz contract before artifact writing or playback.
4. Use stateful streaming resampling for Qwen's native output to avoid chunk-boundary discontinuities.
5. Preserve existing WAV artifact, waveform, playback, audiobook, and export formats.
6. Include engine/profile, model revision, language, voice, instruction, text, speed, and silence settings in relevant cache identities.
7. Improve long-text segmentation for CJK punctuation and avoid inserting inappropriate spaces when joining CJK segments.

### Model management and runtime

1. Keep Qwen dependencies and weights optional; existing VieNeu users must not require PyTorch/Qwen packages.
2. Provide separate install/readiness state for Qwen CustomVoice and Qwen Base.
3. Support on-demand download, offline cache use, disk-space preflight, progress, cancellation, and retry.
4. Provide runtime/device diagnostics for missing `qwen-tts`, PyTorch, CUDA, and insufficient resources.
5. Use CUDA acceleration when available and an explicit CPU fallback when CUDA is unavailable; do not promise real-time CPU performance before benchmarks establish it.
6. Unload/close the previous backend before loading another model and avoid keeping all three models resident unnecessarily.
7. Quantized/GGUF Qwen runtimes are deferred to a separate optimization track.

### Existing synthesis surfaces

Qwen support must ultimately work through the existing free-text, paragraph, imported-file, batch, audiobook, playback, and export paths. Implementation order may be phased: common backend contract, interactive text, shared document/batch paths, then audiobook integration.

## Non-Functional Requirements

- Preserve VieNeu behavior and existing compatibility for Vietnamese users.
- Preserve the current artifact format and 48 kHz playback contract.
- Keep backend-specific behavior behind backend/profile capabilities rather than repository-string checks scattered through controllers and QML.
- Maintain offline operation after models and optional runtime components are installed.
- Model loading, inference, and close remain owned by the same worker thread.
- Do not load models during application startup unless the existing explicit prewarm policy requests it.
- Surface capability, dependency, model, device, and audio-conversion failures with actionable messages.
- Follow the repository's TDD and quality-gate workflow.

## Acceptance Criteria

1. VieNeu remains selectable and existing Vietnamese synthesis, cloning, playback, export, batch, and audiobook behavior remains intact.
2. Qwen CustomVoice can synthesize at least English, Chinese, and Korean using compatible fixed speakers and natural-language instructions.
3. Qwen CustomVoice rejects cloning requests with a targeted capability error.
4. Qwen Base can synthesize with a persisted reference voice for a supported language and rejects invalid/missing reference audio clearly.
5. Qwen rejects Vietnamese with a targeted compatibility message directing users to VieNeu.
6. Switching backends closes the previous backend before loading the next and does not create a second inference worker.
7. Qwen output is normalized to valid 48 kHz mono float32 artifacts and live playback receives correctly timed audio.
8. Qwen models can be installed on demand, used from the offline cache, cancelled/retried, and reported with truthful progress/readiness state.
9. Saved voices are engine-isolated and cannot be selected by an incompatible backend.
10. All existing synthesis surfaces route through the same backend contract without duplicated Qwen-specific inference logic.
11. Existing tests remain green, and new tests cover capability validation, backend contracts, resampling boundaries, cache identity, model lifecycle, and representative Qwen workflows with injected fakes.

## Out of Scope

- Vietnamese synthesis using Qwen.
- Per-paragraph or per-document automatic engine switching.
- User-selectable output sample rates or backend-native sample rates throughout the application.
- GGUF, community quantized, or alternate Qwen runtimes.
- A real-time CPU latency guarantee for Qwen.
- Loading all backends simultaneously.
- Switching between preset and cloned voice sources inside one synthesis job.
- Replacing VieNeu's existing model installer or CPU baseline with Qwen artifacts.
