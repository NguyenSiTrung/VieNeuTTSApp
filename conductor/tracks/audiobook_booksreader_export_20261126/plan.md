# Implementation Plan: BooksReader-Compatible Audiobook Export (`audiobook_booksreader_export_20261126`)

## Overview

The plan has 5 phases (12 TDD tasks) organized for maximal safe parallelism without file ownership conflicts. Read `conductor/patterns.md` and this track's `learnings.md` before starting.

Notable rules for this track:
- Zero new third-party dependencies (stdlib + existing `soundfile`/`numpy`; managed/PATH `ffmpeg` only for M4B and WebP/GIF→PNG cover conversion).
- Atomic writes keep the target extension (`.part.mp3`, `.part.epub`, `.part.m4b`, `.part.<img_ext>`) with bounded Windows `PermissionError` retries and cleanup on error/cancel.
- Native CLI test stubs (`ffmpeg`/`ffprobe`) are pure-Python `#!/usr/bin/env python3` scripts dispatched via `cli_command()` (Windows `WinError 216` rule).
- Never load full chapters or whole books into RAM (stream in 65,536-frame / 64 KiB chunks; measure WAV durations via `soundfile.info`).
- `Settings` per-field clamping in `core/settings.py`; never reset unrelated settings on invalid values.
- QML smoke contracts: preserve every existing `objectName` in `AudiobookTab.qml` and `SettingsExport.qml`, and keep `exportAllReady(dest_dir)` and `exportAllTo(url)` backward-compatible.
- Parallel sub-agents run scoped tests only; serialize commits through the orchestrator with the full gate.

Per task:
1. Write failing red tests first (folding into existing unit suites where they exist; new suites for new modules).
2. Implement minimal clean code until green.
3. Run `ruff check .`, `ruff format --check .`, and `pytest` (plus `scripts/update_i18n.sh` on UI copy changes).
4. Commit locally with conventional commit prefix + `git notes add -m "..."` task summary. **Never push.**
5. Append discoveries to `learnings.md`.

Verification is automated only; there are no manual phase gates.

**Phase dependency graph:**
- **Phase 1** (`depends: `): 4 independent tasks in parallel (`core/epub.py`, `core/id3.py`, `core/audio.py`, Settings `m4b_bitrate`).
- **Phase 2** (`depends: phase1`): `core/audiobook.py` workspace metadata/cover persistence + upgraded ID3v2.4 MP3 export.
- **Phase 3** (`depends: phase2`) and **Phase 4** (`depends: phase2`): run **in parallel** (`core/epub_export.py` vs `core/m4b_export.py`).
- **Phase 5** (`depends: phase3, phase4`): `AudiobookController`, `AudiobookTab.qml`, i18n, smoke contracts, and full gate.

---

## Phase 1: Core Primitives & Settings Foundation
<!-- execution: parallel -->
<!-- depends: -->

- [x] **Task 1.1: Extended EPUB metadata & cover extraction (`core/epub.py`) (FR-1.1)** (`4bdcc67`)
  <!-- files: src/vienetts_app/core/epub.py, tests/unit/test_epub.py -->
  - Red tests (`tests/unit/test_epub.py`):
    - First non-empty `dc:title` wins over secondary titles (fallback `path.stem`).
    - Multi-author `dc:creator` collects `opf:role="aut"` and unset roles in document order, honors EPUB 3 `<meta refines="#id" property="role">`, filters out non-`aut` roles (`ill`, `trl`, `edt`), deduplicates, and sets `author = "; ".join(authors)`.
    - `language` normalizes BCP 47 (`vi`, `en_US` → `en-US`, empty when absent); `isbn` extracts ISBN-10/13 from `opf:scheme="ISBN"`, `urn:isbn:`, `isbn:`, and EPUB 3 `identifier-type` (`15`/`02`/`isbn`), stripping separators; `year` parses 4-digit year preferring `opf:event="publication"` / unset; `description` strips HTML tags and collapses whitespace.
    - Cover extraction supports EPUB 3 `properties="cover-image"` and EPUB 2 `<meta name="cover" content="<id>"/>`, sniffs magic bytes for JPEG/PNG/GIF/WebP, rejects remote URIs / >32 MiB entries / corrupt or unrecognized bytes silently.
    - `extract_epub_metadata(path)` returns metadata + cover without parsing chapter XHTML.
  - Green implementation:
    - Extend `EpubBook` with default-valued fields (`authors: tuple[str, ...] = ()`, `language: str = ""`, `isbn: str = ""`, `year: int | None = None`, `description: str = ""`, `cover_bytes: bytes | None = None`, `cover_ext: str = ""`) keeping existing positional/keyword callers working.
    - Add `EpubMetadata` dataclass and `extract_epub_metadata(path)` + shared magic-byte sniffer (`sniff_image_ext(data)`).

- [x] **Task 1.2: Pure-Python ID3v2.4.0 tag builder (`core/id3.py`) (FR-2.1)** (`a9b49d4`)
  <!-- files: src/vienetts_app/core/id3.py, tests/unit/test_id3.py -->
  - Red tests (`tests/unit/test_id3.py`):
    - Binary header starts with `b"ID3\x04\x00\x00"` followed by valid 4-byte synchsafe size matching frame payload length; every frame header uses ID3v2.4 synchsafe frame size.
    - UTF-8 (`0x03`) encoding preserves Vietnamese diacritics verbatim across `TALB`, `TIT2`, `TPE1`, `TPE2`, `TRCK` (`f"{track}/{total}"`), `TPOS` (`"1/1"`), `TXXX` (`narrator`), and `TCOM`.
    - `APIC` frame encodes `0x03` + MIME (`image/jpeg`, `image/png`, `image/gif`, `image/webp`) + picture type `0x03` (Front Cover) + empty description + raw bytes; oversized (>32 MiB) or unknown cover is omitted; long text frames are bounded ≤ 64 KiB.
    - Round-trip verification with a pure-Python ID3v2.4 parser helper in tests confirms every frame decodes identically and synchsafe math handles sizes > 127 bytes and > 16,383 bytes.
  - Green implementation:
    - Create `src/vienetts_app/core/id3.py` with `Id3Metadata`, `build_id3v24_tag(...) -> bytes`, `should_embed_cover(cover_bytes, track_number) -> bool` (always on track 1; on all tracks when ≤ 512 KiB), and MIME helper.

- [x] **Task 1.3: Streaming multi-part MP3 encoder with optional ID3v2.4 prepend & cancel (`core/audio.py`) (FR-2.2)** (`78226f7`)
  <!-- files: src/vienetts_app/core/audio.py, tests/unit/test_audio.py -->
  - Red tests (`tests/unit/test_audio.py`):
    - `export_mp3_parts([wav1, wav2], dest, id3_bytes=tag)` streams both WAVs into a single valid MP3 whose duration matches `dur(wav1) + dur(wav2)` within MPEG frame tolerance, starts with `tag`, and is readable by `soundfile.info(..., format="MP3")`.
    - Rejects mismatched sample rates / channel counts cleanly with an actionable error and leaves no `.part.mp3` temp files on error.
    - `cancel_check` returning `True` mid-stream raises `ExportCancelled` and removes all temporary `.part.mp3` files.
    - Bounded memory: streams in `block_frames=65_536` blocks and streams raw MP3 bytes behind `id3_bytes` in 64 KiB chunks.
  - Green implementation:
    - Add `ExportCancelled(RuntimeError)` and `export_mp3_parts(sources, destination, *, id3_bytes=None, block_frames=65_536, cancel_check=None) -> Path` in `src/vienetts_app/core/audio.py` with atomic `.part.mp3` promotion, Windows lock retry, and full temp cleanup.

- [x] **Task 1.4: Configurable M4B AAC bitrate in Settings & `SettingsExport.qml` (FR-5.1)** (`4d95526`)
  <!-- files: src/vienetts_app/core/models.py, src/vienetts_app/core/settings.py, src/vienetts_app/ui/controller.py, src/vienetts_app/ui/qml/SettingsTab.qml, src/vienetts_app/ui/qml/settings/SettingsExport.qml, tests/unit/test_settings.py, tests/unit/test_controller.py -->
  - Red tests (`tests/unit/test_settings.py`, `tests/unit/test_controller.py`):
    - `Settings().m4b_bitrate == "96k"`; accepts `"64k"`, `"96k"`, `"128k"`; rejects invalid values.
    - `load_settings()` clamps an invalid `m4b_bitrate` back to `"96k"` without discarding other saved settings.
    - `AppController.m4bBitrate` getter/setter persists, emits `m4bBitrateChanged`, and rejects invalid values onto `errorText`.
  - Green implementation:
    - Add `m4b_bitrate: str = "96k"` and `_M4B_BITRATES = frozenset(("64k", "96k", "128k"))` in `core/models.py`; per-field clamp in `core/settings.py`.
    - Expose `m4bBitrate` / `m4bBitrateChanged` in `ui/controller.py`.
    - Add `m4bBitrateOptions` in `SettingsTab.qml` and `m4bBitrateRow` / `m4bBitrateCombo` in `SettingsExport.qml` (wired into `hasMatch`).

---

## Phase 2: Audiobook Workspace Persistence & Upgraded MP3 Chapter Export
<!-- execution: sequential -->
<!-- depends: phase1 -->

- [x] **Task 2.1: Workspace metadata/cover persistence & legacy fallback (`core/audiobook.py`) (FR-1.2)** (`aae7490`)
  <!-- files: src/vienetts_app/core/audiobook.py, tests/unit/test_audiobook.py -->
  - Red tests (`tests/unit/test_audiobook.py`):
    - `add_book()` persists `authors`, `language`, `isbn`, `year`, `description`, `cover_file` in `book.json`, writes `<book_id>/cover.<ext>` atomically, and stores `cover_file` in `library.json`.
    - Re-adding an already-imported legacy book backfills new metadata/cover without mutating chapters, `state.json`, or cached WAVs.
    - Loading a legacy `book.json` / `library.json` without the new keys succeeds (`authors` derived from `author`, `cover_file=""`).
    - `resolve_book_export_metadata(book_id)` returns stored metadata + cover bytes when present, and opportunistically reads from an existing `source_path` `.epub` for legacy workspaces without mutating cached audio; degrades silently when `source_path` is gone.
  - Green implementation:
    - Extend `BookRecord` with default-valued metadata fields and `cover_path(book_id, cover_file)` helper.
    - Implement atomic cover file write helper, legacy backfill on `add_book`, and `resolve_book_export_metadata(book_id)` in `AudiobookLibrary`.

- [x] **Task 2.2: Upgraded ID3v2.4 MP3 export & oversize stream-merge (`core/audiobook.py`) (FR-2.3)** (`32e9b60`)
  <!-- files: src/vienetts_app/core/audiobook.py, tests/unit/test_audiobook.py -->
  - Red tests (`tests/unit/test_audiobook.py`):
    - Single-chapter `export_chapter(book_id, i, dest, "mp3")` writes flat `NN - Title.mp3` with valid ID3v2.4 tags (`TALB`, `TIT2`, `TPE1`, `TPE2`, `TRCK`, `TPOS`, `TXXX:narrator`, `TCOM`, `APIC`).
    - Oversize multi-part chapter `export_chapter(book_id, i, dest, "mp3")` stream-merges all parts into **1** MP3 file (`01 - Chương dài.mp3`) whose duration equals the sum of parts.
    - Oversize WAV export `export_chapter(book_id, i, dest, "wav")` stays 100% unchanged (`01 - Chương dài - 01.wav`, `02.wav`).
    - Bulk MP3 export (`export_mp3_chapters(...)`) writes into `<dest_dir>/<Sanitized Book Title>/` with zero-padded names (`max(2, len(str(total)))`), `TRCK` `1/N..N/N` on every file, cover embedded on track 1 (and all tracks when ≤ 512 KiB), progress callback, and `cancel_check`.
  - Green implementation:
    - Upgrade `AudiobookLibrary.export_chapter()` for `format="mp3"` to use `build_id3v24_tag` + `export_mp3_parts` (keeping WAV untouched).
    - Add `AudiobookLibrary.export_mp3_chapters(book_id, indices, dest_dir, *, narrator="", book=None, progress=None, cancel_check=None) -> list[Path]`.

---

## Phase 3: Narrated EPUB 3 with Media Overlays (`core/epub_export.py` — Tier T0)
<!-- execution: sequential -->
<!-- depends: phase2 -->

- [x] **Task 3.1: Adaptive timeline clip coalescer & XHTML/SMIL builders (`core/epub_export.py`) (FR-3.2, FR-3.4)** (`0a68320`)
  <!-- files: src/vienetts_app/core/epub_export.py, tests/unit/test_audiobook_epub_mo.py -->
  - Red tests (`tests/unit/test_audiobook_epub_mo.py`):
    - Paragraph rendering creates `<h1>{title}</h1>` (untargeted) + `<p>` blocks from `split_paragraphs` with `<br/>` for internal newlines, wrapping each clip in `<span id="pN">` without crossing `<p>` boundaries (straddling spans split cleanly and target the first non-empty span).
    - Degenerate/zero-length/out-of-order spans are filtered/clamped so emitted `(charOffset, audioTimeMs)` anchors are strictly monotonically non-decreasing and `0 <= clipBegin < duration_ms`, `clipBegin <= clipEnd <= duration_ms`.
    - Fallback to `estimate_timeline(text, duration_ms)` when `.timeline.json` is absent.
    - **Budget stress test:** synthetic book with 10,000+ timeline segments automatically coalesces (paragraph boundaries first, then uniform stride) so total `<par>` count ≤ `4,000` (strictly ≤ BooksReader's `4,390`) AND cumulative uncompressed XML bytes (`container.xml` + `package.opf` + all `.smil`) ≤ `700,000` (strictly ≤ `781,100`).
  - Green implementation:
    - Create `src/vienetts_app/core/epub_export.py` with constants (`MAX_SAFE_PARS = 4_000`, `MAX_SAFE_XML_BYTES = 700_000`), clip normalization/coalescing planner, compact clock formatter (`12.35s`), strict DTD-free XHTML builder, and compact SMIL 3.0 builder.

- [x] **Task 3.2: Full EPUB 3 OCF packager with streamed MP3s, partial renders & cancel (`core/epub_export.py`) (FR-3.1, FR-3.3, FR-3.4)** (`0a68320`)
  <!-- files: src/vienetts_app/core/epub_export.py, tests/unit/test_audiobook_epub_mo.py -->
  - Red tests (`tests/unit/test_audiobook_epub_mo.py`):
    - ZIP entry 0 is uncompressed (`ZIP_STORED`) `mimetype == b"application/epub+zip"`.
    - Zero `<!DOCTYPE` anywhere in `container.xml`, `package.opf`, `nav.xhtml`, `c*.xhtml`, or `s*.smil`.
    - Strict namespace-aware `ElementTree.fromstring` succeeds on every XML/XHTML/SMIL entry; `package.opf` carries `dc:identifier` (ISBN or UUID), `dc:title`, all `dc:creator` authors, `dc:language` (default `"vi"`), `dcterms:modified`, `media:duration` per SMIL + book total, `media:active-class`, `nav` excluded from spine, optional cover (`cover-image` + EPUB 2 `meta name="cover"`).
    - Every SMIL `<text src="c{k}.xhtml#p{n}"/>` resolves to a unique non-whitespace `id` inside `<body>`; every `<audio src="a{k}.mp3"/>` is `ZIP_STORED` valid MP3 (never WAV) matching multi-part oversize chapter duration; global `(charOffset, audioTimeMs)` is monotonically non-decreasing across the entire book.
    - Partial render (2 of 5 chapters ready) exports all 5 readable chapters in spine + TOC with Media Overlays attached only to the 2 ready chapters; 0 ready chapters raises `AudiobookError`.
    - Cancellation mid-export cleans up `.part.epub` and temp audio files with no output left behind.
  - Green implementation:
    - Implement `export_epub_media_overlays(library, book_id, dest_dir, *, ready_indices=None, book=None, progress=None, cancel_check=None) -> Path` in `src/vienetts_app/core/epub_export.py`, streaming each ready chapter via `export_mp3_parts` into temp MP3s packed as `ZIP_STORED` and atomically promoting `.part.epub` → `<dest_dir>/<Sanitized Book Title>.epub`.

---

## Phase 4: Single-File M4B Audiobook Exporter (`core/m4b_export.py` — Tier T1)
<!-- execution: sequential -->
<!-- depends: phase2 -->

- [x] **Task 4.1: `FFMETADATA1` generator, WAV-header chapter timing & concat list (`core/m4b_export.py`) (FR-4.2)** (`22677a9`)
  <!-- files: src/vienetts_app/core/m4b_export.py, tests/unit/test_audiobook_m4b.py -->
  - Red tests (`tests/unit/test_audiobook_m4b.py`):
    - `escape_ffmetadata_value()` escapes `\`, `=`, `;`, `#`, and newlines while preserving Vietnamese Unicode verbatim.
    - Sample-accurate cumulative chapter timings from WAV headers (`soundfile.info`, no full audio decode) sum multi-part oversize chapters under 1 chapter entry, enforce strictly monotonic `START < total_duration_ms`, and emit valid `[CHAPTER]` blocks with `TIMEBASE=1/1000`.
    - Book metadata fields (`title`, `album`, `artist`, `album_artist`, `composer`, `date`, `comment`) populate cleanly and omit empty fields.
    - Concat list escapes single quotes and handles paths with spaces / Vietnamese characters.
  - Green implementation:
    - Create `src/vienetts_app/core/m4b_export.py` with `M4bChapter`, `escape_ffmetadata_value`, `build_ffmetadata`, `build_concat_list`, and header-only WAV duration probe.

- [x] **Task 4.2: `ffmpeg` M4B encoder, WebP/GIF cover conversion, progress/cancel & MP4 validation (`core/m4b_export.py`) (FR-4.1, FR-4.3, FR-4.4)** (`22677a9`)
  <!-- files: src/vienetts_app/core/m4b_export.py, tests/unit/test_audiobook_m4b.py -->
  - Red tests (`tests/unit/test_audiobook_m4b.py`):
    - Missing `ffmpeg` raises actionable `AudiobookError` mentioning ffmpeg installation and EPUB 3 / MP3 alternatives; 0 ready chapters raises `AudiobookError`.
    - Stub `ffmpeg` (via `cli_command`) verifies argv includes `-c:a aac -b:a <bitrate> -ac 1 -ar 48000`, `-map 0:a -map_metadata 1 -map_chapters 1 -f mp4 -movflags +faststart`, and attached JPEG/PNG cover (`-map 2:v -c:v copy -disposition:v:0 attached_pic`).
    - WebP/GIF cover triggers a pre-conversion `ffmpeg` call to PNG; if conversion fails, export logs warning and succeeds cleanly without cover art.
    - Progress relay parses `out_time_us`/`out_time_ms` (µs) capped at 0.95 until finalize; `cancel_check` terminates the stub child and leaves no `.part.m4b` or output file.
    - Pure-Python MP4 validation accepts `ftyp` + `moov` and rejects corrupt/truncated outputs.
    - Real-`ffmpeg` integration test (`pytest.mark.skipif` when `ffmpeg` absent) verifies a genuine `.m4b` has `ftyp`, `moov`, AAC `soun` stream, chapter markers, `ilst` metadata, and embedded cover.
  - Green implementation:
    - Implement `export_m4b_audiobook(library, book_id, dest_dir, *, ready_indices=None, bitrate="96k", narrator="", data_dir=None, book=None, progress=None, cancel_check=None) -> Path` using `resolve_ffmpeg`/`resolve_ffprobe` and `core/processes.py`, pure-Python top-level MP4 box validator, and atomic `.part.m4b` → `<dest_dir>/<Sanitized Book Title>.m4b`.

---

## Phase 5: Audiobook Controller, QML UI, i18n & Full Verification
<!-- execution: sequential -->
<!-- depends: phase3, phase4 -->

- [x] **Task 5.1: `AudiobookController` export modes, `cancelExport()`, cover URLs & narrator resolution (`ui/audiobook_controller.py`) (FR-5.2)** (`981a08d`)
  <!-- files: src/vienetts_app/ui/audiobook_controller.py, tests/unit/test_audiobook_controller.py -->
  - Red tests (`tests/unit/test_audiobook_controller.py`):
    - `books` rows include `coverUrl` (`file://...` when cover file exists on disk, `""` otherwise).
    - `exportAudiobook(dest_dir, mode)` dispatches `"epub3"`, `"m4b"`, `"mp3"`, and `"wav"` on `_run_bg`, passing `m4bBitrate`, `data_dir`, narrator display name, progress relay, and cancellation token.
    - Single-file modes (`epub3`, `m4b`) refuse when a ready chapter is actively playing or when 0 chapters are ready; bulk `mp3` writes into `<dest_dir>/<Sanitized Book Title>/`; `exportAllReady(dest_dir)` stays backward-compatible (WAV flat, MP3 into book subfolder).
    - `cancelExport()` stops an in-flight export cleanly (emits `exportFinished(0, "")`, clears `exporting`, leaves no partial files); `shutdown()` cancels any active export.
    - Invalid format mode sets actionable `errorText` and returns `False`.
  - Green implementation:
    - Update `_refresh_books()` with `coverUrl` (`path_to_file_url`), add `exportAudiobook(dest_dir, format_mode)` and `cancelExport()`, add generation + `threading.Event` cancellation wired into all four exporters and `shutdown()`, and add narrator display-name resolution helper.

- [x] **Task 5.2: `AudiobookTab.qml` export menu, cancel button, cover thumbnails, i18n & full verification (FR-5.3)** (`5b907d8`)
  <!-- files: src/vienetts_app/ui/qml/AudiobookTab.qml, src/vienetts_app/ui/i18n/vienetts_en.ts, src/vienetts_app/ui/i18n/vienetts_en.qm, tests/smoke/test_ui_tabs.py -->
  - Red tests (`tests/smoke/test_ui_tabs.py`):
    - Verify `audiobookExportMenu`, `exportEpub3MenuItem`, `exportM4bMenuItem`, `exportMp3MenuItem`, `exportWavMenuItem`, `cancelExportButton`, shelf cover `Image` + initials fallback, and `m4bBitrateCombo` in `SettingsExport.qml`.
    - Verify all existing `AudiobookTab.qml` and `SettingsExport.qml` `objectName` contracts and `exportAllTo(url)` → `exportAllReady(...)` backward compatibility remain intact.
    - Verify selecting a format item in `audiobookExportMenu` + accepting `exportAllDialog` calls `audiobook.exportAudiobook(path, mode)`.
  - Green implementation:
    - Wire `AppMenu` (`audiobookExportMenu`) onto `exportAllButton`, add `cancelExportButton` beside it while `audiobook.exporting`, render cover `Image` with initials fallback in `shelfRow`.
    - Run `scripts/update_i18n.sh`, translate all new Vietnamese strings in `src/vienetts_app/ui/i18n/vienetts_en.ts`, re-run `scripts/update_i18n.sh` until 0 unfinished.
    - Run final full gate: `ruff check .`, `ruff format --check .`, and `pytest`.
