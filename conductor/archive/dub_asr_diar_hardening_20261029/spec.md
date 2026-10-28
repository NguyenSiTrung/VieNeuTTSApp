# Specification: Dubbing & Subtitles Hardening — Local ASR/Diarization Runtimes, Render Safety, Quality & Performance (`dub_asr_diar_hardening_20261029`)

## Overview

A 2026-10-29 audit of the Dubbing & Subtitles pipeline covered `core/asr.py`, `core/diarization.py`, `core/ai_models.py`, `core/video_dub.py`, `core/align.py`, `core/subtitle_project.py`, `core/subtitles.py`, `ui/subtitle_controller.py`, `ui/bg_ops.py` and the Subtitles QML. Two independent review passes reproduced the defects below against the real modules. This track fixes **all** of them.

1. **Local ASR is unusable out of the box on macOS, and nothing provisions ffmpeg on any OS.**
   - `available_whisper_cpp_variants()` (`ai_models.py:736-772`) has no darwin branch, so a Mac downloads `whisper-bin-ubuntu-*.tar.gz`. Upstream `b5454` ships only an xcframework for Apple, not a CLI.
   - faster-whisper is not a dependency, yet Settings always offers it (`SettingsModels.qml:384`).
   - A Finder-launched app never sees Homebrew's PATH.
   - `resolve_ffmpeg` / `resolve_ffprobe` look in `<data_dir>/runtimes/ffmpeg/bin` and then PATH, but nothing installs ffmpeg there, and the README never mentions it. Without ffmpeg there is no video import, extraction or mux.
   - audio.cpp variants omit the Windows CUDA zips that upstream ships, and give Linux arm64 the x64 build.
2. **Concurrency and lifecycle bugs corrupt or wedge a dub.**
   - **H1:** `render()` checks only `_rendering` (`subtitle_controller.py:1695-1705`), and the Render/Play buttons ignore `aiBusy` (`SubtitleActionDock.qml:107`, `:146`).
     - An ASR, diarization or LLM result landing mid-render mixes new text onto old timings, because `_submit_next_unit` re-reads `self._project` (`:1832`).
     - The renderer's start snapshot then overwrites the AI edit.
     - An ASR re-run with fewer cues raises `IndexError` and leaves `rendering` stuck.
   - **H2:** the LLM `progress_cb` (`:2998`) writes `_set_ai_state` from the worker thread with no generation check. A cancelled run switches `aiBusy` back on. All AI progress writes controller state off the GUI thread.
   - **M1:** `cancelRender` is a no-op once the synthesis job has finished (`:1783-1790`).
   - **M2:** `importSrt`/`importVideo` don't cancel AI, and the diarization and LLM `done` handlers check only `self._project is None`. A stale result can therefore replace another project. `on_enrolled` assigns a voice to whichever project is open later.
   - **M3:** export cancel ignores the export generation, both exports share `.retimed_mux.srt`, and cancelling `exportTrack`/`exportSrt` still produces the file.
   - **M4:** `shutdown()` (`:3117-3122`) never cancels AI or export, so `whisper-cli`, `audiocpp_cli` and `ffmpeg` children outlive the app.
   - **M5:** a policy change skips the cue-row rebuild (`:1291-1294`), so the `fitRate`/`overflowMs`/`overBudget` badges go stale.
   - **M7:** `removeProject` deletes the folder before stopping render, export or AI (`:1674-1686`), and on Windows `rmtree` fails on the open `.part.wav`.
   - **L1:** video setters (`:1017-1078`), `setLlmGlossary` (`:720-722`) and re-import (`:1422`) save the in-memory project over the rendered `project.json`. That forces full re-synthesis, and `setSpeakerVoice` skips the staged-invalidation prompt.
   - **L2:** `projectsChanged` is never emitted on import, ASR import or `clear()`.
   - **L3:** `_OneShotBridge` is never `deleteLater()`'d (`bg_ops.py:31-110`), so every background op leaks a QObject plus its closures.
   - **L4:** `exportSrt` lacks the `adjusted or cues` fallback.
   - **L6:** `playSpeakerSample` plays without the guarded playback path.
   - **L6b:** `playSpeakerSample` also bumps the shared `_ai_generation` (`:2834`).
   - **Plausible:** a cue edit during `renderer.finish()` is overwritten. `_can_apply_now()` is True while rendering (`:486`).
3. **Recognition and dub quality.**
   - whisper.cpp runs without VAD, with previous-text conditioning on (`-mc` default), and with SRT-only output, so there are no word timings (`asr.py:582-718`).
   - No backend filters hallucinations: `no_speech_prob`, `compression_ratio` and `avg_logprob` are ignored, and repeated lines and outro phrases ("Hãy subscribe cho kênh Ghiền Mì Gõ…") are kept.
   - A cue that spans a speaker change gets a single max-overlap speaker (`diarization.py:259-290`).
   - Merged speech units are cut by character weight, and each piece is re-anchored at its own cue start (`subtitle_project.py:905-950`, `align.py:556-584`). The repro has 1.5–2.4 s of silence spliced mid-sentence, with hard edges.
   - Push drift never recovers. 4 adjacent 2 s cues with natural lengths `[3500, 2000, 2000, 2000]` at cap 1.5 keep a 333 ms drift (`align.py:250`, `:274-283`).
   - VAD-tightened bounds over-compress. A cue `[1000,4000]` with speech `[1000,1300]` and natural length 2500 is fitted at 1.5× instead of 1.0× (`:266-273`).
   - Silent cues lose `offset_ms` and break SRT order (`align.py:476-489`).
   - Ducking is capped at 200 spans: a 1,500-cue film ducks music across multi-second dialogue-free gaps (`video_dub.py:680-710`, O(n²) collapse).
   - The final `amix` has no limiter.
   - Soft-track language tags are `language[:3]` (`vi`, `zh-`), not ISO 639-2.
   - Parsing edge cases:
     - `[door slams]` becomes a speaker;
     - `[MUSIC PLAYING]` is spoken;
     - `x < y and y > z` becomes `x z`;
     - `♪♪` is kept;
     - VTT `MM:SS.mmm` is rejected;
     - cp1258 / UTF-16-without-BOM files have no fallback;
     - a 0-sample unit fails the whole render (`subtitle_controller.py:1953`).
4. **Performance.**
   - The render cache is all-or-nothing, and placement-only changes re-synthesize every unit.
     - Policy and timing are fingerprinted, and `_release_artifact` deletes raw unit WAVs.
     - A failure at unit N also discards units 0..N-1.
   - ASR and Nemotron diarization run sequentially, although Nemotron turns don't depend on cues.
   - `_cluster_embeddings` is O(n³) (`diarization.py:528-637`).
   - `refine_cues_speech_bounds` re-reads the WAV per cue and scans every turn for every cue (O(C×T)).
   - A WAV input is converted to 16 kHz twice (`input_16k.wav` and `source_wav_16k.wav`). Conversion loads the whole file and resamples with `np.interp`, with no anti-aliasing.
   - The GUI thread is blocked by:
     - speech-bound refinement on import (`:1392`, `:1507`);
     - `videoOverrunMs`, which runs two ffprobes and is invalidated by unrelated `videoChanged` emissions;
     - `projects`/`projectCount` re-parsing every `project.json` on each of 3 QML bindings;
     - linear cue scans per video tick (`syncActiveCueFromVideo`, `SubtitleVideoPreview.qml` `cueAtTime`).
   - ffmpeg progress reads `out_time_ms`, which is in µs, as ms, so progress jumps to 99 %. Non-burn exports report no progress at all because `duration_ms=0`, and the `+faststart` rewrite runs after 1.0.
   - The `_media_cache` (≈230 MB per 2 h video) is never evicted.
5. **Platform hygiene.**
   - `video_dub.py` uses `text=True` without an encoding (`:114, :360, :444, :447, :589, :608, :668`). On Windows a Vietnamese filename in ffmpeg's stderr raises `UnicodeDecodeError`, after which `extract_video_audio` deletes a good WAV.
   - No call sets `CREATE_NO_WINDOW`, so console windows flash. `qwen_engine.py:1073` and `detector.py:146` already set it.
   - ffprobe and extraction calls have no timeout.
   - The whisper.cpp output prefix lands beside the user's file for 16 kHz mono WAV inputs.
   - A cancelled ASR API or LLM request blocks for up to its 120 s timeout.

### Decisions (2026-10-29)

These were taken while drafting from the user's request "create new track to handle all these". Revise them with `/conductor-revise`.

- **D1 — Provisioning: project-published packs plus well-known directories.**
  - Missing native runtimes come from project-built or project-mirrored packs. An opt-in workflow publishes them to the existing gh-pages `packs/` base (the `qwen-gguf-runtime-build.yml` precedent), and each pack is pinned by size and SHA-256 in a shipped manifest. They are installed by the existing staged installer: probe, atomic swap, rollback.
  - The packs are:
    - macOS `whisper-cli`, a static build of the `b5454` source (arm64 with Metal, plus x64 CPU);
    - per-platform `ffmpeg` + `ffprobe`, mirrored from pinned upstream static builds.
  - The ffmpeg packs are GPL builds, because burn-in needs `libx264`, `libvpx-vp9` and `libass`. They run only as separate processes and are never linked into or bundled with the Apache-2.0 app. `LICENSE` and `SOURCE.txt` ship inside each pack.
  - Lookup order is managed runtime → PATH → well-known directories (`/opt/homebrew/bin`, `/usr/local/bin`, `/opt/local/bin`, `/usr/bin`).
  - Publishing requires a push plus a workflow dispatch, so it is a **user-gated** step. Until it happens, cells stay `published: false` and the app guides the user to install via PATH or Homebrew.
- **D2 — Model-based upgrades are in scope.**
  - Silero VAD runs in whisper.cpp (`--vad`, `ggml-silero-v6.2.0.bin`) and, as ONNX, for speech bounds and the acoustic fallback.
  - An ONNX speaker-embedding model replaces the handcrafted 24-dim embedding when it is installed.
  - `onnxruntime`, `kaldi-native-fbank` and `soxr` are already direct dependencies of `vieneu==3.3.0`, so they ship in every build. Both models are optional managed downloads, and the current algorithms remain the fallbacks.
- **D3 — Defaults.**
  - `asr_model` defaults to `large-v3-turbo-q5_0` for new settings; saved values are kept.
  - `asr_language` stays `auto`, because dubbing foreign-language sources is a primary use. The detected language is reported instead.
  - If the configured model isn't installed, the best installed catalog model is used.
- **D4 — The cloud ASR fallback is opt-in.** A new setting `asr_cloud_fallback: bool = False` controls it. Choosing the `openai_api` backend explicitly is unaffected.
- **D5 — Track settings.**
  - Priority is high and there are no track dependencies.
  - Parallel annotations are enabled.
  - Verification is automated only; there is no manual phase gate, as in the previous tracks.
  - The parallel ASR+diarization default is bench-gated (patterns: "Bench-gate every default").

---

## Functional Requirements

### FR-1: Controller concurrency, lifecycle & persistence (`ui/subtitle_controller.py`, `ui/bg_ops.py`, Subtitles QML)

- **FR-1.1 One busy owner (H1).**
  - Each of the following refuses, with a `disabledReason` and the hint channel, while a different owner (`ai`, `render`, `export`) is busy:
    - `render()`;
    - `play()` (when it would start a render);
    - every export slot;
    - every cue-mutating slot: ASR, diarize, LLM refine/translate/fit, cue edit, speaker or voice assignment, policy setters.
  - AI ops refuse while rendering or exporting.
  - The render synthesizes from its start snapshot (`_render_cues`), never from `self._project`.
  - Knob changes during a render are staged until it ends (`_can_apply_now()` is False while rendering).
  - The Render, Play and Export controls are disabled while `aiBusy`.
  - The project delete button is disabled while any owner is busy.
- **FR-1.2 GUI-thread, generation-checked progress (H2).**
  - Every AI worker callback marshals to the GUI thread through a queued relay that carries the op's generation. This covers ASR and diarization stage progress, LLM batch progress, and model/runtime download progress in `ui/controller.py`.
  - `_set_ai_state` runs only on the GUI thread.
  - Progress or results that arrive after cancel, finish or a project switch are dropped.
- **FR-1.3 Per-op generations.**
  - `playSpeakerSample` uses its own `_sample_generation` and plays through the guarded playback path used by `_play_track`.
  - A cached `ref_<speaker>.wav` is reused only while its sidecar key still matches: the chosen clip's bounds plus the video's stat.
- **FR-1.4 Project switch & removal (M2, M7).**
  - `importSrt`, `importVideo`, `openProject` and `removeProject` cancel in-flight AI, render and export first.
  - Every async delivery compares a captured `project_id`, and a mismatch drops the result. This includes diarization, LLM and `on_enrolled`, which persists the voice into the project that started enrollment.
  - `removeProject` runs in this order: cancel → clear → release video → delete. It also removes that project's `_media_cache` entry and managed SRT when no other project references them.
- **FR-1.5 Cancel & shutdown (M1, M4).**
  - `cancelRender` fails the render immediately (`_fail_render("")`), whatever the job state.
  - `shutdown()` cancels AI and export, then terminates every registered child process (`whisper-cli`, `audiocpp_cli`, `ffmpeg`, `ffprobe`): terminate → bounded wait → kill.
- **FR-1.6 Persistence safety (L1).**
  - Field setters (`videoMixMode`, `videoBgGainDb`, embed mode, subtitle language, `setLlmGlossary`) and re-import apply a load-modify-save of only the changed field onto the latest stored project. A render's `stats`/`adjusted`/fingerprint is never overwritten.
  - `renderer.finish()` merges render outputs onto the latest stored project, so a concurrent cue edit survives.
  - `setSpeakerVoice` goes through the staged-invalidation prompt and sets `_stale_reason`.
- **FR-1.7 Export correctness (M3, L4).**
  - Export cancel checks are generation-based (`generation != self._export_generation`).
  - The mux temp SRT is unique per export and removed in `finally`.
  - `_export_target` reserves distinct names for overlapping exports.
  - A cancelled `exportTrack`/`exportSrt` leaves no output file.
  - `exportSrt` uses `project.adjusted or project.cues`, the same source `canExportSrt` and `exportVideo` use.
- **FR-1.8 Bridge hygiene (L3).** `_OneShotBridge` drops its `work`/`on_done`/`on_error` references and `deleteLater()`s itself after delivering success or error.
- **FR-1.9 Fresh rows (M5, L2).**
  - `_rebuild_for_policy` always calls `_emit_cues()`; `DictListModel` diffs per role.
  - `projectsChanged` fires once per list-affecting change: import, ASR import, clear, render complete, remove.

### FR-2: Native runtime provisioning & process hygiene (`core/processes.py`, `core/ai_models.py`, `core/video_dub.py`, `core/asr.py`, `core/diarization.py`, workflow, manifest, `ui/controller.py`)

- **FR-2.1 One process helper.** `core/processes.py` gains:
  - `find_executable(name, *, managed_dirs)`, using the D1 lookup order;
  - `run_cli(...)` / `spawn_cli(...)`, which apply:
    - `creationflags=CREATE_NO_WINDOW` on Windows;
    - `encoding="utf-8", errors="replace"` for text pipes;
    - timeouts: ffprobe 30 s, extraction `max(120 s, 2 × media duration)`, `--help`/`-version` probes 15 s;
    - an optional `cancel_check`;
  - a child registry with `terminate_children(timeout)`.
  
  Every native spawn in `video_dub.py`, `asr.py`, `diarization.py` and `ai_models.py` goes through these helpers. `cli_command()` keeps its Windows shebang-stub dispatch.
- **FR-2.2 macOS whisper-cli.**
  - `available_whisper_cpp_variants()` gains a darwin branch that resolves `macos-arm64-metal` and `macos-x64-cpu` cells from the shipped manifest. `auto`/`metal` map to the Metal cell on arm64.
  - An unpublished cell reports "not yet available — install `whisper-cpp` with Homebrew" instead of downloading a foreign binary.
- **FR-2.3 Managed ffmpeg runtime.**
  - Per-platform cells: windows-x64, linux-x64, linux-arm64, macos-arm64, macos-x64. Each holds `ffmpeg` + `ffprobe`, installed staged into `<data_dir>/runtimes/ffmpeg/bin`.
  - The staged probe requires `-encoders` to list `libx264`, `aac` and `libvpx-vp9`, and `-filters` to list `subtitles` (libass). A failing pack is rejected and the previous runtime is kept.
  - `ffmpeg_runtime_status()`, `install_ffmpeg_runtime()` and `remove_ffmpeg_runtime()` mirror the whisper.cpp and audio.cpp installers.
- **FR-2.4 audio.cpp variants.**
  - Windows offers the upstream `cuda12.4`/`cuda13.3` CLI zips when the existing hardware probe finds an NVIDIA driver. Asset names and sizes are pinned.
  - Linux arm64 reports "unsupported" instead of downloading x64.
- **FR-2.5 Backend availability.**
  - `AppController.asrBackends` lists `faster_whisper` only when `importlib.util.find_spec("faster_whisper")` succeeds.
  - A stored `faster_whisper` with no module present resolves to `auto` and adds a note.
- **FR-2.6 Publishing workflow.**
  - `.github/workflows/media-runtime-build.yml` is opt-in via `workflow_dispatch`, with inputs `cells`, `publish` and `downloads_base`. A plan job shrinks the matrix; job-level `if:` never names `matrix`.
  - It builds static `whisper-cli` at the pinned `b5454` source and verifies `--help` and `--vad`.
  - It mirrors pinned upstream ffmpeg static builds and verifies their upstream checksums. Candidate sources are BtbN FFmpeg-Builds for Windows/Linux and ffmpeg.martin-riedl.de for macOS; the final choice and versions are recorded in `packaging/media-runtime-requirements.json`.
  - It adds `LICENSE`/`SOURCE.txt`, probes, and locks each pack by size and SHA-256.
  - `scripts/lock_media_runtime.py` + `scripts/render_media_runtime_manifests.py` render `src/vienetts_app/core/media_runtime_manifests.json` (listed as frozen data in `packaging/vienetts-app.spec`).
  - With `publish=true` it pushes packs to gh-pages and commits the manifest to main.

### FR-3: ASR quality & throughput (`core/asr.py`, `core/audio_prep.py`, `core/llm_client.py`, `core/models.py`, `core/settings.py`)

- **FR-3.1 whisper.cpp invocation.**
  - Always pass `-mc 0`.
  - When the catalog model `silero-v6.2.0` is installed, pass `--vad -vm <path>`. The model lives in `<data_dir>/models/whisper/` and is pinned by size and SHA-256.
  - Pass `-ojf`, with the `-of` prefix always inside the media cache directory.
  - Segments and word timings come from the JSON-full output: token offsets are merged into words and special tokens skipped. SRT is not parsed when JSON exists.
  - ASR caches carry an `asr_version` field, and a mismatch triggers a clean re-run.
- **FR-3.2 Hallucination filter.** `filter_hallucinations(segments, *, duration_ms, notes)` applies to every backend:
  - drop a segment when `no_speech_prob > 0.6` and `avg_logprob < -1.0`;
  - drop a segment when `compression_ratio > 2.4`;
  - collapse ≥ 3 consecutive identical normalized lines to one;
  - remove known outro or credit phrases (a vi/en list, e.g. "Hãy subscribe cho kênh Ghiền Mì Gõ…", "Cảm ơn các bạn đã theo dõi", "Subtitles by the Amara.org community") only when the segment has low confidence or sits in the final 30 s with no VAD speech.
  
  Counts are reported in `notes`. The whisper.cpp JSON supplies token probabilities for `avg_logprob`; faster-whisper and API `verbose_json` pass their native fields.
- **FR-3.3 Defaults (D3).**
  - `asr_model` defaults to `large-v3-turbo-q5_0`.
  - When the configured model isn't installed, the largest installed catalog model is used, with a note. With none installed, a typed `AsrBackendMissing` names the Settings download.
  - The detected language is reported in `notes` and cached.
  - Missing-backend detection uses typed exceptions, not Vietnamese message matching.
- **FR-3.4 Cloud fallback opt-in (D4).**
  - `asr_cloud_fallback: bool = False` has its own per-field clamp, and a non-bool value is dropped.
  - Auto mode appends `openai_api` only when this flag is set and an ASR URL is configured.
  - Every cloud transcription adds the note "Đã dùng Whisper API (đám mây)".
- **FR-3.5 Cancellable network requests.**
  - `core/llm_client.py` gains `cancellable_request(...)`, which runs the blocking `urlopen` on a daemon thread and returns `cancelled` within ≤ 1 s of `cancel_check` turning true; the abandoned result is discarded. Both `post_chat_completion` and the ASR upload use it.
  - The ASR API sends `timestamp_granularities[]=word` and `timestamp_granularities[]=segment`.
- **FR-3.6 Shared 16 kHz prep (`core/audio_prep.py`, new).**
  - `ensure_source_16k(media, cache_dir, ...)` produces one `source_16k.wav` plus sidecar per media for both ASR and diarization, so WAV inputs are converted once. Legacy `input_16k.wav` and `source_wav_16k.wav` files are adopted when their sidecar matches.
  - Audio inputs are resampled in blocks with `soxr.ResampleStream` (anti-aliased, bounded memory); video inputs are extracted by ffmpeg.
- **FR-3.7 Threads & memory.**
  - The automatic `asr_threads` uses the Apple Silicon performance-core count (`sysctl hw.perflevel0.physicalcpu`), otherwise `os.cpu_count() // 2`, capped at 8.
  - `_FASTER_WHISPER_CACHE` is released after 10 min idle and at `shutdown`.

### FR-4: Diarization quality & throughput (`core/diarization.py`, `core/onnx_audio.py`, `core/transcribe_pipeline.py`)

- **FR-4.1 Speaker-turn splitting.**
  - `split_segments_at_turns(segments, turns, *, min_part_ms=600, min_words=2)` splits an ASR segment at the word boundary nearest each turn change when every part has ≥ 600 ms and ≥ 2 words. It runs before cue normalization, and parts are labelled by max overlap.
  - Without word timings, the current max-overlap behaviour remains.
- **FR-4.2 Concurrent ASR + Nemotron.**
  - When diarization resolves to `nemotron_gguf`, turn extraction runs concurrently with ASR in `core/transcribe_pipeline.py`. Thread budgets are split (`threads // 2` each, minimum 1), progress is combined and monotonic, and a cancel stops both.
  - A diarization failure keeps the ASR output.
  - The acoustic fallback stays sequential, because it needs cues.
  - The concurrent default is bench-gated by `scripts/benchmarks/transcribe_parallel.py`. Enable it only if it wins ≥ 15 % wall time on the executing host. If no real runtime is available there, ship it disabled and file a follow-up bead.
- **FR-4.3 Algorithmic costs.**
  - `_cluster_embeddings` uses a cached similarity matrix with Lance–Williams average-linkage updates, O(n²) per merge pass or better. Its labels must equal the current implementation's, which is kept as a test oracle.
  - `refine_cues_speech_bounds` opens the WAV once and sweeps sorted cues and turns with two pointers (O(C + T)).
  - Nemotron progress is relayed in ≥ 10 monotonic steps, parsed from `audiocpp_cli` output or estimated from elapsed time against duration.
- **FR-4.4 Reference-clip scoring.** `select_speaker_reference_clip` scores each candidate on:
  - duration within [3, 12] s;
  - VAD speech ratio;
  - RMS level;
  - clipped-sample ratio;
  - no overlap with other speakers' turns.
  
  Duration breaks ties.
- **FR-4.5 Optional ONNX models (D2).**
  - The managed catalog gains `silero-vad` ONNX (MIT) and one speaker-embedding ONNX model (Apache-2.0 or MIT). Candidates are WeSpeaker ResNet34-LM and 3D-Speaker CAM++, with `kaldi-native-fbank` features.
  - The winner is chosen by DER on a generated multi-voice fixture (`scripts/benchmarks/diarization_der.py`), then pinned by size and SHA-256 and recorded in learnings.
  - With Silero installed, speech bounds and the acoustic fallback segmentation use it. With the embedding model installed, the acoustic fallback clusters neural embeddings.
  - Without them, the energy VAD and 24-dim embedding run unchanged.
  - Inference goes through an injectable `session_factory` seam.

### FR-5: Render, alignment & mux (`core/align.py`, `core/subtitle_project.py`, `core/video_dub.py`, controller render path)

- **FR-5.1 Per-unit synthesis cache.**
  - Raw unit clips are cached under `<project>/units/<sha256>.wav`, keyed by:
    - unit text;
    - voice id plus the voice file's stat stamp;
    - engine profile and model revision;
    - speed;
    - synthesis-context fingerprint.
  - The cache is LRU with a 2 GB default cap. `_release_artifact` no longer deletes cached clips.
  - Re-rendering synthesizes only units whose key is missing, and re-runs placement for all units. Placement-only changes (offset, rate cap, max gap, mode, timing, speech bounds) synthesize nothing. A failure at unit N keeps units 0..N-1 cached.
  - A 0-sample unit is placed with `add_silent_cue` instead of failing the render.
- **FR-5.2 Merged-unit placement.**
  - Only the first piece of a merged unit anchors to its cue's SRT start, and later pieces follow contiguously, so a unit never gains silence its synthesis didn't have.
  - `split_unit_audio` snaps each weighted split to the lowest-RMS 10 ms frame within ±150 ms, bounded by its neighbours.
  - Every split edge gets the 5 ms `apply_micro_fade`.
- **FR-5.3 Alignment fixes.**
  - When `start_ms > srt_start` (the cue was pushed), rates are planned against the effective windows `speech_end - start_ms` and `extended_end - start_ms`, so drift is absorbed up to `rate_cap`.
  - The borrow branch uses `extended_end = max(srt_end, borrow_cap)`, still bounded by `next_start - MIN_BREATH_GAP_MS`.
  - The no-next-cue branch follows the archived spec (`max(...)`).
  - Silent fits get `offset_ms` (clamped to ≥ 0) and are clamped between their neighbours.
- **FR-5.4 Ducking at scale.**
  - Ducking scales to ≥ 5,000 spans without merging gaps longer than `merge_gap_ms`. A precomputed envelope (an `asendcmd` command file or a low-rate gain track) is passed by path, replacing the inline `between()` sum and the O(n²) collapse loop.
  - The command line stays under 8 KB at any span count.
- **FR-5.5 Mix safety & tags.**
  - The final mix ends with `alimiter` (limit −1 dBFS).
  - Soft subtitle tracks are tagged in ISO 639-2/B: `vi`→`vie`, `en`→`eng`, `zh*`→`chi`, `ja`→`jpn`, `ko`→`kor`, …; unknown codes become `und`.
- **FR-5.6 ffmpeg progress.**
  - Progress is parsed from `out_time_us`, or from `out_time_ms` treated as µs.
  - Duration is probed whenever a progress callback is passed, with the dub WAV duration as fallback.
  - Progress is monotonic and capped at 0.95 until the `+faststart` finalize completes.
- **FR-5.7 Crash-consistent commit & fingerprint hygiene.**
  - The render commit order is: timeline (with `render_id`) → `project.json` (with `render_id`) → promote `track.wav`. The splice save-back (`:1084-1195`) follows the same order.
  - `cached_render` adopts a track only when all three `render_id`s agree.
  - Speakers that no cue uses, and whitespace-padded voice names, no longer change `render_fingerprint` after a reload. Existing digests stay byte-identical for normalized projects.

### FR-6: GUI responsiveness & subtitle parsing (`ui/subtitle_controller.py`, `SubtitleVideoPreview.qml`, `SubtitlesTab.qml`, `core/subtitles.py`)

- **FR-6.1 Off-thread work.**
  - These move to `_run_bg` and apply by generation:
    - `refine_cues_speech_bounds` on activation and import;
    - the `videoOverrunMs` probes (only invalidated by the wav or video stat, not by mix/gain/embed changes);
    - the `projects` scan, whose rows are cached and keyed by each `project.json` stat stamp and served from a property.
  - The GUI thread never runs ffprobe or reads WAV data.
- **FR-6.2 Cue lookup.**
  - A sorted start array plus `bisect` backs `syncActiveCueFromVideo` and a new `cueIndexAt(ms)` slot.
  - `SubtitleVideoPreview.qml` calls the slot instead of scanning rows through `m.get(i)`.
- **FR-6.3 Media-cache eviction.**
  - `subtitles/_media_cache` is capped at 5 GB, and entries unused for 30 days are evicted (LRU by sidecar mtime).
  - Eviction runs off-thread at startup and after ASR, and never touches the active project's entry.
  - `_media_cache_dir` lookups no longer create directories.
- **FR-6.4 Parsing edge cases.**
  - Bracketed or parenthesised sound cues (`[door slams]`, `(laughs)`, `[MUSIC PLAYING]`) are not speakers. Alone, they are non-speech: kept in transcript output and skipped in synthesis.
  - Only real tags are stripped, so `x < y and y > z` survives.
  - Punctuation-, emoji- and `♪`-only cues are non-speakable.
  - VTT `MM:SS.mmm` parses.
  - Decoding falls back BOM → UTF-8 → UTF-16 (NUL-pattern heuristic) → cp1258 → cp1252, with a note.

### FR-7: Settings UI, hints, i18n & docs

- **FR-7.1 Settings › AI dubbing card.**
  - An FFmpeg runtime row: install, repair, remove, version, source/license link.
  - A Whisper VAD model row.
  - Silero VAD and speaker-embedding model rows.
  - An "Allow cloud fallback" toggle (`asrCloudFallbackToggle`).
  - The ASR backend list bound to `asrBackends`.
  - A "Khuyên dùng" badge on `large-v3-turbo-q5_0`.
  - Unpublished pack cells show Homebrew/apt/winget guidance.
- **FR-7.2 Hints.**
  - Importing a video without ffmpeg shows `subtitleFfmpegMissingHint`, which opens Settings.
  - Transcribing without a usable ASR runtime or model shows an actionable hint.
- **FR-7.3 i18n.** `scripts/update_i18n.sh` leaves 0 unfinished entries.
- **FR-7.4 README.** A "Dubbing runtimes" section covering:
  - FFmpeg: managed install, Homebrew/apt/winget, and why the packs are GPL;
  - whisper.cpp packs per OS;
  - VAD and the speaker-embedding models;
  - GPU notes;
  - the cloud-fallback opt-in;
  - the media-cache cap.

---

## Non-Functional Requirements

- **No new resolved packages.** Code imports only the stdlib, numpy, soundfile and the packages `vieneu==3.3.0` already requires: `onnxruntime`, `kaldi-native-fbank`, `soxr`. Any directly imported package is declared in `pyproject.toml` at the already-locked range, and `uv.lock` gains no new packages.
- **Backward compatibility.**
  - Existing `settings.json`, `project.json`, ASR/diarization caches, managed SRTs and render fingerprints keep loading.
  - Changed cache formats are versioned and re-run cleanly; they are never misread.
  - `render_fingerprint` digests of normalized projects stay byte-identical.
- **Licensing.**
  - Every downloadable runtime or model records its license in the catalog or manifest and ships its notice.
  - No GPL code is linked into or bundled with the frozen app.
- **Determinism.**
  - Tests never touch the network or download real models.
  - Native CLIs are pure-Python shebang stubs dispatched by `cli_command()` (Windows `WinError 216` rule).
  - ONNX paths are tested through the `session_factory` seam, plus a skip-if-absent real-model test.
  - Cancel tests fire on events, never on fixed waits.
- **Thread safety.** Controller state is written only on the GUI thread. Every async delivery checks the op generation and `project_id`.
- **Performance budgets.**
  - Re-rendering after editing 1 cue of 300 synthesizes exactly 1 unit; a policy-only change synthesizes 0.
  - Clustering 2,000 embeddings takes < 2 s; speech-bound refinement of 1,500 cues over 1 h takes < 3 s. Both are `benchmark`-marked tests.
  - Extra RSS of the 16 kHz conversion stays < 64 MB, whatever the input length.
  - No ffprobe call or WAV read happens on the GUI thread; spies assert the calling thread.
- **Gates.** `ruff check .`, `ruff format --check .`, `pytest`, `pytest -m benchmark` (for the new benchmark tests and `scripts/benchmarks/` changes) and `scripts/update_i18n.sh` all pass. Commit per task with a `git notes add` summary. Never push.

---

## Acceptance Criteria

1. **Busy-owner gating.**
   - While a fake ASR or LLM op runs, `render()` and `play()` start nothing and the Render button is disabled with a reason. While rendering, `refineWithLlm` refuses.
   - A cue edit injected mid-render never reaches a later unit's synthesized text.
2. **Stale callbacks.**
   - An LLM progress callback fired from a worker thread after `cancelAiTask()` leaves `aiBusy` False, and every `_set_ai_state` call is on the GUI thread.
   - An AI op started during a speaker-sample extraction completes normally.
3. **Project switching and removal.**
   - `importSrt` during ASR cancels it and keeps the imported project. A diarization or LLM result for project A is dropped after switching to project B.
   - `removeProject(active)` during render, export or AI cancels, clears, releases and then deletes — in that order — with no error, and removes its media-cache entry.
4. **Cancel and shutdown.**
   - `cancelRender()` after the synthesis job has completed ends the render at once, and no further unit is submitted.
   - `shutdown()` leaves no stub child process alive.
   - 1,000 background ops leave at most 1 live `_OneShotBridge`.
5. **Persistence.**
   - Changing `videoMixMode` on a rendered project keeps `stats`, `adjusted` and the fingerprint in `project.json`. Moving a policy knob away and back re-adopts the render with 0 synthesis.
   - Two overlapping exports use distinct temp SRTs, and the cancelled one stops.
   - A legacy rendered project exports a non-empty SRT.
6. **Lookup and process hygiene.**
   - With an empty PATH on darwin, a stub `whisper-cli` and `ffmpeg` in `/opt/homebrew/bin` are found.
   - On `nt`, every native spawn carries `CREATE_NO_WINDOW` and UTF-8 decoding. A stub ffmpeg printing `Phụ đề.mp4` to stderr keeps the extracted WAV.
7. **Catalog.**
   - On darwin, the whisper.cpp variants come from the manifest's macOS cells, and an unpublished cell reports unavailable rather than offering the Ubuntu asset.
   - A stub ffmpeg pack installs and verifies, while a pack missing `subtitles` is rejected and the previous runtime is kept.
   - On Windows with an NVIDIA driver, the audio.cpp CUDA variants are offered; Linux arm64 reports unsupported.
8. **whisper.cpp invocation.** whisper.cpp argv contains `-mc 0`, `-ojf`, and `--vad -vm …` exactly when the VAD model exists. Outputs appear only in the media cache, and cues carry word bounds parsed from JSON-full.
9. **Hallucination filter.** It drops a 4× repeated line and a low-confidence trailing "Hãy subscribe cho kênh Ghiền Mì Gõ" segment, keeps a confident genuine repeat, and reports counts.
10. **ASR defaults and fallback.**
    - New settings default `asr_model` to `large-v3-turbo-q5_0`, while a saved `base` is kept. With only `small` installed, `small` is used and a note is added.
    - Auto mode with an ASR URL and `asr_cloud_fallback=False` never uploads.
    - A delayed mock server cancels within 1 s, for both LLM and ASR uploads.
11. **16 kHz conversion.** A 44.1 kHz WAV is converted to 16 kHz exactly once across ASR and diarization. A 10 kHz tone at 48 kHz leaves < −40 dB alias energy after resampling.
12. **Speaker turns and concurrency.**
    - A segment whose words span turns A→B, with each side ≥ 600 ms and ≥ 2 words, becomes two cues with the right speakers. A segment without words keeps max overlap.
    - With stub runners, Nemotron starts before ASR finishes, and the result equals the sequential path.
13. **Clustering and speech bounds.** `_cluster_embeddings` matches the oracle on 50 seeded sets. `refine_cues_speech_bounds` opens the WAV once and matches the previous bounds. Both meet the benchmark budgets.
14. **Unit cache.**
    - Editing 1 of 30 cues re-synthesizes exactly 1 unit; an offset change re-synthesizes 0.
    - After a failure at unit 5, a retry synthesizes from unit 5.
    - A 0-sample unit renders as silence.
15. **Alignment regressions.**
    - The `[3500, 2000, 2000, 2000]` repro ends within 150 ms of its anchor, and the borrow repro fits at 1.0×.
    - Silent cues under offset 5000 give a monotonic SRT.
    - The merged-unit repro has no mid-sentence silence beyond the snapped pause, with faded split edges.
16. **Mux.**
    - A stub emitting `out_time_ms=5000000` reports 5 s, and a non-burn export reports progress.
    - 1,500 duck spans go through an envelope file: the command line stays under 8 KB and gaps longer than `merge_gap_ms` stay at full level.
    - The mix contains `alimiter`, and `vi` is tagged `vie`.
17. **Crash consistency.** A crash injected after the timeline write leaves the project un-adopted (stale); it is never "rendered" with mismatched stats.
18. **GUI responsiveness.**
    - Import and activation run speech-bound refinement and ffprobe off the GUI thread.
    - `projects` parses each `project.json` at most once per stat change.
    - The preview uses `cueIndexAt` with no per-tick row scan.
    - Media-cache eviction keeps the active entry.
19. **Parsing.** `[door slams] Get out!` has no speaker; `01:02.500 --> 01:04.000` VTT parses; `♪♪` is skipped in synthesis; a cp1258 SRT decodes with a note.
20. **Gates.** `ruff check .`, `ruff format --check .`, `pytest`, `pytest -m benchmark` and `scripts/update_i18n.sh` (0 unfinished) all pass.

---

## Out of Scope

- Bundling ffmpeg or whisper-cli inside the frozen app; they remain managed downloads or PATH tools.
- Building ffmpeg from source in CI. The packs mirror pinned upstream static builds; revisit if a source is retired.
- **Publishing the packs itself.** The workflow is delivered, but dispatching it with `publish=true` requires `git push` and is a user-gated follow-up (plan Task 8.3).
- New ASR engines (Parakeet, PhoWhisper fine-tunes), pyannote or NeMo integrations, and streaming or real-time ASR.
- LLM prompt changes, TTS engine changes, or Subtitles layout changes beyond the listed rows and hints.
- GPU video encoders for burn-in.
