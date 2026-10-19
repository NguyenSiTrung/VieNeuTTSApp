# Subtitle (SRT) Studio Integration — Design (2026-10-19)

Scope: **Windowed SRT-aware Studio editing + per-cue speed/auto-fit + chunked save-back to SRT project**.

## 1. Surface & Entry Points

- **`SubtitleCard.qml`**:
  - Expose `subtitleController.projectId` (`""` when no project is loaded).
  - Add `subtitleStudioButton` (`Mở trong Studio`, `iconKind: "studio"`, `variant: "secondary"`, `size: "lg"`) in the action `Flow` beside `Xuất WAV` and `Xuất SRT`.
  - Enabled iff `root.hasTrack && !root.busy && !root.exporting && !controller.busy`.
  - Clicking calls `controller.openSubtitleInStudio(subtitleController.projectId, startCue)` (where `startCue = subtitleController.activeCue >= 0 ? Math.floor(subtitleController.activeCue / 50) * 50 : 0`) and switches `bridge.setCurrentTab("studio")`.
- **`StudioTab.qml`**:
  - Empty-state `guideFlow` adds a 4th `GuideCard` (`studioGuideSubtitleButton`, `heading: qsTr("Phụ đề (SRT)")`, `onGo: root.openCreate("subtitles")`).
  - When `controller.studioIsSubtitle` is `true`, display `studioSubtitleBar` above the project workspace:
    - Metadata: subtitle project title, fit mode (`Lồng tiếng` / `Bản thoại tự nhiên`), and total cue count.
    - Window navigation (shown when `controller.studioSubtitleTotalCues > 0`):
      - `studioSubtitlePrevButton` (`‹ Trước`, enabled when `studioSubtitleWindowStart > 0`)
      - `studioSubtitleWindowCombo` (`AppCombo` listing 50-cue pages `Đoạn 1–50 / N (00:00 → 03:42)`)
      - `studioSubtitleNextButton` (`Sau ›`, enabled when `studioSubtitleWindowEnd < studioSubtitleTotalCues`)
    - Save-back action: `studioSaveSubtitleButton` (`Lưu về Phụ đề`, `variant: "secondary"`, enabled when `rackEnabled && !controller.exporting`) that splices the edited window back into the SRT project's `track.wav` + `track.timeline.json` + `adjusted` cues.
- **`StudioClipRow.qml`**:
  - For SRT clips (`modelData.isSubtitle === true`):
    - Number badge shows `#<cueIndex + 1>` (`modelData.label`).
    - Sub-row shows SRT timing (`modelData.srtRangeLabel`), fitted speed (`Tốc độ: 1.25×`, tinted `Theme.warningText` when `modelData.rateCapped` or `modelData.overflowMs > 0`), and leading silence (`Nghỉ trước: 320 ms` when `modelData.leadSilenceMs > 0`), alongside engine provenance.
    - Reorder (`chevronUp` / `chevronDown`) and delete (`studioDeleteClipButton`) buttons are hidden so cue order and indices never desync from the SRT file.
- **`studioRegenDialog` (`StudioTab.qml`)**:
  - When editing an SRT clip (`regenDialog.isSubtitle === true`):
    - Shows the cue's SRT target window (`00:04:15,800 → 00:04:18,000 · 2.20s`).
    - Speed fit selector (`studioRegenSpeedModeSwitch`):
      - `auto`: **Tự động theo SRT** (fits via `project.subtitle_meta.policy`).
      - `custom`: **Tốc độ tùy chỉnh** (`studioRegenSpeedSlider`, `0.50×–2.00×`, step `0.05`).
    - Secondary action `studioApplyClipSpeedButton` (`Chỉ đổi tốc độ`) applies the selected speed mode to the clip's existing audio without re-running TTS synthesis.

## 2. Core Data Model & Windowed Partial Loading (`core/audio.py`, `core/studio.py`)

- **`read_wav_range(path, start_frame=0, stop_frame=None) -> tuple[np.ndarray, int]`** (`core/audio.py`):
  - Uses `soundfile.read(str(path), start=max(0, int(start_frame)), stop=stop_frame, dtype="float32", always_2d=False)` and `soundfile.info(str(path))` so loading a 50-cue window from a 2-hour `track.wav` reads only `[start_frame, stop_frame)` (~10–25 MB float32) without materializing the full file in RAM.
- **`MAX_STUDIO_SRT_CUES = 50`** (`core/studio.py`):
  - Maximum number of cues loaded into a `StudioProject` window at once.
- **`SubtitleStudioMeta`** (`core/studio.py`, frozen dataclass on `StudioProject.subtitle_meta: SubtitleStudioMeta | None = None`):
  - `project_id: str`
  - `title: str`
  - `total_cues: int`
  - `window_start: int` (inclusive 0-based cue index)
  - `window_end: int` (exclusive 0-based cue index)
  - `policy: FitPolicy`
  - `sample_rate: int = 48_000`
  - `prev_cue_end_ms: int | None = None` (placed `end_ms` of cue `window_start - 1`, or `None` when `window_start == 0`)
  - `orig_window_start_frame: int = 0` (frame offset in `track.wav` where this window's replacement region starts: `0` when `window_start == 0`, else `frames_for_ms(prev_cue_end_ms)`)
  - `orig_window_end_frame: int = 0` (frame offset in `track.wav` where the last cue in this window ends: `frames_for_ms(last_placed_end_ms)`)
  - `windows: tuple[dict[str, Any], ...] = ()` (summary of all 50-cue pages `{start, end, label}` for the QML window selector)
- **SRT fields on `StudioClip`** (all optional with defaults so non-SRT clips stay unchanged):
  - `cue_index: int | None = None`
  - `srt_start_ms: int = 0`
  - `srt_end_ms: int = 0`
  - `placed_start_ms: int = 0`
  - `placed_end_ms: int = 0`
  - `lead_silence_ms: int = 0`
  - `fit_rate: float = 1.0`
  - `overflow_ms: int = 0`
  - `raw_audio: np.ndarray | None = None` (uncompressed 1.0× speech buffer when available; used so repeated speed adjustments or re-fits never compound WSOLA artifacts)
- **`load_project_from_subtitle(store, project_id, start_cue=0, max_cues=MAX_STUDIO_SRT_CUES) -> StudioProject`**:
  - Validates `store.require(project_id)`, `store.has_track(project_id)`, and `store.load_timeline(project_id)`.
  - Clamps `start_cue` to `[0, len(cues) - 1]` and `window_end = min(len(cues), start_cue + max_cues)`.
  - Performs a single `read_wav_range(wav_path, first_cue_frame, last_cue_frame)` for `[timeline.segments[start_cue].start_ms .. timeline.segments[window_end - 1].end_ms]`.
  - Builds one `StudioClip` per cue in `[start_cue, window_end)` with exact `lead_silence_ms = max(0, seg.start_ms - prev_end_ms)` and estimated/recorded `fit_rate`.
- **SRT-aware `_concat(project, gap_frames, *, include_first_lead=False)`**:
  - When `project.subtitle_meta is None`, behaves identically to existing `_concat`.
  - When `project.subtitle_meta is not None`:
    - If the user committed/staged an explicit `GapOp`, uses `gap_frames` between clips.
    - Otherwise, inserts `frames_for_ms(clip.lead_silence_ms)` before each clip (for `clip[0]`, includes `lead_silence_ms` when `window_start == 0` or `include_first_lead=True`, and `0` during in-Studio preview of `window_start > 0` so playback begins immediately at the first cue of the window).

## 3. Per-Cue Speed & Auto-Fit on Regen (`core/studio.py`, `ui/controller.py`)

- **`refit_subtitle_clips(project, ChangedClipIndex, new_raw_audio, *, new_text=None, context=None, custom_rate=None) -> StudioProject`**:
  - Replaces `raw_audio` (and `text`/`context`) on the target clip.
  - If `custom_rate is None` (auto-fit): runs `plan_cue` + `stretch_clip_to` with `project.subtitle_meta.policy`.
  - If `custom_rate is not None` (`0.5 <= custom_rate <= 2.0`): stretches `raw_audio` by `custom_rate` via `time_stretch_audio` and computes the resulting duration and overflow against `[srt_start_ms, srt_end_ms]`.
  - Propagates forward placement (`prev_end_ms`) across subsequent clips in the window so `placed_start_ms`, `placed_end_ms`, and `lead_silence_ms` never overlap.
- **`set_subtitle_clip_speed(project, clip_id, custom_rate=None) -> StudioProject`**:
  - Adjusts an SRT clip's speed without re-synthesizing: derives base audio from `clip.raw_audio` (or unstretches `clip.audio` by `1.0 / clip.fit_rate` when `raw_audio is None` and `fit_rate != 1.0`), then calls `refit_subtitle_clips`.
- **Controller slots**:
  - `openSubtitleInStudio(project_id: str, start_cue: int = 0) -> bool` (loads off the GUI thread via `_run_bg`, guarded by `_studio_open_generation`).
  - `studioRegenClip(clip_id: str, voice: str, new_text: str = "", custom_rate: float = 0.0) -> bool` (`custom_rate <= 0` means auto-fit for SRT clips).
  - `studioSetClipSpeed(clip_id: str, custom_rate: float = 0.0) -> bool` (re-fits clip speed off-thread or inline without TTS).
  - `studioSaveToSubtitle() -> bool` (splices the active Studio project back into the SRT project off-thread).

## 4. Chunked Save-Back to Subtitle Project (`core/subtitle_project.py`, `ui/controller.py`)

- **`splice_studio_window_into_subtitle(store: SubtitleProjectStore, project: StudioProject) -> SubtitleProject`**:
  - Requires `project.subtitle_meta is not None`.
  - Renders the window's audio with `include_first_lead=True` (so the exact gap between cue `window_start - 1` and `window_start` is written) and applies the project's `ops` stack (`GainOp`, `NormalizeOp`, `FadeOp`, `SpeedOp`, `TrimOp`, `CutOp`, `SilenceTrimOp`).
  - Opens a new `StreamingWavWriter` part file in `store` and copies from existing `track.wav` in bounded 65,536-frame chunks:
    1. Prefix `[0 : meta.orig_window_start_frame)` from `track.wav`.
    2. Rendered Studio window audio.
    3. Suffix `[meta.orig_window_end_frame : total_frames)` from `track.wav`.
  - Computes `delta_ms = new_window_duration_ms - orig_window_duration_ms`.
  - Updates `Timeline.segments` and `SubtitleProject.adjusted`:
    - Within `[window_start, window_end)`: uses the clips' updated `placed_start_ms` / `placed_end_ms` (scaled proportionally if a whole-window `SpeedOp` / `TrimOp` / `CutOp` changed the window's total length).
    - After `window_end`: shifts `start_ms` and `end_ms` by `delta_ms`.
  - Atomically promotes `track.wav`, saves `track.timeline.json` and `project.json`, and returns the updated `SubtitleProject`.
- **`SubtitleController.reloadProject(project_id: str = "") -> bool`**:
  - Reloads the updated project and timeline from `SubtitleProjectStore` and emits `cuesChanged`, `durationChanged`, `statsChanged`, `renderedChanged` so `SubtitleCard` is immediately up to date.

## 5. Error Handling & Verification

- **Invariants**:
  - Studio never loads more than `MAX_STUDIO_SRT_CUES = 50` cues into RAM at once.
  - All disk I/O (`openSubtitleInStudio`, `studioSaveToSubtitle`, `studioSetClipSpeed`) runs off the GUI thread via `_run_bg` with generation guards.
  - Reorder and delete are refused on SRT projects (`move_clip` / `delete_clip` raise `ValueError` when `project.subtitle_meta is not None`).
- **Tests**:
  - Unit tests in `tests/unit/test_studio.py` and `tests/unit/test_subtitle_project.py`: ranged WAV read, 50-cue window pagination, SRT `_concat` gap fidelity, auto-fit vs custom-rate regen, per-cue speed change, and chunked `track.wav` + `timeline` + `adjusted` splice with downstream timestamp shift.
  - Controller unit tests in `tests/unit/test_studio_controller.py`: `openSubtitleInStudio`, window navigation, `studioRegenClip` with SRT auto-fit/custom rate, `studioSetClipSpeed`, and `studioSaveToSubtitle` reloading `SubtitleController`.
  - Smoke tests in `tests/smoke/test_ui_studio.py` and `tests/smoke/test_ui_tabs.py`: `subtitleStudioButton`, `studioGuideSubtitleButton`, `studioSubtitleBar`, window navigation controls, and `studioSaveSubtitleButton`.
