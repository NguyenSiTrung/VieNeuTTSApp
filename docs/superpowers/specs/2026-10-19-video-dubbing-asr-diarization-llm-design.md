# Video Dubbing, On-Device ASR, Speaker Diarization & LLM Subtitle Refinement — Design (2026-10-19)

Scope: **End-to-end Video Dubbing inside the Subtitle (`Phụ đề`) & Audio Studio workflow**:
1. **Video + `.srt` Dubbing & Stream-Copy Muxing** (`core/video_dub.py`): attach or import a video (`.mp4`, `.mkv`, `.mov`, `.webm`), auto-pair matching `.srt` files, render aligned `track.wav`, and mux into a dubbed video via `ffmpeg` (`-c:v copy` zero video re-encoding) with **Replace** or **Duck/Mix background audio** modes and optional soft-subtitle embedding.
2. **On-Device Whisper ASR** (`core/asr.py`): generate timestamped `Cue`s directly from a video/audio file without an existing `.srt`, using local Whisper (`faster-whisper` / `whisper.cpp` CLI / subprocess) or an OpenAI-compatible Whisper endpoint, with energy/VAD segmentation.
3. **Speaker Diarization & Multi-Speaker Dubbing** (`core/diarization.py`): label cues by speaker (`SPEAKER_01`, `SPEAKER_02`, …) via NVIDIA NeMo Diarizer/Sortformer (when available in an isolated runtime) or built-in acoustic speaker-embedding clustering over speech segments; map each speaker to a distinct TTS voice (`speaker_voices`) and support 1-click reference-clip extraction from the video to enroll a cloned voice per speaker.
4. **Configurable LLM Provider for Subtitle & Timing Refinement** (`core/llm_refine.py`): OpenAI-compatible LLM client (local Ollama / LM Studio / vLLM / llama.cpp or cloud APIs) for:
   - **ASR & Diarization Cleanup** (`refine_asr`): punctuation, homophones, sentence boundaries, speaker turn smoothing.
   - **Timing-Aware Condensation** (`fit_timing`): rewrite cues whose spoken length exceeds (or would exceed) their `[start_ms, end_ms]` window so dubbing stays natural without hitting `rateCap` distortion.
   - **Dubbing Translation** (`translate_dub`): translate cues into the target language while constraining syllable/character budgets to each cue's duration window.

---

## 1. Core Data Models (`core/subtitles.py`, `core/subtitle_project.py`, `core/models.py`)

### 1.1 Speaker-Aware `Cue` (`core/subtitles.py`)
- Extend `Cue` with an optional `speaker: str = ""` field:
  ```python
  @dataclass(frozen=True)
  class Cue:
      index: int
      start_ms: int
      end_ms: int
      text: str
      speaker: str = ""
  ```
- **SRT speaker prefix round-trip**:
  - `parse_cues(source: str)` recognizes optional speaker prefixes at the start of a cue body: `[SPEAKER_01]: Hello`, `[Speaker 1] Hello`, or `SPEAKER_01: Hello` (matching `^\[(?P<spk>[A-Za-z0-9_ -]{1,32})\]:?\s+` or `^(?P<spk>SPEAKER_\d+):\s+`). When matched, `cue.speaker` stores the normalized speaker tag and `cue.text` stores only the spoken text (so TTS never speaks `"SPEAKER_01"` aloud).
  - `format_srt(cues: Iterable[Cue], *, include_speakers: bool = False) -> str`: defaults to `include_speakers=False` for clean player-ready subtitles; when `include_speakers=True`, prefixes `[<speaker>] ` when `cue.speaker` is non-empty.

### 1.2 Video & Multi-Speaker `SubtitleProject` (`core/subtitle_project.py`)
- Extend `SubtitleProject`:
  - `video_path: str = ""` (path to attached source video, or `""` for audio-only `.srt` projects).
  - `speaker_voices: dict[str, str] = field(default_factory=dict)` (mapping `speaker_id -> voice_key`).
  - `video_mix_mode: str = "replace"` (`"replace"` | `"duck"`).
  - `video_bg_gain_db: float = -16.0` (attenuation in dB applied to original video audio in `"duck"` mode, clamped to `[-36.0, 0.0]`).
  - `embed_subtitles: bool = True` (whether `exportVideo` muxes the retimed `.srt` stream into the output video container).
- **Fingerprint & Cache Invalidation (`render_fingerprint`)**:
  - Includes `speaker` on each cue and `speakerVoices` (sorted mapping of active speakers) in the fingerprint payload so changing a speaker's assigned voice invalidates `track.wav` as expected, while projects without speakers produce the exact same fingerprint as before.
- **Speech Unit Grouping (`core/align.py`)**:
  - `speech_units(cues, *, merge, max_chars=UNIT_MAX_CHARS, gap_ms=UNIT_GAP_MS)` breaks units whenever `cue.speaker != previous.speaker` so consecutive cues spoken by different speakers are never merged into a single TTS call.
- **Studio Window Integration (`core/studio.py`)**:
  - `StudioClip` gains `speaker: str = ""`.
  - `load_project_from_subtitle` populates `clip.speaker = cue.speaker`, and `splice_studio_window_into_subtitle` preserves `cue.speaker` and `sub_proj.video_path`, `sub_proj.speaker_voices`, `sub_proj.video_mix_mode`, `sub_proj.video_bg_gain_db`, and `sub_proj.embed_subtitles`.

### 1.3 Settings Persistence (`core/models.py`, `core/settings.py`)
- Extend `Settings` with validated, persisted fields for ASR, Diarization, and LLM providers:
  - `asr_backend: str = "auto"` (`"auto"` | `"faster_whisper"` | `"whisper_cpp"` | `"openai_api"`)
  - `asr_model: str = "base"` (e.g., `"tiny"`, `"base"`, `"small"`, `"medium"`, `"large-v3-turbo"`, or a local path/GGUF file)
  - `asr_language: str = "auto"` (`"auto"`, `"vi"`, `"en"`, `"zh"`, `"ja"`, `"ko"`, `"fr"`, `"de"`, `"es"`, `"ru"`)
  - `diarization_backend: str = "auto"` (`"auto"` | `"acoustic"` | `"nemo"`)
  - `diarization_max_speakers: int = 0` (`0` = auto-detect `1..8`, or fixed `1..16`)
  - `llm_base_url: str = "http://localhost:11434/v1"` (OpenAI-compatible base URL; works with Ollama, LM Studio, vLLM, OpenAI, Gemini, DeepSeek)
  - `llm_api_key: str = ""` (optional bearer token; empty allowed for local Ollama/LM Studio)
  - `llm_model: str = "qwen2.5:7b"` (model identifier sent to `/chat/completions`)
- `_clamp_engine_fields` in `core/settings.py` gracefully clamps invalid/unknown values to defaults so corrupt settings files never reset user data.

---

## 2. Video Inspection, Audio Extraction & Muxing (`core/video_dub.py`)

- **Supported Video Extensions**:
  - `VIDEO_EXTENSIONS = (".mp4", ".mkv", ".mov", ".webm", ".avi", ".m4v")`
  - `is_video_path(path: str | Path) -> bool`
  - `find_companion_srt(video_path: str | Path) -> Path | None` and `find_companion_video(srt_path: str | Path) -> Path | None`: auto-discovers `<stem>.srt` or `<stem>.<lang>.srt` alongside `<stem>.mp4` (and vice versa).
- **FFmpeg Discovery (`resolve_ffmpeg() -> Path`, `resolve_ffprobe() -> Path | None`)**:
  - Checks `<data_dir>/runtimes/ffmpeg/bin/ffmpeg`, bundled app paths, and `shutil.which("ffmpeg")`. Raises actionable `VideoDubError` when `ffmpeg` is missing.
- **Audio Extraction (`extract_video_audio(video_path, output_wav, *, sample_rate=16_000, mono=True, start_ms=None, end_ms=None) -> Path`)**:
  - Streams audio out of `video_path` via `ffmpeg -nostdin -y -i <video> -vn -ac <1|2> -ar <sr> -f wav <part.wav>` with atomic promotion.
  - Supports optional `[start_ms, end_ms]` slicing (`-ss` / `-to`) so extracting a 6-second speaker reference clip from a 2-hour movie takes < 100 ms without decoding the full file.
- **Zero-Reencode Video Muxing (`mux_dubbed_video(...) -> Path`)**:
  ```python
  def mux_dubbed_video(
      video_path: str | Path,
      dub_wav_path: str | Path,
      output_path: str | Path,
      *,
      srt_path: str | Path | None = None,
      mix_mode: str = "replace",       # "replace" | "duck"
      bg_gain_db: float = -16.0,
      embed_subtitles: bool = True,
  ) -> Path:
  ```
  - Writes to a temporary `.part<ext>` file in `output_path.parent` and atomically promotes via `os.replace`.
  - **`mix_mode == "replace"`**:
    - Copies video stream untouched (`-c:v copy`), maps `dub_wav_path` as primary audio encoded as AAC (`-c:a aac -b:a 192k`).
  - **`mix_mode == "duck"`**:
    - Uses `ffmpeg` filtergraph to attenuate original audio by `bg_gain_db` (and sidechain-duck under dubbed speech when original audio stream exists; falls back cleanly to `"replace"` if the source video has no audio stream):
      `[0:a:0]volume={bg_gain_db}dB[bg];[bg][1:a:0]amix=inputs=2:duration=first:dropout_transition=0:weights=1 1[aout]`
  - **Optional Soft Subtitles (`embed_subtitles=True` and `srt_path` provided)**:
    - For `.mp4` / `.mov` / `.m4v`: `-c:s mov_text`.
    - For `.mkv`: `-c:s srt`.
    - For `.webm`: omits subtitle stream or converts to `webvtt` so muxing never fails on container constraints.

---

## 3. On-Device Whisper ASR (`core/asr.py`)

- **Unified Entry Point**:
  ```python
  @dataclass(frozen=True)
  class AsrConfig:
      backend: str = "auto"        # "auto" | "faster_whisper" | "whisper_cpp" | "openai_api"
      model: str = "base"
      language: str = "auto"
      api_base_url: str = ""
      api_key: str = ""

  def transcribe_media_to_cues(
      media_path: str | Path,
      config: AsrConfig,
      *,
      work_dir: Path | None = None,
      progress_cb: Callable[[float, str], None] | None = None,
      cancel_check: Callable[[], bool] | None = None,
  ) -> list[Cue]:
  ```
- **Backend Resolution (`backend == "auto"`)**:
  1. Checks if `faster_whisper` is importable (or available in `<data_dir>/runtimes/asr`) → runs `faster-whisper` with `vad_filter=True`.
  2. Checks if `whisper-cli` / `whisper-cpp` binary is on `PATH` or under `<data_dir>/runtimes/whisper` and a model path exists → runs `whisper-cli --output-srt`.
  3. Checks if `openai_whisper` (`whisper` Python package) is importable → runs local Whisper.
  4. If `config.api_base_url` is configured (or `backend == "openai_api"`), sends extracted 16 kHz mono WAV/MP3 to `{api_base_url}/audio/transcriptions` (`response_format="srt"` or `"verbose_json"`).
  5. Otherwise raises a clear, actionable `AsrError` explaining how to enable local `faster-whisper` / `whisper.cpp` or point to a local/remote Whisper endpoint in Settings.
- **Cue Post-Processing (`normalize_asr_segments_to_cues`)**:
  - Splits overly long ASR segments (> 84 chars or > 7.0 s) at punctuation/clause boundaries with proportional timestamp interpolation.
  - Merges micro-fragments (< 400 ms) into adjacent segments when separated by < 150 ms.
  - Guarantees strictly valid, non-overlapping `Cue`s (`1..N`).

---

## 4. Speaker Diarization & Voice Cloning from Video (`core/diarization.py`)

- **Unified Entry Point**:
  ```python
  @dataclass(frozen=True)
  class DiarizationConfig:
      backend: str = "auto"        # "auto" | "acoustic" | "nemo"
      max_speakers: int = 0        # 0 = auto (1..8), >0 = cap

  @dataclass(frozen=True)
  class SpeakerCandidateClip:
      speaker: str
      start_ms: int
      end_ms: int
      text: str
      score: float

  def diarize_cues(
      audio_wav_path: str | Path,
      cues: Sequence[Cue],
      config: DiarizationConfig | None = None,
      *,
      progress_cb: Callable[[float], None] | None = None,
  ) -> list[Cue]:
  ```
- **Two Diarization Engines**:
  1. **NVIDIA NeMo Backend (`backend == "nemo"` or `"auto"` when `nemo.collections.asr` / managed NeMo script is available)**:
     - Invokes NeMo clustering/Sortformer diarizer in a subprocess (`python -m ...`), parses RTTM speaker turns `(start_s, duration_s, speaker_label)`, and assigns each `Cue` to the speaker with maximum temporal overlap.
  2. **Built-in Acoustic Speaker Clustering (`backend == "acoustic"` or automatic fallback when NeMo is not installed)**:
     - Pure NumPy/SciPy-free spectral + pitch + MFCC-like filterbank feature extractor on the 16 kHz / 48 kHz mono WAV over each cue's `[start_ms, end_ms]` active voiced frames:
       - Computes L2-normalized speaker embedding vectors per cue (fundamental frequency $F_0$ distribution via autocorrelation, spectral centroid/rolloff, and 20-band log-mel energy statistics: mean + std).
       - Runs cosine-distance agglomerative clustering with silhouette-score model selection (when `max_speakers == 0`, selects optimal $K \in \{1..6\}$; when `max_speakers > 0`, clusters up to $K = \text{max\_speakers}$) + temporal continuity smoothing (short < 600 ms cues inherit speaker on borderline similarity).
     - Works 100% offline out-of-the-box with zero extra downloads!
- **Best Reference Clip Selection for Voice Cloning (`select_speaker_reference_clip`)**:
  - Given `cues: Sequence[Cue]` and `speaker: str`, ranks cues belonging to `speaker` by suitability for voice cloning:
    - Ideal duration window: `3,500 ms .. 8,000 ms` (or concatenates consecutive clean cues of the same speaker up to `7,500 ms`).
    - Penalizes compressed/noisy/tiny cues.
  - `extract_speaker_reference_wav(media_path, cues, speaker, output_wav) -> tuple[Path, str]` extracts the 48 kHz mono WAV slice and returns `(wav_path, transcript_text)` so `SubtitleController.cloneSpeakerFromVideo(speaker, clone_name)` can enroll the voice directly into `AppController`!

---

## 5. Configurable LLM Subtitle & Timing Refinement (`core/llm_refine.py`)

- **Zero-Dependency OpenAI-Compatible Client**:
  - Uses Python's standard `urllib.request` against `{base_url.rstrip('/')}/chat/completions`.
  - Supports local **Ollama** (`http://localhost:11434/v1`), **LM Studio** (`http://localhost:1234/v1`), **llama.cpp / vLLM**, and cloud **OpenAI / Gemini / DeepSeek / OpenRouter**.
  - `test_llm_connection(config: LlmConfig) -> tuple[bool, str]` verifies connectivity and returns a user-readable status message.
- **Three Refinement Operations (`refine_cues_with_llm`)**:
  ```python
  @dataclass(frozen=True)
  class LlmConfig:
      base_url: str = "http://localhost:11434/v1"
      api_key: str = ""
      model: str = "qwen2.5:7b"
      timeout_s: float = 60.0

  def refine_cues_with_llm(
      cues: Sequence[Cue],
      config: LlmConfig,
      *,
      task: str = "refine_asr",      # "refine_asr" | "fit_timing" | "translate_dub"
      target_language: str = "vi",
      rate_cap: float = 1.35,
      batch_size: int = 25,
      progress_cb: Callable[[float], None] | None = None,
      cancel_check: Callable[[], bool] | None = None,
  ) -> list[Cue]:
  ```
  - **Batched JSON Protocol**: Sends cues in windows of `batch_size = 25` with `{index, startMs, endMs, durationMs, maxChars, speaker, text}` where `maxChars` is computed from `durationMs` and target speaking rate (~14 chars/sec for Vietnamese/English at `1.0×`, scaled by `min(rate_cap, 1.25)`).
  - **`task == "refine_asr"`**: Fixes ASR recognition errors, restores proper capitalization and punctuation, and resolves speaker label flips while preserving cue indices and timestamps.
  - **`task == "fit_timing"`**: Identifies cues whose text length exceeds `maxChars` (or whose measured `fit_rate > 1.25`) and rewrites/condenses them to fit within `maxChars` while keeping natural spoken phrasing. Cues that already fit are kept untouched.
  - **`task == "translate_dub"`**: Translates every cue into `target_language` while constraining the translated phrasing to `<= maxChars` so the dubbed translation matches the video's lip/timing window.
  - **Resilient Parsing**: Extracts JSON arrays even if wrapped in markdown fences; validates that cue indices match and never drops a cue (falls back to original cue text if any single entry is malformed).

---

## 6. Controller & UI Surface (`ui/subtitle_controller.py`, `ui/controller.py`, QML)

### 6.1 `SubtitleController` Extensions (`src/vienetts_app/ui/subtitle_controller.py`)
- **New Properties**:
  - `hasVideo: bool` (`notify=videoChanged`)
  - `videoPath: str` (`notify=videoChanged`)
  - `videoName: str` (`notify=videoChanged`)
  - `videoMixMode: str` (`"replace"` | `"duck"`, rw, `notify=videoChanged`)
  - `videoBgGainDb: float` (`[-36.0, 0.0]`, rw, `notify=videoChanged`)
  - `embedSubtitles: bool` (rw, `notify=videoChanged`)
  - `speakers: QVariantList` (`[{id, label, cueCount, voice, sampleDurationMs}]`, `notify=speakersChanged`)
  - `hasSpeakers: bool` (`notify=speakersChanged`)
  - `aiBusy: bool` (`notify=aiBusyChanged`) — true while ASR, Diarization, or LLM refinement is running off-thread
  - `aiStageText: str` (`notify=aiBusyChanged`) — human-readable progress stage (`"Đang nhận diện giọng nói (ASR)…"`, `"Đang phân tách người nói…"`, `"Đang tinh chỉnh phụ đề bằng AI…"`)
  - `aiProgress: float` (`0.0..1.0`, `notify=aiProgressChanged`)
- **Cue Row Roles (`CUE_ROLES`)**:
  - Adds `"speaker"` and `"voice"` to `CUE_ROLES = ("index", "startMs", "endMs", "startLabel", "endLabel", "text", "speaker", "voice")`.
- **Multi-Speaker Synthesis in `SubtitleController`**:
  - `_effective_voice_for_cue(cue: Cue) -> str`: returns `project.speaker_voices.get(cue.speaker) or self._effective_voice()`.
  - `_submit_next_unit()` resolves the unit's speaker voice and submits with the matching `SynthesisContext` for that voice!
- **New Slots on `SubtitleController`**:
  - `importVideo(path: str) -> bool`:
    - If a companion `.srt` exists alongside the video, imports the `.srt` and attaches `video_path`.
    - If a project is already loaded, attaches `video_path` to the current project.
    - If no `.srt` exists and no project is loaded, sets `videoPath` in a pending-video state ready for 1-click **"Tạo phụ đề từ video (ASR)"** (`transcribeVideo(with_diarization)`).
  - `attachVideo(path: str) -> bool` / `clearVideo() -> None`
  - `transcribeVideo(with_diarization: bool = True) -> bool`: runs `transcribe_media_to_cues` (and `diarize_cues` when `with_diarization=True`) off-thread on `videoPath`, builds the `SubtitleProject` with `video_path` attached, and loads it into the workspace.
  - `runDiarization() -> bool`: runs `diarize_cues` off-thread on the current project's `video_path` (or rendered `track.wav`), updating cue speaker tags and auto-assigning distinct voices from the active engine's catalog when unset.
  - `setSpeakerVoice(speaker: str, voice_key: str) -> bool`: assigns `voice_key` to `speaker` in `project.speaker_voices`, updates the project fingerprint, and persists.
  - `cloneSpeakerFromVideo(speaker: str, clone_name: str = "") -> bool`: extracts the best reference WAV slice for `speaker` from `videoPath` via `extract_speaker_reference_wav` and submits `controller.addVoice` / enrolls the speaker voice, then binds `speaker` to the new voice.
  - `refineWithLlm(task: str = "refine_asr", target_language: str = "") -> bool`: runs `refine_cues_with_llm` off-thread (`"refine_asr"`, `"fit_timing"`, or `"translate_dub"`), updates `project.cues`, and saves the workspace.
  - `cancelAiTask() -> None`: cancels an in-flight ASR, diarization, or LLM task.
  - `exportVideo(dest_dir: str) -> str`: requires `rendered and hasVideo`; runs `mux_dubbed_video` off-thread and emits `exportFinished(path, error)`.

### 6.2 `AppController` Settings Properties (`src/vienetts_app/ui/controller.py`)
- Expose validated properties + signals for Settings QML:
  - `asrBackend` (`str`), `asrModel` (`str`), `asrLanguage` (`str`)
  - `diarizationBackend` (`str`), `diarizationMaxSpeakers` (`int`)
  - `llmBaseUrl` (`str`), `llmApiKey` (`str`), `llmModel` (`str`)
  - `llmTestStatus` (`str`), `llmTesting` (`bool`)
  - Slot `testLlmConnection() -> bool`: probes `test_llm_connection` off-thread and updates `llmTestStatus`.

### 6.3 QML Surfaces (`SubtitleCard.qml`, `SettingsModels.qml`)
- **`SubtitleCard.qml`**:
  - Header action row adds **`subtitleImportVideoButton`** (`Nhập Video…`, opens `subtitleVideoDialog` filtering `*.mp4 *.mkv *.mov *.webm *.avi *.m4v`) alongside `subtitleImportButton` (`Nhập .srt…`).
  - **Video Bar (`subtitleVideoBar`)**: shown when `subtitleController.hasVideo` is true:
    - Shows attached video filename (`subtitleVideoLabel`), mix mode selector (`subtitleVideoMixCombo`: `Thay thế âm gốc` / `Giữ nhạc nền (Ducking)`), background gain field (`subtitleVideoBgGainField`, shown when `"duck"`), embed-SRT toggle (`subtitleEmbedSrtToggle`), and a button to run ASR (`subtitleTranscribeButton`: `Tạo phụ đề (ASR)`) + diarization toggle (`subtitleDiarizeToggle`).
  - **Speaker Cast Strip (`subtitleSpeakerBar`)**: shown when `subtitleController.hasSpeakers` is true:
    - Lists each detected speaker (`SPEAKER_01`, `SPEAKER_02`, …) with cue count, a voice selector combo (`subtitleSpeakerVoiceCombo`), and a `"Lấy mẫu giọng"` (`subtitleCloneSpeakerButton`) button when `hasVideo` is true.
  - **AI Assist Bar (`subtitleAiBar`)**: shown when `loaded` or `hasVideo`:
    - `subtitleDiarizeButton` (`Phân tách người nói`)
    - `subtitleRefineAsrButton` (`AI Làm sạch phụ đề`)
    - `subtitleFitTimingButton` (`AI Rút gọn khớp thời gian`)
    - `subtitleTranslateDubButton` (`AI Dịch lồng tiếng`)
  - **Cue Row (`subtitleCueRow`)**: displays a compact speaker pill badge (`[SPEAKER_01]`) when `modelData.speaker !== ""`.
  - **Action Bar**: adds **`subtitleExportVideoButton`** (`Xuất Video lồng tiếng`, variant `"primary"` when `hasVideo && hasTrack`, enabled when `hasTrack && hasVideo && !busy && !exporting`).
- **`SettingsModels.qml`**:
  - Adds **`settingsAiDubbingCard`** (`AI Phụ đề & Lồng tiếng Video (ASR / Diarization / LLM)`):
    - Whisper ASR backend (`asrBackendCombo`), model (`asrModelField`), language (`asrLanguageCombo`).
    - Diarization backend (`diarizationBackendCombo`: `Tự động` / `Phân cụm âm học (Offline)` / `NVIDIA NeMo`), max speakers (`diarizationMaxSpeakersField`).
    - LLM Provider Base URL (`llmBaseUrlField`), API Key (`llmApiKeyField`), Model (`llmModelField`), and `llmTestConnectionButton` + `llmTestStatusLabel`.

---

## 7. Verification Plan

- **Unit Tests**:
  - `tests/unit/test_video_dub.py`: companion `.srt`/video discovery, audio extraction & time-sliced clip extraction via `ffmpeg`, stream-copy video muxing (`replace` and `duck` modes + soft `.srt` embedding).
  - `tests/unit/test_asr.py`: segment normalization (long-cue splitting, micro-cue merging), `whisper.cpp` SRT output parsing, OpenAI-compatible transcription client, and `faster-whisper` adapter with deterministic fakes.
  - `tests/unit/test_diarization.py`: acoustic feature extraction + multi-speaker clustering on synthetic distinct-pitch voices (e.g. 120 Hz male vs 230 Hz female), NeMo RTTM overlap assignment, and best reference clip extraction for voice cloning.
  - `tests/unit/test_llm_refine.py`: batched JSON refinement (`refine_asr`, `fit_timing` with character budgets, `translate_dub`), markdown-fenced JSON recovery, and connection testing against a local mock HTTP server.
  - `tests/unit/test_subtitle_controller.py`: video import + companion `.srt` auto-pairing, ASR transcription flow, multi-speaker voice assignment + per-speaker synthesis routing, LLM refinement slots, and `exportVideo`.
- **Smoke & Visual Verification**:
  - `tests/smoke/test_ui_tabs.py`: verify `subtitleImportVideoButton`, `subtitleVideoBar`, `subtitleSpeakerBar`, `subtitleAiBar`, `subtitleExportVideoButton`, and `settingsAiDubbingCard`.
  - Render offscreen screenshots of `SubtitleCard` (with video + multi-speaker cues loaded) and `SettingsModels` (`settingsAiDubbingCard`) and inspect with `view_media`.
