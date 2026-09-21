"""SubtitleController: QML-facing SRT dub/transcript studio.

Registered by app.py as the QML context property ``subtitleController``. Owns the
subtitle workspace (:mod:`core.subtitle_project`) and reuses the app's engine
through AppController's synthesis-listener seam — one worker, one model, no
second load — exactly like AudiobookController and BatchFileController.

How a render works
------------------
The parsed cues are grouped into *speech units* (:func:`core.align.speech_units`):
one unit per cue, or whole sentences when ``mergeSentences`` is on. Units are
synthesized sequentially through the listener seam; each finished unit's WAV is
read back, split across its cues when merged, planned forward and streamed into
``track.wav`` by :class:`core.subtitle_project.SubtitleTrackRenderer`. Nothing
holds the whole track in RAM, so a two-hour film is fine.

Two fit policies (see :mod:`core.align`):

``dub``         SRT clock is master — compress an overlong take up to
                ``rateCap``, push later cues on overflow. Export the WAV and
                the retimed SRT to mux back onto the video.
``transcript``  voice is master — never compress, reproduce the subtitle's
                pauses capped at ``maxGapMs``. Cue times drift later; synced
                playback highlights from the *measured* timeline, so the
                highlight stays correct.

Changing any policy knob changes the render fingerprint, so the next ``render``
re-renders; an unchanged fingerprint reuses the cached ``track.wav``
(NFR-A1: never re-synthesize what is cached).

QML surface (context property ``subtitleController``):
    loaded bool                    title / sourcePath str
    cueCount int                   cues QVariantList [{index,startMs,endMs,
                                   startLabel,endLabel,text}]
    activeCue int (-1 = none)      activeCharStart / activeCharEnd int
    mode "dub"|"transcript" (rw)   rateCap float (rw)
    maxGapMs int (rw)              offsetMs int (rw)
    mergeSentences bool (rw)       voice str (rw; "" → app defaultVoice)
    policySummary QVariantMap
    rendering bool                 renderProgress float 0..1
    rendered bool                  durationMs int
    statsSummary str
    playerState "stopped"|"playing"|"paused"
    positionMs int
    exporting bool
    errorText str
    importSrt(path)->bool   render()   cancelRender()   clear()
    play() pause() resume() stopPlay() seek(ms) seekToCue(index)
    exportTrack(dir)->str   exportSrt(dir)->str   shutdown()

Controller error copy is Vietnamese (UI language); engine/store passthrough
messages surface verbatim.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

from PySide6.QtCore import Property, QObject, Signal, Slot

from vienetts_app.core.align import (
    MODE_DUB,
    MODE_TRANSCRIPT,
    MODES,
    FitPolicy,
    clamp_rate_cap,
    policy_summary,
    speech_units,
    split_unit_audio,
)
from vienetts_app.core.artifacts import SynthesisArtifact
from vienetts_app.core.audio import DEFAULT_SAMPLE_RATE, export_audio_file, read_wav
from vienetts_app.core.paths import normalize_local_path, sanitize_filename
from vienetts_app.core.subtitle_project import (
    SubtitleProject,
    SubtitleProjectError,
    SubtitleProjectStore,
    SubtitleTrackRenderer,
    build_project,
    export_srt_file,
    render_fingerprint,
)
from vienetts_app.core.subtitles import (
    SubtitleError,
    cues_text,
    format_timestamp,
    read_cues,
)
from vienetts_app.core.synthesis_context import SynthesisContext
from vienetts_app.core.timeline import (
    Timeline,
    active_word,
    locate_segment,
    word_spans,
)
from vienetts_app.ui.bg_ops import run_on_thread_pool
from vienetts_app.ui.playback import PlaybackController

logger = logging.getLogger(__name__)


def _default_player_factory() -> PlaybackController:
    return PlaybackController()


def _default_store_factory(data_dir: Path) -> SubtitleProjectStore:
    return SubtitleProjectStore(Path(data_dir) / "subtitles")


class SubtitleController(QObject):
    """SRT timeline studio exposed to QML; dependencies injectable."""

    loadedChanged = Signal()
    titleChanged = Signal()
    sourcePathChanged = Signal()
    cuesChanged = Signal()
    activeCueChanged = Signal()
    activeSpanChanged = Signal()
    renderingChanged = Signal()
    renderProgressChanged = Signal()
    renderedChanged = Signal()
    durationChanged = Signal()
    statsChanged = Signal()
    playerStateChanged = Signal()
    positionMsChanged = Signal()
    modeChanged = Signal()
    rateCapChanged = Signal()
    maxGapMsChanged = Signal()
    offsetMsChanged = Signal()
    mergeSentencesChanged = Signal()
    voiceChanged = Signal()
    policyChanged = Signal()
    exportingChanged = Signal()
    errorTextChanged = Signal()
    exportFinished = Signal(str, str)  # path, error ("" = ok)

    def __init__(
        self,
        app_controller: Any,
        *,
        data_dir: Path | None = None,
        player_factory: Callable[[], PlaybackController] | None = None,
        store_factory: Callable[[Path], SubtitleProjectStore] | None = None,
        bg_runner: Callable[..., None] | None = None,
    ) -> None:
        super().__init__()
        from vienetts_app.core.settings import default_data_dir

        self._app = app_controller
        self._data_dir = default_data_dir() if data_dir is None else Path(data_dir)
        factory = _default_store_factory if store_factory is None else store_factory
        self._store = factory(self._data_dir)
        player_factory = _default_player_factory if player_factory is None else player_factory
        self._player = player_factory()
        self._run_bg = bg_runner if bg_runner is not None else run_on_thread_pool

        self._project: SubtitleProject | None = None
        self._renderer: SubtitleTrackRenderer | None = None
        self._units: list[tuple[int, ...]] = []
        self._unit_index = 0
        self._job_id: str | None = None
        self._rendering = False
        self._render_progress = 0.0
        self._unit_progress = 0.0
        self._play_after_render = False
        self._error_text = ""
        self._exporting = False
        # Invalidated by clear(): a stale export callback for a discarded
        # project must not emit exportFinished or touch the error text.
        self._export_generation = 0

        self._mode = MODE_DUB
        self._rate_cap = 1.5
        self._max_gap_ms = 0
        self._offset_ms = 0
        self._merge_sentences = False
        self._voice = ""

        # Reader / karaoke state (mirrors AudiobookController's sync reader).
        self._timeline: Timeline | None = None
        self._words: list[tuple[int, int]] = []
        self._word_starts: list[int] = []
        self._active_cue = -1
        self._active_char_start = -1
        self._active_char_end = -1

        self._player_state = "stopped"
        self._position_ms = 0
        self._cues_cache: list[dict[str, Any]] | None = None

        self._wire_player()

    # ── player wiring ────────────────────────────────────────────────────────

    def _wire_player(self) -> None:
        with contextlib.suppress(Exception):
            self._player.stateChanged.connect(self._on_player_state_changed)
            self._player.finished.connect(self._on_player_finished)
            self._player.positionChanged.connect(self._on_player_position)
            self._player.durationChanged.connect(self._on_player_duration)
            self._player.errorTextChanged.connect(self._on_player_error)

    def _on_player_state_changed(self) -> None:
        state = str(getattr(self._player, "state", "stopped") or "stopped")
        if state != self._player_state:
            self._player_state = state
            self.playerStateChanged.emit()
        if state in ("stopped", "paused"):
            self._reset_active_span()

    def _on_player_position(self, ms: int) -> None:
        if ms != self._position_ms:
            self._position_ms = ms
            self.positionMsChanged.emit()
        self._update_active_span()

    def _on_player_duration(self, ms: int) -> None:
        # Duration comes from the rendered project (measured at render time);
        # the player's value only confirms it and needs no state change.
        return

    def _on_player_error(self) -> None:
        message = str(getattr(self._player, "errorText", "") or "")
        if message:
            self._set_error(message)

    def _on_player_finished(self) -> None:
        self._reset_active_span()

    # ── model ────────────────────────────────────────────────────────────────

    def _cues_model(self) -> list[dict[str, Any]]:
        if self._cues_cache is None:
            project = self._project
            cues = project.cues if project is not None else ()
            self._cues_cache = [
                {
                    "index": cue.index,
                    "startMs": cue.start_ms,
                    "endMs": cue.end_ms,
                    "startLabel": format_timestamp(cue.start_ms),
                    "endLabel": format_timestamp(cue.end_ms),
                    "text": cue.text,
                }
                for cue in cues
            ]
        return self._cues_cache

    def _emit_cues(self) -> None:
        self._cues_cache = None
        self.cuesChanged.emit()

    def _set_error(self, message: str) -> None:
        if message != self._error_text:
            self._error_text = message
            self.errorTextChanged.emit()

    # ── properties ───────────────────────────────────────────────────────────

    @Property(bool, notify=loadedChanged)
    def loaded(self) -> bool:
        return self._project is not None

    @Property(str, notify=titleChanged)
    def title(self) -> str:
        return "" if self._project is None else self._project.title

    @Property(str, notify=sourcePathChanged)
    def sourcePath(self) -> str:
        return "" if self._project is None else self._project.source_path

    @Property(int, notify=cuesChanged)
    def cueCount(self) -> int:
        return 0 if self._project is None else self._project.cue_count

    @Property("QVariantList", notify=cuesChanged)
    def cues(self) -> list[dict[str, Any]]:
        return self._cues_model()

    @Property(int, notify=activeCueChanged)
    def activeCue(self) -> int:
        return self._active_cue

    @Property(int, notify=activeSpanChanged)
    def activeCharStart(self) -> int:
        return self._active_char_start

    @Property(int, notify=activeSpanChanged)
    def activeCharEnd(self) -> int:
        return self._active_char_end

    @Property(bool, notify=renderingChanged)
    def rendering(self) -> bool:
        return self._rendering

    @Property(float, notify=renderProgressChanged)
    def renderProgress(self) -> float:
        return self._render_progress

    @Property(bool, notify=renderedChanged)
    def rendered(self) -> bool:
        project = self._project
        return project is not None and project.rendered and self._store.has_track(project.id)

    @Property(int, notify=durationChanged)
    def durationMs(self) -> int:
        return 0 if self._project is None else self._project.total_ms

    @Property(str, notify=statsChanged)
    def statsSummary(self) -> str:
        stats = None if self._project is None else self._project.stats
        if stats is None:
            return ""
        parts = [self.tr("{n} phụ đề").format(n=stats.cues)]
        if stats.compressed:
            parts.append(
                self.tr("{n} nén (tối đa {rate}×)").format(
                    n=stats.compressed, rate=f"{stats.max_rate:.2f}".rstrip("0").rstrip(".")
                )
            )
        if stats.overflowed:
            parts.append(
                self.tr("{n} tràn (tối đa {ms} ms)").format(
                    n=stats.overflowed, ms=stats.max_overflow_ms
                )
            )
        if stats.pushed:
            parts.append(self.tr("{n} đẩy lùi").format(n=stats.pushed))
        return " · ".join(parts)

    @Property(str, notify=playerStateChanged)
    def playerState(self) -> str:
        return self._player_state

    @Property(int, notify=positionMsChanged)
    def positionMs(self) -> int:
        return self._position_ms

    @Property(str, notify=modeChanged)
    def mode(self) -> str:
        return self._mode

    @mode.setter
    def mode(self, value: str) -> None:  # noqa: F811
        value = value if value in MODES else MODE_DUB
        if value == self._mode:
            return
        self._mode = value
        if value == MODE_TRANSCRIPT and not self._merge_sentences:
            # Transcript mode has no rate knob; keep the effective policy honest.
            self._merge_sentences = True
            self.mergeSentencesChanged.emit()
        self.modeChanged.emit()
        self.policyChanged.emit()
        self._rebuild_for_policy()

    @Property(float, notify=rateCapChanged)
    def rateCap(self) -> float:
        return self._rate_cap

    @rateCap.setter
    def rateCap(self, value: float) -> None:  # noqa: F811
        value = clamp_rate_cap(value)
        if value == self._rate_cap:
            return
        self._rate_cap = value
        self.rateCapChanged.emit()
        self.policyChanged.emit()
        self._rebuild_for_policy()

    @Property(int, notify=maxGapMsChanged)
    def maxGapMs(self) -> int:
        return self._max_gap_ms

    @maxGapMs.setter
    def maxGapMs(self, value: int) -> None:  # noqa: F811
        try:
            value = max(0, min(60_000, int(value)))
        except (TypeError, ValueError):
            return
        if value == self._max_gap_ms:
            return
        self._max_gap_ms = value
        self.maxGapMsChanged.emit()
        self.policyChanged.emit()
        self._rebuild_for_policy()

    @Property(int, notify=offsetMsChanged)
    def offsetMs(self) -> int:
        return self._offset_ms

    @offsetMs.setter
    def offsetMs(self, value: int) -> None:  # noqa: F811
        try:
            value = max(-600_000, min(600_000, int(value)))
        except (TypeError, ValueError):
            return
        if value == self._offset_ms:
            return
        self._offset_ms = value
        self.offsetMsChanged.emit()
        self.policyChanged.emit()
        self._rebuild_for_policy()

    @Property(bool, notify=mergeSentencesChanged)
    def mergeSentences(self) -> bool:
        return self._merge_sentences

    @mergeSentences.setter
    def mergeSentences(self, value: bool) -> None:  # noqa: F811
        value = bool(value)
        if value == self._merge_sentences:
            return
        self._merge_sentences = value
        self.mergeSentencesChanged.emit()
        self.policyChanged.emit()
        self._rebuild_for_policy()

    @Property(str, notify=voiceChanged)
    def voice(self) -> str:
        return self._voice

    @voice.setter
    def voice(self, value: str) -> None:  # noqa: F811
        value = value or ""
        if value == self._voice:
            return
        self._voice = value
        self.voiceChanged.emit()
        self.policyChanged.emit()
        self._rebuild_for_policy()

    @Property("QVariantMap", notify=policyChanged)
    def policySummary(self) -> dict[str, Any]:
        return policy_summary(self._fit_policy())

    @Property(bool, notify=exportingChanged)
    def exporting(self) -> bool:
        return self._exporting

    @Property(str, notify=errorTextChanged)
    def errorText(self) -> str:
        return self._error_text

    # ── policy ───────────────────────────────────────────────────────────────

    def _fit_policy(self) -> FitPolicy:
        if self._mode == MODE_TRANSCRIPT:
            return FitPolicy.transcript(
                offset_ms=self._offset_ms,
                max_gap_ms=self._max_gap_ms,
                merge_sentences=self._merge_sentences,
            )
        return FitPolicy.dub(
            rate_cap=self._rate_cap,
            offset_ms=self._offset_ms,
            max_gap_ms=self._max_gap_ms,
            merge_sentences=self._merge_sentences,
        )

    def _effective_voice(self) -> str:
        return self._voice or str(getattr(self._app, "defaultVoice", "") or "")

    # ── engine identity (Phase 5 Task 5.3) ──────────────────────────────────

    def _probe_context(self) -> tuple[SynthesisContext | None, bool]:
        """The identity this project would render with: ``(context, refused)``.

        Read-only (``report=False``): a refusal is the render's to report, not
        a knob rebuild's. ``(None, False)`` when the app has no identity seam
        (a bare fake app, smoke scenarios — the track is then fingerprinted
        without one, exactly as before provenance existed).
        """
        probe = getattr(self._app, "submission_context_for", None)
        if not callable(probe):
            return None, False
        try:
            context = probe(self._effective_voice(), report=False)
        except TypeError:  # an app double with the older one-argument seam
            logger.debug("app seam has no read-only context probe", exc_info=True)
            return None, False
        return context, context is None

    def _submission_context(self) -> tuple[SynthesisContext | None, bool]:
        """The identity for a render: ``(context, refused)``, reporting refusals."""
        probe = getattr(self._app, "submission_context_for", None)
        if not callable(probe):
            return None, False
        try:
            context = probe(self._effective_voice())
        except TypeError:  # an app double with the older one-argument seam
            logger.debug("app seam has no context gate", exc_info=True)
            return None, False
        return context, context is None

    def _project_with_context(
        self, project: SubtitleProject, context: SynthesisContext | None
    ) -> SubtitleProject:
        """Re-derive ``project`` for ``context`` (a fresh render identity).

        The fingerprint must follow the CURRENT profile/language/voice, or a
        cached track could be adopted for an engine that never produced it.
        """
        if context is None and project.context is None:
            return project
        return replace(
            project,
            context=context,
            fingerprint=render_fingerprint(
                project.cues,
                project.policy,
                project.sample_rate,
                self._effective_voice(),
                context,
            ),
        )

    def _rebuild_for_policy(self) -> None:
        """Re-derive the project from the current knobs (a new fingerprint).

        When the rebuilt fingerprint matches a stored render — e.g. a knob
        moved away and back — the stored project (stats, adjusted cues,
        timeline) is adopted again instead of staying on the fresh one.
        """
        if self._project is None or self._rendering:
            return
        context, refused = self._probe_context()
        candidate = build_project(
            self._project.source_path,
            self._project.cues,
            self._fit_policy(),
            sample_rate=self._project.sample_rate,
            voice_key=self._effective_voice(),
            title=self._project.title,
            created_at=self._project.created_at,
            context=context,
        )
        # A refused combination adopts nothing: its cache belongs to an engine
        # the active profile cannot vouch for.
        cached = None if refused else self._store.cached_render(candidate)
        self._project = cached or candidate
        self._load_reader()  # restores the measured timeline on a cache hit
        if self._render_progress != 0.0:
            self._render_progress = 0.0
            self.renderProgressChanged.emit()
        self._emit_cues()
        self.renderedChanged.emit()
        self.durationChanged.emit()
        self.statsChanged.emit()

    # ── import ───────────────────────────────────────────────────────────────

    @Slot(str, result=bool)
    def importSrt(self, path: str) -> bool:  # type: ignore[override]
        """Parse an .srt into a workspace; returns False with ``errorText`` set."""
        source = normalize_local_path(path)
        if not str(source) or not source.is_file():
            self._set_error(self.tr("Không tìm thấy tệp phụ đề: {name}").format(name=source.name))
            return False
        self._stop_render()
        self._stop_playback()
        try:
            cues = read_cues(source)
        except SubtitleError as exc:
            self._set_error(str(exc))
            return False
        except OSError as exc:
            self._set_error(
                self.tr("Không đọc được tệp phụ đề '{name}': {error}").format(
                    name=source.name, error=exc
                )
            )
            return False
        if not cues:
            self._set_error(
                self.tr(
                    "Tệp '{name}' không có phụ đề nào đọc được. "
                    "Hãy kiểm tra định dạng SubRip (.srt)."
                ).format(name=source.name)
            )
            return False
        try:
            context, refused = self._probe_context()
            project = build_project(
                source,
                cues,
                self._fit_policy(),
                sample_rate=DEFAULT_SAMPLE_RATE,
                voice_key=self._effective_voice(),
                context=context,
            )
            # Re-importing a file whose render is already on disk adopts the
            # stored project (stats, adjusted cues, timeline) instead of
            # saving an unrendered one over it. A refused combination neither
            # adopts nor overwrites: the stored render is another engine's.
            cached = None if refused else self._store.cached_render(project)
            if cached is not None:
                project = cached
            elif not refused:
                self._store.save(project)
        except SubtitleProjectError as exc:
            self._set_error(str(exc))
            return False
        self._project = project
        self._load_reader()
        self._reset_active_span()
        self._position_ms = 0
        self.positionMsChanged.emit()
        self._set_error("")
        self.loadedChanged.emit()
        self.titleChanged.emit()
        self.sourcePathChanged.emit()
        self._emit_cues()
        self.renderedChanged.emit()
        self.durationChanged.emit()
        self.statsChanged.emit()
        self.renderProgressChanged.emit()
        return True

    def _load_reader(self) -> None:
        project = self._project
        if project is None:
            self._timeline = None
            self._words = []
            self._word_starts = []
            return
        text = cues_text(project.cues)
        self._words = word_spans(text)
        self._word_starts = [span[0] for span in self._words]
        # The project id is content-derived, so an unrendered fingerprint can
        # still map to the previous render's timeline on disk — never adopt it.
        self._timeline = self._store.load_timeline(project.id) if project.rendered else None

    # ── rendering ────────────────────────────────────────────────────────────

    @Slot()
    def render(self) -> None:
        """Render the current project (cached tracks are reused).

        The engine identity is snapshotted here — the one place that produces
        audio — so a track rendered by another profile/language/voice is
        re-rendered instead of adopted, and an unsupported combination fails
        with the capability table's own reason.
        """
        project = self._project
        if project is None or self._rendering:
            return
        context, refused = self._submission_context()
        if refused:
            self._set_error(
                str(getattr(self._app, "errorText", "") or "")
                or self.tr("Không thể tạo tác vụ tổng hợp.")
            )
            return
        project = self._project_with_context(project, context)
        self._project = project
        cached = self._store.cached_render(project)
        if cached is not None:
            # A matching track exists: adopt the stored project (stats,
            # adjusted cues, measured timeline), never re-synthesize.
            self._project = cached
            self._load_reader()
            self._emit_cues()
            self.renderedChanged.emit()
            self.durationChanged.emit()
            self.statsChanged.emit()
            self._after_render_ready()
            return
        if project.rendered:
            # The stored track belongs to another identity: its measured
            # alignment is stale, so the surface reports unrendered until the
            # new track lands (the same posture as a policy change).
            project = replace(project, total_ms=0, stats=None, adjusted=())
            self._project = project
            self._load_reader()  # drops the previous track's timeline
            self.renderedChanged.emit()
            self.durationChanged.emit()
            self.statsChanged.emit()
        self._units = speech_units(project.cues, merge=project.policy.merge_sentences)
        self._unit_index = 0
        self._unit_progress = 0.0
        self._render_progress = 0.0
        renderer = SubtitleTrackRenderer(self._store, project)
        try:
            renderer.open()
        except Exception as exc:  # noqa: BLE001 - a raw OSError must never escape a slot
            logger.exception("subtitle render failed to open its track file")
            with contextlib.suppress(Exception):
                renderer.abort()
            self._set_error(self.tr("Không thể mở tệp ghi phụ đề: {error}").format(error=exc))
            return
        self._renderer = renderer
        self._rendering = True
        self._set_error("")
        self.renderingChanged.emit()
        self.renderProgressChanged.emit()
        self._submit_next_unit()

    @Slot()
    def cancelRender(self) -> None:
        self._play_after_render = False
        if self._job_id is not None:
            with contextlib.suppress(Exception):
                self._app.cancel_job(self._job_id)
            return
        if self._rendering:
            self._fail_render("")

    def _stop_render(self) -> None:
        self._play_after_render = False
        if self._job_id is not None:
            with contextlib.suppress(Exception):
                self._app.cancel_job(self._job_id)
            self._job_id = None
        if self._renderer is not None:
            try:
                self._renderer.abort()
            except Exception:  # noqa: BLE001 - cleanup must not wedge the controller
                logger.exception("subtitle render abort failed")
            self._renderer = None
        if self._rendering:
            self._rendering = False
            self._render_progress = 0.0
            self.renderingChanged.emit()
            self.renderProgressChanged.emit()

    def _submit_next_unit(self) -> None:
        project = self._project
        if project is None or self._renderer is None:
            self._fail_render(self.tr("Không thể tạo tác vụ tổng hợp."))
            return
        if self._unit_index >= len(self._units):
            self._finish_render()
            return
        unit = self._units[self._unit_index]
        text = " ".join(project.cues[index].text for index in unit).strip()
        submit = getattr(self._app, "submit_stream_for_listener", None)
        if not callable(submit):
            self._fail_render(self.tr("Không thể tạo tác vụ tổng hợp."))
            return
        context = project.context
        try:
            if context is None:
                job_id = submit(text, self._effective_voice() or None, self, kind="bulk")
            else:
                job_id = submit(
                    text, self._effective_voice() or None, self, kind="bulk", context=context
                )
        except Exception as exc:  # noqa: BLE001 - engine seam errors are render failures
            logger.exception("submitting a subtitle synthesis unit failed")
            self._fail_render(self.tr("Không thể tạo tác vụ tổng hợp: {error}").format(error=exc))
            return
        if not job_id:
            self._fail_render(self.tr("Không thể tạo tác vụ tổng hợp."))
            return
        self._job_id = job_id
        self._unit_progress = 0.0

    # ── synthesis-listener contract (called by AppController) ────────────────

    def on_synthesis_progress(self, event: Any) -> None:
        if getattr(event, "job_id", None) != self._job_id:
            return
        total = int(getattr(event, "total", 0) or 0)
        done = int(getattr(event, "done", 0) or 0)
        self._unit_progress = (done / total) if total > 0 else 0.0
        self._emit_progress()

    def on_synthesis_chunk(self, event: Any) -> None:
        # Subtitle renders are file-bound; the live-audio lane is not used.
        return

    def on_synthesis_terminal(self, event: Any) -> None:
        job_id = getattr(event, "job_id", None)
        if job_id is None or job_id != self._job_id:
            self._release_artifact(getattr(event, "value", None))
            return
        self._job_id = None
        state = str(getattr(event, "state", "") or "")
        if state == "completed":
            self._consume_unit(getattr(event, "value", None), job_id)
        elif state == "cancelled":
            self._release_artifact(getattr(event, "value", None))
            self._fail_render("")  # user cancel: no error banner
        else:
            self._release_artifact(getattr(event, "value", None))
            message = str(getattr(event, "error", "") or "") or self.tr("Tổng hợp thất bại.")
            self._fail_render(message)

    def _consume_unit(self, artifact: Any, expected_job_id: str) -> None:
        project = self._project
        renderer = self._renderer
        if project is None or renderer is None:
            self._release_artifact(artifact)
            return
        unit = self._units[self._unit_index]
        if (
            not isinstance(artifact, SynthesisArtifact)
            or artifact.job_id != expected_job_id
            or not artifact.path.is_file()
            or artifact.samples <= 0
        ):
            self._release_artifact(artifact)
            self._fail_render(self.tr("Tệp âm thanh vừa tạo không hợp lệ."))
            return
        try:
            audio, sample_rate = read_wav(artifact.path)
        except Exception as exc:  # noqa: BLE001 - unreadable unit is a render failure
            logger.exception("reading synthesized subtitle unit failed")
            self._release_artifact(artifact)
            self._fail_render(self.tr("Không đọc được âm thanh vừa tạo: {error}").format(error=exc))
            return
        if sample_rate != project.sample_rate:
            self._release_artifact(artifact)
            self._fail_render(
                self.tr("Âm thanh vừa tạo có tần số lấy mẫu không hỗ trợ ({rate} Hz).").format(
                    rate=sample_rate
                )
            )
            return
        try:
            pieces = split_unit_audio(audio, unit, project.cues, sample_rate)
            for cue_index, piece in zip(unit, pieces, strict=True):
                renderer.add_clip(cue_index, piece)
        except SubtitleProjectError as exc:
            self._release_artifact(artifact)
            self._fail_render(str(exc))
            return
        except Exception as exc:  # noqa: BLE001 - split/place failures are render failures
            logger.exception("placing a synthesized subtitle unit failed")
            self._release_artifact(artifact)
            self._fail_render(
                self.tr("Không ghép được âm thanh vào phụ đề: {error}").format(error=exc)
            )
            return
        self._release_artifact(artifact)
        self._unit_index += 1
        self._unit_progress = 0.0
        self._emit_progress()
        self._submit_next_unit()

    def _emit_progress(self) -> None:
        total = len(self._units) or 1
        progress = min(1.0, (self._unit_index + self._unit_progress) / total)
        if progress != self._render_progress:
            self._render_progress = progress
            self.renderProgressChanged.emit()

    def _finish_render(self) -> None:
        renderer = self._renderer
        project = self._project
        if renderer is None or project is None:
            self._fail_render(self.tr("Tổng hợp thất bại."))
            return
        try:
            renderer.finish()
            # The renderer is done: a reload failure below must fail the
            # render WITHOUT aborting it — the promoted track is already live.
            self._renderer = None
            self._project = self._store.require(project.id)
        except SubtitleProjectError as exc:
            self._fail_render(str(exc))
            return
        except Exception as exc:  # noqa: BLE001 - a raw OSError must never escape a slot
            logger.exception("finishing the subtitle track failed")
            self._fail_render(
                self.tr("Không thể hoàn tất tệp phụ đề âm thanh: {error}").format(error=exc)
            )
            return
        self._load_reader()
        self._rendering = False
        self._render_progress = 1.0
        self.renderingChanged.emit()
        self.renderProgressChanged.emit()
        self._emit_cues()
        self.renderedChanged.emit()
        self.durationChanged.emit()
        self.statsChanged.emit()
        self._after_render_ready()

    def _fail_render(self, message: str) -> None:
        if self._renderer is not None:
            try:
                self._renderer.abort()
            except Exception:  # noqa: BLE001 - cleanup must not wedge the controller
                logger.exception("subtitle render abort failed")
            self._renderer = None
        self._job_id = None
        self._play_after_render = False
        self._rendering = False
        self._render_progress = 0.0
        self.renderingChanged.emit()
        self.renderProgressChanged.emit()
        if message:
            self._set_error(message)

    def _after_render_ready(self) -> None:
        self.renderedChanged.emit()
        if self._play_after_render:
            self._play_after_render = False
            self._play_track()

    def _release_artifact(self, value: Any) -> None:
        """Delete an interactive-store artifact WAV — only under OUR data dir."""
        if not isinstance(value, SynthesisArtifact):
            return
        try:
            path = value.path.resolve()
            root = self._data_dir.resolve()
            if root == path or root not in path.parents:
                return
            path.unlink(missing_ok=True)
        except OSError:
            logger.warning("could not release subtitle artifact %s", value.path)

    # ── playback + karaoke ───────────────────────────────────────────────────

    @Slot()
    def play(self) -> None:
        project = self._project
        if project is None:
            return
        if self._rendering:
            self._play_after_render = True
            return
        cached = self._store.cached_render(project)
        if cached is None:
            self._play_after_render = True
            self.render()
            return
        self._project = cached
        if self._timeline is None:
            self._load_reader()
        self._play_track()

    def _play_track(self) -> None:
        project = self._project
        if project is None:
            return
        wav = self._store.wav_path(project.id)
        if not wav.is_file():
            self._set_error(self.tr("Chưa có tệp âm thanh để phát."))
            return
        self._reset_active_span()
        try:
            self._player.play(str(wav))
        except Exception:  # noqa: BLE001 - playback must never crash the UI
            logger.exception("subtitle playback failed")
            self._set_error(self.tr("Hệ thống này không phát được âm thanh."))

    @Slot()
    def pause(self) -> None:
        with contextlib.suppress(Exception):
            self._player.pause()

    @Slot()
    def resume(self) -> None:
        with contextlib.suppress(Exception):
            self._player.resume()

    @Slot()
    def stopPlay(self) -> None:
        self._stop_playback()

    def _stop_playback(self) -> None:
        with contextlib.suppress(Exception):
            self._player.stop()
        self._reset_active_span()

    @Slot(int)
    def seek(self, ms: int) -> None:
        with contextlib.suppress(Exception):
            self._player.seek(int(ms))

    @Slot(int)
    def seekToCue(self, index: int) -> None:
        project = self._project
        if project is None or self._timeline is None or not 0 <= index < len(project.cues):
            return
        if index >= len(self._timeline.segments):
            return
        segment = self._timeline.segments[index]
        if segment.end_ms <= segment.start_ms:
            return
        self.seek(segment.start_ms)

    def _reset_active_span(self) -> None:
        # The cue list itself never changes here — delegates read the
        # highlight from ``activeCue``, so transitions emit no ``cuesChanged``.
        if self._active_cue != -1:
            self._active_cue = -1
            self.activeCueChanged.emit()
        if self._active_char_start != -1 or self._active_char_end != -1:
            self._active_char_start = -1
            self._active_char_end = -1
            self.activeSpanChanged.emit()

    def _update_active_span(self) -> None:
        project = self._project
        if project is None or self._timeline is None:
            self._reset_active_span()
            return
        span_index = locate_segment(self._timeline, self._position_ms)
        if span_index < 0 or span_index >= len(project.cues):
            self._reset_active_span()
            return
        segment = self._timeline.segments[span_index]
        if (
            segment.char_start < 0
            or segment.end_ms <= segment.start_ms
            or not segment.start_ms <= self._position_ms < segment.end_ms
        ):
            # locate_segment clamps to the nearest segment — a position inside
            # rendered silence between cues means NO cue is active.
            self._reset_active_span()
            return
        fraction = (self._position_ms - segment.start_ms) / (segment.end_ms - segment.start_ms)
        fraction = min(1.0, max(0.0, fraction))
        char_index = round(segment.char_start + fraction * (segment.char_end - segment.char_start))
        word_start, word_end = active_word(self._words, char_index, starts=self._word_starts)
        if word_start < 0:
            self._reset_active_span()
            return
        if span_index != self._active_cue:
            self._active_cue = span_index
            self.activeCueChanged.emit()
        if (word_start, word_end) != (self._active_char_start, self._active_char_end):
            self._active_char_start = word_start
            self._active_char_end = word_end
            self.activeSpanChanged.emit()

    # ── export ───────────────────────────────────────────────────────────────

    @Slot(str, result=str)
    def exportTrack(self, dest_dir: str) -> str:  # type: ignore[override]
        """Export ``track.wav`` off-thread; ``exportFinished(path, error)`` lands later."""
        project = self._project
        if project is None or self._rendering or self._exporting:
            return ""
        if not self.rendered:
            self._set_error(self.tr("Chưa có tệp âm thanh để xuất. Hãy tạo trước."))
            return ""
        target = self._export_target(dest_dir, project.title, self._export_extension())
        source = self._store.wav_path(project.id)
        self._export_generation += 1
        generation = self._export_generation
        self._exporting = True
        self.exportingChanged.emit()

        def work() -> str:
            export_audio_file(source, target)
            return str(target)

        def done(path: str) -> None:
            if not self._end_export(generation):
                return
            self.exportFinished.emit(path, "")

        def failed(exc: BaseException) -> None:
            if not self._end_export(generation):
                return
            self._set_error(self.tr("Không thể xuất tệp âm thanh: {error}").format(error=exc))
            self.exportFinished.emit("", str(exc))

        try:
            self._run_bg(work, done, self, on_error=failed)
        except Exception as exc:  # noqa: BLE001 - a rejected pool must not wedge exporting
            failed(exc)
            return ""
        return str(target)

    @Slot(str, result=str)
    def exportSrt(self, dest_dir: str) -> str:  # type: ignore[override]
        """Export the retimed SRT off-thread; ``exportFinished(path, error)`` lands later.

        Refuses before a render exists — the adjusted cues (and therefore a
        faithful retimed file) only exist once a render lands.
        """
        project = self._project
        if project is None or self._rendering or self._exporting:
            return ""
        if not self.rendered:
            self._set_error(self.tr("Chưa có phụ đề đã chỉnh để xuất. Hãy tạo trước."))
            return ""
        cues = project.adjusted
        target = self._export_target(dest_dir, project.title, ".srt")
        self._export_generation += 1
        generation = self._export_generation
        self._exporting = True
        self.exportingChanged.emit()

        def work() -> str:
            export_srt_file(target, cues)
            return str(target)

        def done(path: str) -> None:
            if not self._end_export(generation):
                return
            self.exportFinished.emit(path, "")

        def failed(exc: BaseException) -> None:
            if not self._end_export(generation):
                return
            self._set_error(self.tr("Không thể xuất tệp phụ đề: {error}").format(error=exc))
            self.exportFinished.emit("", str(exc))

        try:
            self._run_bg(work, done, self, on_error=failed)
        except Exception as exc:  # noqa: BLE001 - a rejected pool must not wedge exporting
            failed(exc)
            return ""
        return str(target)

    def _end_export(self, generation: int) -> bool:
        """Unlatch ``_exporting``; False when the callback belongs to a cleared project."""
        if generation != self._export_generation:
            if self._exporting:
                self._exporting = False
                self.exportingChanged.emit()
            return False
        self._exporting = False
        self.exportingChanged.emit()
        return True

    def _export_extension(self) -> str:
        fmt = str(getattr(self._app, "exportFormat", "wav") or "wav").lower()
        return ".mp3" if fmt == "mp3" else ".wav"

    def _export_target(self, dest_dir: str, stem: str, extension: str) -> Path:
        base = str(dest_dir or "").strip()
        directory = normalize_local_path(base) if base else Path.home() / "Music" / "VieNeuTTS"
        with contextlib.suppress(OSError):
            directory.mkdir(parents=True, exist_ok=True)
        safe = sanitize_filename(stem, fallback="subtitles")
        candidate = directory / f"{safe}{extension}"
        suffix = 2
        while candidate.exists():
            candidate = directory / f"{safe}_{suffix}{extension}"
            suffix += 1
        return candidate

    # ── lifecycle ────────────────────────────────────────────────────────────

    @Slot()
    def clear(self) -> None:
        self._stop_render()
        self._stop_playback()
        # Invalidate any in-flight export's callbacks; the latch itself stays
        # held until that stale callback lands, so a new export cannot race
        # the old one's target.
        self._export_generation += 1
        self._project = None
        self._timeline = None
        self._words = []
        self._word_starts = []
        self._active_cue = -1
        self._active_char_start = -1
        self._active_char_end = -1
        self._position_ms = 0
        self._error_text = ""
        self.errorTextChanged.emit()
        self.loadedChanged.emit()
        self.titleChanged.emit()
        self.sourcePathChanged.emit()
        self._emit_cues()
        self.renderedChanged.emit()
        self.durationChanged.emit()
        self.statsChanged.emit()

    @Slot()
    def shutdown(self) -> None:
        self._stop_render()
        self._stop_playback()
