# Video Dubbing, On-Device ASR, Speaker Diarization & LLM Subtitle Refinement Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Enable end-to-end video dubbing in the Subtitle & Audio Studio workflow: video import with companion `.srt` auto-pairing, stream-copy `ffmpeg` video muxing (`replace` and `duck` modes + soft `.srt`), on-device Whisper ASR for videos without `.srt`, speaker diarization + multi-speaker voice mapping + 1-click speaker voice cloning from video, and configurable OpenAI-compatible LLM subtitle/timing/translation refinement.

**Architecture:** Extend `core/subtitles.py`, `core/align.py`, `core/subtitle_project.py`, `core/studio.py`, and `core/models.py` with speaker tags, video dubbing metadata, and AI provider settings. Add four focused pure-Python core modules (`core/video_dub.py`, `core/asr.py`, `core/diarization.py`, `core/llm_refine.py`), expose off-thread slots and properties on `SubtitleController` and `AppController`, and wire `SubtitleCard.qml`, `StudioClipRow.qml`, and `SettingsModels.qml`.

**Tech Stack:** Python 3.13, NumPy, soundfile, ffmpeg/ffprobe, urllib.request, PySide6 + QML, pytest.

**Spec:** `docs/superpowers/specs/2026-10-19-video-dubbing-asr-diarization-llm-design.md`

## Global Constraints

- 48 kHz mono float32 throughout TTS synthesis and track rendering (`DEFAULT_SAMPLE_RATE = 48_000`); 16 kHz mono WAV for ASR extraction.
- Zero new mandatory PyPI dependencies in `pyproject.toml` — base installer size and startup speed remain unchanged.
- Video muxing uses `-c:v copy` (stream-copy, zero video re-encoding) with atomic `.part` file promotion.
- All disk I/O, `ffmpeg` execution, ASR, diarization, and LLM HTTP calls run off the GUI thread via `_run_bg` with generation/cancellation guards.
- Quality gates per task: `ruff check .`, `ruff format --check .`, and scoped `pytest` green before committing.

---

### Task 1: Speaker-Aware Cues, Video Metadata on `SubtitleProject`, and AI Provider Settings

**Files:**
- Modify: `src/vienetts_app/core/subtitles.py`
- Modify: `src/vienetts_app/core/align.py`
- Modify: `src/vienetts_app/core/subtitle_project.py`
- Modify: `src/vienetts_app/core/studio.py`
- Modify: `src/vienetts_app/core/models.py`
- Modify: `src/vienetts_app/core/settings.py`
- Test: `tests/unit/test_subtitles.py`, `tests/unit/test_subtitle_project.py`, `tests/unit/test_settings.py`

**Interfaces:**
- Produces:
  - `Cue(index: int, start_ms: int, end_ms: int, text: str, speaker: str = "")` in `core/subtitles.py`
  - `parse_cues(source: str) -> list[Cue]` parsing optional `[SPEAKER_01]:` / `SPEAKER_01:` prefixes into `Cue.speaker`
  - `format_srt(cues: Iterable[Cue], *, include_speakers: bool = False) -> str`
  - `speech_units(cues, *, merge, ...)` breaking units on speaker change in `core/align.py`
  - `SubtitleProject` fields: `video_path: str = ""`, `speaker_voices: dict[str, str]`, `video_mix_mode: str = "replace"`, `video_bg_gain_db: float = -16.0`, `embed_subtitles: bool = True`
  - `Settings` fields: `asr_backend`, `asr_model`, `asr_language`, `diarization_backend`, `diarization_max_speakers`, `llm_base_url`, `llm_api_key`, `llm_model`

- [ ] **Step 1: Write failing unit tests in `tests/unit/test_subtitles.py`, `tests/unit/test_subtitle_project.py`, and `tests/unit/test_settings.py`**
- [ ] **Step 2: Run `pytest tests/unit/test_subtitles.py tests/unit/test_subtitle_project.py tests/unit/test_settings.py -v` to verify failure**
- [ ] **Step 3: Implement speaker-aware `Cue`, `SubtitleProject` video/speaker fields, `StudioClip.speaker`, and `Settings` AI fields**
- [ ] **Step 4: Run `pytest tests/unit/test_subtitles.py tests/unit/test_subtitle_project.py tests/unit/test_settings.py -v` and verify green**
- [ ] **Step 5: Commit Task 1**

---

### Task 2: Video Inspection, Audio Extraction & Zero-Reencode Muxing (`core/video_dub.py`)

**Files:**
- Create: `src/vienetts_app/core/video_dub.py`
- Test: `tests/unit/test_video_dub.py`

**Interfaces:**
- Consumes: `core/paths.py`, `ffmpeg` CLI.
- Produces:
  - `VIDEO_EXTENSIONS: tuple[str, ...]`
  - `VideoDubError(RuntimeError)`
  - `is_video_path(path: str | Path) -> bool`
  - `find_companion_srt(video_path: str | Path) -> Path | None`
  - `find_companion_video(srt_path: str | Path) -> Path | None`
  - `resolve_ffmpeg(data_dir: Path | None = None) -> Path`
  - `probe_has_audio_stream(video_path: str | Path, *, data_dir: Path | None = None) -> bool`
  - `extract_video_audio(video_path: str | Path, output_wav: str | Path, *, sample_rate: int = 16_000, mono: bool = True, start_ms: int | None = None, end_ms: int | None = None, data_dir: Path | None = None) -> Path`
  - `mux_dubbed_video(video_path: str | Path, dub_wav_path: str | Path, output_path: str | Path, *, srt_path: str | Path | None = None, mix_mode: str = "replace", bg_gain_db: float = -16.0, embed_subtitles: bool = True, data_dir: Path | None = None) -> Path`

- [ ] **Step 1: Write failing unit tests in `tests/unit/test_video_dub.py` using real `ffmpeg` synthetic test videos**
- [ ] **Step 2: Run `pytest tests/unit/test_video_dub.py -v` to verify failure**
- [ ] **Step 3: Implement `src/vienetts_app/core/video_dub.py`**
- [ ] **Step 4: Run `pytest tests/unit/test_video_dub.py -v` and verify green**
- [ ] **Step 5: Commit Task 2**

---

### Task 3: On-Device Whisper ASR & Segment Normalization (`core/asr.py`)

**Files:**
- Create: `src/vienetts_app/core/asr.py`
- Test: `tests/unit/test_asr.py`

**Interfaces:**
- Consumes: `Cue`, `parse_cues` from `core/subtitles.py`, `extract_video_audio` from `core/video_dub.py`.
- Produces:
  - `AsrError(RuntimeError)`
  - `AsrConfig(backend: str = "auto", model: str = "base", language: str = "auto", api_base_url: str = "", api_key: str = "")`
  - `RawSegment(start_ms: int, end_ms: int, text: str, speaker: str = "")`
  - `normalize_asr_segments_to_cues(segments: Sequence[RawSegment], *, max_chars: int = 84, max_duration_ms: int = 7000) -> list[Cue]`
  - `transcribe_media_to_cues(media_path: str | Path, config: AsrConfig | None = None, *, work_dir: Path | None = None, data_dir: Path | None = None, progress_cb: Callable[[float, str], None] | None = None, cancel_check: Callable[[], bool] | None = None) -> list[Cue]`

- [ ] **Step 1: Write failing unit tests in `tests/unit/test_asr.py` covering segment splitting/merging, `faster-whisper` adapter, `whisper.cpp` CLI adapter, and OpenAI-compatible ASR HTTP endpoint**
- [ ] **Step 2: Run `pytest tests/unit/test_asr.py -v` to verify failure**
- [ ] **Step 3: Implement `src/vienetts_app/core/asr.py`**
- [ ] **Step 4: Run `pytest tests/unit/test_asr.py -v` and verify green**
- [ ] **Step 5: Commit Task 3**

---

### Task 4: Speaker Diarization & Video Reference Clip Extraction (`core/diarization.py`)

**Files:**
- Create: `src/vienetts_app/core/diarization.py`
- Test: `tests/unit/test_diarization.py`

**Interfaces:**
- Consumes: `Cue` from `core/subtitles.py`, `read_wav_range` / `write_wav_file` from `core/audio.py`, `extract_video_audio` from `core/video_dub.py`.
- Produces:
  - `DiarizationError(RuntimeError)`
  - `DiarizationConfig(backend: str = "auto", max_speakers: int = 0)`
  - `RttmTurn(start_ms: int, end_ms: int, speaker: str)`
  - `parse_rttm_turns(rttm_text: str) -> list[RttmTurn]`
  - `assign_speakers_from_turns(cues: Sequence[Cue], turns: Sequence[RttmTurn]) -> list[Cue]`
  - `diarize_cues(media_path: str | Path, cues: Sequence[Cue], config: DiarizationConfig | None = None, *, work_dir: Path | None = None, data_dir: Path | None = None, progress_cb: Callable[[float], None] | None = None) -> list[Cue]`
  - `select_speaker_reference_clip(cues: Sequence[Cue], speaker: str) -> tuple[int, int, str] | None`
  - `extract_speaker_reference_wav(media_path: str | Path, cues: Sequence[Cue], speaker: str, output_wav: str | Path, *, data_dir: Path | None = None) -> tuple[Path, str]`

- [ ] **Step 1: Write failing unit tests in `tests/unit/test_diarization.py` covering acoustic multi-speaker clustering, NeMo RTTM alignment, and best reference clip extraction**
- [ ] **Step 2: Run `pytest tests/unit/test_diarization.py -v` to verify failure**
- [ ] **Step 3: Implement `src/vienetts_app/core/diarization.py`**
- [ ] **Step 4: Run `pytest tests/unit/test_diarization.py -v` and verify green**
- [ ] **Step 5: Commit Task 4**

---

### Task 5: Configurable LLM Provider for Subtitle & Timing Refinement (`core/llm_refine.py`)

**Files:**
- Create: `src/vienetts_app/core/llm_refine.py`
- Test: `tests/unit/test_llm_refine.py`

**Interfaces:**
- Consumes: `Cue` from `core/subtitles.py`.
- Produces:
  - `LlmRefineError(RuntimeError)`
  - `LlmConfig(base_url: str = "http://localhost:11434/v1", api_key: str = "", model: str = "qwen2.5:7b", timeout_s: float = 60.0)`
  - `max_chars_for_window(duration_ms: int, *, chars_per_sec: float = 14.0, max_rate: float = 1.25) -> int`
  - `test_llm_connection(config: LlmConfig) -> tuple[bool, str]`
  - `refine_cues_with_llm(cues: Sequence[Cue], config: LlmConfig, *, task: str = "refine_asr", target_language: str = "vi", rate_cap: float = 1.35, batch_size: int = 25, progress_cb: Callable[[float], None] | None = None, cancel_check: Callable[[], bool] | None = None) -> list[Cue]`

- [ ] **Step 1: Write failing unit tests in `tests/unit/test_llm_refine.py` covering `refine_asr`, `fit_timing`, `translate_dub`, markdown-fence JSON parsing, and connection testing**
- [ ] **Step 2: Run `pytest tests/unit/test_llm_refine.py -v` to verify failure**
- [ ] **Step 3: Implement `src/vienetts_app/core/llm_refine.py`**
- [ ] **Step 4: Run `pytest tests/unit/test_llm_refine.py -v` and verify green**
- [ ] **Step 5: Commit Task 5**

---

### Task 6: `SubtitleController` & `AppController` Wiring for Video Dubbing, ASR, Diarization & LLM

**Files:**
- Modify: `src/vienetts_app/ui/subtitle_controller.py`
- Modify: `src/vienetts_app/ui/controller.py`
- Test: `tests/unit/test_subtitle_controller.py`, `tests/unit/test_settings_controller.py`

**Interfaces:**
- Consumes: `core/video_dub.py`, `core/asr.py`, `core/diarization.py`, `core/llm_refine.py`.
- Produces:
  - `SubtitleController` properties: `hasVideo`, `videoPath`, `videoName`, `videoMixMode`, `videoBgGainDb`, `embedSubtitles`, `speakers`, `hasSpeakers`, `aiBusy`, `aiStageText`, `aiProgress`
  - `SubtitleController` slots: `importVideo(path) -> bool`, `attachVideo(path) -> bool`, `clearVideo()`, `transcribeVideo(with_diarization=True) -> bool`, `runDiarization() -> bool`, `setSpeakerVoice(speaker, voice_key) -> bool`, `cloneSpeakerFromVideo(speaker, clone_name="") -> bool`, `refineWithLlm(task="refine_asr", target_language="") -> bool`, `cancelAiTask()`, `exportVideo(dest_dir) -> str`
  - `AppController` properties & slots: `asrBackend`, `asrModel`, `asrLanguage`, `diarizationBackend`, `diarizationMaxSpeakers`, `llmBaseUrl`, `llmApiKey`, `llmModel`, `llmTesting`, `llmTestStatus`, `testLlmConnection() -> bool`

- [ ] **Step 1: Write failing controller unit tests in `tests/unit/test_subtitle_controller.py`**
- [ ] **Step 2: Run `pytest tests/unit/test_subtitle_controller.py -k "video or speaker or asr or llm" -v` to verify failure**
- [ ] **Step 3: Implement `SubtitleController` and `AppController` properties, multi-speaker synthesis routing, and off-thread slots**
- [ ] **Step 4: Run `pytest tests/unit/test_subtitle_controller.py -v` and verify green**
- [ ] **Step 5: Commit Task 6**

---

### Task 7: QML Surfaces (`SubtitleCard.qml`, `StudioClipRow.qml`, `SettingsModels.qml`) + Smoke & Visual Verification

**Files:**
- Modify: `src/vienetts_app/ui/qml/components/SubtitleCard.qml`
- Modify: `src/vienetts_app/ui/qml/components/StudioClipRow.qml`
- Modify: `src/vienetts_app/ui/qml/settings/SettingsModels.qml`
- Test: `tests/smoke/test_ui_tabs.py`

**Interfaces:**
- Consumes: `SubtitleController` and `AppController` properties/slots from Task 6.
- Produces:
  - `subtitleImportVideoButton`, `subtitleVideoDialog`, `subtitleVideoBar`, `subtitleVideoLabel`, `subtitleVideoMixCombo`, `subtitleVideoBgGainField`, `subtitleEmbedSrtToggle`, `subtitleTranscribeButton`, `subtitleDiarizeToggle`, `subtitleSpeakerBar`, `subtitleAiBar`, `subtitleDiarizeButton`, `subtitleRefineAsrButton`, `subtitleFitTimingButton`, `subtitleTranslateDubButton`, `subtitleExportVideoButton` in `SubtitleCard.qml`
  - Speaker badge in `subtitleCueRow` and `StudioClipRow.qml`
  - `settingsAiDubbingCard` (`asrBackendCombo`, `asrModelField`, `asrLanguageCombo`, `diarizationBackendCombo`, `diarizationMaxSpeakersField`, `llmBaseUrlField`, `llmApiKeyField`, `llmModelField`, `llmTestConnectionButton`, `llmTestStatusLabel`) in `SettingsModels.qml`

- [ ] **Step 1: Write failing smoke assertions in `tests/smoke/test_ui_tabs.py`**
- [ ] **Step 2: Run `QT_QPA_PLATFORM=offscreen pytest tests/smoke/test_ui_tabs.py -k subtitle -v` to verify failure**
- [ ] **Step 3: Implement QML updates in `SubtitleCard.qml`, `StudioClipRow.qml`, and `SettingsModels.qml`**
- [ ] **Step 4: Run full unit + smoke suite, ruff gates, and capture/inspect UI screenshots with `view_media`**
- [ ] **Step 5: Commit Task 7**
