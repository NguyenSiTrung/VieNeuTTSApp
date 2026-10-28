# Implementation Plan: Subtitles Tab Video Preview Panel (`video_preview_panel_20261028`)

## Overview

3-phase TDD implementation for bead `VieNeuTTSApp-t80a` across `src/vienetts_app/ui/subtitle_controller.py`, `src/vienetts_app/ui/qml/components/SubtitleVideoPreview.qml`, `src/vienetts_app/ui/qml/components/SubtitleCard.qml`, `src/vienetts_app/ui/qml/qmldir`, `src/vienetts_app/ui/qml/components/qmldir`, `src/vienetts_app/ui/i18n/vienetts_en.ts`, and `docs/ui-audit/SUBTITLES_TAB_UX_AUDIT.md` — automated verification (`ruff check .`, `ruff format --check .`, `pytest`, `scripts/update_i18n.sh`).

---

## Phase 1: Controller Video-Preview Seam, Unrendered Cue Seeking & Audio Exclusivity
<!-- execution: sequential -->
<!-- depends: -->

- [x] **Task 1.1: Expose `videoUrl`, `videoSeekRequested`, `videoReleaseRequested`, unrendered video cue seeking, and audio exclusivity in `SubtitleController`** (e2d9492)
  <!-- files: src/vienetts_app/ui/subtitle_controller.py, tests/unit/test_subtitle_controller.py -->
  - Red tests:
    - `videoUrl` returns `QUrl.fromLocalFile(videoPath).toString()` when `hasVideo` is `True` and `""` when `hasVideo` is `False`; `clearVideo()`, `clear()`, `openProject()`, `removeProject()`, and `exportVideo()` emit `videoReleaseRequested` before touching disk.
    - When `hasVideo` is `True` and the project is unrendered (`_timeline is None`), `seekToCue(i)` does not call `self._player.seek` (`player.seeks == []`), does not set the unrendered-seek refusal `hintText`, sets `activeCue == i` and `positionMs == project.cues[i].start_ms`, and emits `videoSeekRequested(project.cues[i].start_ms)`.
    - When `hasVideo` is `False` and unrendered, `seekToCue(i)` preserves the refusal `hintText` (`"Chưa có bản âm thanh — hãy tạo âm thanh trước khi tua phụ đề."`) and leaves `activeCue == -1`.
    - When rendered (`_timeline is not None`) and `hasVideo` is `True`, `seekToCue(i)` seeks `self._player` to `segment.start_ms` and emits `videoSeekRequested(project.cues[i].start_ms)`.
    - `notifyVideoPreviewPlaying()` stops active dub/audition playback on `self._player`, and `syncActiveCueFromVideo(ms)` updates `activeCue` to the cue spanning `ms` (or `-1` in silence) when dub playback is not active.
  - Green implementation:
    - Add `videoSeekRequested = Signal(int)` and `videoReleaseRequested = Signal()`, `@Property(str, notify=videoChanged) def videoUrl`, `@Slot() def notifyVideoPreviewPlaying()`, and `@Slot(int) def syncActiveCueFromVideo(ms: int)` to `SubtitleController`.
    - Update `seekToCue(index)` to support unrendered video seeking when `self.hasVideo` is `True`.
    - Emit `videoReleaseRequested` in `clearVideo()`, `clear()`, `openProject()`, `removeProject()`, and `exportVideo()`.

---

## Phase 2: Extracted `SubtitleVideoPreview.qml` Component
<!-- execution: sequential -->
<!-- depends: -->

- [x] **Task 2.1: Create `SubtitleVideoPreview.qml` with dual-mode `MediaPlayer`/`VideoOutput`, live caption overlay, mini transport, collapse toggle, and codec fallback** (e210942)
  <!-- files: src/vienetts_app/ui/qml/components/SubtitleVideoPreview.qml, src/vienetts_app/ui/qml/qmldir, src/vienetts_app/ui/qml/components/qmldir, tests/unit/test_subtitle_components.py -->
  - Red tests:
    - Add `"SubtitleVideoPreview.qml"` to `COMPONENTS` in `tests/unit/test_subtitle_components.py` and verify standalone instantiation under `QT_QPA_PLATFORM=offscreen` with `StubController`.
    - Verify `objectName` contract: `subtitleVideoPreview`, `subtitleVideoViewport`, `subtitleVideoCaptionOverlay`, `subtitleVideoCaptionToggle`, `subtitleVideoPlayButton`, `subtitleVideoScrubSlider`, `subtitleVideoTimecodeLabel`, `subtitleVideoAudioModeSwitch`, `subtitleVideoCollapseButton`, `subtitleVideoFallbackLabel`.
    - Verify collapse/expand toggle behavior, auto-collapse in `compact` mode outside `"source"` stage, caption overlay visibility/text binding, and automatic transition to muted `"dub"` sync mode when `subtitleController.playerState === "playing"`.
  - Green implementation:
    - Build `src/vienetts_app/ui/qml/components/SubtitleVideoPreview.qml` using `QtMultimedia` (`MediaPlayer`, `AudioOutput`, `VideoOutput`), `AppCard`, `AppButton`, `AppIconButton`, `AppSlider`, and `StatusBadge`, and register `SubtitleVideoPreview` in both `qmldir` files.
    - Wire `Connections` to `subtitleController` (`onVideoSeekRequested`, `onVideoReleaseRequested`, `onPositionMsChanged`, `onPlayerStateChanged`, `onVideoChanged`).
    - Extend `StubController` and component assertions in `tests/unit/test_subtitle_components.py`.

---

## Phase 3: `SubtitleCard` Integration, `API_ONLY_PROPERTIES` Contract, Smoke Tests & Localization
<!-- execution: sequential -->
<!-- depends: phase1, phase2 -->

- [x] **Task 3.1: Integrate `SubtitleVideoPreview` into `SubtitleCard.qml` across `Source`, `Prepare`, and `Produce` stages and update `API_ONLY_PROPERTIES`** (99ec44c)
  <!-- files: src/vienetts_app/ui/qml/components/SubtitleCard.qml, src/vienetts_app/ui/subtitle_controller.py, tests/unit/test_subtitle_controller.py, tests/smoke/test_ui_tabs.py -->
  - Red tests:
    - `test_every_property_is_consumed_or_api_only` in `tests/unit/test_subtitle_controller.py` passes with QML-consumed properties (`positionMs`, `durationMs`, `videoPath`, `videoUrl`) reconciled in `API_ONLY_PROPERTIES`.
    - Smoke test in `tests/smoke/test_ui_tabs.py` asserts `subtitleVideoPreview` visibility across `source`, `prepare`, and `produce` when `hasVideo` is `True`, 2-column layout in `prepare` and `produce` at wide widths (`>= 900 px`), compact auto-collapse at `640×420` with `subtitleActionDock` remaining visible, and unrendered cue-row click seeking the video preview.
  - Green implementation:
    - Mount `SubtitleVideoPreview` in `SubtitleCard.qml` (in `source` below `SubtitleSourcePanel`, and in the right-hand column of the `Prepare`/`Produce` workspace `GridLayout` stacked above `subtitleTimingPanel`).
    - Reconcile `API_ONLY_PROPERTIES` and `CUE_ROLES` in `src/vienetts_app/ui/subtitle_controller.py`.
    - Update `FakeSubtitleController` and subtitle smoke scenarios in `tests/smoke/test_ui_tabs.py`.

- [x] **Task 3.2: Update English translations (`vienetts_en.ts`), audit table (`SUBTITLES_TAB_UX_AUDIT.md`), and verify full gates** (52e4f25)
  <!-- files: src/vienetts_app/ui/i18n/vienetts_en.ts, docs/ui-audit/SUBTITLES_TAB_UX_AUDIT.md, tests/unit/test_i18n.py -->
  <!-- depends: task1 -->
  - Red tests:
    - `scripts/update_i18n.sh` + `pytest tests/unit/test_i18n.py` pass with 0 unfinished entries.
  - Green implementation:
    - Populate English translations in `src/vienetts_app/ui/i18n/vienetts_en.ts` and compile `vienetts_en.qm` via `scripts/update_i18n.sh`.
    - Update finding 3.6 in `docs/ui-audit/SUBTITLES_TAB_UX_AUDIT.md` to reference `video_preview_panel_20261028` / `VieNeuTTSApp-t80a`.
