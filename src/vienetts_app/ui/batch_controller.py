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
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

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

    def _on_player_state(self) -> None:
        state = str(getattr(self._player, "state", "stopped") or "stopped")
        if state == "stopped" and self._playing_index != -1:
            self._set_playing_index(-1)

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
        if self._playing_index == -1:
            return  # nothing of ours is playing — no stray stop on the player
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
            ok = bool(reveal(Path(item.wav_path).parent))
        except Exception:  # noqa: BLE001
            logger.exception("reveal batch wav failed")
            ok = False
        if not ok:
            self._set_error(self.tr("Không mở được thư mục chứa tệp."))
        return ok

    @staticmethod
    def _default_reveal(directory: Path) -> bool:
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices

        return bool(QDesktopServices.openUrl(QUrl.fromLocalFile(str(directory))))

    def _set_playing_index(self, index: int) -> None:
        if index != self._playing_index:
            self._playing_index = index
            self.playingIndexChanged.emit()

    def _on_play_released(self) -> None:
        self._set_playing_index(-1)
