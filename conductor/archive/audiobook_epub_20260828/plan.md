# Implementation Plan: audiobook_epub_20260828

TDD per `conductor/workflow.md`: failing tests first, minimal implementation,
`ruff check` + `ruff format --check` + `pytest` gate, conventional commit +
git note per task.

## Phase A: EPUB parser (core, stdlib-only)

- [x] Task A1: `src/vienetts_app/core/epub.py` — pure EPUB reader
  - `EpubChapter` (index, title, text) and `EpubBook` (title, author,
    chapters, source_path, content_hash) dataclasses with validation.
  - `import_epub(path) -> EpubBook`: zipfile + container.xml → OPF →
    manifest/spine; per-doc XHTML → text via ElementTree (block-aware
    newline joins, h1–h6 → chapter title), `html.parser` fallback for
    malformed docs; skip EPUB3 `nav` docs and textless docs; DRM
    (`META-INF/encryption.xml`) → actionable error; whitespace normalization.
  - Files: `src/vienetts_app/core/epub.py`, `tests/unit/test_epub.py`,
    `tests/fixtures/make_epub.py` (fixture builder, lint-clean per patterns).
  - Tests: multi-chapter parse w/ titles+texts, nav/cover exclusion,
    DRM error, malformed-XHTML fallback, missing container/OPF errors,
    whitespace normalization, unicode/Vietnamese diacritics safety.

## Phase B: Audiobook library & persistence (core)

- [x] Task B1: `src/vienetts_app/core/audiobook.py` — disk workspace
  - `AudiobookLibrary(root_dir)`: `add_book(epub:EpubBook) -> BookRecord`
    (content-hash id, workspace mkdir, `book.json` write, `library.json`
    index update), `list_books()`, `load_book(book_id)`, `remove_book()`,
    `chapter_wav_path(book_id, index)`, `save_chapter_audio(book_id, index,
    audio)` (atomic write: temp + rename), `chapter_status` map
    (`pending/ready` + error), progress persistence (`current_chapter`,
    `position_ms`, `voice`), `export_chapter(book_id, index, dest_dir)`.
  - Corrupt `book.json`/`library.json` → degrade to empty + warning (never
    crash; same posture as voices.json).
  - Files: `src/vienetts_app/core/audiobook.py`,
    `tests/unit/test_audiobook.py`.
  - Tests: add/list/load round-trip, same-content dedupe id, chapter WAV
    write+read, status transitions, progress save/restore, export filename
    sanitization + valid WAV, remove cleanup, corrupt-index degradation.

## Phase C: Job-routing seam + AudiobookController

- [x] Task C1: `AppController` job-listener seam (minimal, non-breaking)
  - Attachable `synthesis_listener` (duck-typed: `on_synthesis_progress`,
    `on_synthesis_done(audio)`, `on_synthesis_error(msg)`); while attached,
    `_on_progress`/`_on_done`/`_on_error` delegate instead of touching app
    tab state. Cancel path (`CANCELLED_MESSAGE`) routes to the listener too.
    `shutdown()` detaches. Existing behavior byte-identical when detached —
    all current controller tests must stay green untouched.
  - Files: `src/vienetts_app/ui/controller.py` (additive),
    `tests/unit/test_controller.py` (new tests only).
- [x] Task C2: `src/vienetts_app/ui/audiobook_controller.py`
  - `AudiobookController(QObject)` (context property `audiobook`):
    `books` (shelf model), `chapters` (per-book model w/ status+current),
    `currentBookId/currentBookTitle+Author`, `currentChapterIndex`,
    `playerState/positionMs/chapterDurationMs`, `renderingBookBusy`,
    `autoAdvance` (default true), `errorText`.
  - Slots: `openEpub(path)`, `selectBook(id)`, `removeBook(id)`,
    `playChapter(index)` (ready → file play; pending → render-then-play),
    `pause()`, `resume()`, `stopPlay()`, `prevChapter()`, `nextChapter()`,
    `renderChapter(index)`, `renderAllPending()`, `cancelRender()`,
    `exportChapter(index, dir)`, `exportAllReady(dir)`, `shutdown()`.
  - Uses its own `PlaybackController` instance (injectable factory) for
    chapter files; `finished()` → advance; pipelined pre-render of the next
    pending chapter while playing; progress persisted on change; renders
    submitted via the AppController listener seam with
    `controller.generateStream`-equivalent submission (stream-mode requests).
  - Files: `src/vienetts_app/ui/audiobook_controller.py`,
    `tests/unit/test_audiobook_controller.py`.
  - Tests (fake worker + fake player via seams): render→ready→cached-WAV,
    no-resynthesis on replay, play pending chapter renders first,
    finished→auto-advance (ready & pipelined), pause/resume/prev/next,
    resume persistence across rebuild, cancel mid-render, error surfacing,
    coexistence (listener detach restores normal controller behavior).

## Phase D: QML tab + wiring

- [x] Task D1: `AudiobookTab.qml` + nav registration + app wiring
  - `bridge.TABS` += `("audiobook", "Sách nói")`; `Main.qml` StackLayout adds
    the tab; `app.create_app` builds + registers `audiobook` context
    property (anchored on the engine); `AudiobookTab.qml` per spec FR-A7:
    shelf column (add button, FileDialog `.epub`, drag-drop), book header,
    chapter `ListView` with status badges + per-chapter render/export,
    player bar (prev/play-pause/next, chapter title, position/duration,
    seek Slider), voice picker, render-all, error banner — Signal design
    system components (`PageShell`, `AppCard`, `AppButton`, `StatusBadge`,
    `VoicePicker`, `AppIcon`).
  - Files: `src/vienetts_app/ui/qml/AudiobookTab.qml`,
    `src/vienetts_app/ui/qml/Main.qml`, `src/vienetts_app/ui/bridge.py`,
    `src/vienetts_app/app.py`, `tests/smoke/test_ui_tabs.py` (additions).
  - Tests: tab contract objectNames render offscreen, nav label present,
    empty-shelf state, wiring smoke (controller properties reachable).

## Phase E: E2E fake flow, docs, close-out

- [x] Task E1: end-to-end fake flow — build EPUB fixture on disk →
  `openEpub` → render chapter (FakeVieneu worker) → WAV cached → play
  (fake player) → finished → advance → shutdown; asserts persistence.
  Files: `tests/smoke/test_e2e_flows.py` (additions).
- [x] Task E2: docs sync — `conductor/product.md` (feature list),
  `conductor/tech-stack.md` (EPUB stdlib note), `PROJECT_PLAN.md` status
  note, track `learnings.md` capture, elevate patterns, `conductor/tracks.md`
  status, beads close.

## Execution

Sequential (single implementer session; phases depend on each other:
B needs A's types, C needs B, D needs C, E needs all).
