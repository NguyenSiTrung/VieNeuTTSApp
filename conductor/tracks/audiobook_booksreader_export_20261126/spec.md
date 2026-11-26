# Specification: BooksReader-Compatible Audiobook Export (`audiobook_booksreader_export_20261126`)

## Overview

Upgrade VieNeuTTSApp's Audiobook export pipeline so `.epub` books imported and synthesized in the app export cleanly across all three BooksReader-compatible consumption tiers while preserving full backward compatibility with existing WAV chapter export, cached audiobook workspaces, and UI smoke contracts:

1. **Narrated EPUB 3 with Media Overlays (`.epub` — BooksReader Tier T0):** single self-contained file with readable XHTML text, streamed MP3 narration (zero `ffmpeg`), and synchronized SMIL Media Overlays built from `ch_XXXX.timeline.json` (with `estimate_timeline` fallback) under BooksReader's hard XML and `<par>` limits.
2. **Single-File M4B Audiobook (`.m4b` — BooksReader Tier T1):** mono AAC-LC audio in an MP4 container (`+faststart`) with QuickTime (`tref/chap`) + Nero (`chpl`) chapter markers, iTunes `ilst` metadata, and embedded cover art (`covr`), built via VieNeuTTSApp's managed `ffmpeg` resolver.
3. **ID3v2.4-Tagged MP3 Chapters (`.mp3` — BooksReader Tier T1, zero `ffmpeg`):** pure-Python ID3v2.4.0 tagging (`TALB`, `TIT2`, `TPE1`, `TPE2`, `TRCK`, `TPOS`, `TXXX:narrator`, `TCOM`, `APIC`), 1-file-per-chapter streaming merge for oversize chapters, and `<dest_dir>/<Sanitized Book Title>/` folder output for bulk exports.
4. **WAV Chapters (`.wav`):** preserved 100% unchanged (flat in `dest_dir`, split parts for oversize chapters).

### Design Decisions (2026-11-26)

- **D1 — Export UI & Backward Compatibility:** `exportAllButton` in `AudiobookTab.qml` opens an `AppMenu` offering `epub3`, `m4b`, `mp3`, and `wav`, which opens `exportAllDialog` (`FolderDialog`) and calls a new `AudiobookController.exportAudiobook(dest_dir, format_mode)` slot. `AudiobookController.exportAllReady(dest_dir)`, `AudiobookTab.exportAllTo(url)`, and `Settings.export_format` (`"wav" | "mp3"`) remain backward-compatible.
- **D2 — WAV vs. MP3 Output Layout:** WAV export stays unchanged (flat in `dest_dir`, split `NN - Title - 01.wav` parts for oversize chapters). MP3 export stream-merges oversize chapters into 1 MP3 per chapter, writes flat for single-chapter `exportChapter()`, and writes into `<dest_dir>/<Sanitized Book Title>/` for bulk MP3 exports (`exportAudiobook(..., "mp3")` and `exportAllReady()` when `exportFormat == "mp3"`).
- **D3 — Included Polish & Controls:**
  - Export cancellation (`AudiobookController.cancelExport()`, `cancelExportButton` in `AudiobookTab.qml`, cooperative cancellation across MP3/EPUB3/WAV loops and `ffmpeg` child termination on cancel or `shutdown()`, cleaning up `.part` files).
  - Automatic WebP/GIF → PNG cover conversion via `ffmpeg` when embedding `covr` in M4B (degrading cleanly to no cover if conversion fails).
  - Extracted EPUB cover thumbnail display in `AudiobookTab.qml` shelf tiles (falling back to title initials when absent/unreadable).
  - Configurable M4B AAC bitrate (`Settings.m4b_bitrate`: `"64k" | "96k" | "128k"`, default `"96k"`) exposed in `Settings › Export`.
- **D4 — Track Settings:** Priority `high`, `depends_on: []`, parallel execution enabled across independent tasks and phases, automated verification only (no manual phase gates).

---

## Functional Requirements

### FR-1: Rich EPUB Metadata & Cover Extraction (`core/epub.py`, `core/audiobook.py`)

- **FR-1.1 Extended metadata extraction (`core/epub.py`):**
  - `title`: first non-empty `dc:title` (fallback `path.stem`).
  - `authors: tuple[str, ...]`: collect all non-blank `dc:creator` elements in document order whose role (`opf:role` attribute or EPUB 3 `<meta refines="#id" property="role">`) is `"aut"` (case-insensitive) or unset; deduplicate while preserving order. Keep `author: str = "; ".join(authors)` for backward compatibility.
  - `language: str`: first non-empty `dc:language`, normalized to BCP 47 (trim, `_` → `-`, lowercase primary language subtag, e.g., `"vi"`, `"en"`, `"en-US"`; else `""`).
  - `isbn: str`: first `dc:identifier` recognized as ISBN (`opf:scheme="ISBN"`, `urn:isbn:`, `isbn:`, or EPUB 3 `<meta refines="#id" property="identifier-type">` matching `15`, `02`, or `isbn` case-insensitive), stripped of prefix and separators (`-`, spaces) if valid ISBN-10 or ISBN-13; else `""`.
  - `year: int | None`: 4-digit year (`1000..2999`) from `dc:date`, preferring `opf:event="publication"` or unset `opf:event`.
  - `description: str`: plain-text `dc:description` (HTML tags stripped if present, whitespace collapsed), else `""`.
  - **Cover extraction (`cover_bytes: bytes | None`, `cover_ext: str`):**
    - Locate via EPUB 3 manifest item whose `properties` token list contains `"cover-image"`, else EPUB 2 `<meta name="cover" content="<id>"/>` referencing a manifest item.
    - Ignore remote URIs (`http://`, `https://`, `data:`), check uncompressed ZIP entry size ≤ 32 MiB before reading, and sniff real format by magic bytes: JPEG (`FF D8 FF` → `"jpg"`), PNG (`89 50 4E 47 0D 0A 1A 0A` → `"png"`), GIF (`GIF8` → `"gif"`), WebP (`RIFF....WEBP` → `"webp"`).
    - Silently ignore missing, oversize, corrupt, or unrecognized cover entries.
  - Provide a lightweight `extract_epub_metadata(path)` helper so legacy workspaces can opportunistically recover metadata/cover on-the-fly from `source_path` without re-extracting all chapter XHTML.
- **FR-1.2 Workspace persistence & backward compatibility (`core/audiobook.py`):**
  - Extend `BookRecord` (with default values for backward compatibility) with `authors: tuple[str, ...] = ()`, `language: str = ""`, `isbn: str = ""`, `year: int | None = None`, `description: str = ""`, `cover_file: str = ""`.
  - Persist these optional fields in `<book_id>/book.json` and `cover_file` in `library.json` (for fast shelf cover lookup without parsing `book.json`).
  - Write extracted cover atomically (`temp` + `os.replace` with Windows lock retry) to `<book_id>/cover.<ext>`.
  - Re-adding an already-imported book (`add_book`) backfills missing metadata/cover fields without touching chapters, state, or cached WAVs.
  - Legacy `book.json` / `library.json` lacking the new fields load cleanly (deriving `authors` from non-empty `author` split on `;` if `authors` is absent). At export time, if metadata/cover is missing and `source_path` still exists as an `.epub`, read metadata/cover on-the-fly without mutating cached audio.

### FR-2: ID3v2.4-Tagged MP3 Chapters (`core/id3.py`, `core/audio.py`, `core/audiobook.py`)

- **FR-2.1 Pure-Python ID3v2.4.0 builder (`core/id3.py`, zero new deps):**
  - Build valid ID3v2.4.0 headers (`ID3\x04\x00\x00` + 4-byte synchsafe size) and frames (4-byte synchsafe frame sizes, UTF-8 `0x03` encoding for text frames, text frames bounded ≤ 64 KiB, `APIC` ≤ 32 MiB):
    - `TALB`: book title
    - `TIT2`: chapter title
    - `TPE1`: `"; ".join(authors)` (omitted if empty)
    - `TPE2`: album artist (same as `TPE1`)
    - `TRCK`: `f"{track_number}/{total_tracks}"` (1-based positive integers, written on **every** exported MP3)
    - `TPOS`: `"1/1"`
    - `TXXX`: description `"narrator"`, value = narrator/voice display name (when known)
    - `TCOM`: narrator/voice display name (when known)
    - `APIC`: picture type `0x03` (Front Cover), MIME (`image/jpeg`, `image/png`, `image/gif`, `image/webp`), empty description, raw bytes. Embedded on track 1 at minimum, and on every track when cover ≤ 512 KiB.
  - Prepend tag bytes ahead of the libsndfile-encoded MPEG frame stream so `Xing`/`Info` frames and audio frames remain untouched.
- **FR-2.2 Streaming multi-part MP3 merge (`core/audio.py`):**
  - Add a streaming multi-source MP3 helper (`export_mp3_parts`) that reads 1..N WAV files sequentially in 65,536-frame blocks into a single `sf.SoundFile(..., format="MP3", subtype="MPEG_LAYER_III")` writer (constant ~256 KB RAM), checks optional `cancel_check`, validates the encoded MP3 via `_validate_mp3_artifact`, prepends optional ID3v2.4 bytes by streaming copy (never loading whole MP3s into RAM), and atomically promotes `.part.mp3` → destination via `os.replace` with Windows lock retry and cleanup on error/cancel.
- **FR-2.3 Chapter & bulk MP3 export (`core/audiobook.py`):**
  - WAV (`format="wav"`): completely unchanged (flat in `dest_dir`, split `NN - Title - 01.wav`, `02.wav` for oversize chapters).
  - MP3 (`format="mp3"`): stream-merges oversize chapters into **1 MP3 per chapter** (`{chapter_num:0{pad}d} - <Sanitized Chapter Title>.mp3`, `pad = max(2, len(str(total_chapters)))`), tagged with full ID3v2.4 metadata (`TRCK = f"{track_num}/{total_tracks}"`).
  - Single-chapter `export_chapter(..., format="mp3")` writes flat into `dest_dir`.
  - Bulk MP3 export writes into `<dest_dir>/<Sanitized Book Title>/` with zero-padded filenames and contiguous/ordered track numbers (`1/N .. N/N` across the exported ready tracks so BooksReader's `AudioOrdering.kt` rule always holds while preserving chapter order).

### FR-3: Narrated EPUB 3 with Media Overlays (`core/epub_export.py` — BooksReader Tier T0)

- **FR-3.1 OCF / ZIP packaging & strict XML:**
  - First ZIP entry MUST be `mimetype`, stored uncompressed (`ZIP_STORED`), exact ASCII bytes `b"application/epub+zip"`.
  - `META-INF/container.xml` declares `<rootfile full-path="EPUB/package.opf" media-type="application/oebps-package+xml"/>`.
  - **NEVER** emit `<!DOCTYPE ...>` in `META-INF/container.xml`, `EPUB/package.opf`, `EPUB/nav.xhtml`, any `EPUB/c*.xhtml`, or any `EPUB/s*.smil`.
  - Store `a*.mp3` and `cover.*` with `ZIP_STORED`; store XML/XHTML/SMIL/OPF/CSS with `ZIP_DEFLATED`.
  - Atomic output write: `<dest_dir>/<Sanitized Book Title>.epub` via `.part.epub` → `os.replace` with Windows lock retry and cleanup on error/cancel.
- **FR-3.2 Hard BooksReader T0 budget enforcement:**
  - Enforce hard BooksReader caps (`MAX_PARS = 4,390`, `MAX_XML_BYTES = 781,100`) using safe targets `MAX_SAFE_PARS = 4,000` and `MAX_SAFE_XML_BYTES = 700,000`.
  - Emit ultra-compact unindented XML, short names (`c1.xhtml`, `s1.smil`, `a1.mp3`), short IDs (`p1`, `p2`, …), and compact clock values (`12.35s` / `0s`).
  - Adaptive clip coalescing across all rendered chapters before writing XHTML/SMIL:
    - If total timeline segments ≤ `MAX_SAFE_PARS` and projected XML bytes (`container.xml` + `package.opf` + all `s*.smil`) ≤ `MAX_SAFE_XML_BYTES`: emit 1 `<span id="pN">` + 1 `<par>` per valid timeline segment.
    - Otherwise: first coalesce segments within each chapter to paragraph boundaries; if still above either budget, merge consecutive clips per chapter (`ceil` stride, preserving `[first.char_start, last.char_end)` and `[first.start_ms, last.end_ms)`) and re-measure serialized XML byte totals iteratively until both `total_pars <= MAX_SAFE_PARS` and `total_xml_bytes <= MAX_SAFE_XML_BYTES` strictly hold.
- **FR-3.3 Package (`EPUB/package.opf`) & Navigation (`EPUB/nav.xhtml`):**
  - EPUB 3.0 `<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="uid" prefix="media: http://www.idpf.org/epub/vocab/overlays/#">`.
  - Metadata: `dc:identifier id="uid"` (`urn:isbn:<isbn>` if valid ISBN else deterministic `urn:uuid:<uuid5>`), `dc:title`, one `dc:creator` per author (no non-`aut` roles), `dc:language` (normalized BCP 47, default `"vi"` when unknown), optional `dc:description` and `dc:date`, `<meta property="dcterms:modified">YYYY-MM-DDTHH:MM:SSZ</meta>`, per-overlay `<meta property="media:duration" refines="#s{k}">...</meta>`, book total `<meta property="media:duration">...</meta>`, `<meta property="media:active-class">-epub-media-overlay-active</meta>`.
  - Manifest: `nav.xhtml` (`properties="nav"`), optional cover (`cover.<ext>`, `properties="cover-image"` + EPUB 2 `<meta name="cover" content="cover"/>`), minimal `style.css` (highlighting `.-epub-media-overlay-active`), every chapter `c{k}.xhtml` (`media-overlay="s{k}"` attached **only** for ready chapters), and `s{k}.smil` + `a{k}.mp3` for ready chapters.
  - Spine: linear `<itemref idref="c1"/>`, … in exact chapter order; `nav` excluded from spine.
  - `EPUB/nav.xhtml`: valid EPUB 3 `<nav epub:type="toc" id="toc"><ol><li><a href="c{k}.xhtml">{title}</a></li>...</ol></nav>`.
- **FR-3.4 Chapter XHTML, alignment invariants & embedded audio:**
  - Build `c{k}.xhtml` from `book.json` chapter text and `load_chapter_timeline(book_id, index)` (falling back to `estimate_timeline(chapter.text, duration_ms)` when `.timeline.json` is absent).
  - Emit `<h1>{escaped_title}</h1>` at top of `<body>` (not SMIL-targeted), followed by paragraphs from `split_paragraphs(chapter.text)` as `<p>...</p>` with `<br/>` for internal newlines.
  - Wrap each clip's text slice in `<span id="p{n}">...</span>` without crossing `<p>` boundaries (splitting cleanly across `<p>` elements when a segment straddles a break and targeting the first non-empty span).
  - Enforce invariants:
    - Every `<text src="c{k}.xhtml#p{n}"/>` targets a unique `id` in `<body>` containing non-whitespace text.
    - Emitted `(charOffset, audioTimeMs)` anchors are strictly monotonically non-decreasing on both axes across every chapter and across the book; degenerate/zero-length/out-of-order spans are filtered or clamped.
    - `0 <= clipBegin < audio_duration_ms` and `clipBegin <= clipEnd <= audio_duration_ms`.
  - Stream each ready chapter's WAV(s) into one `a{k}.mp3` via libsndfile (never WAV, zero `ffmpeg` required).
  - Partial render support: all chapters appear as readable XHTML in the spine; ready chapters carry Media Overlays. Refuse with an actionable error if 0 chapters are ready.

### FR-4: Single-File M4B Audiobook (`core/m4b_export.py` — BooksReader Tier T1)

- **FR-4.1 `ffmpeg` discovery & error handling:**
  - Reuse `resolve_ffmpeg(data_dir)` / `resolve_ffprobe(data_dir)` from `core/video_dub.py` and `run_cli` / `spawn_cli` from `core/processes.py` (`CREATE_NO_WINDOW`, UTF-8 decoding, `cancel_check`, child registry).
  - If `ffmpeg` is missing, raise an actionable `AudiobookError` advising the user to install `ffmpeg` (via Settings › Models or system PATH) or use EPUB 3 / MP3 export.
- **FR-4.2 Chapters & `FFMETADATA1`:**
  - Export all ready chapters in book order (refuse with actionable error if 0 ready chapters).
  - Measure exact per-chapter durations from WAV headers (`soundfile.info(path).frames / sr`, no full decode), summing multi-part oversize chapter WAVs into a single chapter marker so chapter boundaries match the source `.epub` 1:1.
  - Generate `FFMETADATA1` with book metadata (`title`, `album`, `artist` = `"; ".join(authors)`, `album_artist`, `composer` = narrator name, `date` = year if known, `comment` = description if known) and `[CHAPTER]` sections with `TIMEBASE=1/1000`, strictly monotonic `START < total_duration_ms`, `END`, and properly escaped `title` (`\`, `=`, `;`, `#`, newlines).
  - Generate an FFmpeg concat list file (`file '...'` properly escaped) referencing all ready chapter WAVs in order inside a temporary work directory.
- **FR-4.3 Cover art (`covr`) & encoding:**
  - If cover is JPEG or PNG (≤ 32 MiB): attach directly (`-i cover.<ext> -map 2:v -c:v copy -disposition:v:0 attached_pic`).
  - If cover is WebP or GIF (≤ 32 MiB): convert to PNG in the temp work directory via `ffmpeg -nostdin -y -i <cover> -frames:v 1 <out.png>`; if conversion fails, log a warning and continue cleanly without cover art.
  - Encode mono AAC-LC (`-c:a aac -b:a <m4b_bitrate> -ac 1 -ar 48000`, bitrate from `Settings.m4b_bitrate`, default `"96k"`), `-map 0:a -map_metadata 1 -map_chapters 1 -f mp4 -movflags +faststart`.
  - Stream progress via `-progress pipe:1` (`out_time_us` / `out_time_ms` as µs, capped at 0.95 until `+faststart` completes), supporting `cancel_check`.
- **FR-4.4 Validation & atomic output:**
  - Verify the `.part.m4b` container starts with a valid `ftyp` box and contains a `moov` box (plus optional `ffprobe` audio-stream check when available), then atomically promote to `<dest_dir>/<Sanitized Book Title>.m4b` via `os.replace` with Windows lock retry and cleanup on error/cancel.

### FR-5: Settings, Controller, QML UI & i18n

- **FR-5.1 M4B bitrate setting (`core/models.py`, `core/settings.py`, `ui/controller.py`, `SettingsTab.qml`, `SettingsExport.qml`):**
  - Add `m4b_bitrate: str = "96k"` to `Settings` (allowed: `"64k"`, `"96k"`, `"128k"`), with per-field clamping in `core/settings.py` so invalid values drop cleanly without resetting other settings.
  - Expose `m4bBitrate` (`str`, notify `m4bBitrateChanged`) on `AppController`.
  - Add an `AppCombo` row (`objectName: "m4bBitrateCombo"`) in `SettingsExport.qml` backed by `m4bBitrateOptions` in `SettingsTab.qml` (`64 kbps`, `96 kbps (mặc định)`, `128 kbps`), wired into `hasMatch` for settings search filtering.
- **FR-5.2 `AudiobookController` (`ui/audiobook_controller.py`):**
  - Add `coverUrl` (`file://...` via `path_to_file_url` when cover exists, else `""`) to each `books` shelf row.
  - Add `@Slot(str, str, result=bool) def exportAudiobook(self, dest_dir: str, format_mode: str) -> bool` supporting `"epub3" | "m4b" | "mp3" | "wav"`.
    - For single-file exports (`epub3`, `m4b`): refuse if a ready chapter is currently playing (`"Chương đang phát — hãy dừng rồi xuất lại."`) so no ready chapter is silently omitted; refuse if 0 ready chapters exist.
    - For bulk chapter exports (`mp3`, `wav`): preserve the existing playing-chapter skip-and-report behavior; bulk MP3 writes into `<dest_dir>/<Sanitized Book Title>/` with ID3v2.4 tags and oversize chapter merge, while WAV stays flat/unchanged.
  - Keep `exportAllReady(dest_dir)` backward-compatible (routes using `controller.exportFormat`: `"wav"` or `"mp3"`, applying the upgraded bulk MP3 behavior when `"mp3"`).
  - Add `@Slot() def cancelExport(self) -> None`: sets a cancellation event + bumps export generation so running MP3/EPUB3/WAV loops or `ffmpeg` M4B encodes abort cleanly, delete `.part` files, clear `exporting`, and emit `exportFinished(0, "")` (silent cancel, matching `cancelRender` policy). `shutdown()` cancels any active export and calls `terminate_children()` for safety.
  - Resolve narrator display name from chapter `SynthesisContext` / `renderVoice` / `progress.voice` (falling back to voice catalog display name when available) and pass it into ID3v2.4 and M4B metadata.
- **FR-5.3 `AudiobookTab.qml` & i18n:**
  - Clicking `exportAllButton` opens an `AppMenu` (`objectName: "audiobookExportMenu"`) with four `AppMenuItem` entries:
    - `exportEpub3MenuItem`: EPUB 3 Read-Aloud (`.epub`) — Khuyên dùng cho BooksReader
    - `exportM4bMenuItem`: Sách nói M4B (`.m4b`) — Một tệp có mục lục chương
    - `exportMp3MenuItem`: Các chương MP3 (`.mp3`) — Thẻ ID3v2.4 & ảnh bìa
    - `exportWavMenuItem`: Các chương WAV (`.wav`) — Âm thanh không nén
  - Picking an option records the pending format and opens `exportAllDialog` (`FolderDialog`). Accepting calls `audiobook.exportAudiobook(toLocalPath(url), pendingFormat)`. Calling `exportAllTo(url)` directly without a menu pick continues calling `audiobook.exportAllReady(toLocalPath(url))` so existing smoke tests pass unchanged.
  - Add `cancelExportButton` (`objectName: "cancelExportButton"`, danger small button) visible while `audiobook.exporting` is true, calling `audiobook.cancelExport()`.
  - Render extracted cover thumbnails inside the `40×52` cover tile of `shelfRow` (`Image` with `fillMode: Image.PreserveAspectCrop`, `asynchronous: true`, clipped to `radius`), falling back to the title initials `Label` when `coverUrl === ""` or `status !== Image.Ready`.
  - Preserve every existing `objectName` in `AudiobookTab.qml` and `SettingsExport.qml`.
  - Add Vietnamese source copy (`qsTr`/`self.tr`) and matching English translations in `src/vienetts_app/ui/i18n/vienetts_en.ts`, verified clean with `scripts/update_i18n.sh` (0 unfinished).

---

## Non-Functional Requirements

- **Zero new dependencies:** pure Python stdlib + existing `soundfile`/`numpy` (and managed/PATH `ffmpeg` only for M4B and WebP/GIF→PNG cover conversion). No `ebooklib`, `lxml`, `mutagen`, or GPL/AGPL Python packages.
- **Bounded memory:** stream WAV→MP3 and ID3+MP3 concatenation in 65,536-frame / 64 KiB chunks; measure M4B chapter durations from WAV headers only; never load multi-hour audio buffers into RAM.
- **Atomic writes & Windows safety:** every exported file and persisted cover writes to a unique `.part.<ext>` temp file and promotes via `os.replace` with bounded Windows lock retries, cleaning up temp artifacts on error or cancel. All `ffmpeg` spawns use `core/processes.py` (`CREATE_NO_WINDOW`, UTF-8 `errors="replace"`).
- **Thread safety:** all disk/encoding/`ffmpeg` export work runs off the GUI thread via `_run_bg`, marshaling progress and completion back via queued signals with generation checks.
- **Backward compatibility:** existing `<data_dir>/audiobooks/<book_id>/` workspaces, legacy `book.json`, legacy `settings.json`, `exportChapter`, `exportAllReady`, and all QML smoke `objectName` contracts continue working cleanly.
- **Quality gates:** `ruff check .`, `ruff format --check .`, `pytest`, and `scripts/update_i18n.sh` (0 unfinished) must all pass.

---

## Acceptance Criteria

1. **Metadata & cover (`tests/unit/test_epub.py`, `tests/unit/test_audiobook.py`):**
   - Multi-author `dc:creator` filters out non-`aut` roles (both `opf:role` and EPUB 3 `refines`), preserves order, deduplicates, and formats `author = "; ".join(authors)`.
   - `language`, `isbn` (ISBN-10/13 from `opf:scheme`, `urn:isbn:`, or EPUB 3 `identifier-type`), `year` (preferring publication date), `description`, and EPUB 3 `cover-image` / EPUB 2 `meta name="cover"` with magic-byte sniffing (JPEG/PNG/GIF/WebP, ≤ 32 MiB cap, remote/corrupt ignored) all parse accurately.
   - Legacy `book.json` without the new fields loads cleanly and recovers metadata/cover on-the-fly from `source_path` at export time if available.
2. **ID3v2.4 & MP3 chapters (`tests/unit/test_id3.py`, `tests/unit/test_audiobook.py`):**
   - Binary ID3v2.4 verification: `ID3\x04\x00\x00`, valid synchsafe header and frame sizes, UTF-8 Vietnamese diacritics preserved verbatim across `TALB`, `TIT2`, `TPE1`, `TPE2`, `TRCK` (`i/N` on every file), `TPOS`, `TXXX:narrator`, `TCOM`, and `APIC` (type 3 Front Cover).
   - Oversize multi-part chapter streams into 1 MP3 per chapter with duration matching the sum of its parts; WAV oversize export still produces split `NN - Title - 01.wav`, `02.wav`.
   - Bulk MP3 export lands in `<dest_dir>/<Sanitized Book Title>/` with zero-padded filenames; single-chapter `exportChapter(..., "mp3")` writes flat in `dest_dir`.
3. **Narrated EPUB 3 (`tests/unit/test_audiobook_epub_mo.py`):**
   - ZIP entry 0 is uncompressed `mimetype == b"application/epub+zip"`.
   - Zero `<!DOCTYPE` occurrences in `container.xml`, `package.opf`, `nav.xhtml`, `c*.xhtml`, or `s*.smil`.
   - Namespace-aware XML parse passes for all XML/XHTML/SMIL files; every `<text src="...#id"/>` resolves to a real non-whitespace element inside the linear spine document's `<body>`; every `<audio src="..."/>` exists in the ZIP as `ZIP_STORED` valid MP3 (never WAV) with `0 <= clipBegin < duration_ms` and `clipBegin <= clipEnd <= duration_ms`.
   - Global `(charOffset, audioTimeMs)` anchors are strictly monotonically non-decreasing across multi-chapter and multi-part oversize books.
   - **Budget stress test:** synthetic book with 10,000+ timeline segments coalesces cleanly to `<= 4,390` `<par>` elements AND `<= 781,100` cumulative uncompressed bytes across `container.xml` + `package.opf` + all `.smil`.
   - Partial book (e.g., 2 of 5 chapters ready) exports all 5 readable spine chapters with valid Media Overlays on the 2 ready chapters; 0 ready chapters refuses with an actionable error.
4. **M4B Audiobook (`tests/unit/test_audiobook_m4b.py`):**
   - `FFMETADATA1` escaping (`=`, `;`, `#`, `\`, newlines, Vietnamese titles) and sample-accurate cumulative chapter timestamps across standard and multi-part chapters are verified deterministically.
   - Stub-ffmpeg tests verify command args (`-b:a`, `-movflags +faststart`, `-map_chapters 1`, `attached_pic`), WebP/GIF→PNG conversion + graceful fallback on conversion failure, missing-ffmpeg error, progress relay, and cancellation killing the stub process with no `.part.m4b` left behind.
   - Real-`ffmpeg` integration test (skipped gracefully if `ffmpeg` is unavailable) verifies `.m4b` starts with `ftyp`, contains `moov`, AAC `soun` track, chapter markers, `ilst` tags, and embedded cover.
5. **Controller, Settings, UI & i18n (`tests/unit/test_audiobook_controller.py`, `tests/unit/test_settings.py`, `tests/smoke/test_ui_tabs.py`):**
   - `exportAudiobook(dest_dir, mode)` works for all 4 modes, `exportAllReady(dest_dir)` remains backward-compatible, `cancelExport()` cancels in-flight exports cleanly, and `m4bBitrate` persists + clamps safely.
   - `AudiobookTab.qml` exposes `audiobookExportMenu`, format items, `cancelExportButton`, and cover thumbnails with initials fallback while passing all existing smoke contracts; `SettingsExport.qml` exposes `m4bBitrateCombo`.
   - `ruff check .`, `ruff format --check .`, `pytest`, and `scripts/update_i18n.sh` (0 unfinished) are all green.

---

## Out of Scope

- Bundling `ffmpeg` inside the frozen application (uses existing managed runtime / PATH / well-known dirs).
- Editing book metadata or uploading custom cover art in the UI.
- Word-level forced alignment (e.g., Whisper/MFA re-alignment of synthesized audiobook chapters) — Media Overlays use VieNeuTTSApp's measured render segment timelines (`ch_XXXX.timeline.json`) and `estimate_timeline` fallback.
- Exporting DRM-protected EPUBs or non-EPUB documents as narrated EPUB 3 / M4B.
- Adding third-party tagging or EPUB Python dependencies (`mutagen`, `ebooklib`, `lxml`, `Pillow`).
