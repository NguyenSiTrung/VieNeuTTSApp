# Implementation Plan: Dubbing & Subtitles Hardening — Local ASR/Diarization Runtimes, Render Safety, Quality & Performance (`dub_asr_diar_hardening_20261029`)

## Overview

The plan has 8 phases, every task TDD. Read `conductor/patterns.md` before starting. Notable rules for this track:
- Windows shebang CLI stubs go through `cli_command()`.
- Never register `"index"` as a role.
- `bool` subclasses `int`.
- Burn-in ASS escaping.
- Bench-gate every default.
- Low-value test criteria apply.

Per task:
1. Write the red tests, folding them into the existing suite for that function. Keep regression, branch, error, cancel and contract tests. No default-value or round-trip micro-tests.
2. Implement until green.
3. Run `ruff check .`, `ruff format --check .` and `pytest`. Run `pytest -m benchmark` too when the task adds benchmark tests or touches `scripts/benchmarks/`.
4. Commit locally and add a `git notes add` summary. **Never push.**
5. Append discoveries to `learnings.md`.

Verification is automated only; there are no manual phase gates.

Test seams and fakes:
- Native CLIs (`whisper-cli`, `audiocpp_cli`, `ffmpeg`, `ffprobe`) are pure-Python `#!/usr/bin/env python3` stubs that record argv and emit scripted output.
- Network calls use local `http.server` mocks.
- ONNX goes through a `session_factory` seam.
- Cancel and concurrency tests fire on events, never on fixed sleeps.

**Phase graph:**
- Phase 1 is the foundation.
- Phases 2, 3 and 6 can run in parallel after Phase 1.
- Phases 4 and 5 can run in parallel after Phase 3.
- Phase 7 integrates 2, 4, 5 and 6.
- Phase 8 is the UI, i18n and docs layer.

`ui/subtitle_controller.py` is owned by exactly one phase at a time: Phase 2, then Phase 7. i18n has a single owner: Task 8.1.

---

## Phase 1: Foundations — process helpers, background-op hygiene, shared 16 kHz prep
<!-- execution: parallel -->
<!-- depends: -->

- [x] **Task 1.1: `core/processes.py` lookup/spawn helpers + child registry; migrate every native spawn (FR-2.1)**
  <!-- files: src/vienetts_app/core/processes.py, src/vienetts_app/core/video_dub.py, src/vienetts_app/core/asr.py, src/vienetts_app/core/diarization.py, src/vienetts_app/core/ai_models.py, tests/unit/test_processes.py, tests/unit/test_video_dub.py -->
  - Red tests:
    - **`find_executable`:** the order is managed dir → PATH → well-known dirs. Inject `well_known_dirs` and `sys.platform`, and use temp stub executables. A darwin stub in a fake `/opt/homebrew/bin` is found with `PATH=""`.
    - **Spawn options:** `run_cli`/`spawn_cli` pass `creationflags=CREATE_NO_WINDOW` when `os.name == "nt"` (monkeypatched, with a `subprocess.Popen` spy) and always pass `encoding="utf-8", errors="replace"`.
    - **Timeouts:** a timeout raises `CliTimeout` and kills the child. `cancel_check` turning true terminates the child within ≤ 0.5 s.
    - **Registry:** a sleeping stub child is gone after `terminate_children(timeout=1)` (terminate → kill), and the registry empties as children exit.
    - **Review A regression:** `extract_video_audio` with a stub ffmpeg that writes `Phụ đề.mp4` (UTF-8 bytes incl. `0x81`) to stderr keeps the WAV.
  - Green implementation:
    - Add the helpers, reusing the `CREATE_NO_WINDOW` idiom from `qwen_engine.py:1073` / `detector.py:146`.
    - Replace the raw `subprocess` calls:
      - `video_dub.py:114, :360, :444, :447, :589, :608, :668`;
      - `asr.py:655`;
      - `diarization.py:885`;
      - `ai_models.py:591` (`_probe_runtime`).
    - Timeouts: ffprobe 30 s; extraction `max(120 s, 2 × duration)` and cancellable; probes 15 s.
    - `resolve_ffmpeg`/`resolve_ffprobe`/`resolve_whisper_cpp_cli`/the audiocpp lookup call `find_executable`.
    - Keep `cli_command()`'s shebang dispatch.

- [x] **Task 1.2: `_OneShotBridge` hygiene + GUI-thread `ProgressRelay` (FR-1.2 infrastructure, FR-1.8)**
  <!-- files: src/vienetts_app/ui/bg_ops.py, tests/unit/test_bg_ops.py -->
  - Red tests (`qcoreapp` fixture, pump `processEvents`):
    - After 1,000 `run_on_thread_pool` ops (success and error mixed), the parent has ≤ 1 `_OneShotBridge` child, and a `weakref` to an object captured by `work` dies after delivery.
    - `ProgressRelay.report(generation, value, text)` called from a `threading.Thread` invokes the GUI callback on the GUI thread (`QThread.currentThread()` identity), drops reports whose generation ≠ `current_generation()`, and keeps reports monotonic within a generation.
  - Green implementation:
    - `_deliver` clears `_work`/`_on_done`/`_on_error` in `finally` and calls `deleteLater()`.
    - Add `ProgressRelay(QObject)`: a queued `Signal(int, float, str)`, an injectable `current_generation` callable, and the GUI-side callback.

- [x] **Task 1.3: Shared `source_16k.wav` with streaming anti-aliased resampling (FR-3.6)**
  <!-- files: src/vienetts_app/core/audio_prep.py, src/vienetts_app/core/asr.py, src/vienetts_app/core/diarization.py, tests/unit/test_audio_prep.py, tests/unit/test_asr.py, tests/unit/test_diarization_hardening.py, pyproject.toml, uv.lock -->
  <!-- depends: task1 -->
  - Red tests:
    - **Single conversion:** a 44.1 kHz stereo WAV is converted once when ASR runs and then diarization. A converter spy sees 1 call, and both read the same `source_16k.wav` + sidecar.
    - **Legacy adoption:** an existing `input_16k.wav` or `source_wav_16k.wav` whose sidecar matches is adopted without reconversion.
    - **Anti-aliasing:** a 48 kHz input with a 10 kHz tone leaves < −40 dB energy at the 6 kHz alias after conversion.
    - **Bounded memory:** conversion reads in blocks (spy on block size). `tracemalloc` peak stays < 64 MB for a synthetic 20-minute input generated in blocks.
    - **Video inputs** still use ffmpeg extraction through `run_cli`.
  - Green implementation:
    - Add `ensure_source_16k(media_path, cache_dir, *, ffmpeg=None, cancel_check=None) -> Path`. It uses `soxr.ResampleStream` (`VHQ`) over `soundfile.SoundFile.blocks`, writes atomically with a sidecar, and is called from `asr._ensure_16k_wav` and the diarization 16 kHz normalizer.
    - Declare `soxr` in `pyproject.toml` at the already-locked range; `uv lock` must add no packages.

---

## Phase 2: Controller concurrency, lifecycle & persistence safety
<!-- execution: sequential -->
<!-- depends: phase1 -->

- [x] **Task 2.1: One busy owner — gate render/play/export vs AI; render from a snapshot (FR-1.1, H1)**
  <!-- files: src/vienetts_app/ui/subtitle_controller.py, src/vienetts_app/ui/qml/components/SubtitleActionDock.qml, src/vienetts_app/ui/qml/SubtitlesTab.qml, tests/unit/test_subtitle_controller.py, tests/unit/test_subtitle_components.py -->
  - Red tests:
    - While an injected slow AI runner is in flight, `render()`/`play()` start nothing and set `disabledReason`. While rendering or exporting, `transcribeVideo`/`diarizeSpeakers`/`refineWithLlm` and cue-mutating slots refuse.
    - A cue text changed mid-render, by forcing an AI delivery through the seam, never reaches a later unit: spy on the submitted unit texts.
    - A knob change while rendering is staged, not applied (`_can_apply_now()` is False).
    - QML (stub controller): the Render/Play/Export buttons are disabled when `aiBusy`, and the project delete button is disabled while any owner is busy.
  - Green implementation:
    - Add an `_owner_busy(except_owner)` helper and gate every entry slot with it.
    - Snapshot `_render_cues` at render start and read units from it (`:1832`).
    - Make `_can_apply_now()` render-aware.
    - Add the QML `enabled` bindings: Render, Play and Export at `SubtitleActionDock.qml:107`, `:146` and `:170`; the delete button at `SubtitlesTab.qml:266`.

- [x] **Task 2.2: GUI-thread, generation-checked AI progress; per-op generations; guarded sample playback (FR-1.2, FR-1.3, H2, L6)**
  <!-- files: src/vienetts_app/ui/subtitle_controller.py, tests/unit/test_subtitle_controller.py -->
  - Red tests:
    - An LLM `progress_cb` invoked from a worker thread after `cancelAiTask()` leaves `aiBusy` False.
    - ASR and diarization progress from a thread arrives on the GUI thread (a thread-recording spy on `_set_ai_state`).
    - `playSpeakerSample` leaves `_ai_generation` unchanged, and an AI op started during extraction completes normally.
    - A stale `ref_<speaker>.wav` is re-extracted when the chosen clip's bounds change (sidecar key).
    - Playback goes through the `_play_track` guard.
  - Green implementation:
    - Route every AI progress and result through `ProgressRelay`. This replaces the worker-thread `_set_ai_state` calls in `report_asr`/`report_diar` (`:2609-2623`) and in the LLM `progress_cb` lambda (`:2998`).
    - Add `_sample_generation` and a sidecar key for reference clips.

- [~] **Task 2.3: Project switch, removal, cancel & shutdown; `projectsChanged` (FR-1.4, FR-1.5, FR-1.9 part, M1, M2, M4, M7, L2)**
  <!-- files: src/vienetts_app/ui/subtitle_controller.py, tests/unit/test_subtitle_controller.py -->
  - Red tests:
    - **Import during ASR:** `importSrt` while a fake ASR runs cancels it; its `done` is dropped and the imported project stays.
    - **Stale deliveries:** a diarization or LLM `done` for project A is dropped after switching to B. `on_enrolled` stores the voice in the enrolling project, never in the active one.
    - **`removeProject(active)`** during render, export or AI calls cancel → clear → `videoReleaseRequested` → `store.remove`, in that order (a call-order spy). It also removes the project's `_media_cache` entry and managed SRT when they are unreferenced.
    - **`cancelRender`:** after the fake job has completed, `cancelRender()` ends the render immediately and no further unit is submitted.
    - **Shutdown:** `shutdown()` calls `cancelAiTask`, bumps the export generation, and calls `terminate_children()`. A real sleeping stub child is gone afterwards.
    - **`projectsChanged`** is emitted on import, ASR import, `clear()`, render complete and remove (signal spy).
  - Green implementation:
    - Capture `project_id` in every async closure.
    - Reorder `removeProject`; make `cancelRender` call `_fail_render("")`; extend `shutdown()`; add the emissions.

- [ ] **Task 2.4: Persistence & export correctness; always-fresh cue rows (FR-1.6, FR-1.7, FR-1.9 part, L1, L4, M3, M5)**
  <!-- files: src/vienetts_app/ui/subtitle_controller.py, tests/unit/test_subtitle_controller.py -->
  - Red tests:
    - **Setters keep the render:** on a rendered project, each of `videoMixMode`, `videoBgGainDb`, embed mode, subtitle language and `setLlmGlossary` keeps `stats`, `adjusted` and the fingerprint in `project.json`. Moving a policy knob away and back re-adopts the render with 0 synthesis. Re-importing the same file keeps the render.
    - **Speaker voice:** `setSpeakerVoice` on a rendered project goes through the staged-invalidation prompt and sets `_stale_reason`.
    - **Finish race:** a cue edit saved during `renderer.finish()` survives.
    - **`exportSrt`** on a legacy rendered project with empty `adjusted` writes the cues, not an empty file.
    - **Overlapping exports:** export 1 is cancelled and export 2 started. Export 1's ffmpeg stub stops (generation-based `cancel_check`), the temp SRTs differ, the output names differ, and the cancelled `exportTrack`/`exportSrt` leave no file.
    - **Fresh rows:** after a rate-cap or mode change, the rows' `fitRate`/`overflowMs`/`overBudget` reflect the new plan.
  - Green implementation:
    - Add `_save_field(**changes)`: a load-modify-save onto the latest stored project.
    - Make finish merge onto the latest store state.
    - Use `adjusted or cues`; give each export a generation-scoped `cancel_check`, a unique temp SRT and a name reservation.
    - Call `_emit_cues()` unconditionally in `_rebuild_for_policy`.

---

## Phase 3: Native runtime & model provisioning
<!-- execution: parallel -->
<!-- depends: phase1 -->

- [x] **Task 3.1: Catalog & installers — macOS whisper-cli cells, managed ffmpeg runtime, audio.cpp variants, optional audio models (FR-2.2, FR-2.3, FR-2.4, FR-3.1 catalog, FR-4.5 catalog)**
  <!-- files: src/vienetts_app/core/ai_models.py, src/vienetts_app/core/media_runtime_manifests.json, packaging/vienetts-app.spec, tests/unit/test_ai_models.py -->
  - Red tests:
    - **macOS whisper cells:** darwin/arm64 → `available_whisper_cpp_variants()` returns `macos-arm64-metal` from the manifest, and darwin/x86_64 → `macos-x64-cpu`; neither offers an `ubuntu` asset. A `published: false` cell reports unavailable with Homebrew guidance and never downloads.
    - **ffmpeg install:** the runtime installs from a local stub pack (an archive built in-test with Python stub `ffmpeg`/`ffprobe` answering `-version`/`-encoders`/`-filters`) with size and SHA verified.
    - **ffmpeg rejection and removal:** a pack lacking the `subtitles` filter or the `libx264` encoder is rejected and the previous runtime survives. `remove_ffmpeg_runtime` is `is_relative_to`-guarded.
    - **audio.cpp variants:** Windows plus an NVIDIA probe offers `cuda12.4`/`cuda13.3` with pinned asset names. Without the probe, only cpu/vulkan are offered. Linux/aarch64 → empty list with the reason "unsupported".
    - **Optional models:** catalog entries `whisper-vad-silero-v6.2.0` (ggml, from `ggml-org/whisper-vad`), `silero-vad` (ONNX, MIT) and `speaker-embedding` (ONNX, default candidate) download to their model dirs with pinned size and SHA.
    - **Frozen data:** the frozen spec lists `media_runtime_manifests.json` as data (cross-file invariant test).
  - Green implementation:
    - Manifest schema `{schema: 1, runtimes: {whisper_cpp: {cells: {...}}, ffmpeg: {cells: {...}}}}`; each cell has `url`, `size`, `sha256`, `published`, `license` and `source`.
    - Add a darwin branch and the ffmpeg installer, reusing `_stream_download`, the staged probe and the atomic swap.
    - Reuse the NVIDIA probe from `detect_hardware`.
    - Record the real sizes and SHA-256 values of the VAD and ONNX model files during this task.

- [~] **Task 3.2: Opt-in `media-runtime-build.yml` + lock/render scripts (FR-2.6)**
  <!-- files: .github/workflows/media-runtime-build.yml, packaging/media-runtime-requirements.json, scripts/lock_media_runtime.py, scripts/render_media_runtime_manifests.py, tests/unit/test_media_runtime_manifest.py -->
  <!-- depends: task1 -->
  - Red tests:
    - The renderer turns locked packs plus requirements into `media_runtime_manifests.json`, with `published: true` only for locked cells. It refuses size or SHA mismatches and cells absent from the requirements.
    - Every requirements cell names a `license`, a `source` URL and the expected probe tokens (cross-file invariant).
    - The workflow YAML never names `matrix` in a job-level `if:` (static check).
  - Green implementation:
    - `workflow_dispatch` inputs: `cells`, `publish`, and `downloads_base` (default `https://nguyensitrung.github.io/VieNeuTTSApp/packs`).
    - A plan job shrinks the matrix.
    - macOS jobs build a static `whisper-cli` at the `b5454` source:
      - arm64: `-DBUILD_SHARED_LIBS=OFF -DGGML_METAL=ON -DGGML_METAL_EMBED_LIBRARY=ON`;
      - x64: CPU;
      - smoke `--help` and the `--vad` flag.
    - ffmpeg jobs fetch pinned upstream static builds, verify upstream checksums, add `LICENSE`/`SOURCE.txt`, and probe `-encoders`/`-filters`. Candidates are BtbN FFmpeg-Builds (GPL, win64/linux64/linuxarm64) and ffmpeg.martin-riedl.de (macOS); record the final picks in the requirements file.
    - Then lock → render → upload artifacts. With `publish=true`, push packs to gh-pages and commit the manifest to main, mirroring `qwen-gguf-runtime-build.yml`.

- [~] **Task 3.3: AppController runtime/model surface, `asr_cloud_fallback` setting, thread-safe download progress (FR-2.5, FR-3.4 setting, FR-1.2 downloads)**
  <!-- files: src/vienetts_app/ui/controller.py, src/vienetts_app/core/models.py, src/vienetts_app/core/settings.py, tests/unit/test_controller.py, tests/unit/test_settings.py -->
  <!-- depends: task1 -->
  - Red tests:
    - **`asrBackends`** includes `faster_whisper` only when `find_spec` succeeds (monkeypatched). A stored `faster_whisper` with no module resolves to `auto` and adds a note.
    - **New slots** run through `_run_bg` and report status: generic catalog-driven `downloadAudioModel(model_id)` / `removeAudioModel(model_id)` / `audioModels`, plus `installFfmpegRuntime` / `removeFfmpegRuntime` / `ffmpegRuntimeStatus`.
    - **Download progress** emitted from a worker thread reaches the GUI thread (thread identity), with the ≥ 1 % throttle kept.
    - **`asr_cloud_fallback`:** `Settings(asr_cloud_fallback=1)` is rejected (bool rule). `load_settings` drops only a bad value. The `asrCloudFallback` property persists and emits `aiSettingsChanged`.
  - Green implementation:
    - Add the properties and slots, and route download progress through `ProgressRelay`.
    - Add the settings field and its per-field clamp.

---

## Phase 4: ASR quality & throughput
<!-- execution: sequential -->
<!-- depends: phase1, phase3 -->

- [~] **Task 4.1: whisper.cpp invocation — `-mc 0`, Silero `--vad`, JSON-full word timings, cache-dir outputs, `asr_version` (FR-3.1)**
  <!-- files: src/vienetts_app/core/asr.py, tests/unit/test_asr.py -->
  - Red tests (the stub `whisper-cli` records argv and writes `<prefix>.json` in JSON-full shape):
    - argv has `-mc 0` and `-ojf`, plus `--vad -vm <path>` exactly when the VAD model file exists.
    - The `-of` prefix is inside the media cache dir even for a 16 kHz mono WAV beside the user's file, and no stray files appear next to the source.
    - Tokens merge into words, with special tokens (`[_BEG_]`, `[_TT_*]`) skipped. Segments carry word bounds.
    - SRT is not read when JSON exists.
    - A cache without the current `asr_version` re-runs.
  - Green implementation: build the argv; add the JSON-full parser; move the output prefix; version the cache key.

- [ ] **Task 4.2: Hallucination filter for every backend (FR-3.2)**
  <!-- files: src/vienetts_app/core/asr.py, tests/unit/test_asr.py -->
  - Red tests:
    - Drops a segment when `no_speech_prob > 0.6` and `avg_logprob < -1.0`, and when `compression_ratio > 2.4`.
    - Collapses a 4× repeated normalized line to one, but keeps a confident 2× genuine repeat.
    - Removes a low-confidence trailing "Hãy subscribe cho kênh Ghiền Mì Gõ…" segment, but keeps the same phrase when confident and mid-video.
    - `notes` report the counts.
    - faster-whisper, whisper.cpp JSON (token probabilities → `avg_logprob`) and API `verbose_json` all feed the filter.
  - Green implementation: add `filter_hallucinations(segments, *, duration_ms, notes)` and a phrase list (vi/en); call it before `normalize_asr_segments_to_cues`.

- [ ] **Task 4.3: Defaults, installed-model fallback, cloud-fallback gating, typed backend errors, detected language (FR-3.3, FR-3.4)**
  <!-- files: src/vienetts_app/core/asr.py, src/vienetts_app/core/models.py, tests/unit/test_asr.py, tests/unit/test_settings.py -->
  - Red tests:
    - **Defaults:** `Settings().asr_model == "large-v3-turbo-q5_0"`, while a stored `"base"` loads as `"base"`.
    - **Model fallback:** with only `small` installed and `asr_model="base"`, `small` is used and a note is added. With none installed, `AsrBackendMissing` names the Settings download.
    - **Cloud gating:** auto mode with an ASR URL set and `cloud_fallback=False` never hits `/audio/transcriptions`. With `True`, it uploads only after a local `AsrBackendMissing`, and the cloud note appears.
    - **Detected language** appears in `notes` and in the cache.
    - **Typed errors:** no Vietnamese message matching remains (grep-free assertion via a typed-exception path test).
  - Green implementation:
    - Add `AsrConfig.cloud_fallback`, `resolve_whisper_model`, the `AsrBackendMissing` typing, and the detected-language plumbing.
    - Change the default in `models.py`.

- [ ] **Task 4.4: Cancellable requests, API word timestamps, thread defaults, faster-whisper cache release (FR-3.5, FR-3.7)**
  <!-- files: src/vienetts_app/core/llm_client.py, src/vienetts_app/core/asr.py, tests/unit/test_llm_client.py, tests/unit/test_asr.py -->
  - Red tests:
    - **Cancel:** against a mock server that stalls 5 s, `post_chat_completion` and the ASR upload return `cancelled` within 1 s of `cancel_check` (event-driven).
    - **Word timestamps:** the multipart body includes `timestamp_granularities[]=word` and `timestamp_granularities[]=segment`. API words are offset per chunk (extend the existing chunk test).
    - **Threads:** `default_asr_threads()` uses the injected darwin/arm64 `sysctl hw.perflevel0.physicalcpu`, otherwise `cpu_count() // 2`, capped at 8.
    - **Cache release:** `_FASTER_WHISPER_CACHE` is released after 10 min idle (injected clock) and by `release_asr_models()`.
  - Green implementation: add `cancellable_request` (daemon thread + 250 ms poll), the multipart fields, the threads helper and the idle release.

---

## Phase 5: Diarization quality & throughput
<!-- execution: sequential -->
<!-- depends: phase1, phase3 -->

- [x] **Task 5.1: `split_segments_at_turns` with word timings (FR-4.1)**
  <!-- files: src/vienetts_app/core/diarization.py, tests/unit/test_diarization.py -->
  - Red tests:
    - A segment whose words span A→B, with each part ≥ 600 ms and ≥ 2 words, splits at the nearest word boundary into two parts labelled A and B.
    - A short side (< 600 ms or a single word) does not split.
    - A segment with no words is unchanged.
    - Three turns produce three parts.
    - The parts' speech bounds come from their words.
  - Green implementation: add the pure function (wired in Task 7.1).

- [~] **Task 5.2: Clustering, speech-bound sweep and Nemotron progress (FR-4.3)**
  <!-- files: src/vienetts_app/core/diarization.py, tests/unit/test_diarization.py, tests/unit/diarization_reference.py -->
  - Red tests:
    - **Clustering oracle:** new `_cluster_embeddings` labels equal the oracle's (`diarization_reference.py`, a verbatim copy of today's implementation) on 50 seeded sets with n ≤ 60, kept short because of float near-ties.
    - **Clustering budget:** `@pytest.mark.benchmark` — 2,000 embeddings in < 2 s.
    - **Speech-bound sweep:** `refine_cues_speech_bounds` opens the WAV once (spy) and returns bounds identical to the oracle on a fixture.
    - **Sweep budget:** `@pytest.mark.benchmark` — 1,500 cues over a synthetic 1 h WAV in < 3 s.
    - **Nemotron progress:** a stub printing progress lines yields ≥ 10 monotonic callbacks. With unparseable output, the elapsed/duration estimate is used (injected clock).
  - Green implementation:
    - Cached similarity plus Lance–Williams average-linkage updates.
    - A single-open, two-pointer sweep.
    - Stdout progress parsing with the estimate fallback.

- [ ] **Task 5.3: Reference-clip scoring (FR-4.4)**
  <!-- files: src/vienetts_app/core/diarization.py, tests/unit/test_diarization_hardening.py -->
  - Red tests:
    - From four candidates — a 2 s clean clip, an 8 s clean clip, a 10 s clip with 5 % clipped samples, and a 9 s clip overlapping another speaker's turn — the 8 s clean clip wins.
    - Duration breaks ties.
  - Green implementation: add the scoring function, using the energy VAD (Silero when installed after Task 5.4).

- [ ] **Task 5.4: Optional ONNX Silero VAD + speaker embeddings, DER-chosen model (FR-4.5)**
  <!-- files: src/vienetts_app/core/onnx_audio.py, src/vienetts_app/core/diarization.py, src/vienetts_app/core/ai_models.py, tests/unit/test_onnx_audio.py, tests/unit/test_diarization.py, tests/unit/test_ai_models.py, scripts/benchmarks/diarization_der.py, pyproject.toml, uv.lock -->
  - Red tests (fake `session_factory`):
    - **Silero VAD:** 512-sample framing at 16 kHz carries the state tensor across frames, and the hysteresis thresholds produce the expected speech spans from scripted probabilities.
    - **Embedding pipeline:** `kaldi-native-fbank` 80-dim features → session → L2-normalized vectors.
    - **Model selection:** the acoustic fallback uses neural embeddings when the model is installed and the 24-dim embedding otherwise. `refine_cues_speech_bounds` uses Silero when installed.
    - **Real model:** a skip-if-absent test runs the actual model files.
  - Green implementation:
    - Add `onnx_audio.py` with lazy `onnxruntime` import, a single shared session per model and a CPU provider.
    - Wire the selection.
    - Declare `onnxruntime`/`kaldi-native-fbank` in `pyproject.toml` at the locked ranges; `uv lock` adds no packages.
    - Run `scripts/benchmarks/diarization_der.py`. It builds a multi-voice fixture from the app's own TTS voices with known turns and compares DER for WeSpeaker ResNet34-LM, 3D-Speaker CAM++ and the 24-dim baseline.
    - Pin the winner (size, SHA-256, license) in the catalog and record the numbers in `learnings.md`.

---

## Phase 6: Render, alignment & mux core
<!-- execution: parallel -->
<!-- depends: phase1 -->

- [x] **Task 6.1: Alignment fixes + contiguous merged-unit placement with snapped, faded splits (FR-5.2, FR-5.3)**
  <!-- files: src/vienetts_app/core/align.py, src/vienetts_app/core/subtitle_project.py, tests/unit/test_align.py, tests/unit/test_subtitle_project.py -->
  - Red tests (the reviewer repros become regression tests):
    - **Drift:** 4 adjacent 2 s cues with natural `[3500, 2000, 2000, 2000]` and cap 1.5 end within 150 ms of the last anchor.
    - **Borrow:** cue `[1000,4000]` with speech `[1000,1300]`, natural 2500 and next cue at 6000 fits at 1.0×. The no-next-cue branch uses `max(...)`.
    - **Silent cues:** with offset 5000, the adjusted cues are monotonic and non-overlapping.
    - **Merged unit:** `"Hello there my"` `[0,3000]` + `"good friend."` `[3100,5000]` with a 1.5 s clip.
      - The second piece starts at the first piece's end, within the snap window, in both dub and transcript modes.
      - A 100 ms pause injected 80 ms from the weighted split is where the cut lands.
      - Both split edges ramp over 5 ms.
  - Green implementation:
    - Plan against the effective window when pushed; apply `extended_end = max(srt_end, borrow_cap)` within `next_start - MIN_BREATH_GAP_MS`.
    - Offset and clamp silent fits.
    - Add the `split_unit_audio` snap (lowest-RMS 10 ms within ±150 ms).
    - Place contiguous pieces in `add_unit_clip`, with `apply_micro_fade` at every split.

- [~] **Task 6.2: Per-unit clip cache, crash-consistent commit, fingerprint hygiene (FR-5.1 core, FR-5.7)**
  <!-- files: src/vienetts_app/core/subtitle_project.py, tests/unit/test_subtitle_project.py -->
  <!-- depends: task1 -->
  - Red tests:
    - **Cache key:** `UnitClipCache.key(...)` is stable across runs and changes with text, voice stamp, engine profile, model revision, speed and context fingerprint.
    - **Cache storage:** put/get round-trips audio. LRU eviction honours the byte cap (oldest access first).
    - **Commit order:** timeline → `project.json` → promote, each carrying `render_id`.
    - **Crash consistency:** a crash injected after the timeline write leaves `cached_render` refusing the track (`render_id` mismatch). The splice save-back follows the same order.
    - **Fingerprint:** unused `speakerVoices` entries and whitespace-padded voice names no longer change `render_fingerprint` after a reload. A golden digest for a normalized project is unchanged.
  - Green implementation: add `UnitClipCache` under `<project>/units/`, `render_id` plumbing and the fingerprint normalization.

- [x] **Task 6.3: Mux — progress units/duration, ducking envelope at scale, limiter, ISO 639-2 tags, finalize stage (FR-5.4, FR-5.5, FR-5.6)**
  <!-- files: src/vienetts_app/core/video_dub.py, tests/unit/test_video_dub.py -->
  - Red tests:
    - **Progress units:** fix the stub at `test_video_dub.py:476` to emit real-ffmpeg values. `out_time_us=5000000` / `out_time_ms=5000000` report 5 s.
    - **Progress coverage:** a non-burn export without clamp reports progress. With ffprobe absent, the dub WAV duration is used. Progress is monotonic and capped at 0.95 until the `+faststart` finalize ends.
    - **Ducking at scale:** 1,500 spans produce an envelope file passed by path. The command line stays < 8 KB, every span is ducked, gaps longer than `merge_gap_ms` stay at full level, and no O(n²) collapse remains.
    - **Limiter:** the final graph contains `alimiter`.
    - **Tags:** `vi`→`vie`, `zh-CN`→`chi`, unknown → `und`.
  - Green implementation: parse `out_time_us`; probe duration whenever there is a progress callback; build the envelope (`asendcmd` or a gain track); add the limiter, the ISO 639-2/B map and the finalize stage.
  - ffmpeg-dependent assertions skip when ffmpeg is absent (existing marker), with stub-level coverage kept.

- [x] **Task 6.4: Subtitle parsing edge cases (FR-6.4)**
  <!-- files: src/vienetts_app/core/subtitles.py, tests/unit/test_subtitles.py -->
  - Red tests:
    - **Sound cues:** `[door slams] Get out!` → no speaker, text `Get out!`. `(laughs)` / `[MUSIC PLAYING]` alone → non-speech, kept in transcript output and non-speakable.
    - **Tag stripping:** `x < y and y > z` keeps its text.
    - **Non-speakable cues:** `♪♪`, `...`, `-` and emoji-only cues are non-speakable.
    - **VTT timing:** `01:02.500 --> 01:04.000` parses.
    - **Decoding fallback:** cp1258 and UTF-16LE-without-BOM files decode via the fallback chain, with a note.
  - Green implementation: tighten the speaker regex and tag stripper; add an `is_speakable` helper, the optional VTT hours, and the decode chain.

---

## Phase 7: Controller integration & GUI responsiveness
<!-- execution: sequential -->
<!-- depends: phase2, phase4, phase5, phase6 -->

- [ ] **Task 7.1: `core/transcribe_pipeline.py` — concurrent ASR + Nemotron, speaker-turn splitting, combined progress; controller wiring (FR-4.1 wiring, FR-4.2, FR-3.4 wiring)**
  <!-- files: src/vienetts_app/core/transcribe_pipeline.py, src/vienetts_app/ui/subtitle_controller.py, tests/unit/test_transcribe_pipeline.py, tests/unit/test_subtitle_controller.py, scripts/benchmarks/transcribe_parallel.py -->
  - Red tests (event-driven stub runners):
    - **Concurrency:** the Nemotron stub starts before the ASR stub finishes, and thread budgets are split (`threads // 2`, minimum 1).
    - **Equivalence:** results equal the sequential path, and `split_segments_at_turns` is applied when words exist.
    - **Failure and cancel:** cancel stops both runners. A diarization failure keeps the ASR cues and adds a note. The acoustic fallback stays sequential. Combined progress is monotonic.
    - **Config:** `_asr_config()` passes `cloud_fallback` and the resolved threads.
  - Green implementation:
    - Add `run_transcription(media, asr_cfg, diar_cfg, *, parallel, progress, cancel_check)` and replace the body of `_transcribe_video_now`.
    - Bench gate: run `scripts/benchmarks/transcribe_parallel.py` with real runtimes. Enable `parallel` by default only if it wins ≥ 15 % wall time.
    - If no real runtime exists on the host, ship `parallel=False`, record "unmeasured" in learnings, and file a follow-up bead.

- [ ] **Task 7.2: Render path — unit-cache wiring, placement-only re-render, zero-sample units (FR-5.1 wiring)**
  <!-- files: src/vienetts_app/ui/subtitle_controller.py, tests/unit/test_subtitle_controller.py -->
  - Red tests:
    - **Single-unit edit:** editing 1 of 30 cues in a rendered project re-synthesizes exactly 1 unit (spy on `submit_stream_for_listener`).
    - **Placement-only changes:** an offset or rate-cap change synthesizes 0 units.
    - **Retry:** after a failure at unit 5, a retry synthesizes from unit 5.
    - **Release:** `_release_artifact` keeps cached clips.
    - **Zero samples:** a 0-sample artifact becomes `add_silent_cue` and the render completes.
  - Green implementation: consult `UnitClipCache` before submitting; feed cached audio to the renderer; replace the samples ≤ 0 failure at `:1953`.

- [ ] **Task 7.3: Off-thread work, `cueIndexAt` bisect, media-cache eviction (FR-6.1, FR-6.2, FR-6.3)**
  <!-- files: src/vienetts_app/ui/subtitle_controller.py, src/vienetts_app/ui/qml/components/SubtitleVideoPreview.qml, src/vienetts_app/ui/qml/SubtitlesTab.qml, tests/unit/test_subtitle_controller.py, tests/unit/test_subtitle_components.py -->
  - Red tests:
    - **Off-thread work:** `_activate_cues_project`/`importVideo` run `refine_cues_speech_bounds` off the GUI thread (thread-recording spy) and apply by generation.
    - **`videoOverrunMs`** returns immediately and computes off-thread. Mix, gain and embed changes do not invalidate its cache.
    - **`projects`/`projectCount`** parse each `project.json` at most once per stat change (spy).
    - **Cue lookup:** `cueIndexAt(ms)` matches a linear oracle on edge times. `syncActiveCueFromVideo` uses it. The QML stub counts no `get(i)` loop per position tick.
    - **Media cache:** `_media_cache_dir` lookups create nothing. Eviction keeps the active entry and removes the oldest beyond the cap off-thread.
  - Green implementation: `_run_bg` plus generation for the three blockers; a cached `projects` model; a bisect index rebuilt on `_emit_cues`; the QML slot call; the eviction sweep at startup and after ASR.

---

## Phase 8: Settings UI, hints, i18n & docs
<!-- execution: parallel -->
<!-- depends: phase3, phase7 -->

- [ ] **Task 8.1: Settings › AI dubbing rows, Subtitles missing-runtime hints, smoke coverage, i18n (FR-7.1, FR-7.2, FR-7.3)**
  <!-- files: src/vienetts_app/ui/qml/settings/SettingsModels.qml, src/vienetts_app/ui/qml/components/SubtitleSourcePanel.qml, tests/smoke/test_ui_tabs.py, tests/unit/test_subtitle_components.py, src/vienetts_app/ui/i18n/vienetts_en.ts, src/vienetts_app/ui/i18n/vienetts_en.qm -->
  - Red tests (an existing consolidated smoke scenario; no new QML instantiation):
    - **New Settings controls exist:** `settingsFfmpegRuntimeRow` (install/remove/status/license link), `settingsWhisperVadRow`, `settingsSileroVadRow`, `settingsSpeakerEmbeddingRow` and `asrCloudFallbackToggle`.
    - **Backend list:** the ASR backend combo is built from `asrBackends`, with no `faster_whisper` when it is absent.
    - **Badge and guidance:** a "Khuyên dùng" badge sits on `large-v3-turbo-q5_0`, and unpublished cells show install guidance.
    - **Import hint:** importing a video with ffmpeg missing shows `subtitleFfmpegMissingHint`, which opens Settings.
    - **Fakes:** the fake controllers gain NOTIFY `@Property` members.
  - Green implementation:
    - Add the QML with `AppTextField`/`AppSwitch`/existing row components, using `Accessible.name` and the 44 px targets.
    - Run `scripts/update_i18n.sh`, translate, and run it again until 0 entries are unfinished.

- [ ] **Task 8.2: README "Dubbing runtimes" section (FR-7.4)**
  <!-- files: README.md -->
  <!-- depends: task1 -->
  - Docs only; still run the full gate before committing. The section covers:
    - FFmpeg: managed install, plus Homebrew (`brew install ffmpeg whisper-cpp`), apt and winget alternatives, and why the packs are GPL and separate;
    - whisper.cpp packs per OS and the Metal note;
    - Whisper and Silero VAD and the speaker-embedding model;
    - the cloud-fallback opt-in;
    - the media-cache cap.

- [ ] **Task 8.3: USER-GATED — publish the media runtime packs (FR-2.6 follow-through)**
  <!-- files: src/vienetts_app/core/media_runtime_manifests.json -->
  <!-- depends: task1, task2 -->
  - Needs an explicit user request, because it requires `git push` and a workflow dispatch, which the git policy forbids without approval. Never run it automatically. Steps once requested:
    1. Push.
    2. Run `gh workflow run media-runtime-build.yml -f publish=true` (default `cells` first, then macOS).
    3. Verify the regenerated manifest commit on `main`.
    4. Pull it **only on the user's request**.
    5. Re-run `pytest tests/unit/test_ai_models.py tests/unit/test_media_runtime_manifest.py` and a real install + transcribe on macOS arm64.
  - Until then, the cells stay `published: false` and the app falls back to PATH or well-known directories with guidance.
