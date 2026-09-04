# Paragraph Tab Multi-File Batch Synthesis

**Date:** 2026-09-04
**Beads:** `VieNeuTTSApp-qef`
**Status:** Approved design (chat), awaiting spec review

## Goal

Let a user turn a folder's worth of documents into speech in one action on the
Paragraph tab: select or drop multiple supported files, press one button, and
every file is synthesized in sequence with each finished WAV auto-saved to the
output folder named after its source file. The existing single-file/editor flow
stays exactly as it is.

## Problems to solve

- The Paragraph tab's import replaces the editor with one file
  (`FileDialog.OpenFile`, drag-and-drop takes `drop.urls[0]`), so batch work
  means one file at a time, repeated by hand.
- Each synthesis produces one ephemeral artifact; exporting N files means N
  manual "Xuất WAV" clicks.
- The Audiobook tab already proves the pattern (sequential auto-render with
  per-item status and cached WAVs), but it is EPUB-library-centric and does
  not accept loose documents.

## Non-goals

- No parallel synthesis: the worker is a single-owner serial queue; batch items
  render strictly one at a time.
- No persistent library for batch files (no re-open of a past batch, no
  progress resume across app restarts). The queue lives for the session.
- No per-file voice overrides: one voice (the tab picker's current selection)
  applies to the whole run.
- No merging of outputs: each source file maps to exactly one WAV.
- No changes to the audiobook tab or its controller.

## Architecture

### `BatchFileController` (new)

`src/vienetts_app/ui/batch_controller.py`, constructed in `create_app` with an
injectable `batch_factory` (same pattern as `audiobook_factory`) and exposed to
QML as context property `batchController`. It references the shared
`AppController` and owns:

```python
@dataclass
class BatchItem:
    source_path: Path        # original document on disk
    text: str                # imported text ("" until import completes)
    status: str              # importing | pending | rendering | saving | ready | failed
    error: str               # "" or the item's failure reason
    wav_path: str            # exported WAV path ("" until saved)
    job_id: str | None       # active synthesis job (rendering items only)
```

All synthesis flows through the **existing**
`AppController.submit_stream_for_listener(text, voice, self, kind="bulk")`
seam (`JobKind` already has `"bulk"`; no `core/jobs.py` change) and cancels via
`AppController.cancel_job(job_id)`. `BatchFileController` implements the
duck-typed listener contract (`on_synthesis_progress` /
`on_synthesis_chunk` / `on_synthesis_terminal`). Zero new seams on
`AppController`.

Document parsing reuses `core.importers.import_document(path,
keep_srt_raw=app.srtKeepTimestamps)` run through the controller's injectable
background runner (`run_on_thread_pool` in production, inline in tests) — the
same off-the-GUI-thread posture as `AppController.importDocument`.

Per-item export copies the finished artifact WAV to the same destination logic
as `AppController._default_export_path` (settings `output_dir`, else
`Music/VieNeuTTS`), but named `<source stem>.wav` with collision-safe `_2`,
`_3`… suffixes when the target exists. After a successful copy the interactive
artifact file is deleted (the exported copy is the keeper), mirroring the
audiobook's `_release_managed_artifact` posture.

### QML surface (`batchController`)

Properties (NOTIFY-backed): `items` (QVariantList of
`{uid, sourcePath, fileName, status, error, wavPath, progress}`),
`running` (a run is in flight), `currentFileName`, `currentIndex`,
`progress` (current item's 0..1), `runAllDone` / `runAllTotal` (whole-run
"x/y"), `errorText`, `renderVoice` (get/set), `hasPending`, `playingIndex`
(which row is previewing). QML derives row counts from `items.length`.

Slots:

- `addFiles(paths)` — validate extensions against
  `SUPPORTED_EXTENSIONS` up front (unsupported → `errorText`, nothing queued),
  then import each off-thread; items appear immediately as `importing` and
  flip to `pending` (or `failed` with the parse error) as texts land.
- `removeItem(index)` — allowed while the item is not `rendering`/`saving`;
  also allowed mid-run for `pending` items.
- `clearFinished()` — drop all `ready`/`failed` items (never mid-run state).
- `runAll()` — start (or continue) the sequential run over every `pending`
  item; no-op while already running or when the app is shutting down.
- `cancel()` — cancel the in-flight job (that item returns to `pending`) and
  halt auto-advance; `ready` items and their WAVs are untouched.
- `playItem(index)` / `stopPlay()` — per-row preview through the controller's
  own `PlaybackController` (same injectable factory pattern as the audiobook
  controller; lazy, never constructed at startup).
- `showInFolder(index)` — `QDesktopServices` reveal of a `ready` item's WAV
  (same error posture as `openModelDir`).

Voice: read at each item's submission from the `renderVoice` property
(mirroring `AudiobookController.renderVoice`); the Paragraph tab keeps it in
sync with the tab's existing `voicePicker`.

### Render loop

`runAll()` sets `running` and calls `_kick()`. Run totals are **recomputed on
every state change** (the audiobook's re-count posture — a mid-run change must
not strand the in-flight item outside the counts): `runAllDone` counts this
run's `ready` + `failed` items, `runAllTotal` = `runAllDone` + the current
`rendering`/`saving` item + remaining `pending` items.

1. If not `running`, or an item is `rendering`/`saving`, return.
2. Find the first `pending` item; if none, the run is done (`running=False`).
   Files added mid-run land as `pending` and are picked up by the next `_kick()`.
3. Reject oversize texts (`> GENERATE_CHAR_LIMIT`, same limit and Vietnamese
   message shape as the editor flow) → item `failed`, continue at step 2.
4. Submit via `submit_stream_for_listener`; a `None` return marks the item
   `failed` ("could not submit") and continues at step 2.
5. Item flips to `rendering`; `on_synthesis_progress` updates its `progress`;
   `on_synthesis_terminal(completed)` flips it to `saving`, copies the WAV
   off-thread, then `ready` + `runAllDone += 1` + `_kick()`; a
   failed/cancelled terminal marks the item `failed`/returns it to `pending`
   (cancel) and `_kick()` continues the run — except a user cancel, which
   stops the run without advancing.

The loop does **not** check `app.busy`: listener jobs legitimately queue
behind a foreground synthesis (the worker serializes; `busy` belongs to the
interactive tab actions only).

## UI (`ParagraphTab.qml` + new `BatchQueueCard.qml`)

New component `src/vienetts_app/ui/qml/components/BatchQueueCard.qml` (the
tab file stays focused; it is already ~590 lines), placed between the editor
card and the voice/actions card. The card is always mounted (its "Thêm tệp…"
button is the click entry point when the queue is empty); the file list and
run footer show only when items exist, with a one-line empty-state hint
otherwise:

- **Header row:** title "Hàng đợi tệp", item count, "Thêm tệp…" (opens a
  second dialog: `FileDialog.OpenFiles`, objectName `batchImportDialog`,
  same name filters as the single-file dialog), "Xóa hết" (→ `clearFinished`).
- **Item rows:** file name; status badge (`Chờ`, `Đang tạo`, `Đang lưu`,
  `Sẵn sàng`, `Lỗi` — StatusBadge statuses already in the design system);
  inline error text for failed rows; a thin indeterminate-free progress bar on
  the `rendering` row; per-row actions — remove (quiet, when allowed),
  play/stop (when `ready`), reveal-in-folder (when `ready`).
- **Footer:** "Tạo tất cả" primary (enabled when a `pending` item exists and
  not `running`), "Hủy" danger (visible while `running`), overall progress
  "x/y" plus the rendering item's percentage. Keyboard: `Ctrl+Return` keeps
  editor semantics; batch run is mouse/button only.

Selection routing:

- One file picked from the existing single-file dialog or dropped alone →
  editor, exactly as today (`importDialog`, `importPath` untouched — their
  objectNames are the tested smoke contract).
- "Thêm tệp…" multi-select dialog or a multi-URL drop (2+ files) →
  `batchController.addFiles(...)`.

Drag-and-drop on the editor area changes only its `onDropped` branch:
`drop.urls.length === 1` → today's path; otherwise map every URL through the
existing `toLocalPath` helper into `addFiles`.

## Error handling

- Unsupported extension, blank import result: surfaced via
  `batchController.errorText` in the queue card's notice row; never a modal.
- Parse failure: item `failed` with `error` shown inline; run continues.
- Render failure (worker error): item `failed`; run continues.
- Cancel: in-flight item returns to `pending`; run halts silently (no error
  banner — same silent-reset contract as the app-wide cancel).
- Export copy failure: item `failed` with the OS error text; the artifact
  file is kept on disk for manual recovery; run continues.
- Two sources with the same stem: second export gets `_2` suffix; never
  overwrites.

## Testing

- **Unit** (`tests/unit/test_batch_controller.py`, fake app + inline bg
  runner + fake playback, the `test_audiobook_controller.py` pattern):
  addFiles happy path (statuses `importing`→`pending`), unsupported-extension
  rejection, parse failure → `failed` item, sequential auto-advance order,
  per-item progress mapping, export path/naming/collision suffixes, oversize
  rejection message, failure-then-continue, cancel (pending restore + halt),
  SRT flag pass-through to `import_document`, playItem/stopPlay state,
  showInFolder failure posture, clearFinished/removeItem guards.
- **Smoke** (`tests/smoke/test_ui_tabs.py`): queue card invisible when empty;
  visible with items after `addFiles`; `batchImportDialog` exists with
  OpenFiles mode; "Tạo tất cả"/"Hủy" enablement contract; per-row status
  badges and actions; multi-URL drop routes to `batchController.addFiles`
  while single-URL drop still fills the editor.
- **i18n:** every new user-facing string via `qsTr`; `.ts` regen
  (`pyside6-lupdate`, contexts = QML filenames — known gotchas: no
  function-wrapped `qsTr` without a NOTIFY read, `QT_TRANSLATE_NOOP` needs a
  class context) and English translations filled so `test_i18n.py` stays
  green.

## Files touched

| File | Change |
| --- | --- |
| `src/vienetts_app/ui/batch_controller.py` | New controller + `BatchItem`. |
| `src/vienetts_app/app.py` | Construct + expose `batchController` (injectable `batch_factory`). |
| `src/vienetts_app/ui/qml/components/BatchQueueCard.qml` | New queue card component. |
| `src/vienetts_app/ui/qml/ParagraphTab.qml` | Mount card, multi-URL drop branch, `renderVoice` sync, batch dialog. |
| `translations/*` (`.ts`) | New strings + English translations. |
| `tests/unit/test_batch_controller.py`, `tests/smoke/test_ui_tabs.py`, i18n tests | As above. |

No changes to `AppController` seams, `core/jobs.py`, the worker, or the
audiobook subsystem.
