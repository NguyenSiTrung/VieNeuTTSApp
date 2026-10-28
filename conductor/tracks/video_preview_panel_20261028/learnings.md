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
