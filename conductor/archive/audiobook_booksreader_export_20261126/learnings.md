# Track Learnings: audiobook_booksreader_export_20261126

Patterns, gotchas, and context discovered during implementation.

## Codebase Patterns (Inherited)

Key inherited patterns from `conductor/patterns.md` (252 patterns) and archived track `audiobook_epub_20260828`:

- **Quality gates:** `.venv/bin/ruff check .`, `.venv/bin/ruff format --check .`, `.venv/bin/pytest` with `QT_QPA_PLATFORM=offscreen` (unset `PYTHONDONTWRITEBYTECODE`), plus `scripts/update_i18n.sh` (0 unfinished) whenever user-facing strings change. Local commit per task with `git notes add -m "..."`; never push.
- **Atomic audio/file writes:** temp files must keep the target container extension (`x.part.wav`, `x.part.mp3`, `x.part.epub`, `x.part.m4b`) because `soundfile` and `ffmpeg` infer formats from the suffix; promote via `os.replace` with bounded `PermissionError` retries for Windows file locks and always clean up temp artifacts on error/cancel.
- **Native CLI spawning:** every `ffmpeg`/`ffprobe` invocation goes through `core/processes.py` (`run_cli`/`spawn_cli` + `find_executable`) and `core/ai_models.cli_command()` (Windows `CREATE_NO_WINDOW`, UTF-8 `errors="replace"`, `cancel_check`, child registry, and shebang CLI test-stub dispatch).
- **EPUB spine/href rules (`audiobook_epub_20260828`):** EPUB hrefs are percent-encoded and relative to the OPF directory — `unquote`, normalize forward slashes, and prefix `opf_dir`; pure stdlib only (no `ebooklib`/`lxml`), preserving Vietnamese diacritics verbatim.
- **Bounded memory:** stream WAV→MP3 in 65,536-frame blocks, copy raw MP3 bytes behind ID3 headers in 64 KiB chunks, and probe chapter durations via `soundfile.info` headers only.
- **GUI-thread offload & straggler guards:** heavy I/O / encoding / `ffmpeg` work runs off the GUI thread via `_run_bg` with per-operation generation counters and cancellation events; controllers accept injectable `bg_runner` (`run_sync` in tests).
- **Settings clamping:** validate new settings in `core/models.py` and clamp per-field in `core/settings.py` so one invalid/future value never resets unrelated user settings.
- **QML & smoke contracts:** preserve all existing `objectName`s, use `path_to_file_url` for local file URLs (percent-encoding `#`/`?`/diacritics and Windows `file:///C:/`), use `AppButton`/`AppCombo`/`AppMenu`/`AppMenuItem` skins, activate hidden `StackLayout` tabs + `processEvents` in smoke drivers, and give QML context fakes real `@Property(..., notify=...)` descriptors.
- **Parallel sub-agent discipline:** sub-agents own disjoint files and run scoped tests only; serialize commits and full gates through the orchestrator to avoid `.git/index.lock` races.

---

<!-- Learnings from implementation will be appended below -->

## [2026-11-26 12:24] - Phase 1 Tasks 1.1–1.4: Core Primitives & Settings Foundation
- **Implemented:** Extended OPF metadata + cover extraction (`core/epub.py`), pure-Python ID3v2.4.0 tag builder (`core/id3.py`), streaming multi-part MP3 encoder with ID3 prepend & cancel (`core/audio.py`), and `m4b_bitrate` setting + `SettingsExport.qml` combo.
- **Commits:** `4bdcc67` (1.1), `a9b49d4` (1.2), `78226f7` (1.3), `4d95526` (1.4)
- **Learnings:**
  - Patterns: `libsndfile` writes raw MPEG frames (`\xff\xfb...`) with no ID3 header, and `soundfile.info()` cleanly skips a valid prepended ID3v2.4 header; on encoding abort/cancel, use `_force_close_soundfile(writer)` instead of `writer.close()` so flush errors on half-written streams never mask `ExportCancelled` while still releasing Windows file handles.
  - Gotchas: `tests/fixtures/sample.epub` declares `<item id="cover-image" .../>` without `properties="cover-image"` or `<meta name="cover" .../>`, so its extracted cover is `None` (downstream tests needing covers should craft an EPUB with explicit cover metadata).
  - Gotchas: `SettingRow` in `SettingsExport.qml` filters on `[label, keywords]` (not `description`) — keep `"tốc độ"` out of `m4bBitrateRow`'s label/keywords so it doesn't collide with `test_ui_tabs.py` speed-slider filter assertions.
  - Context: `AppController.m4bBitrate` exposes both `@m4bBitrate.setter` and `@Slot(str) def setM4bBitrate`; `m4bBitrateCombo.onActivated` re-binds `currentIndex` via `Qt.binding(...)` so programmatic controller updates keep syncing.
---

## [2026-11-26 12:32] - Phase 2 Tasks 2.1–2.2: Workspace Metadata/Cover Persistence & Upgraded MP3 Export
- **Implemented:** Extended `BookRecord` + `BookExportMetadata` + `cover_path()` + atomic cover write + legacy backfill & on-the-fly `source_path` fallback (`aae7490`); upgraded `export_chapter(..., "mp3")` to stream-merge oversize chapters into 1 ID3v2.4-tagged MP3 flat in `dest_dir` while keeping WAV unchanged, and added `export_mp3_chapters()` writing zero-padded tracks into `<dest>/<Sanitized Book Title>/` with contiguous `TRCK` `1/N..N/N`, cover threshold policy, progress, and cancel (`32e9b60`).
- **Commits:** `aae7490` (2.1), `32e9b60` (2.2)
- **Learnings:**
  - Patterns: `export_chapter` and `export_mp3_chapters` let `ExportCancelled` propagate untouched while wrapping filesystem/encode errors in `AudiobookError`, so cancellation stays distinct from failure across the controller boundary.
  - Context: `resolve_book_export_metadata(book_id, book=state)` accepts an already-loaded `BookState` so multi-chapter exports never re-parse `book.json`.
---

## [2026-11-26 12:45] - Phases 3 & 4 (Parallel): EPUB 3 Media Overlays & M4B Audiobook Exporters
- **Implemented:** `core/epub_export.py` (Narrated EPUB 3 Media Overlays with adaptive clip coalescing, DTD-free XHTML 5 / SMIL 3.0, entry-0 uncompressed mimetype, `ZIP_STORED` streamed MP3s, partial renders, cancel cleanup — `0a68320`); `core/m4b_export.py` (Single-file M4B via managed `ffmpeg`, `FFMETADATA1` escaping/chapters, header-only `Fraction`-accumulated WAV durations, WebP/GIF→PNG cover conversion with coverless fallback, `-progress pipe:1`, streaming ISO BMFF `ftyp`+`moov` validator + optional `ffprobe` check — `22677a9`).
- **Commits:** `0a68320` (Phase 3), `22677a9` (Phase 4)
- **Learnings:**
  - Patterns: Format SMIL clock values using exact integer millisecond math (`divmod(ms, 1000)` + stripping trailing zeros) instead of rounding to centiseconds, which can round `clipBegin`/`clipEnd` upward past `audio_duration_ms` or invert adjacent clips.
  - Patterns: Accumulate WAV chapter durations with `fractions.Fraction(frames, samplerate)` across multi-part oversize chapters and book boundaries so cumulative chapter timestamps have zero floating-point/rounding drift.
  - Gotchas: Never monkeypatch global `time.sleep` in tests that spawn subprocesses via `run_cli` (`subprocess.Popen.wait(timeout=...)` polls with `time.sleep`); patch module-level `REPLACE_LOCK_DELAY_S = 0.0` instead.
  - Gotchas: Strip illegal XML 1.0 control chars before both span trimming and XHTML/OPF serialization so strict `ElementTree.fromstring` parses never fail on dirty source text.
---

## [2026-11-26 12:49] - Phase 5 Task 5.1: AudiobookController Export Modes, Cancel, Cover URLs & Narrator
- **Implemented:** `coverUrl` (`path_to_file_url`) on `books` shelf rows, `exportAudiobook(dest_dir, epub3|m4b|mp3|wav)`, backward-compatible `exportAllReady(dest_dir)`, generation-guarded `cancelExport()` + `terminate_children()` in `shutdown()`, and `_resolve_narrator_name()` reading chapter context + voice catalog labels (`981a08d`).
- **Commit:** `981a08d`
- **Learnings:**
  - Patterns: Emit `_exportStep(gen, done, total)` with the export generation so late progress ticks from a cancelled export are dropped on the GUI thread without touching the UI.
  - Context: `AppController`'s `catalog` callback returns dicts (`{"name": ..., "description": ...}`) rather than tuples, which `_build_voices()` turns into `{"id": name, "label": display_label}` rows.
---

## [2026-11-26 12:59] - Phase 5 Task 5.2: AudiobookTab Export Menu, Cancel Button, Covers, i18n & Full Verification
- **Implemented:** `AudiobookTab.qml` (`audiobookExportMenu` `AppMenu` with EPUB 3 Read-Aloud / M4B / MP3 / WAV items, danger-styled `cancelExportButton` while exporting, shelf cover `Image` + initials fallback, backward-compatible `exportAllTo(url)`), full `tests/smoke/test_ui_tabs.py` assertions (`m4bBitrateCombo`, export menu dispatch, cancel button, cover thumbnails, legacy `exportAllTo`), and vi/en i18n catalog (`scripts/update_i18n.sh`: 1,301/1,301 finished, 0 unfinished).
- **Commit:** `5b907d8`
- **Verification:** `ruff check .` + `ruff format --check .` (210 files) green; 311/311 affected unit+smoke tests green (`test_epub`, `test_id3`, `test_audio`, `test_settings`, `test_audiobook`, `test_audiobook_epub_mo`, `test_audiobook_m4b`, `test_audiobook_controller`, `test_theme`, `test_ui_tabs`); full `pytest -n 4 -q`: 1,463 passed, 5 skipped, 0 regressions (the 6 failures are pre-existing `linux/aarch64` Qwen/audio.cpp host-cell + system ffmpeg 6.1.1 `video_dub` baselines untouched by this track).
- **Learnings:**
  - Gotchas: `MenuItem` inherits `AbstractButton`, so QML smoke drivers invoke `"click"` (not `"trigger"`, which belongs to `Action`).
  - Gotchas: Opening a `FolderDialog` offscreen steals window focus and breaks later `activeFocus` assertions in the same scenario group — set `DontUseNativeDialog` (`0x00000004`), place dialog assertions after focus-sensitive checks, and close both dialog and opener menu cleanly.
---
