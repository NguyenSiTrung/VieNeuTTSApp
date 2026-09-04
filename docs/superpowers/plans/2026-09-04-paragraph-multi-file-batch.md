# Paragraph Tab Multi-File Batch Synthesis — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the Paragraph tab accept multiple documents, synthesize them sequentially in one auto-run, and auto-save each finished WAV named after its source file.

**Architecture:** A new `BatchFileController` (QML context property `batchController`) owns a list of `BatchItem`s and drives them through the *existing* `AppController.submit_stream_for_listener` seam (`kind="bulk"`), exactly the listener pattern `AudiobookController` uses. A new `BatchQueueCard.qml` mounts on `ParagraphTab` beside the untouched single-file editor flow. Zero changes to `AppController`, `core/jobs.py`, the worker, or the audiobook subsystem.

**Tech Stack:** PySide6 6.x (QObject/Property/Slot), QML (QtQuick.Controls + QtQuick.Dialogs), pytest with the repo's `qcoreapp` fixture.

**Spec:** `docs/superpowers/specs/2026-09-04-paragraph-multi-file-batch-design.md` (approved 2026-09-04)

## Global Constraints

- Run tests with `.venv/bin/python -m pytest` (bare `python` is not on PATH).
- **No commits/pushes without the user's go-ahead** — repo `AGENTS.md` uses the conservative Beads profile. Each task below ends with a *suggested* commit; batch them for one approval if the user prefers.
- Task tracking in Beads under `VieNeuTTSApp-qef`; update the bead (`bd update VieNeuTTSApp-qef --status in_progress`) when starting.
- All user-facing strings are Vietnamese source via `qsTr`/`self.tr`, pinned verbatim in each task — do not reword.
- Controller constructor must never touch the audio stack at import/construction (lazy `PlaybackController` only, NFR-2.1 posture) and must connect to **no** `AppController` signals (so a bare fake app object is a valid dependency).
- App seam reads on `self._app` must go through `getattr(..., default)` so fakes without the full surface never crash bindings.
- Per-request length cap is `GENERATE_CHAR_LIMIT` imported from `vienetts_app.ui.controller` (== `CHAPTER_CHAR_LIMIT`, the worker-RAM OOM guard).

---

### Task 1: `BatchFileController` skeleton — items, addFiles, removeItem, clearFinished

**Files:**
- Create: `src/vienetts_app/ui/batch_controller.py`
- Test: `tests/unit/test_batch_controller.py`

**Interfaces:**
- Consumes: `vienetts_app.core.importers.import_document(path: str, *, keep_srt_raw: bool) -> str`, `SUPPORTED_EXTENSIONS`; `vienetts_app.ui.bg_ops.run_on_thread_pool(work, on_done, parent)` (tests use `run_sync`).
- Produces: `BatchFileController(app_controller, *, data_dir=None, player_factory=None, importer=None, bg_runner=None, reveal_fn=None)`; properties `items` (QVariantList of dicts `{uid, sourcePath, fileName, status, error, wavPath, progress}`), `errorText` (str); slots `addFiles(list)`, `removeItem(int)`, `clearFinished()`; status constants `importing|pending|rendering|saving|ready|failed`.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_batch_controller.py`:

```python
"""BatchFileController (bead VieNeuTTSApp-qef): Paragraph-tab multi-file queue.

Fakes sit at the app seam (submit_stream_for_listener/cancel_job/settings
reads) and at the importer — the same posture as the audiobook suite, but
listener events are driven directly (the contract AppController calls).
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QObject, Property, Signal

from vienetts_app.ui.batch_controller import BatchFileController
from vienetts_app.ui.bg_ops import run_sync


class FakeApp(QObject):
    """Narrow app seam: only what BatchFileController touches."""

    def __init__(self, output_dir: str = "") -> None:
        super().__init__()
        self.defaultVoice = "Minh Đức"
        self.outputDir = output_dir
        self._srt_keep = False
        self.submissions: list[dict] = []
        self.cancelled: list[str] = []
        self._next = 0

    @property
    def srtKeepTimestamps(self) -> bool:
        return self._srt_keep

    def submit_stream_for_listener(self, text, voice, listener, *, kind="requested_chapter"):
        self._next += 1
        job_id = f"job-{self._next}"
        self.submissions.append(
            {"job_id": job_id, "text": text, "voice": voice, "listener": listener, "kind": kind}
        )
        return job_id

    def cancel_job(self, job_id: str) -> bool:
        self.cancelled.append(job_id)
        return True


class FakePlayer:
    """Duck-typed PlaybackController: exactly what the batch controller calls."""

    stateChanged = Signal()

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self._released = None

    def play(self, path, on_released=None):
        self.calls.append(("play", path))
        self._released = on_released

    def stop(self):
        self.calls.append(("stop",))

    def finish(self) -> None:
        if self._released is not None:
            cb, self._released = self._released, None
            cb()


class Harness:
    def __init__(self, tmp_path: Path, *, texts: dict[str, str] | None = None) -> None:
        self.tmp = tmp_path
        self.app = FakeApp(output_dir=str(tmp_path / "out"))
        self.player = FakePlayer()
        texts = {} if texts is None else texts
        self.imports: list[dict] = []

        def importer(path: str, *, keep_srt_raw: bool = False) -> str:
            self.imports.append({"path": path, "keep_srt_raw": keep_srt_raw})
            if path in texts:
                return texts[path]
            raise RuntimeError(f"boom: {path}")

        self.bc = BatchFileController(
            self.app,
            data_dir=tmp_path,
            player_factory=lambda: self.player,
            importer=importer,
            bg_runner=run_sync,
        )


@pytest.fixture()
def harness(qcoreapp, tmp_path: Path) -> Harness:
    return Harness(tmp_path)


def txt(tmp_path: Path, name: str, content: str = "Xin chào") -> Path:
    p = tmp_path / name
    p.write_text(content, encoding="utf-8")
    return p


class TestAddFiles:
    def test_add_pending_items_in_order(self, harness: Harness, tmp_path: Path) -> None:
        a = txt(tmp_path, "a.txt", "nội dung a")
        b = txt(tmp_path, "b.md", "nội dung b")
        harness.bc.addFiles([str(a), str(b)])
        items = harness.bc.items
        assert [i["fileName"] for i in items] == ["a.txt", "b.md"]
        assert all(i["status"] == "pending" for i in items)
        assert all(i["error"] == "" for i in items)

    def test_unsupported_extension_rejected_with_error(self, harness, tmp_path):
        bad = tmp_path / "photo.png"
        bad.write_bytes(b"\x89PNG")
        harness.bc.addFiles([str(bad)])
        assert harness.bc.items == []
        assert "photo.png" in harness.bc.errorText or ".png" in harness.bc.errorText

    def test_parse_failure_marks_item_failed(self, harness, tmp_path):
        missing = tmp_path / "missing.pdf"
        harness.bc.addFiles([str(missing)])
        (item,) = harness.bc.items
        assert item["status"] == "failed"
        assert item["error"] != ""

    def test_srt_flag_passed_to_importer(self, harness, tmp_path):
        srt = txt(tmp_path, "s.srt", "1\n00:00:01,000 --> 00:00:02,000\nChào")
        harness.app._srt_keep = True
        harness.bc.addFiles([str(srt)])
        assert harness.imports[0]["keep_srt_raw"] is True

    def test_empty_text_import_fails_item(self, harness, tmp_path):
        blank = txt(tmp_path, "empty.txt", "   ")
        harness.bc.addFiles([str(blank)])
        (item,) = harness.bc.items
        assert item["status"] == "failed"


class TestItemManagement:
    def test_remove_pending_item(self, harness, tmp_path):
        a = txt(tmp_path, "a.txt")
        b = txt(tmp_path, "b.txt")
        harness.bc.addFiles([str(a), str(b)])
        harness.bc.removeItem(0)
        assert [i["fileName"] for i in harness.bc.items] == ["b.txt"]

    def test_remove_out_of_range_is_noop(self, harness, tmp_path):
        harness.bc.removeItem(5)
        assert harness.bc.items == []

    def test_clear_finished_keeps_unfinished(self, harness, tmp_path):
        a = txt(tmp_path, "a.txt")
        b = txt(tmp_path, "broken.pdf")
        harness.bc.addFiles([str(a), str(b)])
        harness.bc.clearFinished()
        assert [i["fileName"] for i in harness.bc.items] == ["a.txt"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/unit/test_batch_controller.py -x -q`
Expected: collection error — `ModuleNotFoundError: No module named 'vienetts_app.ui.batch_controller'`

- [ ] **Step 3: Write the implementation**

Create `src/vienetts_app/ui/batch_controller.py`:

```python
"""BatchFileController: Paragraph-tab multi-file synthesis queue (bead qef).

Sequential auto-run over imported documents. All synthesis goes through the
existing AppController listener seam (``submit_stream_for_listener``,
``kind="bulk"``) — the same posture as AudiobookController, so the worker
stays single-owner and serializes batch jobs behind interactive ones.

The controller never connects to AppController signals and reads the app
surface via getattr guards: a bare fake app object is a valid dependency
(smoke scenarios), and construction stays audio-stack-free (the player is
an injectable PlaybackController that builds QtMultimedia lazily).
"""

from __future__ import annotations

import contextlib
import logging
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from PySide6.QtCore import Property, QObject, QStandardPaths, QTimer, Signal, Slot

from vienetts_app.core.artifacts import SynthesisArtifact
from vienetts_app.core.importers import SUPPORTED_EXTENSIONS, import_document
from vienetts_app.ui.bg_ops import run_on_thread_pool
from vienetts_app.ui.controller import GENERATE_CHAR_LIMIT

logger = logging.getLogger(__name__)

STATUS_IMPORTING = "importing"
STATUS_PENDING = "pending"
STATUS_RENDERING = "rendering"
STATUS_SAVING = "saving"
STATUS_READY = "ready"
STATUS_FAILED = "failed"

# Interactive length cap message — same wording as AppController's editor
# guard (separate tr context, so the .ts carries one entry per controller).
OVERSIZE_MESSAGE = (
    "Bản văn quá dài ({chars:,} ký tự, giới hạn {limit:,}). "
    "Hãy dùng tab Sách nói (EPUB) để tạo văn bản dài theo từng chương."
)


@dataclass
class BatchItem:
    uid: int
    source_path: Path
    status: str = STATUS_IMPORTING
    text: str = ""
    error: str = ""
    wav_path: str = ""
    progress: float = 0.0
    job_id: str | None = None


def _default_player_factory() -> Any:
    from vienetts_app.ui.playback import PlaybackController

    return PlaybackController()


class BatchFileController(QObject):
    """Paragraph-tab file queue exposed to QML; dependencies injectable."""

    itemsChanged = Signal()
    runningChanged = Signal()
    progressChanged = Signal()
    currentIndexChanged = Signal()
    currentFileNameChanged = Signal()
    runAllDoneChanged = Signal()
    runAllTotalChanged = Signal()
    errorTextChanged = Signal()
    renderVoiceChanged = Signal()
    playingIndexChanged = Signal()
    hasPendingChanged = Signal()

    def __init__(
        self,
        app_controller: Any,
        *,
        data_dir: Path | None = None,
        player_factory: Callable[[], Any] | None = None,
        importer: Callable[..., str] | None = None,
        bg_runner: Callable[[Callable[[], Any], Callable[[Any], None], Any], None] | None = None,
        reveal_fn: Callable[[Path], bool] | None = None,
    ) -> None:
        super().__init__()
        from vienetts_app.core.settings import default_data_dir

        self._app = app_controller
        self._data_dir = default_data_dir() if data_dir is None else Path(data_dir)
        self._importer = import_document if importer is None else importer
        self._run_bg = run_on_thread_pool if bg_runner is None else bg_runner
        self._reveal_fn = reveal_fn
        factory = _default_player_factory if player_factory is None else player_factory
        self._player = factory()
        self._items: list[BatchItem] = []
        self._uid_seq = 0
        self._running = False
        self._job_id: str | None = None
        self._render_uid: int | None = None
        self._progress = 0.0
        self._current_index = -1
        self._run_done = 0
        self._run_total = 0
        self._error_text = ""
        self._render_voice = ""
        self._playing_index = -1
        self._items_cache: list[dict[str, Any]] | None = None
        # Coalesce item-model bursts (N imports landing, status+error flips):
        # one 0 ms single-shot per event-loop cycle, like Audiobook's chapters.
        self._items_emit_timer = QTimer(self)
        self._items_emit_timer.setSingleShot(True)
        self._items_emit_timer.setInterval(0)
        self._items_emit_timer.timeout.connect(self._flush_items)
        with contextlib.suppress(Exception):
            self._player.stateChanged.connect(self._on_player_state)

    # ── item model ───────────────────────────────────────────────────────────

    def _by_uid(self, uid: int | None) -> BatchItem | None:
        if uid is None:
            return None
        for item in self._items:
            if item.uid == uid:
                return item
        return None

    def _items_model(self) -> list[dict[str, Any]]:
        if self._items_cache is None:
            self._items_cache = [
                {
                    "uid": item.uid,
                    "sourcePath": str(item.source_path),
                    "fileName": item.source_path.name,
                    "status": item.status,
                    "error": item.error,
                    "wavPath": item.wav_path,
                    "progress": item.progress,
                }
                for item in self._items
            ]
        return self._items_cache

    def _emit_items(self) -> None:
        self._items_cache = None
        self._items_emit_timer.start()

    def _flush_items(self) -> None:
        self._items_cache = None
        self.itemsChanged.emit()
        self.hasPendingChanged.emit()

    @Property("QVariantList", notify=itemsChanged)
    def items(self) -> list[dict[str, Any]]:
        return self._items_model()

    @Property(bool, notify=hasPendingChanged)
    def hasPending(self) -> bool:
        return any(item.status == STATUS_PENDING for item in self._items)

    @Property(bool, notify=runningChanged)
    def running(self) -> bool:
        return self._running

    @Property(float, notify=progressChanged)
    def progress(self) -> float:
        return self._progress

    @Property(int, notify=currentIndexChanged)
    def currentIndex(self) -> int:
        return self._current_index

    @Property(str, notify=currentFileNameChanged)
    def currentFileName(self) -> str:
        item = self._by_uid(self._render_uid)
        return item.source_path.name if item is not None else ""

    @Property(int, notify=runAllDoneChanged)
    def runAllDone(self) -> int:
        return self._run_done

    @Property(int, notify=runAllTotalChanged)
    def runAllTotal(self) -> int:
        return self._run_total

    @Property(str, notify=errorTextChanged)
    def errorText(self) -> str:
        return self._error_text

    @Property(str, notify=renderVoiceChanged)
    def renderVoice(self) -> str:
        return self._render_voice

    @renderVoice.setter
    def renderVoice(self, value: str) -> None:  # noqa: F811
        value = value or ""
        if value != self._render_voice:
            self._render_voice = value
            self.renderVoiceChanged.emit()

    @Property(int, notify=playingIndexChanged)
    def playingIndex(self) -> int:
        return self._playing_index

    def _set_error(self, message: str) -> None:
        if message != self._error_text:
            self._error_text = message
            self.errorTextChanged.emit()

    # ── queue population ─────────────────────────────────────────────────────

    @Slot(list)
    def addFiles(self, paths: list) -> None:
        added = False
        for raw in paths or []:
            text = str(raw).strip()
            if not text:
                continue
            path = Path(text)
            if path.suffix.lower() not in SUPPORTED_EXTENSIONS:
                suffix = path.suffix or path.name
                self._set_error(self.tr("Không hỗ trợ định dạng tệp: {}").format(suffix))
                continue
            self._uid_seq += 1
            item = BatchItem(uid=self._uid_seq, source_path=path)
            self._items.append(item)
            added = True
            self._import_async(item)
        if added:
            self._emit_items()

    def _import_async(self, item: BatchItem) -> None:
        keep = bool(getattr(self._app, "srtKeepTimestamps", False))
        path, uid = item.source_path, item.uid

        def work() -> tuple[int, str, str]:
            try:
                return (uid, self._importer(str(path), keep_srt_raw=keep), "")
            except Exception as exc:  # noqa: BLE001 - importer errors are item failures
                return (uid, "", str(exc))

        def done(result: tuple[int, str, str]) -> None:
            ruid, text, error = result
            it = self._by_uid(ruid)
            if it is None:
                return  # removed mid-import
            if error or not text.strip():
                it.status = STATUS_FAILED
                it.error = error or self.tr("Không thể nhập tệp")
            else:
                it.status = STATUS_PENDING
                it.text = text
            self._emit_items()
            self._kick()

        self._run_bg(work, done, self)

    @Slot(int)
    def removeItem(self, index: int) -> None:
        if not 0 <= index < len(self._items):
            return
        item = self._items[index]
        if item.status in (STATUS_RENDERING, STATUS_SAVING):
            return  # the active render cannot be pulled mid-flight
        self._items.pop(index)
        self._refresh_run_totals()
        self._emit_items()

    @Slot()
    def clearFinished(self) -> None:
        kept = [i for i in self._items if i.status not in (STATUS_READY, STATUS_FAILED)]
        if len(kept) != len(self._items):
            self._items = kept
            self._refresh_run_totals()
            self._emit_items()

    # Placeholder Task 2+ seams (implemented in later tasks).

    def _kick(self) -> None:  # noqa: D102 - Task 2
        return

    def _refresh_run_totals(self) -> None:  # noqa: D102 - Task 2
        return

    def _on_player_state(self) -> None:  # noqa: D102 - Task 4
        return
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/unit/test_batch_controller.py -q`
Expected: 8 passed.

- [ ] **Step 5: Suggested commit**

```bash
git add src/vienetts_app/ui/batch_controller.py tests/unit/test_batch_controller.py
git commit -m "feat(batch): BatchFileController skeleton — queue items, off-thread import, item management"
```

---

### Task 2: Sequential run loop — runAll, listener contract, oversize, failure-continue, cancel

**Files:**
- Modify: `src/vienetts_app/ui/batch_controller.py`
- Test: `tests/unit/test_batch_controller.py` (append)

**Interfaces:**
- Consumes: Task 1's `BatchItem`, statuses, `_emit_items`; app seam `submit_stream_for_listener(text, voice, listener, *, kind) -> str | None`, `cancel_job(job_id) -> bool`, `defaultVoice` (getattr-guarded).
- Produces: slots `runAll()`, `cancel()`; listener methods `on_synthesis_progress(event)`, `on_synthesis_chunk(event)`, `on_synthesis_terminal(event)` (duck-typed contract AppController already calls); `_refresh_run_totals()` keeping `runAllDone`/`runAllTotal` live; `_fail_item(item, message)`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_batch_controller.py`:

```python
def progress_event(job_id: str, done: int, total: int):
    return SimpleNamespace(job_id=job_id, done=done, total=total, stage="synthesizing")


def terminal_event(job_id: str, state: str, value=None, error: str = ""):
    return SimpleNamespace(job_id=job_id, owner="paragraph", state=state, value=value, error=error)


class TestRunLoop:
    def test_run_all_submits_pending_in_order(self, harness, tmp_path):
        for name in ("a.txt", "b.txt"):
            harness.bc.addFiles([str(txt(tmp_path, name, f"nội dung {name}"))])
        harness.bc.runAll()
        # Strictly sequential: only the FIRST item is submitted so far.
        assert [s["text"] for s in harness.app.submissions] == ["nội dung a.txt"]
        assert harness.bc.running is True
        assert harness.bc.runAllTotal == 2 and harness.bc.runAllDone == 0
        assert harness.bc.items[0]["status"] == "rendering"
        assert harness.bc.items[1]["status"] == "pending"

    def test_progress_maps_to_current_item(self, harness, tmp_path):
        harness.bc.addFiles([str(txt(tmp_path, "a.txt", "nội dung"))])
        harness.bc.runAll()
        job_id = harness.app.submissions[0]["job_id"]
        harness.bc.on_synthesis_progress(progress_event(job_id, 1, 4))
        assert harness.bc.progress == pytest.approx(0.25)
        assert harness.bc.items[0]["progress"] == pytest.approx(0.25)

    def test_foreign_events_ignored(self, harness, tmp_path):
        harness.bc.addFiles([str(txt(tmp_path, "a.txt", "nội dung"))])
        harness.bc.runAll()
        harness.bc.on_synthesis_progress(progress_event("job-999", 3, 4))
        assert harness.bc.progress == 0.0

    def test_chunk_events_are_noop(self, harness, tmp_path):
        harness.bc.addFiles([str(txt(tmp_path, "a.txt", "nội dung"))])
        harness.bc.runAll()
        harness.bc.on_synthesis_chunk(SimpleNamespace(job_id="job-1", sample_count=480, peak=0.1))

    def test_oversize_item_fails_and_run_continues(self, harness, tmp_path):
        big = txt(tmp_path, "big.txt", "x" * (GENERATE_CHAR_LIMIT + 1))
        ok = txt(tmp_path, "ok.txt", "ngắn")
        harness.bc.addFiles([str(big), str(ok)])
        harness.bc.runAll()
        assert harness.bc.items[0]["status"] == "failed"
        assert "quá dài" in harness.bc.items[0]["error"]
        # the run skipped straight to the valid item
        assert harness.app.submissions[0]["text"] == "ngắn"
        assert harness.bc.items[1]["status"] == "rendering"

    def test_failed_terminal_marks_item_and_continues(self, harness, tmp_path):
        harness.bc.addFiles([
            str(txt(tmp_path, "a.txt", "thứ nhất")), str(txt(tmp_path, "b.txt", "thứ hai")),
        ])
        harness.bc.runAll()
        harness.bc.on_synthesis_terminal(terminal_event("job-1", "failed", error="engine boom"))
        assert harness.bc.items[0]["status"] == "failed"
        assert harness.bc.items[0]["error"] == "engine boom"
        assert harness.bc.items[1]["status"] == "rendering"

    def test_cancel_returns_item_to_pending_and_halts(self, harness, tmp_path):
        harness.bc.addFiles([
            str(txt(tmp_path, "a.txt", "thứ nhất")), str(txt(tmp_path, "b.txt", "thứ hai")),
        ])
        harness.bc.runAll()
        harness.bc.cancel()
        assert harness.app.cancelled == ["job-1"]
        assert harness.bc.running is False
        assert harness.bc.items[0]["status"] == "pending"
        assert harness.bc.items[1]["status"] == "pending"
        # the late cancelled terminal is dropped, not re-processed
        harness.bc.on_synthesis_terminal(terminal_event("job-1", "cancelled"))
        assert harness.bc.items[0]["status"] == "pending"
        assert len(harness.app.submissions) == 1

    def test_submission_failure_fails_item(self, harness, tmp_path, monkeypatch):
        harness.app.submit_stream_for_listener = lambda *a, **k: None
        harness.bc.addFiles([str(txt(tmp_path, "a.txt", "nội dung"))])
        harness.bc.runAll()
        assert harness.bc.items[0]["status"] == "failed"
        assert harness.bc.running is False

    def test_voice_resolution_prefers_render_voice(self, harness, tmp_path):
        harness.bc.renderVoice = "Giọng đặc biệt"
        harness.bc.addFiles([str(txt(tmp_path, "a.txt", "nội dung"))])
        harness.bc.runAll()
        assert harness.app.submissions[0]["voice"] == "Giọng đặc biệt"
        assert harness.app.submissions[0]["kind"] == "bulk"
```

Add the import `from vienetts_app.ui.controller import GENERATE_CHAR_LIMIT` at the top of the test file.

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/unit/test_batch_controller.py -q`
Expected: new tests FAIL (`running` never becomes True, no submissions), Task 1 tests still pass.

- [ ] **Step 3: Implement the run loop**

Replace the Task 1 placeholder seams in `src/vienetts_app/ui/batch_controller.py` with:

```python
    # ── sequential run loop ──────────────────────────────────────────────────

    @Slot()
    def runAll(self) -> None:
        """Run every pending item in order, one synthesis at a time."""
        if self._running:
            return
        self._set_error("")
        self._running = True
        self.runningChanged.emit()
        self._run_done = 0
        self._refresh_run_totals()
        self._kick()

    @Slot()
    def cancel(self) -> None:
        """Halt the run; the in-flight item returns to pending."""
        if not self._running and self._job_id is None:
            return
        self._running = False
        self.runningChanged.emit()
        job_id, self._job_id = self._job_id, None
        uid, self._render_uid = self._render_uid, None
        item = self._by_uid(uid)
        if item is not None and item.status == STATUS_RENDERING:
            item.status = STATUS_PENDING
            item.progress = 0.0
            item.job_id = None
        if job_id is not None:
            cancel = getattr(self._app, "cancel_job", None)
            if callable(cancel):
                with contextlib.suppress(Exception):
                    cancel(job_id)
        self._progress = 0.0
        self.progressChanged.emit()
        self._set_current_index(-1)
        self._emit_items()
        self._refresh_run_totals()

    def _set_current_index(self, index: int) -> None:
        if index != self._current_index:
            self._current_index = index
            self.currentIndexChanged.emit()
            self.currentFileNameChanged.emit()

    def _kick(self) -> None:
        if not self._running or self._job_id is not None:
            return
        for index, item in enumerate(self._items):
            if item.status != STATUS_PENDING:
                continue
            if self._start_render(index, item):
                return
            # _start_render already marked the item failed — keep scanning.
        self._running = False
        self.runningChanged.emit()
        self._set_current_index(-1)
        self._refresh_run_totals()

    def _start_render(self, index: int, item: BatchItem) -> bool:
        if len(item.text) > GENERATE_CHAR_LIMIT:
            self._fail_item(
                item, self.tr(OVERSIZE_MESSAGE).format(
                    chars=len(item.text), limit=GENERATE_CHAR_LIMIT
                )
            )
            return False
        submit = getattr(self._app, "submit_stream_for_listener", None)
        if not callable(submit):
            self._fail_item(item, self.tr("Không thể tạo tác vụ tổng hợp."))
            return False
        voice = self._render_voice or str(getattr(self._app, "defaultVoice", "") or "")
        job_id = submit(item.text, voice, self, kind="bulk")
        if not job_id:
            self._fail_item(item, self.tr("Không thể tạo tác vụ tổng hợp."))
            return False
        item.status = STATUS_RENDERING
        item.progress = 0.0
        item.job_id = job_id
        item.error = ""
        self._job_id = job_id
        self._render_uid = item.uid
        self._progress = 0.0
        self.progressChanged.emit()
        self._set_current_index(index)
        self._emit_items()
        self._refresh_run_totals()
        return True

    def _fail_item(self, item: BatchItem | None, message: str) -> None:
        if item is None:
            return
        item.status = STATUS_FAILED
        item.error = message
        item.job_id = None
        self._emit_items()
        self._refresh_run_totals()

    def _refresh_run_totals(self) -> None:
        done = sum(1 for i in self._items if i.status in (STATUS_READY, STATUS_FAILED))
        remaining = sum(
            1 for i in self._items if i.status in (STATUS_PENDING, STATUS_RENDERING, STATUS_SAVING)
        )
        total = done + remaining if self._running or self._run_done or self._run_total else 0
        if self._run_done != done:
            self._run_done = done
            self.runAllDoneChanged.emit()
        if self._run_total != total:
            self._run_total = total
            self.runAllTotalChanged.emit()

    # ── synthesis-listener contract (called by AppController) ────────────────

    def on_synthesis_progress(self, event: Any) -> None:
        if getattr(event, "job_id", None) != self._job_id:
            return
        total = int(getattr(event, "total", 0) or 0)
        done = int(getattr(event, "done", 0) or 0)
        fraction = (done / total) if total > 0 else 0.0
        item = self._by_uid(self._render_uid)
        if item is not None:
            item.progress = fraction
        if fraction != self._progress:
            self._progress = fraction
            self.progressChanged.emit()

    def on_synthesis_chunk(self, event: Any) -> None:
        # Batch renders are silent (no live-audio lane); chunks are ignored.
        return

    def on_synthesis_terminal(self, event: Any) -> None:
        if getattr(event, "job_id", None) != self._job_id or self._job_id is None:
            self._release_artifact_file(getattr(event, "value", None))
            return
        job_id, self._job_id = self._job_id, None
        uid, self._render_uid = self._render_uid, None
        item = self._by_uid(uid)
        state = str(getattr(event, "state", "") or "")
        if state == "completed":
            self._save_item(item, job_id, getattr(event, "value", None))
        elif state == "cancelled":
            if item is not None and item.status == STATUS_RENDERING:
                item.status = STATUS_PENDING
                item.progress = 0.0
                item.job_id = None
            self._progress = 0.0
            self.progressChanged.emit()
            self._emit_items()
            self._refresh_run_totals()
            self._kick()
        else:
            message = str(getattr(event, "error", "") or "") or self.tr("Tổng hợp thất bại.")
            self._fail_item(item, message)
            self._set_current_index(-1)
            self._kick()

    def _save_item(self, item: BatchItem | None, job_id: str | None, artifact: Any) -> None:
        raise NotImplementedError  # Task 3

    def _release_artifact_file(self, artifact: Any) -> None:
        raise NotImplementedError  # Task 3
```

Note the test helper: `FakeApp.submissions` records `job_id` per submission (already in the Task 1 fake), so tests resolve the active job as `harness.app.submissions[0]["job_id"]`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/unit/test_batch_controller.py -q`
Expected: all pass except any test that drives a **completed** terminal (none yet — completion lands in Task 3; `test_failed_terminal…`, cancel, oversize, ordering all pass).

- [ ] **Step 5: Suggested commit**

```bash
git add src/vienetts_app/ui/batch_controller.py tests/unit/test_batch_controller.py
git commit -m "feat(batch): sequential runAll loop with listener contract, oversize guard, cancel"
```

---

### Task 3: Completion + per-file auto-export

**Files:**
- Modify: `src/vienetts_app/ui/batch_controller.py`
- Test: `tests/unit/test_batch_controller.py` (append)

**Interfaces:**
- Consumes: `SynthesisArtifact(job_id, path, sample_rate, samples, duration_ms)` from `core.artifacts`; app seam `outputDir` (getattr-guarded str).
- Produces: `_save_item(item, job_id, artifact)` (completed terminal → `saving` → off-thread copy → `ready`); `_release_artifact_file(artifact)` (deletes the interactive-store WAV only when it lives under `self._data_dir`); `_export_target(stem) -> Path` (settings output dir else `Music/VieNeuTTS`, sanitized `<stem>.wav`, `_2`/`_3`… collision suffixes); slot `showInFolder` comes in Task 4.

- [ ] **Step 1: Write the failing tests**

Append:

```python
def make_artifact(tmp_path: Path, job_id: str, samples: int = 480) -> SynthesisArtifact:
    import numpy as np
    import soundfile as sf

    path = tmp_path / "artifacts" / "interactive" / f"{job_id}.wav"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = np.zeros(samples, dtype=np.float32)
    sf.write(path, payload, 48_000, subtype="FLOAT", format="WAV")
    return SynthesisArtifact(
        job_id=job_id, path=path, sample_rate=48_000,
        samples=samples, duration_ms=int(samples * 1000 / 48_000),
    )


class TestCompletionExport:
    def test_completed_item_saves_named_wav_and_advances(self, harness, tmp_path):
        a = txt(tmp_path, "báo cáo.txt", "thứ nhất")
        b = txt(tmp_path, "cuốn.md", "thứ hai")
        harness.bc.addFiles([str(a), str(b)])
        harness.bc.runAll()
        art = make_artifact(tmp_path, "job-1")
        harness.bc.on_synthesis_terminal(terminal_event("job-1", "completed", value=art))
        out = tmp_path / "out"
        assert harness.bc.items[0]["status"] == "ready"
        assert harness.bc.items[0]["wavPath"] == str(out / "báo cáo.wav")
        assert (out / "báo cáo.wav").read_bytes() == art.path.read_bytes()
        assert not art.path.exists()  # interactive artifact cleaned up
        assert harness.bc.runAllDone == 1 and harness.bc.runAllTotal == 2
        assert harness.bc.items[1]["status"] == "rendering"  # auto-advanced

    def test_same_stem_gets_collision_suffix(self, harness, tmp_path):
        one = txt(tmp_path, "a.txt", "thứ nhất")
        (tmp_path / "sub").mkdir()
        two = tmp_path / "sub" / "a.txt"
        two.write_text("thứ hai", encoding="utf-8")
        harness.bc.addFiles([str(one), str(two)])
        harness.bc.runAll()
        art1 = make_artifact(tmp_path, "job-1")
        harness.bc.on_synthesis_terminal(terminal_event("job-1", "completed", value=art1))
        art2 = make_artifact(tmp_path, "job-2")
        harness.bc.on_synthesis_terminal(terminal_event("job-2", "completed", value=art2))
        wavs = sorted(p.name for p in (tmp_path / "out").glob("*.wav"))
        assert wavs == ["a.wav", "a_2.wav"]

    def test_invalid_artifact_fails_item_and_continues(self, harness, tmp_path):
        a = txt(tmp_path, "a.txt", "thứ nhất")
        b = txt(tmp_path, "b.txt", "thứ hai")
        harness.bc.addFiles([str(a), str(b)])
        harness.bc.runAll()
        harness.bc.on_synthesis_terminal(terminal_event("job-1", "completed", value="nonsense"))
        assert harness.bc.items[0]["status"] == "failed"
        assert harness.bc.items[1]["status"] == "rendering"

    def test_mismatched_job_id_artifact_fails_item(self, harness, tmp_path):
        harness.bc.addFiles([str(txt(tmp_path, "a.txt", "thứ nhất"))])
        harness.bc.runAll()
        art = make_artifact(tmp_path, "job-other")
        harness.bc.on_synthesis_terminal(terminal_event("job-1", "completed", value=art))
        assert harness.bc.items[0]["status"] == "failed"

    def test_export_copy_failure_fails_item_keeps_artifact(self, harness, tmp_path, monkeypatch):
        harness.bc.addFiles([str(txt(tmp_path, "a.txt", "thứ nhất"))])
        harness.bc.runAll()
        art = make_artifact(tmp_path, "job-1")

        def boom(src, dst):
            raise OSError("disk full")

        monkeypatch.setattr("vienetts_app.ui.batch_controller.shutil.copyfile", boom)
        harness.bc.on_synthesis_terminal(terminal_event("job-1", "completed", value=art))
        assert harness.bc.items[0]["status"] == "failed"
        assert "disk full" in harness.bc.items[0]["error"]
        assert art.path.exists()  # kept for manual recovery

    def test_foreign_completed_terminal_releases_artifact(self, harness, tmp_path):
        art = make_artifact(tmp_path, "job-foreign")
        harness.bc.on_synthesis_terminal(terminal_event("job-foreign", "completed", value=art))
        assert not art.path.exists()

    def test_run_completes_and_totals_freeze(self, harness, tmp_path):
        harness.bc.addFiles([str(txt(tmp_path, "a.txt", "thứ nhất"))])
        harness.bc.runAll()
        art = make_artifact(tmp_path, "job-1")
        harness.bc.on_synthesis_terminal(terminal_event("job-1", "completed", value=art))
        assert harness.bc.running is False
        assert harness.bc.runAllDone == 1 and harness.bc.runAllTotal == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/unit/test_batch_controller.py -q`
Expected: completion tests FAIL with `NotImplementedError` from `_save_item`.

- [ ] **Step 3: Implement save + export**

Replace the two `NotImplementedError` methods in `batch_controller.py`:

```python
    def _save_item(self, item: BatchItem | None, job_id: str | None, artifact: Any) -> None:
        if item is None:
            self._release_artifact_file(artifact)
            self._kick()
            return
        if (
            not isinstance(artifact, SynthesisArtifact)
            or artifact.job_id != job_id
            or not artifact.path.is_file()
        ):
            self._release_artifact_file(artifact)
            self._fail_item(item, self.tr("Tệp âm thanh vừa tạo không hợp lệ."))
            self._set_current_index(-1)
            self._kick()
            return
        item.status = STATUS_SAVING
        item.job_id = None
        self._emit_items()
        self._refresh_run_totals()
        target = self._export_target(item.source_path.stem)
        source = artifact.path
        uid = item.uid

        def work() -> tuple[int, str, str]:
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, target)
                return (uid, str(target), "")
            except OSError as exc:
                return (uid, "", str(exc))

        def done(result: tuple[int, str, str]) -> None:
            ruid, wav, error = result
            it = self._by_uid(ruid)
            if error:
                # Keep the interactive artifact for manual recovery.
                if it is not None:
                    self._fail_item(
                        it, self.tr("Không thể lưu tệp âm thanh: {}").format(error)
                    )
                self._set_current_index(-1)
                self._kick()
                return
            if it is not None:
                it.status = STATUS_READY
                it.wav_path = wav
                it.progress = 1.0
            self._release_artifact_file(artifact)
            self._set_current_index(-1)
            self._emit_items()
            self._refresh_run_totals()
            self._kick()

        self._run_bg(work, done, self)

    def _release_artifact_file(self, artifact: Any) -> None:
        """Delete an interactive-store artifact WAV — only under OUR data dir."""
        if not isinstance(artifact, SynthesisArtifact):
            return
        try:
            path = artifact.path.resolve()
            root = self._data_dir.resolve()
            if root == path or root not in path.parents:
                return
            path.unlink(missing_ok=True)
        except OSError:
            logger.warning("could not release batch artifact %s", artifact.path)

    def _export_target(self, stem: str) -> Path:
        base = str(getattr(self._app, "outputDir", "") or "").strip()
        if base:
            directory = Path(base)
        else:
            music = QStandardPaths.writableLocation(
                QStandardPaths.StandardLocation.MusicLocation
            )
            directory = (Path(music) if music else Path.home() / "Music") / "VieNeuTTS"
        safe = "".join(c if (c.isalnum() or c in ("-", "_", " ")) else "_" for c in stem)
        safe = safe.strip() or "audio"
        candidate = directory / f"{safe}.wav"
        suffix = 2
        while candidate.exists():
            candidate = directory / f"{safe}_{suffix}.wav"
            suffix += 1
        return candidate
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/unit/test_batch_controller.py -q`
Expected: all pass.

- [ ] **Step 5: Suggested commit**

```bash
git add src/vienetts_app/ui/batch_controller.py tests/unit/test_batch_controller.py
git commit -m "feat(batch): completed items auto-export as <source>.wav with collision suffixes"
```

---

### Task 4: Per-item playback + reveal-in-folder

**Files:**
- Modify: `src/vienetts_app/ui/batch_controller.py`
- Test: `tests/unit/test_batch_controller.py` (append)

**Interfaces:**
- Consumes: player seam `play(path, on_released=...)` / `stop()` / `stateChanged` (Task 1's `FakePlayer`).
- Produces: slots `playItem(int)`, `stopPlay()`, `showInFolder(int) -> bool`; property `playingIndex` (already declared in Task 1).

- [ ] **Step 1: Write the failing tests**

Append:

```python
def ready_item(harness: Harness, tmp_path: Path, name: str = "a.txt") -> None:
    harness.bc.addFiles([str(txt(tmp_path, name, "thứ nhất"))])
    harness.bc.runAll()
    art = make_artifact(tmp_path, "job-1")
    harness.bc.on_synthesis_terminal(terminal_event("job-1", "completed", value=art))


class TestPlaybackAndReveal:
    def test_play_ready_item_uses_player(self, harness, tmp_path):
        ready_item(harness, tmp_path)
        wav = harness.bc.items[0]["wavPath"]
        harness.bc.playItem(0)
        assert harness.player.calls == [("play", wav)]
        assert harness.bc.playingIndex == 0

    def test_play_toggle_stops(self, harness, tmp_path):
        ready_item(harness, tmp_path)
        harness.bc.playItem(0)
        harness.bc.playItem(0)  # same row again = stop
        assert ("stop",) in harness.player.calls
        assert harness.bc.playingIndex == -1

    def test_released_resets_playing_index(self, harness, tmp_path):
        ready_item(harness, tmp_path)
        harness.bc.playItem(0)
        harness.player.finish()  # playback ran to the end
        assert harness.bc.playingIndex == -1

    def test_play_non_ready_item_is_noop(self, harness, tmp_path):
        harness.bc.addFiles([str(txt(tmp_path, "a.txt", "thứ nhất"))])
        harness.bc.playItem(0)
        assert harness.player.calls == []

    def test_show_in_folder_reveals_parent(self, harness, tmp_path):
        revealed: list[Path] = []
        harness.bc._reveal_fn = lambda p: revealed.append(p) or True
        ready_item(harness, tmp_path)
        assert harness.bc.showInFolder(0) is True
        assert revealed == [tmp_path / "out"]

    def test_show_in_folder_failure_sets_error(self, harness, tmp_path):
        harness.bc._reveal_fn = lambda p: False
        ready_item(harness, tmp_path)
        assert harness.bc.showInFolder(0) is False
        assert harness.bc.errorText != ""
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/unit/test_batch_controller.py -k "Playback or Reveal" -q`
Expected: FAIL (`playItem` missing).

- [ ] **Step 3: Implement playback + reveal**

In `batch_controller.py`, replace the `_on_player_state` placeholder and add:

```python
    # ── per-item preview + reveal ────────────────────────────────────────────

    @Slot(int)
    def playItem(self, index: int) -> None:
        if not 0 <= index < len(self._items):
            return
        item = self._items[index]
        if item.status != STATUS_READY or not item.wav_path:
            return
        if not Path(item.wav_path).is_file():
            return
        if self._playing_index == index:
            self.stopPlay()
            return
        self.stopPlay()
        try:
            self._player.play(item.wav_path, on_released=self._on_play_released)
        except Exception:  # noqa: BLE001 - file playback must never crash the UI
            logger.exception("batch preview playback failed")
            self._set_error(self.tr("Hệ thống này không phát được âm thanh."))
            return
        self._set_playing_index(index)

    @Slot()
    def stopPlay(self) -> None:
        if self._playing_index != -1:
            self._set_playing_index(-1)
        with contextlib.suppress(Exception):
            self._player.stop()

    @Slot(int, result=bool)
    def showInFolder(self, index: int) -> bool:
        if not 0 <= index < len(self._items):
            return False
        item = self._items[index]
        if item.status != STATUS_READY or not item.wav_path:
            return False
        reveal = self._reveal_fn or self._default_reveal
        try:
            ok = bool(reveal(Path(item.wav_path)))
        except Exception:  # noqa: BLE001
            logger.exception("reveal batch wav failed")
            ok = False
        if not ok:
            self._set_error(self.tr("Không mở được thư mục chứa tệp."))
        return ok

    @staticmethod
    def _default_reveal(path: Path) -> bool:
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices

        return bool(QDesktopServices.openUrl(QUrl.fromLocalFile(str(path.parent))))

    def _set_playing_index(self, index: int) -> None:
        if index != self._playing_index:
            self._playing_index = index
            self.playingIndexChanged.emit()

    def _on_play_released(self) -> None:
        self._set_playing_index(-1)

    def _on_player_state(self) -> None:
        state = str(getattr(self._player, "state", "stopped") or "stopped")
        if state == "stopped" and self._playing_index != -1:
            self._set_playing_index(-1)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/unit/test_batch_controller.py -q`
Expected: all pass (full controller suite green).

- [ ] **Step 5: Suggested commit**

```bash
git add src/vienetts_app/ui/batch_controller.py tests/unit/test_batch_controller.py
git commit -m "feat(batch): per-item preview playback and reveal-in-folder"
```

---

### Task 5: `create_app` wiring — `batchController` context property

**Files:**
- Modify: `src/vienetts_app/app.py` (the `create_app` signature and body, right after the audiobook block around line 189–194)

**Interfaces:**
- Consumes: `BatchFileController(app_controller)` from Task 1–4.
- Produces: `create_app(..., batch_factory: Callable[[AppController], BatchFileController | Any] | None = None)`; QML context property `batchController`; lifetime anchor `engine._batch`.

- [ ] **Step 1: Write the failing check (extend an existing shell smoke test)**

In `tests/smoke/test_ui_shell.py`, find the test that loads `create_app` and asserts context properties (the one reading `engine.rootContext().contextProperty("controller")` around line 240) and add to its assertions:

```python
        batch = engine.rootContext().contextProperty("batchController")
        assert batch is not None
        assert hasattr(batch, "addFiles")
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/smoke/test_ui_shell.py -q -k "context or shell"`
Expected: the modified test FAILS (`contextProperty("batchController")` is None).

- [ ] **Step 3: Wire the controller**

In `src/vienetts_app/app.py`:

1. Import: `from vienetts_app.ui.batch_controller import BatchFileController` (next to the `AudiobookController` import).
2. Add the parameter to `create_app` after `audiobook_factory`:

```python
    batch_factory: (Callable[[AppController], BatchFileController | Any] | None) = None,
```

3. Add after the `engine._audiobook = audiobook` block:

```python
    # Paragraph-tab multi-file queue shares the controller's engine/worker
    # (one model load) via the listener seam; construction stays model-free
    # and connects to no app signals, so fake-app smoke scenarios are safe.
    batch = (
        BatchFileController(controller)
        if batch_factory is None
        else batch_factory(controller)
    )
    engine.rootContext().setContextProperty("batchController", batch)
    engine._batch = batch  # noqa: SLF001 — lifetime anchor, same as above
```

- [ ] **Step 4: Run the shell smoke suite to verify no regression**

Run: `.venv/bin/python -m pytest tests/smoke/test_ui_shell.py -q`
Expected: PASS (the default `BatchFileController` over the real `AppController` constructs cleanly — no signal connections, no audio stack).

Also run the existing tab smoke entry point to confirm the fake-controller scenarios still load QML (a real `BatchFileController` now sits over their fakes):

Run: `.venv/bin/python -m pytest tests/smoke/test_ui_tabs.py -q -k "paragraph"`
Expected: PASS (bindings on `batchController` read only its own state; `typeof` guards in QML — added in Task 6 — keep undefined slots safe).

- [ ] **Step 5: Suggested commit**

```bash
git add src/vienetts_app/app.py tests/smoke/test_ui_shell.py
git commit -m "feat(batch): expose batchController QML context property via create_app"
```

---

### Task 6: QML — `BatchQueueCard` + ParagraphTab integration + smoke tests

**Files:**
- Create: `src/vienetts_app/ui/qml/components/BatchQueueCard.qml`
- Modify: `src/vienetts_app/ui/qml/ParagraphTab.qml` (mount card between the editor card and the voice card; multi-URL drop branch; `renderVoice` sync)
- Test: `tests/smoke/test_ui_tabs.py` (new scenario `para_batch` in the paragraph scenario group)

**Interfaces:**
- Consumes: context property `batchController` with `items`/`running`/`progress`/`currentIndex`/`runAllDone`/`runAllTotal`/`errorText`/`renderVoice`/`playingIndex`/`hasPending`, slots `addFiles`/`removeItem`/`clearFinished`/`runAll`/`cancel`/`playItem`/`stopPlay`/`showInFolder`; components `AppCard`, `AppButton`, `AppIconButton`, `StatusBadge`, `Theme`.
- Produces: QML objectNames `batchQueueCard`, `batchImportDialog`, `addFilesButton`, `runAllButton`, `batchCancelButton`, `clearFinishedButton`, `batchFileList`, `batchRunSummary`; ParagraphTab function `handleDroppedUrls(urls)`.

- [ ] **Step 1: Write the failing smoke test**

In `tests/smoke/test_ui_tabs.py`, add `"para_batch"` to the paragraph scenario list and extend the scenario `if` chain with a branch that:

```python
        if scenario == "para_batch":
            class FakeBatch(QObject):
                itemsChanged = Signal()
                runningChanged = Signal()
                progressChanged = Signal()
                currentIndexChanged = Signal()
                runAllDoneChanged = Signal()
                runAllTotalChanged = Signal()
                errorTextChanged = Signal()
                renderVoiceChanged = Signal()
                playingIndexChanged = Signal()
                hasPendingChanged = Signal()

                def __init__(self):
                    super().__init__()
                    self._items = []
                    self.added: list[list[str]] = []
                    self.removed: list[int] = []
                    self.ran = 0
                    self.cancelled = 0
                    self._render_voice = ""

                @Property("QVariantList", notify=itemsChanged)
                def items(self):
                    return self._items

                @items.setter
                def items(self, value):
                    self._items = value
                    self.itemsChanged.emit()

                @Property(bool, notify=runningChanged)
                def running(self):
                    return False

                @Property(bool, notify=hasPendingChanged)
                def hasPending(self):
                    return any(i["status"] == "pending" for i in self._items)

                @Property(float, notify=progressChanged)
                def progress(self):
                    return 0.5

                @Property(int, notify=currentIndexChanged)
                def currentIndex(self):
                    return 0

                @Property(int, notify=runAllDoneChanged)
                def runAllDone(self):
                    return 1

                @Property(int, notify=runAllTotalChanged)
                def runAllTotal(self):
                    return 2

                @Property(str, notify=errorTextChanged)
                def errorText(self):
                    return ""

                @Property(str, notify=renderVoiceChanged)
                def renderVoice(self):
                    return self._render_voice

                @renderVoice.setter
                def renderVoice(self, value):
                    self._render_voice = value
                    self.renderVoiceChanged.emit()

                @Property(int, notify=playingIndexChanged)
                def playingIndex(self):
                    return -1

                @Slot(list)
                def addFiles(self, paths):
                    self.added.append([str(p) for p in paths])

                @Slot(int)
                def removeItem(self, index):
                    self.removed.append(index)

                @Slot()
                def clearFinished(self):
                    pass

                @Slot()
                def runAll(self):
                    self.ran += 1

                @Slot()
                def cancel(self):
                    self.cancelled += 1

                @Slot(int)
                def playItem(self, index):
                    pass

                @Slot()
                def stopPlay(self):
                    pass

                @Slot(int, result=bool)
                def showInFolder(self, index):
                    return True

            fake_batch = FakeBatch()
```

…and pass `batch_factory=lambda _controller: fake_batch` into that scenario's `create_app(...)` call. Then the scenario body asserts (via the paragraph-subtree `pfind` helper):

```python
            card = pfind("batchQueueCard")
            assert card.property("visible") is True  # empty state strip visible
            dialog = pfind("batchImportDialog")
            assert dialog is not None
            # multi-select dialog contract
            from PySide6.QtQuick.Dialogs import QFileDialog
            assert dialog.property("fileMode") == int(QFileDialog.FileMode.OpenFiles)

            # Empty state: list hidden, run-all disabled
            assert pfind("batchFileList").property("visible") is False
            assert pfind("runAllButton").property("enabled") is False

            # Populate two items → list visible, run-all enabled
            fake_batch.items = [
                {"uid": 1, "sourcePath": "/tmp/a.txt", "fileName": "a.txt",
                 "status": "pending", "error": "", "wavPath": "", "progress": 0.0},
                {"uid": 2, "sourcePath": "/tmp/b.txt", "fileName": "b.txt",
                 "status": "ready", "error": "", "wavPath": "/tmp/out/b.wav",
                 "progress": 1.0},
            ]
            wait_for(lambda: pfind("batchFileList").property("visible") is True)
            assert pfind("runAllButton").property("enabled") is True

            # Footer actions drive the fake
            click(pfind("runAllButton"))
            assert fake_batch.ran == 1
            click(pfind("addFilesButton"))  # opens the dialog (accepted via API below)

            # Drop routing: two urls → addFiles, one url → editor import
            invoke(paragraph_tab, "handleDroppedUrls", [
                QUrl.fromLocalFile(str(tmp / "one.txt")),
                QUrl.fromLocalFile(str(tmp / "two.txt")),
            ])
            assert fake_batch.added == [[str(tmp / "one.txt"), str(tmp / "two.txt")]]
            (tmp / "single.txt").write_text("nội dung đơn", encoding="utf-8")
            invoke(paragraph_tab, "handleDroppedUrls", [QUrl.fromLocalFile(str(tmp / "single.txt"))])
            editor = pfind("paragraphEditor")
            assert editor.property("text") == "nội dung đơn"
```

Use the file's existing `QMetaObject.invokeMethod` idiom for `invoke` (mirror the `importPath` calls around line 1324) and its existing click/mouse helpers; `wait_for` mirrors the suite's existing `wait_until` helper. Create `one.txt`/`two.txt` before invoking.

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/smoke/test_ui_tabs.py -q -k paragraph`
Expected: FAIL — `batchQueueCard` lookup raises (no such objectName).

- [ ] **Step 3: Create `BatchQueueCard.qml`**

Create `src/vienetts_app/ui/qml/components/BatchQueueCard.qml`:

```qml
// Multi-file batch queue (bead qef): file rows with live status, sequential
// auto-run footer, per-item play/reveal. Reads the `batchController` context
// property; coexists with the single-file editor card above it.
import QtQuick
import QtQuick.Controls
import QtQuick.Dialogs
import QtQuick.Layouts
import ".."
import "."

AppCard {
    id: root

    objectName: "batchQueueCard"
    title: qsTr("Hàng đợi tệp")
    subtitle: qsTr("Chọn nhiều tệp để chạy tự động theo lượt — mỗi tệp lưu thành một WAV riêng.")

    // True when a batchController context property exists at all.
    readonly property bool available: typeof batchController !== "undefined"
                                      && batchController !== null
    readonly property var model: available ? batchController.items : []

    FileDialog {
        id: batchImportDialog

        objectName: "batchImportDialog"
        fileMode: FileDialog.OpenFiles
        title: qsTr("Chọn một hoặc nhiều tệp văn bản")
        nameFilters: ["Văn bản (*.txt *.md *.docx *.pdf *.srt)"]
        onAccepted: if (root.available)
            batchController.addFiles(selectedFiles.map(u => root.toLocalPath(u)))

        // QUrl → local path (same normalization as ParagraphTab.toLocalPath).
        function toLocalPath(url): string {
            const s = url.toString();
            if (!s.startsWith("file://"))
                return s;
            let path = decodeURIComponent(s.substring(7));
            if (/^\/[A-Za-z]:\//.test(path))
                path = path.substring(1);
            return path;
        }
    }

    function statusInfo(status) {
        switch (status) {
        case "importing": return { label: qsTr("Đang nhập"), tone: "info" };
        case "pending": return { label: qsTr("Chờ"), tone: "neutral" };
        case "rendering": return { label: qsTr("Đang tạo"), tone: "info" };
        case "saving": return { label: qsTr("Đang lưu"), tone: "info" };
        case "ready": return { label: qsTr("Sẵn sàng"), tone: "success" };
        case "failed": return { label: qsTr("Lỗi"), tone: "error" };
        }
        return { label: status, tone: "neutral" };
    }

    headerAction: RowLayout {
        spacing: Theme.spacingSm

        AppButton {
            id: addFilesBtn
            objectName: "addFilesButton"
            variant: "secondary"
            size: "sm"
            iconKind: "upload"
            text: qsTr("Thêm tệp…")
            enabled: root.available && !batchController.running
            onClicked: batchImportDialog.open()
        }

        AppButton {
            objectName: "clearFinishedButton"
            variant: "ghost"
            size: "sm"
            text: qsTr("Xóa đã xong")
            enabled: root.available && batchController.items.length > 0
                     && !batchController.running
            onClicked: batchController.clearFinished()
        }
    }

    ColumnLayout {
        Layout.fillWidth: true
        spacing: Theme.spacingMd

        ListView {
            id: fileList

            objectName: "batchFileList"
            Layout.fillWidth: true
            implicitHeight: Math.min(contentHeight, 280)
            visible: root.model.length > 0
            spacing: Theme.spacingXs
            clip: true

            model: root.model

            ScrollBar.vertical: ScrollBar {
                policy: ScrollBar.AsNeeded
            }

            delegate: Rectangle {
                width: fileList.width
                height: rowLayout.implicitHeight + Theme.spacingSm * 2
                radius: Theme.radiusMd
                color: Theme.surface
                border.color: Theme.borderSubtle
                border.width: 1

                readonly property var info: root.statusInfo(modelData.status)
                readonly property bool isPlaying: root.available
                    && batchController.playingIndex === index

                ColumnLayout {
                    id: rowLayout
                    anchors.fill: parent
                    anchors.margins: Theme.spacingSm
                    spacing: Theme.spacingXs

                    RowLayout {
                        Layout.fillWidth: true
                        spacing: Theme.spacingSm

                        Label {
                            text: modelData.fileName
                            color: Theme.text
                            font.family: Theme.fontFamily
                            font.pixelSize: Theme.fontSizeSm
                            font.weight: Theme.fontWeightMedium
                            elide: Text.ElideMiddle
                            Layout.fillWidth: true
                        }

                        StatusBadge {
                            text: info.label
                            status: info.tone
                        }

                        AppIconButton {
                            iconKind: isPlaying ? "stop" : "play"
                            size: "sm"
                            accessibleLabel: isPlaying ? qsTr("Dừng") : qsTr("Phát")
                            tooltipText: accessibleLabel
                            enabled: modelData.status === "ready"
                            visible: enabled
                            onClicked: batchController.playItem(index)
                        }

                        AppIconButton {
                            iconKind: "folder"
                            size: "sm"
                            accessibleLabel: qsTr("Mở thư mục")
                            tooltipText: accessibleLabel
                            enabled: modelData.status === "ready" && modelData.wavPath !== ""
                            visible: enabled
                            onClicked: batchController.showInFolder(index)
                        }

                        AppIconButton {
                            iconKind: "close"
                            size: "sm"
                            accessibleLabel: qsTr("Xóa khỏi hàng đợi")
                            tooltipText: accessibleLabel
                            enabled: modelData.status !== "rendering"
                                      && modelData.status !== "saving"
                            onClicked: batchController.removeItem(index)
                        }
                    }

                    ProgressBar {
                        Layout.fillWidth: true
                        from: 0
                        to: 1
                        visible: modelData.status === "rendering"
                        value: root.available && batchController.currentIndex === index
                            ? batchController.progress : 0
                        background: Rectangle {
                            implicitHeight: 4
                            radius: 2
                            color: Theme.surfaceAlt
                        }
                        contentItem: Rectangle {
                            visible: value > 0
                            width: parent.width * parent.position
                            height: parent.height
                            radius: 2
                            color: Theme.accent
                        }
                    }

                    Label {
                        Layout.fillWidth: true
                        visible: modelData.error !== ""
                        text: modelData.error
                        color: Theme.error
                        font.family: Theme.fontFamily
                        font.pixelSize: Theme.fontSizeXs
                        wrapMode: Text.Wrap
                    }
                }
            }
        }

        Label {
            Layout.fillWidth: true
            visible: root.model.length === 0
            text: qsTr("Chưa có tệp nào — dùng \"Thêm tệp…\" hoặc kéo thả nhiều tệp vào khung văn bản.")
            color: Theme.textSubtle
            font.family: Theme.fontFamily
            font.pixelSize: Theme.fontSizeXs
            wrapMode: Text.Wrap
        }

        RowLayout {
            Layout.fillWidth: true
            spacing: Theme.spacingSm

            AppButton {
                id: runAllBtn
                objectName: "runAllButton"
                variant: "primary"
                size: "lg"
                iconKind: "wave"
                text: qsTr("Tạo tất cả")
                enabled: root.available && batchController.hasPending
                         && !batchController.running
                onClicked: batchController.runAll()
            }

            AppButton {
                objectName: "batchCancelButton"
                variant: "danger"
                size: "sm"
                text: qsTr("Hủy")
                visible: root.available && batchController.running
                onClicked: batchController.cancel()
            }

            Item { Layout.fillWidth: true }

            Label {
                objectName: "batchRunSummary"
                visible: root.available && batchController.runAllTotal > 0
                text: qsTr("%1/%2 tệp").arg(batchController.runAllDone)
                    .arg(batchController.runAllTotal)
                color: Theme.textMuted
                font.family: Theme.fontFamily
                font.pixelSize: Theme.fontSizeSm
            }
        }
    }
}
```

- [ ] **Step 4: Mount on `ParagraphTab.qml`**

Three edits to `src/vienetts_app/ui/qml/ParagraphTab.qml`:

1. Add `handleDroppedUrls` next to `importPath` and route the `DropArea` through it:

```qml
    // Shared drop router: ONE url keeps today's editor-import behavior;
    // several urls feed the batch queue instead.
    function handleDroppedUrls(urls) {
        const paths = [];
        for (let i = 0; i < urls.length; i++)
            paths.push(root.toLocalPath(urls[i]));
        if (paths.length === 1) {
            root.importPath(paths[0]);
            return;
        }
        if (typeof batchController !== "undefined" && batchController
            && typeof batchController.addFiles === "function")
            batchController.addFiles(paths);
    }
```

and in `DropArea`:

```qml
                        onDropped: if (drop.hasUrls && drop.urls.length > 0) {
                            root.dragOver = false;
                            root.handleDroppedUrls(drop.urls);
                        }
```

2. Keep the queue's voice in sync with the tab picker (place beside the existing `Connections { target: controller … }`):

```qml
    Connections {
        target: voicePicker

        function onSelectedVoiceChanged() {
            if (typeof batchController !== "undefined" && batchController)
                batchController.renderVoice = voicePicker.selectedVoice;
        }
    }
```

3. Mount the card between the editor `AppCard` and the "Giọng đọc & Tổng hợp" `AppCard`:

```qml
        // ── Multi-file Batch Queue Card ────────────────────────────────────
        BatchQueueCard {
            Layout.fillWidth: true
        }
```

(`components/BatchQueueCard.qml` resolves through the file's existing `import "components"`.)

- [ ] **Step 5: Run smoke + unit suites**

Run: `.venv/bin/python -m pytest tests/smoke/test_ui_tabs.py -q -k paragraph`
Expected: PASS including the new `para_batch` scenario.
Run: `.venv/bin/python -m pytest tests/unit/test_batch_controller.py tests/smoke/test_ui_shell.py -q`
Expected: PASS.

- [ ] **Step 6: Suggested commit**

```bash
git add src/vienetts_app/ui/qml/components/BatchQueueCard.qml src/vienetts_app/ui/qml/ParagraphTab.qml tests/smoke/test_ui_tabs.py
git commit -m "feat(batch): BatchQueueCard on the Paragraph tab with multi-file drop routing"
```

---

### Task 7: i18n — extract new strings, fill English translations

**Files:**
- Modify: `src/vienetts_app/ui/i18n/vienetts_en.ts` (regenerated + filled), `src/vienetts_app/ui/i18n/vienetts_en.qm` (recompiled)

**Interfaces:**
- Consumes: new `qsTr` strings from `BatchQueueCard.qml` + `ParagraphTab.qml` (contexts `BatchQueueCard`, `ParagraphTab`) and `self.tr` strings from `BatchFileController` (context `BatchFileController`).
- Produces: complete English catalog so `test_i18n.py` stays green.

- [ ] **Step 1: Regenerate the .ts**

Run: `.venv/bin/pyside6-lupdate src/vienetts_app -ts src/vienetts_app/ui/i18n/vienetts_en.ts -no-obsolete`
Expected: new entries under `BatchQueueCard`, `ParagraphTab`, `BatchFileController` contexts with `type="unfinished"`.

Known gotchas (project memory): function-wrapped `qsTr` calls are NOT extracted unless the binding reads a NOTIFY property — `statusInfo()` is called from delegate bindings that re-evaluate on `itemsChanged`, and all footer strings sit in direct bindings, so extraction is safe; verify `statusInfo`'s labels actually appear in the .ts before proceeding (if missing, hoist the switch into a `readonly property var` idiom).

- [ ] **Step 2: Fill English translations**

Edit the .ts unfinished entries (English renderings):

| Vietnamese source | English |
| --- | --- |
| Hàng đợi tệp | File queue |
| Chọn nhiều tệp để chạy tự động theo lượt — mỗi tệp lưu thành một WAV riêng. | Pick several files to run automatically in turn — each file saves its own WAV. |
| Thêm tệp… | Add files… |
| Xóa đã xong | Clear finished |
| Đang nhập | Importing |
| Chờ | Waiting |
| Đang tạo | Generating |
| Đang lưu | Saving |
| Sẵn sàng | Ready |
| Lỗi | Error |
| Tạo tất cả | Generate all |
| Hủy | Cancel |
| %1/%2 tệp | %1/%2 files |
| Chọn một hoặc nhiều tệp văn bản | Choose one or more text files |
| Chưa có tệp nào — dùng "Thêm tệp…" hoặc kéo thả nhiều tệp vào khung văn bản. | No files yet — use "Add files…" or drop several files onto the text box. |
| Phát / Dừng | Play / Stop |
| Mở thư mục | Open folder |
| Xóa khỏi hàng đợi | Remove from queue |
| Không hỗ trợ định dạng tệp: {} | Unsupported file type: {} |
| Không thể nhập tệp | Could not import file |
| Không thể tạo tác vụ tổng hợp. | Could not create a synthesis job. |
| Tổng hợp thất bại. | Synthesis failed. |
| Tệp âm thanh vừa tạo không hợp lệ. | The generated audio file is invalid. |
| Không thể lưu tệp âm thanh: {} | Could not save the audio file: {} |
| Hệ thống này không phát được âm thanh. | This system cannot play audio. |
| Không mở được thư mục chứa tệp. | Could not open the file's folder. |
| (oversize message, same text as AppController's) | (reuse the existing English wording for the identical AppController entry) |

Mark each entry `type="finished"` by removing the attribute after filling (`<translation>text</translation>`).

- [ ] **Step 3: Recompile the .qm**

Run: `.venv/bin/pyside6-lrelease src/vienetts_app/ui/i18n/vienetts_en.ts -qm src/vienetts_app/ui/i18n/vienetts_en.qm`

- [ ] **Step 4: Run the i18n tests + full suites**

Run: `.venv/bin/python -m pytest tests/unit/test_i18n.py -q`
Expected: PASS (fill any contract the test pins — e.g. source/translation parity — per its failure output).
Run: `.venv/bin/python -m pytest tests/unit/test_batch_controller.py tests/smoke -q`
Expected: PASS.

- [ ] **Step 5: Suggested commit**

```bash
git add src/vienetts_app/ui/i18n/vienetts_en.ts src/vienetts_app/ui/i18n/vienetts_en.qm
git commit -m "i18n(batch): English translations for the multi-file queue strings"
```

---

## Final validation (after Task 7)

Run the full suite and update the bead:

```bash
.venv/bin/python -m pytest -q
bd update VieNeuTTSApp-qef --status in_progress   # while working
bd close VieNeuTTSApp-qef --reason="Multi-file batch queue shipped: BatchFileController + BatchQueueCard, sequential auto-run, per-file auto-export"   # when done + green
```

Manual sanity check (optional, windowed): `.venv/bin/python -m vienetts_app`, Paragraph tab → "Thêm tệp…" → pick 2+ .txt files → "Tạo tất cả" → watch per-file status/progress → confirm WAVs appear in the output folder named after each source file; multi-file drag-and-drop does the same; "Hủy" mid-run leaves remaining items pending.

## Self-review notes

- Spec coverage: queue population (Task 1), sequential run + auto-advance + oversize + cancel (Task 2), auto-export naming/collision/artifact cleanup/export-failure (Task 3), play/reveal (Task 4), wiring (Task 5), UI + drop routing + dialog (Task 6), i18n (Task 7). Non-goals respected — no parallelism, no persistence, no per-file voices, no AppController/core edits.
- Type consistency: `BatchItem.uid` is the stable identity across bg callbacks (indices shift on removal); QML rows call index-based slots which resolve at click time; listener events match on `job_id` equality with `self._job_id` only.
- The spec's property list gains `currentIndex`, `hasPending`, `playingIndex` (additive, QML-facing refinements of "current item's progress" and per-row actions).
