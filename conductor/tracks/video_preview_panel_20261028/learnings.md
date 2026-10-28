# Track Learnings: video_preview_panel_20261028

Patterns, gotchas, and context discovered during implementation.

## Codebase Patterns (Inherited)

📚 **217 patterns** available in `conductor/patterns.md`, plus learnings from `subtitles_ux_overhaul_20261023` and `dub_pacing_alignment_20261026`:

- **Both `qmldir` files must register every new component:** Any component added under `src/vienetts_app/ui/qml/components/` must be listed in both `src/vienetts_app/ui/qml/qmldir` (`ComponentName 1.0 components/ComponentName.qml`) and `src/vienetts_app/ui/qml/components/qmldir` (`ComponentName 1.0 ComponentName.qml`), and added to `COMPONENTS` in `tests/unit/test_subtitle_components.py`.
- **`API_ONLY_PROPERTIES` reverse assertion (`ui/subtitle_controller.py`):** `test_every_property_is_consumed_or_api_only` verifies that every `@Property` on `SubtitleController` is EITHER referenced in QML as `subtitleController.<name>` OR listed in `API_ONLY_PROPERTIES` — never both. Any property bound in QML must be removed from `API_ONLY_PROPERTIES`, and any new Python-only property must be added.
- **Stage-owned visibility (`subtitles_ux_overhaul_20261023` Task 5.1):** A `ColumnLayout` / `GridLayout` child that keeps a non-zero height after its own children turn invisible eats the vertical slack of `SubtitleCueEditor`. Bind the component's root `visible` directly to the stage/video condition so the layout drops the subtree completely when hidden.
- **Window-keyed `wideLayout` (`SubtitleCard.qml`):** `wideLayout` keys off `Window.window.width >= 900` (not card width, which shrinks non-monotonically at 800 px when the nav rail expands).
- **One primary button per screen state:** `SubtitleActionDock`'s `subtitleRenderButton` is the sole `variant: "primary"` CTA when a project is loaded; controls inside `SubtitleVideoPreview` must use `"secondary"` or `"quiet"` / `AppIconButton`.
- **Slider key-step commit pattern:** Qt Quick `Slider` toggles `pressed` around arrow-key steps; use `AppSlider` and guard programmatic position updates during user drag (`!scrubSlider.pressed`).
- **Windows file-handle release before disk mutation (`WinError 32`):** `QMediaPlayer` holds an OS file lock on its `source` URL on Windows; emit `videoReleaseRequested` to stop playback and clear `MediaPlayer.source = ""` before `clearVideo()`, `clear()`, `openProject()`, `removeProject()`, or `exportVideo()` runs.
- **Translation discipline (`scripts/update_i18n.sh`):** Run `scripts/update_i18n.sh`, translate every new string in `src/vienetts_app/ui/i18n/vienetts_en.ts` (identical English for identical Vietnamese source across contexts), and re-run `scripts/update_i18n.sh` so `test_i18n.py` sees 0 unfinished strings.

---

<!-- Learnings from implementation will be appended below -->

## [2026-10-28 09:20] - Phase 1 Task 1: Expose `videoUrl`, `videoSeekRequested`, `videoReleaseRequested`, unrendered video cue seeking, and audio exclusivity in `SubtitleController`
Thread: T-01a1803c-809b-723e-92b1-57566500f0ac
- **Implemented:** Added `videoUrl` (`QUrl.fromLocalFile(videoPath).toString()`), `videoSeekRequested(int)` and `videoReleaseRequested()` signals, unrendered video cue seeking in `seekToCue`, and `notifyVideoPreviewPlaying()` / `syncActiveCueFromVideo(ms)` slots in `SubtitleController`.
- **Files changed:** `src/vienetts_app/ui/subtitle_controller.py`, `tests/unit/test_subtitle_controller.py`
- **Commit:** e2d9492
- **Learnings:**
  - Patterns: Guard `notifyVideoPreviewPlaying()` on `self._player_state != "stopped"` so starting source-video playback after clicking a cue does not call `_reset_active_span()` and wipe `activeCue` back to `-1`.
  - Gotchas: Emit `videoReleaseRequested` before any disk operation (`clearVideo`, `clear`, `openProject`, `removeProject`, `exportVideo`, `shutdown`) so `QMediaPlayer` releases Windows file locks before `ffmpeg` or directory removal runs.
  - Context: `syncActiveCueFromVideo(ms)` is a no-op while `self._player_state == "playing"` so dub karaoke timing owns `activeCue` and `positionMs` during dub playback.
---

## [2026-10-28 09:30] - Phase 2 Task 1: Create `SubtitleVideoPreview.qml` with dual-mode `MediaPlayer`/`VideoOutput`, live caption overlay, mini transport, collapse toggle, and codec fallback
Thread: T-01a1803c-809b-723e-92b1-57566500f0ac
- **Implemented:** Created `src/vienetts_app/ui/qml/components/SubtitleVideoPreview.qml` with `QtMultimedia` (`MediaPlayer`, `AudioOutput`, `VideoOutput`), live caption pill overlay + toggle, mini transport, auto-collapse on compact windows outside Source stage, and codec fallback notice; registered in both `qmldir` files and tested in `tests/unit/test_subtitle_components.py`.
- **Files changed:** `src/vienetts_app/ui/qml/components/SubtitleVideoPreview.qml`, `src/vienetts_app/ui/qml/qmldir`, `src/vienetts_app/ui/qml/components/qmldir`, `tests/unit/test_subtitle_components.py`
- **Commit:** e210942
- **Learnings:**
  - Patterns: In QML helpers that read `subtitleController.cueModel`, support both `typeof m.get === "function"` (`DictListModel`) and `m.length !== undefined` (`QVariantList` in standalone stubs).
  - Gotchas: In PySide6 test stubs, expose list properties to QML as `@Property("QVariantList")` rather than `@Property("QVariant")`, which wraps Python lists as opaque `PyObjectWrapper` values without `.length`.
  - Context: `SubtitleVideoPreview` uses `variant: "chip"` and `AppIconButton` controls so `SubtitleActionDock.subtitleRenderButton` remains the sole `variant: "primary"` button on the screen.
---

## [2026-10-28 09:50] - Phase 3 Task 1: Integrate `SubtitleVideoPreview` into `SubtitleCard.qml` across `Source`, `Prepare`, and `Produce` stages and update `API_ONLY_PROPERTIES`
Thread: T-01a1803c-809b-723e-92b1-57566500f0ac
- **Implemented:** Mounted a single `SubtitleVideoPreview` instance in `SubtitleCard.qml` (`workspaceGrid`) across `Source`, `Prepare`, and `Produce` stages (2-column layout in `Prepare` and `Produce` at `>= 900 px`, auto-collapse at `< 560 px` window height outside `Source`), wrapped `AudioOutput` in a lazy `Loader` so headless QML instantiation never touches PulseAudio/PipeWire until source playback starts, removed `"index"` from `CUE_ROLES` so QtQuick `ListView` delegates receive 0-based `index` (while `modelData.index` retains the 1-based SRT sequence number), reconciled `API_ONLY_PROPERTIES`, and added smoke assertions in `tests/smoke/test_ui_tabs.py`.
- **Files changed:** `src/vienetts_app/ui/qml/components/SubtitleCard.qml`, `src/vienetts_app/ui/qml/components/SubtitleVideoPreview.qml`, `src/vienetts_app/ui/subtitle_controller.py`, `tests/smoke/test_ui_tabs.py`
- **Commit:** 99ec44c
- **Learnings:**
  - Patterns: In QtMultimedia QML components, wrap `AudioOutput` in a lazy `Loader` (`active: root._audioOutputRequested && !root.audioMuted`) so instantiating the component in offscreen/CI environments does not trigger a 5-second PulseAudio connection timeout.
  - Gotchas: Never register a custom role named `"index"` in `QAbstractListModel.roleNames()` (`DictListModel`) when delegates declare `required property int index`: QtQuick `ListView` binds the custom role instead of the 0-based row index, causing off-by-one active highlights and click seeks. `DictListModel` can still key diffs on `row["index"]` while `modelData.index` carries the 1-based sequence number.
  - Context: In a 2-column `GridLayout` where column 0 (`SubtitleCueEditor`) spans multiple rows and column 1 stacks `SubtitleVideoPreview` above `subtitleTimingPanel`, add a `Layout.fillHeight: true` spacer `Item` in row 2 of column 1 (visible only in `produce` 2-col mode) so extra height is absorbed below `subtitleTimingPanel` rather than opening a gap between the two right-column cards.
---

## [2026-10-28 09:55] - Phase 3 Task 2: Update English translations (`vienetts_en.ts`), audit table (`SUBTITLES_TAB_UX_AUDIT.md`), and verify full gates
Thread: T-01a1803c-809b-723e-92b1-57566500f0ac
- **Implemented:** Translated all 15 `SubtitleVideoPreview` strings in `src/vienetts_app/ui/i18n/vienetts_en.ts`, compiled `vienetts_en.qm` via `scripts/update_i18n.sh` (1190/1190 finished, 0 unfinished), and updated finding 3.6 in `docs/ui-audit/SUBTITLES_TAB_UX_AUDIT.md`.
- **Files changed:** `src/vienetts_app/ui/i18n/vienetts_en.ts`, `src/vienetts_app/ui/i18n/vienetts_en.qm`, `docs/ui-audit/SUBTITLES_TAB_UX_AUDIT.md`
- **Commit:** 52e4f25
- **Learnings:**
  - Patterns: Always set `elide: Text.ElideRight` on `SectionLabel` when its text includes a dynamic filename inside a bounded `RowLayout` column so long filenames elide cleanly instead of overlapping adjacent chip buttons.
---


