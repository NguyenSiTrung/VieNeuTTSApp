# Specification: Subtitles Tab Video Preview Panel (`video_preview_panel_20261028`, Bead `VieNeuTTSApp-t80a`)

## Overview

Audit finding 3.6 (`docs/ui-audit/SUBTITLES_TAB_UX_AUDIT.md`) identified that the **Lồng tiếng & Phụ đề** (`SubtitlesTab`) destination allows users to attach, transcribe, translate, and mux a video that they can never see in the app. During `subtitles_ux_overhaul_20261023`, the video preview was carved out into follow-up bead `VieNeuTTSApp-t80a` so it could be built cleanly atop the new `Source → Prepare → Produce` stage architecture and pinned `SubtitleActionDock`.

This track adds a dedicated, extracted `SubtitleVideoPreview.qml` component backed by `QtMultimedia` (`MediaPlayer` + `VideoOutput` + `AudioOutput`) and wired to `SubtitleController`. It operates in a **dual-mode playback architecture**:
1. **Source Preview Mode (Gốc):** Users can play, pause, and scrub the attached video with its original audio in any stage (`Source`, `Prepare`, `Produce`) — even before running ASR or rendering a dub track.
2. **Dub Sync Mode (Lồng tiếng):** When the dubbed audio track plays (via `SubtitleActionDock` or `subtitleController.play()`), the video preview automatically mutes its own `AudioOutput`, pauses independent source playback, and locks its video frame position to `subtitleController.positionMs`. Clicking or arrow-stepping cues in `SubtitleCueEditor` seeks the video frame whether or not a dub track has been rendered.

---

## Functional Requirements

### FR-1: Controller Video-Preview Seam & Unrendered Cue Seeking (`ui/subtitle_controller.py`)

- **FR-1.1 (`videoUrl` & `videoPath` exposure):**
  - Expose `subtitleController.videoPath` and a `videoUrl` (`QUrl.fromLocalFile(videoPath).toString()`) property to QML so `MediaPlayer.source` binds cleanly across Windows/macOS/Linux paths and clears immediately on `clearVideo()` / `clear()`.
  - Update `API_ONLY_PROPERTIES` in `ui/subtitle_controller.py` to reconcile properties now consumed directly by QML (`positionMs`, `videoPath` / `videoUrl`) so `test_every_property_is_consumed_or_api_only` remains exact.
- **FR-1.2 (Unrendered cue seeking when video is attached):**
  - When `seekToCue(index)` is called while `self._timeline is None` (unrendered or stale):
    - If `hasVideo` is `False`, preserve the existing behavior: set `hintText` (`"Chưa có bản âm thanh — hãy tạo âm thanh trước khi tua phụ đề."`) and return without seeking `self._player`.
    - If `hasVideo` is `True` and `0 <= index < len(project.cues)`, do **not** seek `self._player` (which has no rendered WAV loaded), do **not** emit the unrendered-seek refusal hint, update `activeCue` to `index`, update `positionMs` to `project.cues[index].start_ms`, and emit `videoSeekRequested(int)` (carrying the target timestamp in ms) so the video preview seeks to that cue immediately.
  - When `self._timeline is not None` (rendered), `seekToCue(index)` continues to seek the dub player (`self._player.seek(segment.start_ms)`) and also emits `videoSeekRequested(project.cues[index].start_ms)` so the video frame lands on the corresponding original video timestamp.
- **FR-1.3 (Audio exclusivity & active-cue sync between source video and dub player):**
  - Expose `@Slot() def notifyVideoPreviewPlaying()` to stop active dub/audition playback on `self._player` when source-video playback starts, and ensure starting dub playback (`play()`, `resume()`, speaker sample audition) stops active source-video playback so source audio and dubbed audio never play simultaneously.
  - Expose `@Slot(int) def syncActiveCueFromVideo(ms: int)` so source-video playback/scrubbing highlights the cue whose `[start_ms, end_ms]` span covers `ms` (or `-1` in silence) when dub playback is idle.

### FR-2: Extracted `SubtitleVideoPreview.qml` Component (`ui/qml/components/SubtitleVideoPreview.qml`)

- **FR-2.1 (Dual-mode `MediaPlayer` + `VideoOutput` + `AudioOutput`):**
  - Instantiate `MediaPlayer`, `AudioOutput`, and `VideoOutput` inside a self-contained `SubtitleVideoPreview.qml` card (`objectName: "subtitleVideoPreview"`) registered in both `src/vienetts_app/ui/qml/qmldir` and `src/vienetts_app/ui/qml/components/qmldir`.
  - **Source Mode (`"source"`):** `AudioOutput.muted` is `false` (unless user mutes); the panel's Play/Pause button and scrub slider drive `MediaPlayer` directly.
  - **Dub Sync Mode (`"dub"`):** Automatically activates whenever `subtitleController.playerState === "playing"` (or when the user toggles the audio mode switch to `"dub"` while `hasTrack` is true). In Dub Sync Mode, `AudioOutput.muted` is `true`, and `subtitleController.positionMs` drives `MediaPlayer.position` (throttled/clamped to avoid excessive seek jitter).
- **FR-2.2 (Live subtitle caption overlay):**
  - Render a translucent dark pill overlay (`objectName: "subtitleVideoCaptionOverlay"`) near the bottom of the video viewport (`objectName: "subtitleVideoViewport"`) displaying the active cue's speaker badge (if `speaker !== ""`) and text.
  - During Dub Sync Mode, display the cue at `subtitleController.activeCue`.
  - During Source Mode / scrubbing, resolve and display the cue from `subtitleController.cueModel` whose `[startMs, endMs]` span contains the current video position, updating `activeCue` via `syncActiveCueFromVideo`.
  - Include a quick toggle button (`objectName: "subtitleVideoCaptionToggle"`) to show/hide the caption overlay (defaulting to visible when cues are loaded).
- **FR-2.3 (Mini transport & scrub bar):**
  - Provide:
    - Source Play/Pause button (`objectName: "subtitleVideoPlayButton"`)
    - Scrub slider (`AppSlider`, `objectName: "subtitleVideoScrubSlider"`) bound to video duration/position
    - Monospace timecode label (`objectName: "subtitleVideoTimecodeLabel"`, `MM:SS / MM:SS`)
    - Audio source badge/switch (`objectName: "subtitleVideoAudioModeSwitch"`) indicating **Âm gốc** vs **Lồng tiếng**
- **FR-2.4 (Collapsible viewport & codec/backend fallback):**
  - Header bar displays the video title/badge, audio mode indicator, caption toggle, and a collapse/expand button (`objectName: "subtitleVideoCollapseButton"`).
  - When collapsed (`collapsed === true`), hide the 16:9 viewport and scrub row so the card shrinks to a single compact header row.
  - Auto-collapse outside the `source` stage when the window height is compact (`< 560 px`, matching `SubtitleActionDock.compact`) unless the user explicitly expands it.
  - If `MediaPlayer.error !== MediaPlayer.NoError` (e.g., missing OS codec or headless test backend), show a non-fatal inline message (`objectName: "subtitleVideoFallbackLabel"`) explaining that video frames cannot be decoded on this system while keeping all dubbing/ASR/export actions fully functional.

### FR-3: Stage Placement & Responsive Layout in `SubtitleCard.qml`

- **FR-3.1 (All-stage visibility when `hasVideo` is true):**
  - When `root.hasVideo` is `false`, `SubtitleVideoPreview` is hidden (`visible: false`) and takes zero layout height.
  - **Source stage (`stage === "source"`):** `SubtitleVideoPreview` appears directly below `SubtitleSourcePanel` (`subtitleVideoBar`), expanded by default so users can inspect the video before or after running Whisper ASR.
  - **Prepare & Produce stages (`stage === "prepare" || stage === "produce"`):**
    - On wide layouts (`root.wideLayout`, window width `>= 900 px`), the workspace `GridLayout` uses 2 columns whenever `root.stage === "produce" || (root.stage === "prepare" && root.hasVideo)`:
      - Left column: `SubtitleCueEditor` (`Layout.fillWidth: true`, `Layout.fillHeight: true`).
      - Right column: a `ColumnLayout` (`Layout.preferredWidth: 404`, `Layout.maximumWidth: 424`) stacking `SubtitleVideoPreview` on top and `subtitleTimingPanel` (`THỜI GIAN & GIỌNG ĐỌC`, visible in `produce`) below.
    - On narrow layouts (`!root.wideLayout`), `SubtitleVideoPreview` stacks cleanly above `SubtitleCueEditor` and `subtitleTimingPanel`.
- **FR-3.2 (Zero regression on pinned dock & stage model):**
  - `SubtitleActionDock` remains pinned outside `PageShell` and its height/visibility at `640×420` is untouched.
  - Stage transitions (`source` ↔ `prepare` ↔ `produce`) preserve the loaded video state without reloading from disk on each stage switch.

---

## Non-Functional Requirements

- **NFR-1 (Test & Smoke Contract Compatibility):**
  - Existing `objectName` contracts in `SubtitlesTab.qml`, `SubtitleCard.qml`, `SubtitleActionDock.qml`, and `SubtitleSourcePanel.qml` must remain intact.
  - `SubtitleVideoPreview.qml` must be registered in `tests/unit/test_subtitle_components.py` (`COMPONENTS`) and instantiate cleanly in isolated `QT_QPA_PLATFORM=offscreen` subprocesses without crashing or hanging.
- **NFR-2 (Performance & Resource Lifecycle):**
  - Video playback must release file handles immediately (`videoReleaseRequested` → `mediaPlayer.stop(); mediaPlayer.source = ""`) when `clearVideo()`, `clear()`, `openProject()`, `removeProject()`, or `exportVideo()` runs (critical on Windows to avoid `WinError 32` file-locking conflicts during `ffmpeg` muxing or workspace deletion).
- **NFR-3 (Localization & Packaging):**
  - All new user-facing Vietnamese strings in QML/Python must be wrapped in `qsTr()` / `self.tr()` and translated in `vienetts_en.ts` via `scripts/update_i18n.sh` (0 unfinished strings).
  - Precompiled QML (`packaging/qml_aot.py`) and `ruff check .` + `ruff format --check .` must pass cleanly.

---

## Acceptance Criteria

1. **AC-1 (Source video preview):** Attaching a video in `Source` stage displays `SubtitleVideoPreview` with 16:9 viewport, duration/timecode, scrub slider, and source Play/Pause.
2. **AC-2 (All-stage adaptive layout):** Switching between `Source`, `Prepare`, and `Produce` keeps `SubtitleVideoPreview` accessible — side-by-side with `SubtitleCueEditor` on wide windows (`>= 900 px`) in `Prepare`/`Produce`, stacked above `subtitleTimingPanel` in `Produce`, and collapsible (auto-compact at `< 560 px` height outside `Source`).
3. **AC-3 (Dual-mode audio exclusivity & dub sync):** Playing the rendered dub from `SubtitleActionDock` mutes the video player's own audio and syncs the video position to the dub playback clock; playing the source video pauses any active dub playback.
4. **AC-4 (Unrendered & rendered cue seeking):** Clicking a cue or pressing `Up`/`Down` in `SubtitleCueEditor` when `hasVideo` is true seeks the video preview to that cue's timestamp both before and after rendering; when `hasVideo` is false and unrendered, the existing refusal hint is preserved.
5. **AC-5 (Live caption overlay & fallback):** The video viewport renders the active cue's text/speaker overlay (toggleable) and degrades gracefully with an inline notice if `MediaPlayer` reports a decode/backend error.
6. **AC-6 (Windows handle release & quality gates):** Clearing the video, switching/deleting projects, or starting `exportVideo()` unloads `MediaPlayer.source` before touching disk; `test_every_property_is_consumed_or_api_only`, `test_subtitle_components.py`, smoke tests, `ruff`, and `scripts/update_i18n.sh` all pass.

---

## Out of Scope

- Frame-accurate non-linear video editing (cutting/trimming video scenes or multi-track video timelines).
- Custom ASS subtitle font/color styling controls inside the preview overlay (burn-in export continues to use `core/video_dub.py`'s adaptive ASS styling).
- Hardware encoder/decoder configuration settings beyond QtMultimedia's default platform backend.
