# Track: Audiobook Support — EPUB First

**Track ID:** `audiobook_epub_20260828`
**Type:** Feature · **Priority:** High (user-requested) · **Status:** new

User brief: *"research, make plan and then implement feature support audio book,
first target with epub. Priority performance, UX, useful"*

## Overview

Turn the app from a single-shot synthesizer into an audiobook player/studio:
import an EPUB, browse its chapters, render chapters to audio with per-chapter
disk caching, listen with continuous playback (auto-advance, pause/resume,
seek), resume across sessions, and export chapter WAVs. Priorities in order:
**performance** (never re-synthesize what is cached; pipeline the next chapter
while listening; bounded memory), **UX** (real player controls, chapter list
with live status, resume-where-you-left-off), **useful** (export named chapter
files; works fully offline like the rest of the app).

## Research Summary (drives the design)

- **EPUB is a ZIP**: `META-INF/container.xml` → OPF file; OPF carries
  `metadata` (dc:title/dc:creator), `manifest` (items), `spine` (reading
  order). Spine items are XHTML documents. A stdlib reader
  (`zipfile` + `xml.etree.ElementTree`, `html.parser` fallback for malformed
  XHTML) is ~250 lines and avoids a new dependency (project precedent: pypdf
  over PyMuPDF at Phase 0 for license/weight reasons; `ebooklib` would drag
  `lxml` in).
- **Single engine/worker constraint**: exactly one `TTSEngine` + one
  `InferenceWorker` own synthesis (plan §4; two workers = two model loads ≈
  2×~800 MB RSS). Audiobook jobs must therefore share the existing worker.
- **Memory-safe path**: worker `_process_stream` splits text into ≤512-char
  segments (`split_text_for_streaming`) — the arena-safe dispatch (bead u5c).
  Audiobook chapter rendering reuses `mode="stream"`.
- **Playback gap**: the QAudioSink streaming path has no end-of-audio signal,
  no pause/seek. `PlaybackController` (QMediaPlayer) HAS `finished()`,
  pause/resume and position — the right primitive for audiobook listening.
  Therefore: **render-then-play** — synthesize chapter → cache WAV → play
  from file. Synthesis still starts immediately on "play" (first listen), the
  player starts the moment the WAV lands.
- **Signal routing**: `AppController` currently owns all worker signal
  handlers. A second controller reacting to the same signals would double-
  handle jobs. Design: a minimal **job-listener seam** on `AppController` —
  when a listener is attached, worker callbacks delegate to it (see FR-A8).

## Functional Requirements

- **FR-A1 EPUB import**: `.epub` files are parsed into a book: metadata
  (title, author) + ordered chapters (index, title, text). Spine order is
  the chapter order. EPUB3 `nav` documents and textless pages (covers) are
  excluded from the chapter list. DRM-encrypted files fail with an
  actionable error. Malformed XHTML degrades via an HTML fallback extractor,
  never a crash. Chapter titles: first `h1–h6` in the document, else
  "Chương N".
- **FR-A2 Library persistence**: imported books live under
  `<data_dir>/audiobooks/<book_id>/` with `book.json` (metadata + chapter
  index + content hash) and per-chapter `ch_NNNN.wav` cache. `book_id` is
  derived from the file's content hash → re-importing the same file resumes
  the same book. A `library.json` index lists known books for the shelf UI.
- **FR-A3 Chapter rendering**: rendering a chapter submits a stream-mode
  TTSRequest for the chapter text through the shared worker; on `done` the
  audio is written to the chapter WAV cache and the chapter becomes `ready`.
  Statuses: `pending` → `rendering` → `ready` / `failed`(+error text).
  Cancel stops the in-flight render. Oversized chapters (text >
  `CHAPTER_CHAR_LIMIT`) are refused with an actionable message (never
  truncated silently — same policy as `IMPORT_CHAR_LIMIT`).
- **FR-A4 Listening**: playing a `ready` chapter uses the file player
  (pause/resume/stop/seek); playing a pending chapter renders it first (live
  status), then plays. When a chapter finishes, playback auto-advances to the
  next chapter (waiting for its render if still in flight). While a chapter
  is playing, the next unrendered chapter is auto-submitted for render
  (pipelined pre-render).
- **FR-A5 Resume**: current book, current chapter, and player position (for
  ready chapters) are persisted as they change; reopening the app restores
  the shelf and the last position.
- **FR-A6 Export**: a chapter (or all ready chapters) can be exported to a
  user-chosen folder as `NN - <ChapterTitle>.wav` (ordered, sanitized
  filenames).
- **FR-A7 UI**: a new "Sách nói" tab in the nav rail (Signal design system,
  Vietnamese labels): bookshelf + add-EPUB (file dialog & drag-drop), chapter
  list with per-chapter status and progress, player bar
  (play/pause/prev/next + chapter title + position), voice picker, render /
  render-all / export actions, error surface. objectNames are the tested
  contract like every other tab.
- **FR-A8 Coexistence**: audiobook jobs and regular synthesis jobs serialize
  through the ONE worker. A job-routing seam on `AppController` (attachable
  listener) lets `AudiobookController` own worker results while attached;
  regular tabs behave exactly as before when no listener is attached.

## Non-Functional Requirements

- **NFR-A1 Performance**: cached chapters never re-synthesize (WAV reuse);
  next-chapter pre-render overlaps playback; renders use segmented streaming
  so RSS stays on the ~1.1 GB plateau (no `infer()` whole-doc path);
  book/chapter models are loaded lazily and never block the UI thread
  (EPUB parse + hash run on import call, expected < 200 ms for typical
  books).
- **NFR-A2 Zero new dependencies**: EPUB parsing is stdlib-only.
- **NFR-A3 Startup stays model-free**: constructing the audiobook controller
  loads at most the library index JSON — no engine, no QtMultimedia, no EPUB
  parse.
- **NFR-A4 Robustness**: every failure mode (corrupt zip, missing
  container.xml, empty book, unreadable chapter, disk-full on cache write)
  surfaces as actionable `errorText`-style messages; never a crash.

## Acceptance Criteria

- **AC-A1 (parser)**: a programmatically-built EPUB (multi-chapter, cover
  image page, epub3 nav doc, dc metadata) imports to the exact chapter list
  (titles + texts); DRM/`encryption.xml` file → actionable error; malformed
  XHTML chapter → fallback text still extracted.
- **AC-A2 (library)**: import → `book.json` + shelf entry exist; re-import of
  the same content reuses the same `book_id` (no duplicate shelf entry);
  delete removes the workspace.
- **AC-A3 (render)**: fake-worker `done` → WAV cached, status `ready`,
  duration > 0; second play of the chapter issues NO new synthesis job;
  cancel mid-render → status back to `pending`, no partial WAV.
- **AC-A4 (listen)**: chapter `finished()` → next chapter starts (ready
  case) or starts as soon as its render lands (pipelined case); pause/resume
  and prev/next work; last book/chapter/position persisted and restored
  after controller rebuild.
- **AC-A5 (export)**: exported file `NN - Title.wav` exists and is a valid
  WAV (readable via `read_wav`).
- **AC-A6 (gates)**: `ruff check`, `ruff format --check`, `pytest` all green;
  new suites: `tests/unit/test_epub.py`, `tests/unit/test_audiobook.py`,
  `tests/unit/test_audiobook_controller.py`, additions to
  `tests/smoke/test_ui_tabs.py` (audiobook tab contract).

## Out of Scope (v1)

- Other book formats (MOBI/AZW3, PDF-as-book) — the architecture
  (book → chapters) is format-agnostic; only the EPUB reader ships.
- Compressed export formats (MP3/OGG/opus); WAV only (matches the app).
- Position sync inside streamed (sink) playback — v1 always plays chapters
  from cached WAV files, so position/seek are exact.
- Voice mixing per chapter (one voice per render job is supported by using
  the picker's current selection at submit time).
- Background/parallel multi-book rendering (single render queue by design).
