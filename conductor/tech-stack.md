# VieNeuTTS Desktop App — Tech Stack

> Documenting the **existing** stack from `PROJECT_PLAN.md` (brownfield).
> No proposed changes — verified against the plan.
<!-- refreshed 2026-09-10: no pyproject/uv.lock dep drift (vieneu 3.3.0, PySide6 6.11.2, app v0.1.14); v0.1.9 -cuda bundles withdrawn in v0.1.11 for managed CUDA runtime; MP3 export via libsndfile (no new dep); benchmark marker exclusion; release notes v0.1.1–v0.1.14 -->
<!-- refreshed 2026-09-10 (2): CI now main-pushes + PRs only (feature-branch pushes excluded); v0.1.14 pro-audio Studio deck; Paragraph tab document/queue mode split; Settings engine/CUDA/model-source card split; test items 872 collected / 860 selected (12 benchmark deselected) -->
<!-- refreshed 2026-09-14: no pyproject/uv.lock dep drift (vieneu 3.3.0, PySide6 6.11.2, app v0.1.14); SRT dub/transcript studio added (stdlib-only .srt parse + in-app WSOLA rate fit — no new direct dep); Paragraph tab gains a third "Phụ đề (SRT)" mode; test items 1036 collected / 1024 selected (12 benchmark deselected) -->
<!-- refreshed 2026-09-21: track `qwen_multiengine_20260920` implemented on `main` (unreleased, app v0.1.16) — optional isolated Qwen 0.6B engine profiles: their dependency stack (qwen-tts 0.1.1 / transformers 4.57.3 / torch 2.8.0 CPU/CUDA/MPS) lives ONLY in the app-managed runtime install, never in the root pyproject/uv.lock (verified: no dep drift); app-side additions are `core/engine_profiles.py`, `core/synthesis_context.py`, `core/text_segmentation.py`, `core/voice_profiles.py`, `core/qwen_protocol.py`, `core/qwen_engine.py`, `core/qwen_runtime.py`, `core/qwen_model_manager.py` + their pinned manifests, `workers/qwen_host.py`, the `EngineState`/`EngineProfilePicker`/`LanguagePicker` QML components, maintainer `scripts/lock_qwen_runtime.py` + `scripts/fetch_qwen_models.py`, the opt-in `scripts/qwen_release_smoke.py` + `.github/workflows/qwen-runtime-smoke.yml`, and the spec's Qwen-stack excludes + `--qwen-host` re-dispatch; test items 1732 collected / 1720 selected (12 benchmark deselected) -->

<!-- refreshed 2026-09-16: no pyproject/uv.lock dep drift (vieneu 3.3.0, PySide6 6.11.2, pypdf 6.16.2, app v0.1.16); v0.1.15/v0.1.16 Studio tabs are QML-only + stdlib (no new direct dep) — StudioTab decomposed into StudioRackModule/StudioParamRow/StudioClipRow, AppCard gains clickable/cardHovered/cardClicked, AppNumberField added for precise numeric FX entry; test items 1055 collected / 1054 selected (12 benchmark deselected), gate 1054 passed + 1 device-dependent real-QAudioSink host failure (bead VieNeuTTSApp-3iy) -->
<!-- refreshed 2026-09-27: no pyproject/uv.lock dep drift (vieneu 3.3.0, PySide6 6.11.2, pypdf 6.16.2, python-docx 1.2.0, platformdirs 4.11.5, app v0.2.0); GGUF runtime packs now PUBLISHED for linux-x64-cpu, windows-x64-cpu, macos-arm64-cpu, macos-arm64-metal (download URLs in core/qwen_gguf_runtime_manifests.json) but only linux-x64-cpu is probe-verified — windows-x64-cuda and linux-x64-cuda stay unpublished (bead ysl8.7); CI is lint + parallel unit/smoke matrix legs on linux-x64 and windows-x64 (8710f7f, 2026-09-22); 2026-09-27 test-reduction batch (ae93c6a…f5b40e8) took the suite to 2190 collected / 2178 selected (12 benchmark deselected) -->
<!-- refreshed 2026-09-28: no drift — only housekeeping commits since the 2026-09-27 refresh (ruff-format fix 5097342, .bak cleanup e30b349, beads export f95617a); no pyproject/uv.lock or CI changes; suite figures 2190/2178 carried forward (collection not re-run — no .venv in this refresh env) -->
<!-- refreshed 2026-10-07: no root dep drift (vieneu 3.3.0, PySide6>=6.7, app v0.2.0) but dev tooling changed — pytest-cov removed from dev extras and [tool.coverage.*] config dropped (20d1270, 2026-10-04); suite now 1754 collected / 1742 selected (12 benchmark deselected; re-verified live 2026-10-07 via pytest --collect-only); GGUF upstream pin bumped to qwentts.cpp 6fae9291 / ggml 40e16e4a (ggml 0.25.3, 801ffc7) and the 4 published packs re-locked/re-published from CI (31842ba, run 36454786954; pack manifests hold exactly 4 cells — the old "six locked cells" figure is obsolete) — per bead ysl8.7 the pre-bump probe evidence (0cbde9b packs) is stale for all four cells until the smoke gate re-runs per cell (compat doc §6 still records linux-x64-cpu verified from old-pin evidence; macos cells 1/4 on Apple M4); CUDA cells still unpublished; follow-ups 9wun (gh-pages pruning) + 1sf0 (stale pin comments); NEW: audiobook auto-split of oversize chapters (1244363, core/chapter_split.py) -->
<!-- refreshed 2026-10-10: track `perf_hardening_20261009` (2026-10-09, archived 2026-10-10; epic w1in closed) — dep floor `PySide6>=6.9` (was >=6.7; QtQuick.Effects RectangularShadow; uv.lock requires-dist only, resolved 6.11.2 unchanged); packaged builds ship ahead-of-time QML units (`packaging/qml_aot.py`, `qmlcachegen --only-bytecode`; release.yml asserts every bundled .qml/.js has its .qmlc/.jsc); BLAS thread cap defaults to 1 (`Settings.blas_threads`, `core/performance.apply_blas_thread_cap`, applied in `__main__` before numpy loads; RTF 1.43 → 0.80 on 4-core arm64, `docs/performance/tuning-vieneu.md`); ORT/export-chunk/PyTorch-batch knobs added with SDK defaults unchanged; new `ui/list_models.py` (DictListModel), `OrderedExecutor` in `ui/bg_ops.py`, stat-stamp `StampLedger` in `core/managed_install.py` + Settings "Verify files"; suite now 1955 collected / 1937 selected (18 benchmark deselected; verified live 2026-10-10) -->
<!-- refreshed 2026-10-12: no pyproject/uv.lock dep drift since 2026-10-10. Track `ui_shell_redesign_20261010` archived (QML-only: TransportDock, StatusBar, AppSegmented, CreateTab/VoicesTab, offscreen screenshot matrix in scripts/generate_screenshots.py; no new deps). Windows hardening (dcaeb04): qwen_gguf_abi passes ASCII-safe 8.3 path bytes to qwen.dll (actionable error if none), qwen_gguf_host adds SetStdHandle(STD_OUTPUT_HANDLE) on top of dup2, core/processes probes liveness via WaitForSingleObject (exit-code fallback), managed_install applies to_extended() for MAX_PATH, GGUF pack links fall back to hardlink/byte copy and verify by content, import-check subprocess uses CREATE_NO_WINDOW, longer AV/OneDrive lock retries. macOS audit (a55b80f): frozen Info.plist LSMinimumSystemVersion 14.0 (onnxruntime 1.29 arm64 wheel is macosx_14_0; source runs keep the lower floor), host_cell_key rejects macOS < 13 for GGUF packs, _ensure_tls_ca_bundle sets SSL_CERT_FILE=certifi.where() and the spec collects certifi (hiddenimport + cacert.pem), native osascript error alert, normalize_local_path/path_to_file_url authority + percent-encoding fixes, release.yml dmg stages an /Applications symlink -->
<!-- refreshed 2026-10-19: no external dep drift (app v0.3.0 in pyproject.toml/uv.lock since 723948c; tagged v0.3.0 at 8db2677 after 0d190da file-based shell driver and 8db2677 offscreen/minimal crash-dialog guard). Post-v0.3.0 on main: Ko-fi support links in sidebar, Settings > Updates, README, and .github/FUNDING.yml (2f44ee1, coffee icon in AppIcon.qml, KOFI_URL in core/updates.py, controller.supportUrl); suite reduced ~20% via semantic merges (57634dc + bead 7coj d6f70bb..3a5ed39) to 1504 collected / 1486 selected (18 benchmark deselected, verified live 2026-10-19) -->
<!-- refreshed 2026-10-19 (2): no root pyproject/uv.lock dep drift. Post-v0.3.0 on main: (1) Windowed SRT Studio integration (e94d045..02b9181, bead n5ts): ranged WAV reads (`read_wav_range` in `core/audio.py`), bounded 50-cue Studio window loader (`load_project_from_subtitle`, `refit_subtitle_clips`, `set_subtitle_clip_speed` in `core/studio.py`), and 64k-frame chunked save-back (`splice_studio_window_into_subtitle` in `core/subtitle_project.py`); (2) SubtitleCard export button label bound to `exportFormat` (cf39f07); (3) End-to-end Video Dubbing, Whisper ASR, Speaker Diarization, LLM Refinement & Managed AI Model Downloads (ba69a5a..ab1aad1, epic b3nq): `core/video_dub.py` (ffmpeg audio extraction + `-c:v copy` video muxing with `replace`/`duck` mix and soft subtitles), `core/asr.py` (`faster-whisper` / `whisper.cpp` CLI / `openai-whisper` / OpenAI-compatible `/audio/transcriptions` + clause-aware segment normalization), `core/diarization.py` (`nemotron_gguf` via `audiocpp_cli --mode diarize` on CPU/GPU + NVIDIA NeMo RTTM + zero-dep NumPy acoustic clustering + 1-click reference clip extraction for voice cloning), `core/llm_refine.py` (stdlib-urllib OpenAI-compatible `/chat/completions` for `refine_asr`/`fit_timing`/`translate_dub`), and `core/ai_models.py` + `SettingsModels.qml` (`settingsAiDubbingCard` downloads for Whisper GGUF, `audio-cpp/Nemotron-3-Diarization-GGUF` Q8_0/BF16, and `0xShug0/audio.cpp` v0.9.2 `audiocpp_cli`); suite now 1530 collected / 1512 selected (18 benchmark deselected, verified live 2026-10-19) -->
<!-- refreshed 2026-10-20: no root pyproject/uv.lock dep drift. Post-v0.3.0 on main: (1) Dedicated top-level `SubtitlesTab.qml` (`Lồng tiếng & Phụ đề`, `aef1d87`): promoted `subtitles` to a sixth destination in `ShellBridge.TABS`, narrowed `CreateTab` `CREATE_MODES` to `compose`/`document`/`files`, rebuilt `SubtitleCard.qml` into a responsive two-column workstation, and finished all 991 English i18n catalog entries (`vienetts_en.ts`/`.qm`); (2) Video Dubbing Studio integration (`12734a0`, bead `eet9` closed): `SubtitleStudioMeta` video/mix/speaker_voices metadata, per-speaker `StudioClip.voice` & `SynthesisContext` resolution with `studioRegenDialog` voice pre-selection, 1-click `controller.studioExportVideo` window splice + `-c:v copy` muxing, and `controller.studioPlayOriginalCue` source video slice audition; suite now 1532 collected / 1514 selected (18 benchmark deselected, verified live 2026-10-20) -->
<!-- refreshed 2026-10-21: no root pyproject/uv.lock dep drift (vieneu 3.3.0, PySide6 >=6.9, app v0.3.0 — `pyproject.toml` and `uv.lock` untouched since `e08a58e`). Post-v0.3.0 on main: (1) video-dubbing hardening `d17a261` (`core/video_dub.py`, `core/diarization.py`, `core/asr.py`, `core/llm_refine.py`, `core/subtitle_project.py`, `core/studio.py`, `ui/subtitle_controller.py`, `ui/controller.py`: muxing/diarization correctness, inline cue editing, per-speaker sample audition, AI progress bars, Studio staged-ops auto-commit, 15 new QML objectNames incl. `subtitleCueEditButton`/`subtitleSpeakerSampleButton`/`studioEmbedSrtToggle`); (2) `1661604` VoicePicker popup window-edge clipping/padding + `SubtitleCard` layout; (3) soft-vs-burned subtitle embed mode `6e7c747` + `d7b8721` (beads `poph`/`ni2i`/`vxil` closed): `subtitleEmbedMode` persisted per project (`core/subtitle_project.py`, `soft` default), generated ASS burn-in (`probe_video_info` + `_burn_style`/`_fit_cue`/`_ass_escape`/`_wrap_lines`, PlayRes-matched ratios, `_BURN_MAX_LINES = 3` with adaptive shrink, bundled Be Vietnam Pro `fontsdir`, `_burn_pix_fmt` HDR bit-depth preservation), WebVTT soft track for `.webm`, explicit `.avi` no-soft-track hint, `subtitle_language` tagging, ffmpeg `-progress` relay + dub-overrun warning, opt-in `clamp_to_video`; suite now 1541 collected / 1523 selected (18 benchmark deselected, verified live 2026-10-21); `ruff check .` + `ruff format --check .` green after reformatting 5 files of debt (212 files) -->
<!-- refreshed 2026-10-23: no root pyproject/uv.lock dep drift (vieneu 3.3.0, PySide6 >=6.9, app v0.3.0 — `pyproject.toml` and `uv.lock` untouched since `e08a58e`). Post-v0.3.0 on main: (1) diarization hardening (`VieNeuTTSApp-2ifr`, `b41caf7..cb0c1e9`): verified Nemotron-3 GGUF downloads + staged `audiocpp_cli` `--help` probe, 16 kHz mono WAV normalization, `diarization_device`/`diarization_threads`/`subtitle_export_speaker_labels` settings, `weight_type=f32` on CPU-only runtimes, `max_speakers` ceiling merge + label remapping, `DiarizationCancelled`, and removal of fake NeMo UI option; (2) test-runtime reduction (`VieNeuTTSApp-4mqj`, `1542053` + `5eb6f85`): smoke-fixture splitting + `sys.modules["torch"] = None` isolation fix; (3) track `asr_hardening_20261021` (`VieNeuTTSApp-7u58`, `b09dd45..d50f43b`, archived `ed66f6a`): pinned `WHISPER_GGUF_MODELS` sizes/SHA-256 + guarded removal, managed `whisper.cpp` (`b5454`, `whisper-cli`) staged runtime installer, shared per-video `_media_cache` (`source_16k.wav` + `asr_segments_cache.json` + `diar_turns_cache.json`), collision-safe `<stem>_<hash8>.srt` paths, `NON_SPEECH_LABELS` filtering + duration-aware splitting, `AsrCancelled`, and `asrDevice`/`asrThreads` + throttled `aiDownloadProgressChanged`; (4) track `subtitles_ux_overhaul_20261023` (`VieNeuTTSApp-weit`, `2f6fe8a..42ab565`, archived `ed66f6a`): staged `_PendingInvalidation` gate, `renderStale`/`lastStats`/`exportBlockedReason`, project lifecycle switcher, `cancelExport()`, derived `state`/`busyOwner` + `API_ONLY_PROPERTIES`, pinned `SubtitleActionDock`, extracted `SubtitleSourcePanel`/`SubtitlePreparePanel`/`SubtitleCueEditor`/`SubtitleCastPanel`, `AppProgressBar`/`AppTextField`, `Theme.warningText`, `disabledReason`, and `PageShell.ensureFocusVisible` (`pageWindow`). Suite now 1597 collected / 1579 selected (18 benchmark deselected, verified live 2026-10-23); `ruff check .` + `ruff format --check .` green (214 files); i18n 1175/1175 finished -->
<!-- refreshed 2026-10-28 (3): no root `pyproject.toml`/`uv.lock` dep drift (vieneu 3.3.0, PySide6 >=6.9, app v0.3.0 — untouched). Track `llm_provider_hardening_20261028` (`VieNeuTTSApp-f783`, `d6a5ab0..16f8e28`, archived) adds stdlib-only `core/llm_client.py` (`urllib`, `json`, `threading`) and hardens `core/llm_refine.py` (`concurrent.futures`, `ipaddress`), `core/asr.py` (600 s silence-aware PCM16 chunking via `soundfile`), `core/models.py`/`core/settings.py` (per-field clamps + POSIX `0600` `settings.json`), `core/subtitle_project.py` (`llm_glossary`), `ui/controller.py`/`ui/subtitle_controller.py`, `SettingsModels.qml`, and `SubtitlePreparePanel.qml`. Suite re-verified live 2026-10-28 at 1630 collected / 1612 selected (18 benchmark deselected); ruff check + format --check green (216 files); i18n 1216 messages, 0 unfinished. -->
<!-- refreshed 2026-10-28 (2): no root `pyproject.toml`/`uv.lock` dep drift (vieneu 3.3.0, PySide6 >=6.9, app v0.3.0 — untouched since `e08a58e`). Track `video_preview_panel_20261028` (`VieNeuTTSApp-t80a`, `4ddf6f8..2e96c3a`, archived `2faa30a`) is QML + controller-only: new `SubtitleVideoPreview.qml` uses `QtMultimedia` `MediaPlayer`/`VideoOutput`/`AudioOutput`, already resident via the `PySide6` meta-package's `PySide6-Addons` 6.11.2, and the frozen spec's multimedia plugin collection (since `e15bfa3`) already covers its backends; no packaging, CI, or workflow changes. Suite re-verified live 2026-10-28 at 1615 collected / 1597 selected (18 benchmark deselected); ruff check + format --check green (214 files); i18n 1190 messages, 0 unfinished. -->
<!-- refreshed 2026-10-28: no drift — 1 commit since the 2026-10-26 refresh (`c0c482d`: `.beads/issues.jsonl` export + `vienetts_en.ts` `<location>` offsets only). No root `pyproject.toml`/`uv.lock` dep drift (vieneu 3.3.0, PySide6 >=6.9, app v0.3.0); no CI/workflow changes; suite re-verified live 2026-10-28 at 1614 collected / 1596 selected (18 benchmark deselected), ruff check + format --check green (214 files) -->
<!-- refreshed 2026-10-26: no root pyproject/uv.lock dep drift (vieneu 3.3.0, PySide6 >=6.9, app v0.3.0 — `pyproject.toml` and `uv.lock` untouched). Track `dub_pacing_alignment_20261026` (`VieNeuTTSApp-o8af`, `765c10a..dfac5b8`, archived `3d6418b`): optional `Cue.speech_start_ms`/`speech_end_ms` with sparse `project.json` serialization, `trim_speech_edges` (-45 dBFS, 15 ms pad) + 5 ms half-cosine `apply_micro_fade` in `core/audio.py`, 16 kHz RMS VAD + `RttmTurn` speech-bound refinement (`refine_cues_speech_bounds` in `core/diarization.py`) and Whisper `word_timestamps=True` word-boundary splitting (`core/asr.py`), `LANGUAGE_CHARS_PER_SEC` + speech-window/slack `max_chars_for_window` in `core/llm_refine.py`, two-stage elastic `plan_cue`/`plan_alignment` (`COMFORT_RATE = 1.15` -> post-cue gap borrowing up to 1200 ms -> `rate_cap` with <=0.20x same-speaker step clamping) + readable `adjusted_cues` caption hold + punctuation-weighted `split_unit_audio` in `core/align.py`, uniform merged speech-unit placement (`SubtitleTrackRenderer.add_unit_clip`) in `core/subtitle_project.py`, and full speech+dub span background ducking (`build_duck_spans`/`merge_duck_spans` in `core/video_dub.py`). Suite now 1614 collected / 1596 selected (+17 items, 18 benchmark deselected, verified live 2026-10-26); `ruff check .` + `ruff format --check .` green (214 files); i18n 1175/1175 finished -->

## Language & Runtime
- Python `>=3.10,<3.14` — SDK caps at 3.13; provision dev venvs via `uv venv
  --python 3.13` even when system python is newer.
- Single-process app; inference runs in a dedicated worker `QThread`.

## TTS Engine
- `vieneu==3.3.0` — torch-free ONNX Runtime on CPU (int8 backbone by
  default); optional CUDA via `torch`/`torchaudio`.
- Model: `pnnbao-ump/VieNeu-TTS-v3-Turbo` (48 kHz, vi + en
  code-switching), installed on demand. Backbone repo is configurable
  (2026-09-02): a Settings field overrides `TTSEngine`'s backbone repo
  (empty = official); `scripts/fetch_models.py --backbone owner/repo`
  fetches and manifests a custom repo for fully-offline use. Since
  2026-09-03 `scripts/fetch_models.py` derives repos, revisions, and file
  lists from `core/official_model_manifest.py`, and first-run install goes
  through `core/model_manager.py` (SHA-256-pinned official CPU baseline
  ~330 MB backbone + codec, staging-only install, atomic promote, resume,
  free-space preflight, Windows MAX_PATH/long-path handling) — weights are
  NOT frozen into the build by design; offline `backbone/`+`codec/` pack
  import via Settings.

## GPU Dependency (optional)
- `torch==2.8.0` + `torchaudio==2.8.0` (cu128), CUDA >= 12.0 (the cu128 wheels
  bundle their CUDA runtime, so CUDA 12.x minor-version compatibility applies;
  any driver >= R525.60.13 Linux / R527.41 Windows runs them).
- `transformers==4.57.6` (Qwen3 backbone + MOSS codec).
- NVIDIA CUDA only; Windows x64 and Linux x64 support the app-managed runtime
  (`core/cuda_runtime.py` + pinned records in `core/cuda_runtime_manifest.py`,
  v0.1.11): opt-in Settings download of the verified PyTorch runtime into the
  per-user data dir, offline after install, with diagnostics/removal.
  Apple Silicon / AMD / iGPU → ONNX/CPU. The app uses only the
  checksum-verified per-user runtime; it does not execute locally discovered
  Python environments. History: v0.1.9 briefly shipped `-cuda` download
  variants (~2 GB torch bundles per OS); withdrawn in v0.1.11 — all downloads
  are CPU-only again, CUDA is post-install opt-in.
- `scripts/lock_cuda_runtime.py` is a maintainer-only stdlib script. It runs
  pip's JSON-report resolver against official PyTorch/PyPI indexes, downloads
  the resolved direct wheel URLs, verifies their exact sizes and SHA-256
  digests, and renders sorted manifest records. The application never invokes
  the script or pip.

## Optional Qwen Engines (managed, isolated — track `qwen_multiengine_20260920`)

Two opt-in engine profiles beside VieNeu (`core/engine_profiles.py` is the
capability table: `vieneu` | `qwen_custom_0_6b` | `qwen_base_0_6b`, each with
its voices/languages/devices/cloning requirements):

- **Model + runtime are installs, not dependencies.** The Qwen stack
  (`qwen-tts==0.1.1`, `transformers==4.57.3`, `accelerate==1.12.0`,
  `soundfile`, `soxr`, `torch`/`torchaudio==2.8.0` — `+cpu`, `+cu128`, or the
  PyPI macOS wheel for MPS) lives ONLY inside the per-user app-managed runtime
  (`core/qwen_runtime.py` + pinned wheel records in
  `core/qwen_runtime_manifests.json`, rendered by maintainer-only
  `scripts/lock_qwen_runtime.py`). Models are separate checksum-pinned installs
  (`core/qwen_model_manager.py` + `core/qwen_model_manifests.json` from
  `scripts/fetch_qwen_models.py`) for
  `Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice` and `…-0.6B-Base`, sharing one
  tokenizer tree. Neither enters `pyproject.toml`/`uv.lock` and neither is
  frozen into the bundle (the PyInstaller spec excludes them and the packaged
  binary re-dispatches itself as the host via `--qwen-host`).
- **Inference is a subprocess.** `workers/qwen_host.py` runs in the managed
  runtime interpreter and speaks a framed JSON/PCM protocol
  (`core/qwen_protocol.py`); `core/qwen_engine.py` owns its lifecycle
  (spawn/hello/load/stream/cancel/terminate/kill/reap, lazy restart after a
  crash, offline environment, windowless spawn on Windows). The host resamples
  the model's native 24 kHz to the app's 48 kHz incrementally, so the app keeps
  one PCM contract (`core/engine_profiles.py`: `QWEN_SOURCE_RATE` →
  `APP_SAMPLE_RATE`).
- **One submission contract.** `core/synthesis_context.py` carries the
  immutable engine context (profile, language, speaker/clone, model revision)
  on every job and decides cache/Studio reuse; `core/voice_profiles.py` keeps
  clones profile-scoped; `core/text_segmentation.py` bounds Qwen segments.
  QML derives all capability state from one singleton (`EngineState.qml`).
- **Validation.** Ordinary CI never downloads weights: fake-host unit suites
  plus consolidated subprocess scenarios in `tests/smoke/test_e2e_flows.py`
  cover the flows. The real model is exercised only by the opt-in
  `scripts/qwen_release_smoke.py` and `.github/workflows/qwen-runtime-smoke.yml`
  matrix (see `docs/performance/qwen-runtime-compatibility.md`).

## GGUF format variant (native `qwentts.cpp` — track `qwen_gguf_engine_20260923`)

Both Qwen profiles additionally offer a GGUF variant — same profiles, same
capability table, different format/engine pair
(`core/qwen_variants.py`: *official* → `pytorch`, *gguf* + `Q8_0`/`Q4_K_M` →
`qwentts.cpp`). **GGUF is the default format** for those profiles
(`models.Settings.qwen_model_format` defaults to `gguf`; the picker marks the
chip recommended and states the official weights' cost when they are armed —
an explicit stored choice is never overridden):

- **Native runtime is a managed pack, not a dependency.** `libqwen` + the
  ggml backend modules are built per platform cell by
  `scripts/build_qwen_gguf_runtime.py`, locked by
  `scripts/lock_qwen_gguf_runtime.py` into
  `core/qwen_gguf_runtime_manifests.json` (four locked+published cells —
  `linux-x64-cpu`, `windows-x64-cpu`, `macos-arm64-cpu`, `macos-arm64-metal`,
  re-locked/re-published 2026-09-28 at the bumped pin `6fae9291`/ggml 0.25.3;
  per bead `ysl8.7` the pre-bump probe evidence is stale for all four until
  the release smoke re-runs per cell; the two CUDA cells stay unpublished
  until self-hosted hardware builds them), and
  installed offline-verified by `core/qwen_gguf_runtime.py`. No PyTorch, no
  compilers,
  no `PATH`/`cwd` library discovery — the engine locates the library only
  inside the verified promoted pack.
- **Models are per-variant GGUF pairs.** `core/qwen_gguf_models.py` +
  `core/qwen_gguf_model_manifests.json` pin four talkers
  (`Serveurperso/Qwen3-TTS-GGUF` @ `b7ee2e8c`) plus two shared tokenizer/codec
  GGUFs — one codec per quantization shared by both profiles.
- **Same subprocess contract, native flavor.** `workers/qwen_gguf_host.py`
  reuses the framed JSON/PCM protocol; `workers/qwen_gguf_abi.py` is the
  ctypes boundary (loaded only inside the host, never in the GUI process);
  `core/qwen_gguf_engine.py` extends `QwenEngine` — it spawns the host with
  `cwd=<pack dir>` so ggml's `backend_init` finds its modules, and the load
  frame carries format/quantization/device/runtimeDir/talkerPath/codecPath.
  Missing/incomplete packs surface as `runtime_incomplete` (Settings Repair),
  not crashes.
- **Provenance is variant-aware.** `SynthesisContext` fingerprints carry the
  resolved device plus content-addressed runtime/model/tokenizer identities,
  so caches, Studio clips, and exports never replay across formats or
  quantizations; Studio's regen offer restores the exact recorded variant.
- **Packaging.** The frozen binary re-dispatches as the GGUF host via
  `--qwen-gguf-host` alongside `--qwen-host`; the spec bundles the host/ABI/
  protocol modules and all four manifest JSONs but never native packs or
  weights.
- **Validation.** `tests/unit/qwen_gguf_host_fake.py` scripts the native host
  (including a `FAKE_HOST_BACKENDS` refusal injection); the opt-in
  `scripts/qwen_gguf_release_smoke.py` +
  `.github/workflows/qwen-gguf-runtime-smoke.yml` gate the 24-combination
  matrix with per-run identity/device/streaming/cancel/restart/shutdown
  evidence (see `docs/performance/qwen-gguf-compatibility.md`).

## UI Framework
- PySide6 + QML (Qt Quick / Qt6), GPU-rendered.
- Floor `PySide6>=6.9` since 2026-10-09 (`QtQuick.Effects`
  `RectangularShadow` replaces per-card MultiEffect/FBO shadows). Tabs load
  lazily and asynchronously with an idle prebuild after first paint; long
  lists bind a row-level `DictListModel` (`ui/list_models.py`, keyed diff →
  insert/remove/`dataChanged`) instead of a `QVariantList` property;
  waveform Canvases repaint only when visible and changed. Packaged builds
  ship ahead-of-time QML units (`packaging/qml_aot.py` →
  `qmlcachegen --only-bytecode`, `Foo.qmlc` beside `Foo.qml`); units are
  never generated into `src/` (perf_hardening_20261009).
- `Theme.qml` design tokens; dark mode default.
- Shared component library in `ui/qml/components/`, registered in both the root
  `qmldir` (subfolder components declared with relative paths) and
  `ui/qml/components/qmldir`:
  `AppButton`, `AppCard` (whole-card tap targets since v0.1.16 via
  `clickable`/`cardHovered`/`cardClicked`), `AppCombo`, `AppSlider`,
  `AppNumberField` (precise clamped numeric entry synced both ways with its
  slider, v0.1.16), `AppToggle`, `AppProgressBar` and `AppTextField`
  (added in `subtitles_ux_overhaul_20261023` with built-in `Accessible.name`,
  plus `passwordEcho` in `llm_provider_hardening_20261028`),
  `AppIcon`, `AppIconButton` (square 40 px minimum hit target, v0.1.16),
  `AppNotice`, `AppSegmented`, `AppMenu`/`AppMenuItem`, `SectionLabel`,
  `StatusBadge`, `StatusBar`, `TransportDock`, `EmotionChip`, `VoicePicker`
  (with user-only `voiceActivated` signal), `PageShell`/`PageHeader`
  (`PageShell.ensureFocusVisible` + `pageWindow` focus-follow),
  `RemoveConfirmDialog`, the Studio components `StudioClipRow`/`StudioTimeline`/
  `StudioEffectSlider`/`StudioEffectsPanel`/`StudioApplyBar`, the Qwen
  components `EngineProfilePicker`/`LanguagePicker`/`QwenVariantPicker`/
  `QwenInstallCards`/`QwenDevicePicker`/`QwenSetupDialog` + singleton
  `EngineState`, and the Subtitles components `SubtitleActionDock`/
  `SubtitleSourcePanel`/`SubtitlePreparePanel`/`SubtitleCueEditor`/
  `SubtitleCastPanel`/`SubtitleVideoPreview` (extracted from `SubtitleCard.qml`
  in `subtitles_ux_overhaul_20261023`, 1473 → 646 lines, plus
  `SubtitleVideoPreview.qml` in `video_preview_panel_20261028`). Feature-surface cards
  that are NOT in `qmldir` — `BatchQueueCard`, `DocumentEditorCard`,
  `ComposeEditorCard`, `CreateInspector`, `SubtitleCard` — are reached by the
  consuming tab's `import "components"`. Tabs import `"."` for `Theme` and
  `"components"` for these, never a bare absolute path.

## Audio
- QtMultimedia: `QAudioSink` (live preview), `QMediaPlayer` (artifact replay,
  plus video playback since 2026-10-28: `SubtitleVideoPreview.qml` mounts
  `MediaPlayer` + `VideoOutput` + a `Loader`-wrapped `AudioOutput` for
  source/dub preview). Ships in the already-declared `PySide6` meta-package
  (`PySide6-Addons` 6.11.2, resolved through `PySide6>=6.9`) — no new root
  dependency; `packaging/vienetts-app.spec` has collected the
  `multimedia`/`mediaservice`/`audio` Qt plugin trees since `e15bfa3`.
- `soundfile` (transitive via `vieneu`, not a direct dep) for WAV
  encode/decode + reference-clip decode + ranged frame reads
  (`read_wav_range` in `core/audio.py`, 2026-10-19). MP3 export (v0.1.12,
  `exportFormat` setting, WAV default) encodes via libsndfile through
  `core/audio.py` — no new direct dependency.
- Reading speed 0.5–2.0× via NumPy WSOLA + inter-paragraph pause 0–2.0 s
  (`core/audio.py`, v0.1.5), plus vectorized pre-fit TTS edge-silence trimming
  (`trim_speech_edges`, $-45\text{ dBFS}$ threshold with $15\text{ ms}$ attack/release
  pad) and $5\text{ ms}$ half-cosine boundary taper (`apply_micro_fade`,
  `dub_pacing_alignment_20261026`); Mini Audio Studio post-synthesis op stack
  per-segment re-synthesis with 10 ms crossfade); WASAPI restart-storm
  guard on Windows. v0.1.14 redesigns the studio as a pro-audio deck:
  72 px master waveform deck with peak/RMS/dynamic-range telemetry, grouped
  FX rack (Dynamics / Tempo & Cadence / Transitions), op-stack breadcrumbs
  with contextual undo/reset, pause + click-drag waveform seek on the shared
  player, per-clip audition and in-dialog transcript editing, and
  off-GUI-thread render/export with stale-generation guards (`studioBusy` +
  per-op spinners). Windowed SRT Studio integration (2026-10-19, bead
  `VieNeuTTSApp-n5ts`): `load_project_from_subtitle` loads bounded 50-cue
  windows (`MAX_STUDIO_SRT_CUES = 50`) via `read_wav_range`, preserves
  inter-cue `lead_silence_ms`, supports per-cue auto-fit or custom speed
  (`0.50×–2.00×`, `refit_subtitle_clips` / `set_subtitle_clip_speed` on 1.0×
  `raw_audio`), and splices edited windows back into `track.wav` +
  `track.timeline.json` in 64k-frame chunks (`splice_studio_window_into_subtitle`);
  extended 2026-10-20 (`12734a0`, bead `VieNeuTTSApp-eet9`) with video dubbing
  metadata on `SubtitleStudioMeta`, per-speaker `StudioClip.voice` context and
  regen voice pre-selection, 1-click `studioExportVideo` (window splice +
  retimed `.srt` + `mux_dubbed_video`), and `studioPlayOriginalCue` source video
  cue audio audition.

## Performance & threading (perf_hardening_20261009, 2026-10-09)
- GUI thread never does multi-hundred-ms work: book load/export, Studio
  and audition file I/O, Qwen engine preparation (install inspections +
  build planning behind a queuing `_PreparingWorker`) and subtitle dub
  units run off-thread via `ui/bg_ops.py` (`run_on_thread_pool`,
  `OrderedExecutor` for ordered stateful writers) with per-operation
  generation counters; batch encodes the finished item alongside the next
  synthesis (pipeline depth 1).
- Verified installs: full SHA-256 only at install/repair and on the
  Settings **Verify files** action (`verifyQwenFiles`); engine builds
  compare a stored `(size, mtime_ns, inode)` stamp (`StampLedger`,
  `core/managed_install.py`) that must also match the manifest size and be
  ≥ 2 s older than its mtime, else fall back to a full hash.
- Live preview: the artifact writer no longer blocks on playback — the
  bounded (2 s) transport refills from the `.part.wav` tail
  (`core/artifacts.WavPcmReader`) when it falls behind; Int16 fallback
  sinks get converted PCM. Qwen live jobs use progressive (smaller first)
  segments plus one-ahead prefetch; Qwen PCM frames are vectorized.
- DSP: FFT-correlation WSOLA search (`tests/unit/wsola_reference.py`
  keeps the old implementation as the parity oracle); Studio caches the
  time-stretched mix keyed on an input content digest.
- Engine knobs, bench-gated (`docs/performance/tuning-vieneu.md`): BLAS
  thread cap **defaults to 1** (`Settings.blas_threads`, applied by
  `__main__._apply_startup_thread_caps` before numpy loads; Qwen hosts do
  not inherit it); ORT intra-op threads / per-step single thread / spin,
  export `chunk_frames` and PyTorch batched export ship at SDK defaults
  (unmeasured cells: bead `hay8`).

## File Import
- `.txt`/`.md` native; `.docx` via `python-docx`; `.pdf` via **`pypdf`**
  (MIT — chosen over AGPL PyMuPDF at Phase 0; see
  `docs/spike-report.md` §7).
- `.epub` audiobook import is **stdlib-only** (`zipfile` +
  `xml.etree.ElementTree` with an `html.parser` fallback for malformed
  XHTML) — `ebooklib` was rejected because it drags in `lxml`
  (`audiobook_epub_20260828`).
- Audiobook oversize-chapter auto-split (2026-09-29, `1244363`):
  `core/chapter_split.py` cuts a chapter over `CHAPTER_CHAR_LIMIT`
  (`core/audiobook.py`, 60 000 chars) into deterministic ≤limit parts at
  paragraph/sentence boundaries (`ChapterPart`, `split_chapter_parts`),
  rendered sequentially as `ch_XXXX_sYY.wav` under the same logical chapter
  with chained playback and ordered part-file export — no
  refuse-with-zero-audio, no full-chapter float32 handoff.
- Multi-file batch queue (v0.1.7, `ui/batch_controller.py` +
  `BatchQueueCard.qml`): multi-select/drop onto Paragraph tab, off-thread
  import with oversize guard, sequential auto-run with per-file auto-export
  (`<stem>.wav`, collision-safe suffixing), failure-continue.
- Paragraph tab is a mode composition since 2026-09-10 (`0d684ba`, third mode
  2026-09-14): segmented `ModeTabs` ("Một tài liệu" | "Nhiều tệp" |
  "Phụ đề (SRT)") over mutually
  exclusive surfaces (`DocumentEditorCard` vs the queue panel vs the subtitle
  card) with a docked
  `SynthesisBar`, so Generate/Play/Export/Run-all never scroll away;
  `BatchQueueCard` is now a list-only queue panel, and a mode switch keeps the
  page scrolled in place (`e255027`).
- SRT subtitle dub/transcript studio (2026-09-14, bead-driven; extended
  2026-10-19..28 by `VieNeuTTSApp-n5ts`, `VieNeuTTSApp-b3nq`, `ab1aad1`,
  `aef1d87`, `VieNeuTTSApp-eet9` `12734a0`, `VieNeuTTSApp-2ifr`,
  `asr_hardening_20261021` `VieNeuTTSApp-7u58`,
  `subtitles_ux_overhaul_20261023` `VieNeuTTSApp-weit`,
  `dub_pacing_alignment_20261026` `VieNeuTTSApp-o8af`,
  `video_preview_panel_20261028` `VieNeuTTSApp-t80a`, and
  `llm_provider_hardening_20261028` `VieNeuTTSApp-f783`):
  `core/subtitles.py` (pure `.srt` cue model — parse, strip styling /
  `{\an8}`-style override blocks and `NON_SPEECH_LABELS` tokens like
  `[BLANK_AUDIO]`/`[MUSIC]`, collapse whitespace, re-emit; tolerant
  `HH:MM:SS,mmm` stamps; `[SPEAKER_01]` speaker prefix round-trip; optional
  `speech_start_ms`/`speech_end_ms` acoustic speech bounds inside `[start_ms,
  end_ms]` with `effective_speech_start_ms`/`effective_speech_end_ms`/
  `speech_duration_ms`),
  `core/align.py` (cue→clock fit planning: `dub` anchors at
  `effective_speech_start_ms + offset_ms` and fits in two elastic stages —
  `COMFORT_RATE = 1.15` within the speech window, lookahead post-cue silence
  borrowing up to $\min(\text{target\_end} + 1200, \text{next\_start} - 120)\text{ ms}$
  when gap $\ge 250\text{ ms}$, then up to a `1.0–2.0×` rate cap with $\le 0.20\times$
  same-speaker step clamping and pushes later cues on overflow; `transcript`
  never compresses and caps inter-cue pauses at 1500 ms default; optional
  sentence-unit merging that breaks at speaker transitions and splits audio with
  punctuation pause weights via `split_unit_audio`; `adjusted_cues` preserves
  readable caption display hold; NumPy WSOLA reuses the app's reading-speed
  bounds), `core/subtitle_project.py` (workspace store with sparse
  `speechStartMs`/`speechEndMs` and `llmGlossary` JSON emission (capped at
  4,000 chars and excluded from `render_fingerprint` and staged render
  invalidation) + streaming
  `SubtitleTrackRenderer` writing `PCM_16` `track.wav` clip-by-clip with
  pre-fit `trim_speech_edges` + `apply_micro_fade` and uniform merged-unit
  stretching via `add_unit_clip`, a render fingerprint that caches the track
  including per-cue speakers and `speaker_voices`, atomic JSON writes with
  WinError-32 retry, aligned-SRT export with optional `include_speakers` via
  `subtitle_export_speaker_labels`, and chunked 64k-frame
  `splice_studio_window_into_subtitle` save-back from Audio Studio), and
  `ui/subtitle_controller.py` + `ui/qml/SubtitlesTab.qml` +
  (`SubtitleSourcePanel`, `SubtitlePreparePanel`, `SubtitleCueEditor`,
  `SubtitleCastPanel`, `SubtitleVideoPreview`). Since `subtitles_ux_overhaul_20261023`,
  `SubtitleController` enforces a staged `_PendingInvalidation` confirmation
  gate (`renderInvalidationRequested` + confirm/reject), preserves `lastStats`
  under `renderStale` with `exportBlockedReason`, manages project workspaces
  (`projects`, `openProject`, `removeProject`), cancels in-flight video exports
  (`cancelExport`), caches `videoOverrunMs`, coordinates dual-mode video
  preview (`videoUrl`, `videoSeekRequested`, `videoReleaseRequested`,
  `notifyVideoPreviewPlaying`, `syncActiveCueFromVideo`), and drives the UI
  from a single derived `state` + `busyOwner` (`""|ai|render|export`) with
  `API_ONLY_PROPERTIES` contract verification.
- Video dubbing, Whisper ASR, speaker diarization, LLM refinement & managed AI
  downloads (2026-10-19..28, epics `VieNeuTTSApp-b3nq`, `VieNeuTTSApp-2ifr`,
  `VieNeuTTSApp-7u58`, `VieNeuTTSApp-weit`, `VieNeuTTSApp-o8af`,
  `VieNeuTTSApp-t80a`, `VieNeuTTSApp-f783`; zero new root dependencies):
  - `core/video_dub.py`: companion `.srt`/video discovery, `ffmpeg` audio
    extraction (full or `[start_ms, end_ms]` slices), and `-c:v copy`
    stream-copy video muxing (`replace` or `duck` background audio mix with
    `build_duck_spans` / `merge_duck_spans` attenuating background audio across
    the union of original speech `[effective_speech_start_ms,
    effective_speech_end_ms]` and dubbed `[fit.start_ms, fit.end_ms]` spans +
    container-aware soft `.srt` embedding). Since 2026-10-20/21 (`6e7c747` +
    `d7b8721`, beads `poph`/`ni2i`/`vxil`) also **burns subtitles into the
    picture**: `probe_video_info` (never raises — burn-in must work when
    ffprobe is absent) feeds `_burn_style`/`_fit_cue`, which emit a generated
    **ASS** script (`_ass_timestamp`, `_ass_escape`, `_wrap_lines`) with
    `PlayRes` pinned to the real frame, `_BURN_FONT_SIZE_RATIO` /
    `_BURN_MARGIN_X_RATIO` / `_BURN_MARGIN_V_RATIO` / `_BURN_OUTLINE_RATIO` /
    `_BURN_SHADOW_RATIO` as fractions of the picture, a `_BURN_MAX_LINES = 3`
    cap with adaptive shrink down to `_BURN_MIN_FONT_SCALE = 0.55`
    (`_BURN_ABS_MAX_LINES = 5` floor), and `fontsdir` pointing at the bundled
    Be Vietnam Pro (`FONTS_DIR`). `_burn_pix_fmt` keeps the source bit depth so
    HDR 10-bit footage stays 10-bit instead of being force-tagged `yuv420p`.
    `.webm` gets a WebVTT soft track, `.avi` reports that it cannot carry one,
    soft tracks are tagged with `subtitle_language` instead of `und`, and
    `clamp_to_video` is opt-in (speech is never truncated). Burn-in is ~228×
    slower than a soft track, so ffmpeg `-progress` is relayed to a Qt slot and
    a dub-overrun warning is surfaced. The mode persists per project as
    `subtitleEmbedMode` (`core/subtitle_project.py`, `soft` default), and
    `_run_ffmpeg` accepts a `cancel_check` callback wired to
    `SubtitleController.cancelExport()`.
  - `core/asr.py` (hardened in `asr_hardening_20261021` epic
    `VieNeuTTSApp-7u58` and `llm_provider_hardening_20261028` epic
    `VieNeuTTSApp-f783`): on-device Whisper (`faster-whisper` with VAD and
    1-entry model cache, `whisper.cpp` `whisper-cli` with local GGUF/bin weights
    preferred by `auto` when installed, `openai-whisper`) or OpenAI-compatible
    `/audio/transcriptions` with a separate `AsrConfig.api_model` (`whisper-1`
    default), `asr_api_base_url`/`asr_api_model`/`asr_api_key` settings (never
    leaking the LLM API key to a different ASR host, and gating `auto` cloud
    fallback on an explicit `asr_api_base_url`), and silence-aware 600 s /
    24 MiB temporary 16 kHz mono `PCM_16` chunked upload (`_split_audio_chunks`)
    with 200-char prompt continuity and offset segment/word/speech timestamps;
    plus `AsrCancelled` semantics, in-process
    `soundfile`+`numpy` 16 kHz mono WAV normalization (`_ensure_16k_wav` +
    `.json` sidecar), `asr_segments_cache.json` caching under
    `subtitles/_media_cache/<stem>_<hash8>/`, `asr_device`/`asr_threads`
    configuration, shared `NON_SPEECH_LABELS` filtering, Whisper
    `word_timestamps=True` word-boundary clause splitting + bidirectional
    micro-fragment merging (`normalize_asr_segments_to_cues`), and automatic
    `refine_cues_speech_bounds` against `source_16k.wav`).
  - `core/diarization.py` (hardened in epic `VieNeuTTSApp-2ifr`):
    `nemotron_gguf` (`audio-cpp/Nemotron-3-Diarization-GGUF` Q8_0/BF16 via
    `audiocpp_cli --mode diarize` on CPU/GPU with `--no-gpu` fallback and
    `weight_type=f32` on CPU-only runtimes), NVIDIA NeMo RTTM overlap alignment
    (legacy `nemo` backend setting migrated to `auto`), and a built-in
    pure-NumPy acoustic speaker-embedding + cosine agglomerative clustering
    fallback, with 16 kHz mono normalization, `diar_turns_cache.json` caching,
    `max_speakers` (1–8) ceiling merge + speaker-voice label remapping on
    re-run, `DiarizationCancelled` semantics, `diarization_device`/
    `diarization_threads` settings, `refine_cues_speech_bounds` (10 ms RMS
    energy VAD over `read_wav_range` on `source_16k.wav` intersected with
    `RttmTurn` bounds), and `select_speaker_reference_clip` /
    `extract_speaker_reference_wav` for 1-click speaker voice cloning and
    off-thread sample audition.
  - `core/llm_client.py` + `core/llm_refine.py` (hardened in
    `llm_provider_hardening_20261028`, epic `VieNeuTTSApp-f783`): stdlib
    `urllib.request` OpenAI-compatible `/chat/completions` transport
    (`post_chat_completion`, structured error taxonomy, `http_error_detail`
    with credential redaction, `Retry-After`-aware bounded backoff, and a
    lock-guarded `(base_url, model)` capability downgrade cache for HTTP 400
    rejections of `temperature` or `response_format`) backing
    `probe_llm_connection` and `refine_cues_with_llm` (`refine_asr`,
    `fit_timing`, and `translate_dub` with explicit target language,
    `<think>`/fence-tolerant `raw_decode` reply scanning, strict index matching
    without positional fallback, `allowedSpeakers` whitelist in `refine_asr`,
    slim JSON request envelope with per-project `llmGlossary` + read-only
    `contextBefore`/`contextAfter`, per-batch transient failure isolation with
    `notes`, `finish_reason="length"` bisection, missing-cue follow-up,
    verified `fit_timing` within `ceil(maxChars * 1.1)`, bounded
    `ThreadPoolExecutor` concurrency, per-task model overrides
    `llm_model_refine`/`llm_model_translate`/`llm_model_fit`,
    `LANGUAGE_CHARS_PER_SEC` language-aware speaking rates, and
    `speech_duration_ms` + bounded post-cue silence slack budgeting in
    `max_chars_for_window`).
  - `core/ai_models.py` + `SettingsModels.qml` (`settingsAiDubbingCard`):
    size+SHA-256-verified catalog, atomic `.part` downloader, guarded
    `is_relative_to` model removal, and throttled progress
    (`aiDownloadProgressChanged`, $\ge 1\%$ deltas) for all 5 Whisper GGUF
    models (`<data_dir>/models/whisper`), Nemotron-3 Diarization GGUF weights
    (`<data_dir>/models/diarization`), and staged native runtime installers
    (`--help` probe with `cwd=cli.parent`, `runtime.json`, atomic swap +
    rollback) for `whisper.cpp` `b5454` (`whisper-cli` in
    `<data_dir>/runtimes/whisper_cpp`) and `0xShug0/audio.cpp` `v0.9.2`
    (`audiocpp_cli` in `<data_dir>/runtimes/audiocpp`).
- Cross-platform path normalization (v0.1.8, `core/paths.py`): QML `QUrl`
  forms, percent-decoding, quote-stripping, extended-length/UNC paths,
  reserved device names; BOM-safe (`utf-8-sig`) + universal-newline import.

## Persistence
- `platformdirs.user_data_dir("VieNeuTTSApp")` + JSON (`settings.json` with
  per-field validation/clamping in `load_settings` and POSIX `0o600` owner-only
  file permissions since `llm_provider_hardening_20261028`, incl.
  `exportFormat` WAV/MP3 since v0.1.12). Diagnostic `crash.py` (v0.1.8):
  global excepthooks → timestamped `logs/crash.log` + native error dialog
  for windowed builds (Windows `MessageBox`; macOS `osascript` display alert
  since 2026-10-11, giving up after 300 s; skipped when `QT_QPA_PLATFORM` is `offscreen`/`minimal` since `8db2677` so headless CI never blocks on a modal dialog; stderr fallback elsewhere).

## SDK Entry Points (reference)
- `vieneu-web` (Gradio), `vieneu-stream` (FastAPI).

## Build & Dev Tooling
- Build backend: hatchling (wheel packages `src/vienetts_app`); console
  script `vienetts-app` → `vienetts_app.__main__:main`. Current version
  0.3.0.
- Synthesis pipeline (2026-09-03): immutable job values
  (`core/jobs.py`: SynthesisJob/JobChunk/JobTerminal) admitted via FIFO
  (`workers/job_queue.py`) to the single worker; incremental validated WAV
  writer (`core/artifacts.py`: `<job>.part.wav` → atomic promote) feeds
  both replay and export, with live preview through a bounded PCM transport
  (`core/pcm_transport.py`: 2 s / 384 KB cap, 150 ms prebuffer); reading
  speed 0.5–2.0× via NumPy WSOLA in `core/audio.py`.

- **CI (2026-09-02 … 2026-10-10, `.github/workflows/ci.yml`, REMOVED):** the
  per-push/PR workflow (ruff + parallel unit/smoke legs on linux-x64 +
  windows-x64) was removed by request — no CI per commit, only at release.
  The Release workflow below is now the only automatic gate and already runs
  ruff + the full suite on all three platforms before building, so nothing
  was lost at tag time. (removed 2026-10-10)
- **Shipped (2026-08-29):** tag-triggered 3-OS release pipeline
  (`.github/workflows/release.yml`, `v*` tags only). Per OS: quality gates → full
  pytest (offscreen Qt) → **PyInstaller** one-dir CPU build
  (`packaging/vienetts-app.spec`, `pyinstaller>=6,<7` installed
  in-workflow, not a project dep) → `--smoke` binary verified by
  `scripts/check_smoke_wav.py` → artifact upload (Windows/Linux zip, macOS
  `dmg`). A `v*` tag collects all three into a GitHub Release. Since
  2026-09-02 the Linux zip carries `share/linux/` (`.desktop` entry,
  hicolor icons, `install.sh`) for menu-entry install, and the Linux
  runner installs the same GStreamer set so packaged audio works in CI.
- Spec layout contract: `vieneu`/`vieneu_utils`/`sea_g2p`/
  `kaldi_native_fbank` data trees land inside the frozen `vienetts_app`
  package at the same relative layout, so no frozen-mode code paths are
  needed; torch/transformers excluded (CPU build stays torch-free).
- **Shipped (2026-09-04):** curated release notes per version in
  `packaging/release-notes/v0.1.1.md`–`v0.3.0.md`; windowed `.exe`
  stdio→devnull so packaged GUI builds can download + synthesize (184b600).
- **Shipped (2026-09-06…10):** in-app update checks (`core/updates.py`,
  v0.1.6: platform-aware GitHub Releases matching, variant-aware for the
  brief `-cuda` era, background recheck + Settings badge); pytest benchmark
  marker (`-m 'not benchmark'`, benchmarks opt-in only) + smoke/unit
  consolidation (1029 → 872 items collected / 860 selected, same-function
  micro-tests merged); Windows artifact zipping via streaming tar
  (Compress-Archive fails past 2 GB); `shell: bash` on all release steps
  (pwsh backslash parsing); manual CUDA-runtime activation spike
  (`.github/workflows/cuda-runtime-spike.yml`, workflow_dispatch).
- **Shipped (2026-09-10, v0.1.14):** pro-audio Studio deck (telemetry, FX
  rack, op-stack undo/reset, interactive transport, per-clip audition,
  off-thread renders with generation guards); Paragraph tab document/queue
  mode split with docked `SynthesisBar`; Settings engine/ CUDA/model-source
  card split + `Details & diagnostics` disclosure and a `controller.copyText`
  seam; 64-bit QML-facing byte counts + deterministic QML teardown
  (`5a578eb`); CI restricted to `main` pushes + PRs.
- **Shipped (2026-09-14, on `main`, unreleased):** SRT subtitle
  dub/transcript studio — stdlib `.srt` cue model (`core/subtitles.py`),
  `dub`/`transcript` fit policies with WSOLA rate repair (`core/align.py`),
  streaming `PCM_16` track render with a per-workspace fingerprint cache and
  aligned-SRT export (`core/subtitle_project.py`), and the
  `subtitleController` + `SubtitleCard.qml` QML surface as a third
  Paragraph-tab mode; SRT studio English catalog (`617cfdc`); Paragraph
  mode-navigation stays stationary (`e255027`). Test items now 1055 collected
  / 1054 selected (12 benchmark deselected).
- **Shipped (2026-09-15, v0.1.15):** Audio Studio rebuilt around a **pinned
  transport dock** (play/pause/seek + timecodes + previews never scroll away),
  master-waveform **region selection** with one-click trim-to / delete-selection
  (`TrimOp`/`CutOp`), a **truthful dynamic op stack** (rack readouts fold gain
  sums / speed multipliers in real time and apply in place instead of stacking
  duplicates), truthful per-clip auditioning (the dock reflects the clip
  actually playing), **clickable op-history breadcrumb rollback**, keyboard
  transport (space/Esc/arrows), quick actions, and UI integrity down to 640×420.
  Also fixes the Windows soundfile handle release on abort (WinError 32 unlink).
- **Shipped (2026-09-16, v0.1.16):** Studio tab UX refinement +
  **component extraction** — visible seek ±5 s / stop buttons with key hints in
  tooltips, `AppNumberField` precise numeric entry beside every FX slider,
  fade dirty-state parity + reset confirmation, single primary dock CTA (quick
  export demoted to an icon action), clickable equal-height guide cards,
  danger styling for destructive actions, square 40 px icon hit targets,
  neutral-when-idle transport status dot, stable busy labels (no reflow), and
  `StudioTab.qml` decomposed into `StudioRackModule` / `StudioParamRow` /
  `StudioClipRow` (1757 → 1398 lines); `AppCard` gains
  `clickable`/`cardHovered`/`cardClicked` for whole-card tap targets
  (`156c82f`, `d5b2529`).
- **Shipped (2026-09-24, v0.2.0):** the optional Qwen engines — two profiles
  (**CustomVoice 0.6B**: 10 languages + 9 fixed speakers; **Base 0.6B**: the
  same languages with clip+transcript clones) behind one capability table and
  an explicit, global engine profile, installed on demand (checksum-verified
  PyTorch runtime + models, offline-pack import) and served by isolated
  model-host subprocesses (`--qwen-host`) with lazy restart, heartbeats and
  cancellation escalation; plus the **GGUF** path on the pinned `qwentts.cpp`
  native engine (`--qwen-gguf-host`) as the family default (`Q8_0`/`Q4_K_M`),
  with checksum-locked packs published for `windows-x64-cpu`,
  `linux-x64-cpu`, `macos-arm64-cpu`, `macos-arm64-metal`;
  engine-/variant-stamped provenance across artifacts, caches and Studio
  clips; capability-aware QML surfaces. Nothing Qwen-related enters
  `pyproject.toml`, `uv.lock` or the frozen bundle (`release.yml` asserts the
  bundle and the frozen host re-dispatch). Notes:
  `packaging/release-notes/v0.2.0.md`.
- **Shipped (2026-10-12, v0.3.0, tagged `8db2677`):** five-destination UI shell
  redesign, perf hardening (see *Performance & threading* above), oversize
  audiobook chapter auto-split, Windows native-GGUF hardening and macOS 14 /
  frozen-TLS / DMG audit fixes; `release.yml` asserts that the frozen bundle
  ships precompiled QML (`Main.qmlc` present, one `.qmlc`/`.jsc` per bundled
  `.qml`/`.js`); headless `offscreen`/`minimal` Qt skips native modal crash
  dialogs (`8db2677`). Post-v0.3.0 on `main` (2026-10-19): Ko-fi support links
  in sidebar, Settings → Updates, README, and `.github/FUNDING.yml`
  (`2f44ee1`); windowed 50-cue SRT Studio integration with per-cue speed refit
  and chunked 64k-frame save-back (`e94d045..02b9181`, bead `n5ts`);
  `SubtitleCard` export button bound to `exportFormat` (`cf39f07`); and
  end-to-end Video Dubbing (`core/video_dub.py`), Whisper ASR (`core/asr.py`),
  Speaker Diarization (`core/diarization.py`: Nemotron-3-Diarization-GGUF via
  `audio.cpp` on CPU/GPU + NeMo + offline NumPy acoustic clustering + 1-click
  speaker reference clip voice cloning), OpenAI-compatible LLM subtitle/timing
  refinement (`core/llm_refine.py`), and in-app AI model/runtime downloads
  (`core/ai_models.py`, `SettingsModels.qml`, `ba69a5a..ab1aad1`, epic `b3nq`);
  dedicated top-level `SubtitlesTab.qml` destination + two-column `SubtitleCard`
  workstation (`aef1d87`); and Video Dubbing Studio support with per-speaker
  voice context, 1-click `studioExportVideo` splice+mux, and
  `studioPlayOriginalCue` original video audio audition (`12734a0`, bead
  `eet9`). Post-v0.3.0 on `main` (2026-10-20..28): video-dubbing hardening
  (`d17a261` — diarization/muxing correctness, inline cue editing, per-speaker
  sample audition, AI progress bars, Studio staged-ops auto-commit),
  `VoicePicker` popup clipping/padding + `SubtitleCard` layout (`1661604`), the
  soft-vs-burned subtitle embed mode with styled ASS burn-in, HDR bit-depth
  preservation, and streamed export progress (`6e7c747` + `d7b8721`, beads
  `poph`/`ni2i`/`vxil`), diarization hardening (`b41caf7..cb0c1e9`, epic
  `2ifr`), test-runtime reduction + torch isolation fix (`1542053` + `5eb6f85`,
  bead `4mqj`), track `asr_hardening_20261021` (`b09dd45..d50f43b`, epic `7u58`,
  archived `ed66f6a`), track `subtitles_ux_overhaul_20261023`
  (`2f6fe8a..42ab565`, epic `weit`, archived `ed66f6a`), track
  `dub_pacing_alignment_20261026` (`765c10a..dfac5b8`, epic `o8af`, archived
  `3d6418b`), track `video_preview_panel_20261028` (`4ddf6f8..2faa30a`, epic
  `t80a`, archived `2faa30a`), and track `llm_provider_hardening_20261028`
  (`3dfb2b5..4374d0f`, epic `f783`, archived `4374d0f`). Test suite 1630
  collected / 1612 selected (18 benchmark deselected); `ruff check .` +
  `ruff format --check .` green (216 files); i18n 1216/1216 finished; no root
  dependency drift (`pyproject.toml` / `uv.lock` untouched).
- **Not yet:** frozen-in model weights (by design — on-demand verified
  baseline instead), signing/notarization (macOS build
  is ad-hoc codesigned — no Apple Developer ID), `.msi`/`.deb`/AppImage
  installers (Linux has the `install.sh` stopgap only).
