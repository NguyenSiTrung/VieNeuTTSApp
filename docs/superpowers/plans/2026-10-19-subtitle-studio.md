# Subtitle (SRT) Studio Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Enable opening rendered SRT subtitle tracks in Studio with bounded 50-cue windowed loading, SRT-accurate inter-cue gaps, per-cue speed auto-fit/override on regeneration, and chunked save-back to the SRT project.

**Architecture:** Extend `core/audio.py` with ranged WAV reading (`read_wav_range`), `core/studio.py` with `SubtitleStudioMeta` and SRT-aware `StudioClip` fields + `load_project_from_subtitle` / `refit_subtitle_clips` / `set_subtitle_clip_speed`, and `core/subtitle_project.py` with chunked `splice_studio_window_into_subtitle`. Expose off-thread slots on `AppController` and `SubtitleController`, and wire `SubtitleCard.qml`, `StudioTab.qml`, and `StudioClipRow.qml`.

**Tech Stack:** Python 3.13, NumPy, soundfile, PySide6 + QML, pytest.

**Spec:** `docs/superpowers/specs/2026-10-19-subtitle-studio-design.md`

## Global Constraints

- 48 kHz mono float32 throughout (`DEFAULT_SAMPLE_RATE = 48_000`).
- Bounded memory: never load more than `MAX_STUDIO_SRT_CUES = 50` cues into `StudioProject` at once; read only `[start_frame, stop_frame)` from `track.wav` via `read_wav_range`; stream `track.wav` in 65,536-frame chunks when splicing a saved window back into the subtitle workspace.
- Preserve existing non-SRT Studio contracts (`openInStudio`, `openChapterInStudio`, `render_project`, `studioClips`, smoke `objectName`s).
- Quality gates per task: `ruff check .`, `ruff format --check .`, and scoped `pytest` green before committing.

---

### Task 1: Ranged WAV read + Windowed SRT Studio loader & per-cue speed refit

**Files:**
- Modify: `src/vienetts_app/core/audio.py`
- Modify: `src/vienetts_app/core/studio.py`
- Test: `tests/unit/test_studio.py`

**Interfaces:**
- Consumes: `core/align.py` (`FitPolicy`, `frames_for_ms`, `ms_for_frames`, `plan_cue`, `stretch_clip_to`), `core/subtitle_project.py` (`SubtitleProjectStore`).
- Produces:
  - `read_wav_range(path: str | Path, start_frame: int = 0, stop_frame: int | None = None) -> tuple[np.ndarray, int]` in `core/audio.py`
  - `MAX_STUDIO_SRT_CUES = 50`
  - `SubtitleStudioMeta` dataclass
  - SRT fields on `StudioClip` (`cue_index`, `srt_start_ms`, `srt_end_ms`, `placed_start_ms`, `placed_end_ms`, `lead_silence_ms`, `fit_rate`, `overflow_ms`, `raw_audio`)
  - `StudioProject.subtitle_meta: SubtitleStudioMeta | None = None`
  - `load_project_from_subtitle(store, project_id: str, start_cue: int = 0, max_cues: int = MAX_STUDIO_SRT_CUES) -> StudioProject`
  - `refit_subtitle_clips(project: StudioProject, clip_id: str, new_raw_audio: np.ndarray, *, new_text: str | None = None, context: SynthesisContext | None = None, custom_rate: float | None = None) -> StudioProject`
  - `set_subtitle_clip_speed(project: StudioProject, clip_id: str, custom_rate: float | None = None) -> StudioProject`

- [ ] **Step 1: Write the failing unit tests in `tests/unit/test_studio.py`**
- [ ] **Step 2: Run `pytest tests/unit/test_studio.py -k subtitle -v` to verify failure**
- [ ] **Step 3: Implement `read_wav_range` in `core/audio.py` and SRT studio functions in `core/studio.py`**
- [ ] **Step 4: Run `pytest tests/unit/test_studio.py -v` and verify green**
- [ ] **Step 5: Commit Task 1**

---

### Task 2: Chunked save-back to SRT project (`splice_studio_window_into_subtitle`)

**Files:**
- Modify: `src/vienetts_app/core/subtitle_project.py`
- Test: `tests/unit/test_subtitle_project.py`

**Interfaces:**
- Consumes: `StudioProject`, `SubtitleStudioMeta`, `render_project_for_subtitle_save` from `core/studio.py`.
- Produces: `splice_studio_window_into_subtitle(store: SubtitleProjectStore, studio_project: Any) -> SubtitleProject` in `core/subtitle_project.py`.

- [ ] **Step 1: Write failing unit tests in `tests/unit/test_subtitle_project.py` covering single-window and multi-window chunked splice + downstream timestamp shift**
- [ ] **Step 2: Run `pytest tests/unit/test_subtitle_project.py -k splice -v` to verify failure**
- [ ] **Step 3: Implement `splice_studio_window_into_subtitle` in `core/subtitle_project.py`**
- [ ] **Step 4: Run `pytest tests/unit/test_subtitle_project.py -v` and verify green**
- [ ] **Step 5: Commit Task 2**

---

### Task 3: Controller & SubtitleController wiring

**Files:**
- Modify: `src/vienetts_app/ui/subtitle_controller.py`
- Modify: `src/vienetts_app/ui/controller.py`
- Modify: `src/vienetts_app/app.py`
- Test: `tests/unit/test_studio_controller.py`, `tests/unit/test_subtitle_controller.py`

**Interfaces:**
- Consumes: `load_project_from_subtitle`, `refit_subtitle_clips`, `set_subtitle_clip_speed`, `splice_studio_window_into_subtitle`.
- Produces:
  - `SubtitleController.projectId` property and `SubtitleController.reloadProject(project_id: str = "") -> bool` slot
  - `AppController.attach_subtitle_controller(subtitle_controller)`
  - `AppController` properties: `studioIsSubtitle`, `studioSubtitleProjectId`, `studioSubtitleTitle`, `studioSubtitleMode`, `studioSubtitleRateCap`, `studioSubtitleTotalCues`, `studioSubtitleWindowStart`, `studioSubtitleWindowEnd`, `studioSubtitleWindows`
  - `AppController` slots: `openSubtitleInStudio(project_id: str = "", start_cue: int = 0) -> bool`, `studioRegenClip(clip_id: str, voice: str, new_text: str = "", custom_rate: float = 0.0) -> bool`, `studioSetClipSpeed(clip_id: str, custom_rate: float = 0.0) -> bool`, `studioSaveToSubtitle() -> bool`

- [ ] **Step 1: Write failing controller tests in `tests/unit/test_studio_controller.py` and `tests/unit/test_subtitle_controller.py`**
- [ ] **Step 2: Run `pytest tests/unit/test_studio_controller.py tests/unit/test_subtitle_controller.py -k subtitle -v` to verify failure**
- [ ] **Step 3: Implement controller properties, slots, and `app.py` wiring**
- [ ] **Step 4: Run `pytest tests/unit/test_studio_controller.py tests/unit/test_subtitle_controller.py -v` and verify green**
- [ ] **Step 5: Commit Task 3**

---

### Task 4: QML UI (`SubtitleCard.qml`, `StudioTab.qml`, `StudioClipRow.qml`) + Smoke Verification

**Files:**
- Modify: `src/vienetts_app/ui/qml/components/SubtitleCard.qml`
- Modify: `src/vienetts_app/ui/qml/StudioTab.qml`
- Modify: `src/vienetts_app/ui/qml/components/StudioClipRow.qml`
- Test: `tests/smoke/test_ui_studio.py`, `tests/smoke/test_ui_tabs.py`

**Interfaces:**
- Consumes: `AppController` and `SubtitleController` properties/slots from Task 3.
- Produces:
  - `subtitleStudioButton` in `SubtitleCard.qml`
  - `studioGuideSubtitleButton`, `studioSubtitleBar`, `studioSubtitlePrevButton`, `studioSubtitleWindowCombo`, `studioSubtitleNextButton`, `studioSaveSubtitleButton`, `studioRegenSpeedModeSwitch`, `studioRegenSpeedSlider`, `studioApplyClipSpeedButton` in `StudioTab.qml`
  - `studioClipSrtMeta` in `StudioClipRow.qml`

- [ ] **Step 1: Write failing smoke assertions in `tests/smoke/test_ui_studio.py` and `tests/smoke/test_ui_tabs.py`**
- [ ] **Step 2: Run `QT_QPA_PLATFORM=offscreen pytest tests/smoke/test_ui_studio.py -v` to verify failure**
- [ ] **Step 3: Implement QML changes in `SubtitleCard.qml`, `StudioTab.qml`, and `StudioClipRow.qml`**
- [ ] **Step 4: Run smoke and full test suite + ruff gates**
- [ ] **Step 5: Commit Task 4**
