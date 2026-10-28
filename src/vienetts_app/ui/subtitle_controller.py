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
import datetime as _dt
import hashlib
import logging
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Final

from PySide6.QtCore import Q_ARG, Property, QMetaObject, QObject, Qt, QUrl, Signal, Slot

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
from vienetts_app.core.asr import AsrCancelled, AsrConfig, transcribe_media_to_cues
from vienetts_app.core.audio import DEFAULT_SAMPLE_RATE, export_audio_file, read_wav
from vienetts_app.core.diarization import (
    DiarizationCancelled,
    DiarizationConfig,
    diarize_cues,
    extract_speaker_reference_wav,
    refine_cues_speech_bounds,
    remap_speakers_to_previous,
)
from vienetts_app.core.llm_refine import LlmConfig, max_chars_for_window, refine_cues_with_llm
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
    Cue,
    SubtitleError,
    clean_cue_text,
    cues_text,
    format_srt,
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
from vienetts_app.core.video_dub import (
    VideoDubError,
    build_duck_spans,
    dub_overrun_ms,
    find_companion_srt,
    find_companion_video,
    is_video_path,
    mux_dubbed_video,
    supports_soft_subtitles,
)
from vienetts_app.ui.bg_ops import OrderedExecutor, SyncOrderedExecutor, run_on_thread_pool
from vienetts_app.ui.list_models import DictListModel
from vienetts_app.ui.playback import PlaybackController

logger = logging.getLogger(__name__)


# Controller properties with no QML binding. They are public Python surface for
# hosts and integrations (Studio bridges, tests), not dead code: the unit test
# ``test_every_property_is_consumed_or_api_only`` asserts this list stays exact.
#  - state: the canonical derived state machine (QML gates on the mutually
#    exclusive primitives that derive it; busyOwner is read by the dock).
#  - title / sourcePath / videoPath / cueCount / cues / policySummary: project
#    metadata for hosts (Studio opens a project by id, not by parsing QML).
#  - durationMs / positionMs / activeCharStart / activeCharEnd: transport
#    position API used by Python consumers; the card shows activeCue instead.
#  - invalidationReason / exportBlockedReason: machine-readable companions to
#    the human-readable invalidationMessage and export gating.
API_ONLY_PROPERTIES: Final[frozenset[str]] = frozenset(
    {
        "activeCharEnd",
        "activeCharStart",
        "cueCount",
        "cues",
        "durationMs",
        "exportBlockedReason",
        "invalidationReason",
        "policySummary",
        "positionMs",
        "sourcePath",
        "state",
        "title",
        "videoPath",
        "videoUrl",
    }
)


def _default_player_factory() -> PlaybackController:
    return PlaybackController()


def _default_store_factory(data_dir: Path) -> SubtitleProjectStore:
    return SubtitleProjectStore(Path(data_dir) / "subtitles")


# Keys of one `cues` row — the roles of `cueModel`.
CUE_ROLES = (
    "index",
    "startMs",
    "endMs",
    "startLabel",
    "endLabel",
    "text",
    "speaker",
    "voice",
    "fitRate",
    "overflowMs",
    "overBudget",
)

#: Transcript mode's real default: preserve pauses but cap them (FR-1.5).
TRANSCRIPT_DEFAULT_MAX_GAP_MS = 1500


@dataclass
class _PendingInvalidation:
    """A render-destroying change awaiting the user's confirmation (FR-1.1).

    The controller's own state is untouched while this is pending: a rejected
    change needs only the property's notify signal to snap the control back,
    and an accepted change runs ``apply`` exactly once.
    """

    reason: str
    message: str
    apply: Callable[[], None]
    revert_signals: tuple[Any, ...] = ()


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
    hintTextChanged = Signal()
    noticeLevelChanged = Signal()
    exportFinished = Signal(str, str)  # path, error ("" = ok)
    #: Burn-in re-encodes and can take minutes; this carries ffmpeg's fraction
    #: so the card shows a real bar instead of a silent freeze.
    exportProgressChanged = Signal()
    overrunChanged = Signal()
    videoChanged = Signal()
    videoSeekRequested = Signal(int)
    videoReleaseRequested = Signal()
    speakersChanged = Signal()
    aiBusyChanged = Signal()
    aiProgressChanged = Signal()
    #: A render-destroying change is staged and needs a user decision.
    renderInvalidationRequested = Signal(str, str)
    pendingInvalidationChanged = Signal()
    staleChanged = Signal()
    projectsChanged = Signal()
    stateChanged = Signal()
    busyOwnerChanged = Signal()

    def __init__(
        self,
        app_controller: Any,
        *,
        data_dir: Path | None = None,
        player_factory: Callable[[], PlaybackController] | None = None,
        store_factory: Callable[[Path], SubtitleProjectStore] | None = None,
        bg_runner: Callable[..., None] | None = None,
        unit_executor: OrderedExecutor | SyncOrderedExecutor | None = None,
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
        # Dub units are read → split → time-stretched → written on ONE ordered
        # worker (perf_hardening FR-2.4): the track is written cue by cue, so
        # placement must apply in submission order, off the GUI thread. The
        # render generation drops queued work of a cancelled/replaced render.
        self._units_exec = OrderedExecutor(self) if unit_executor is None else unit_executor
        self._render_generation = 0

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
        self._notice_level = "error"
        # FR-3.6: non-fatal feedback for actions that used to return silently.
        self._hint_text = ""
        self._exporting = False
        self._export_progress = 0.0
        # Invalidated by clear(): a stale export callback for a discarded
        # project must not emit exportFinished or touch the error text.
        self._export_generation = 0

        self._mode = MODE_DUB
        self._rate_cap = 1.5
        self._max_gap_ms = 0
        #: True once the user set the gap explicitly; until then transcript
        #: mode uses its real default instead of "uncapped" (FR-1.5).
        self._max_gap_ms_set = False
        self._offset_ms = 0
        self._merge_sentences = False
        self._voice = ""
        self._pending_video_path = ""
        self._video_mix_mode = "duck"
        self._video_bg_gain_db = -16.0
        self._embed_subtitles = True
        self._subtitle_embed_mode = "soft"
        self._ai_busy = False
        self._ai_stage_text = ""
        self._ai_progress = 0.0
        self._ai_generation = 0
        self._ai_cancel_flag = False
        # FR-1.1: the staged change (None when nothing awaits confirmation).
        self._pending_invalidation: _PendingInvalidation | None = None
        # FR-1.2: why the on-disk render no longer matches the current inputs.
        self._stale_reason = ""
        self._last_stats: Any = None
        self._last_stats_project_id = ""
        # FR-1.6: cue edits not yet re-rendered (guards a re-ASR overwrite).
        self._cues_dirty = False
        # FR-1.4: cancellation flag for the running export's worker.
        self._export_cancel = False

        # Reader / karaoke state (mirrors AudiobookController's sync reader).
        self._timeline: Timeline | None = None
        self._words: list[tuple[int, int]] = []
        self._word_starts: list[int] = []
        self._active_cue = -1
        self._active_char_start = -1
        self._active_char_end = -1

        self._player_state = "stopped"
        self._position_ms = 0
        # FR-4.6: probing ffprobe on every QML read (property bindings re-run
        # per frame in a few cases) blocked the GUI thread. Cache the answer
        # under a key that includes the wav's identity; invalidate on the two
        # events that can change it.
        self._overrun_cache: tuple[tuple[Any, ...], int] | None = None
        self._cues_cache: list[dict[str, Any]] | None = None
        # Row-level mirror of `cues` for the QML cue list (perf 6.4): a
        # re-import diffs by cue index instead of rebuilding every row.
        self._cue_list = DictListModel(CUE_ROLES, key="index", parent=self)

        # FR-2.2: one derived state drives every control's gating. It is
        # recomputed from the existing axes whenever one of them notifies.
        self._last_state = ""
        self._last_busy_owner = ""
        for signal in (
            self.loadedChanged,
            self.renderedChanged,
            self.staleChanged,
            self.renderingChanged,
            self.exportingChanged,
            self.aiBusyChanged,
            self.errorTextChanged,
            self.videoChanged,
        ):
            signal.connect(self._refresh_state)

        self.renderedChanged.connect(self._invalidate_overrun)
        self.videoChanged.connect(self._invalidate_overrun)

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
            spk_voices = project.speaker_voices if project is not None else {}
            rates = project.cue_rates if project is not None else ()
            adjusted = project.adjusted if project is not None else ()
            offset_ms = int(project.policy.offset_ms) if project is not None else 0
            rate_cap = float(project.policy.rate_cap) if project is not None else 1.5
            is_dub = (project.policy.mode == MODE_DUB) if project is not None else True
            raw_lang = str(getattr(self._app, "synthesisLanguage", "") or "vi").strip()
            lang = "vi" if raw_lang.lower() in ("", "auto") else raw_lang
            default_v = self._effective_voice()
            rows: list[dict[str, Any]] = []
            for i, cue in enumerate(cues):
                fit_rate = round(float(rates[i]), 2) if i < len(rates) else 1.0
                overflow_ms = (
                    max(0, int(adjusted[i].end_ms) - max(0, int(cue.end_ms) + offset_ms))
                    if i < len(adjusted)
                    else 0
                )
                gap_after_ms = (
                    max(0, cues[i + 1].effective_speech_start_ms - cue.effective_speech_end_ms)
                    if i + 1 < len(cues)
                    else 800
                )
                slack_ms = max(0, min(gap_after_ms - 120, 800))
                max_chars = max_chars_for_window(
                    cue.speech_duration_ms,
                    max_rate=rate_cap,
                    language=lang,
                    slack_ms=slack_ms,
                )
                rows.append(
                    {
                        "index": cue.index,
                        "startMs": cue.start_ms,
                        "endMs": cue.end_ms,
                        "startLabel": format_timestamp(cue.start_ms),
                        "endLabel": format_timestamp(cue.end_ms),
                        "text": cue.text,
                        "speaker": cue.speaker,
                        "voice": spk_voices.get(cue.speaker, default_v)
                        if cue.speaker
                        else default_v,
                        "fitRate": fit_rate,
                        "overflowMs": overflow_ms,
                        "overBudget": bool(is_dub and len(cue.text) > max_chars),
                    }
                )
            self._cues_cache = rows
        return self._cues_cache

    def _emit_cues(self) -> None:
        self._cues_cache = None
        self._cue_list.sync(self._cues_model())
        self.cuesChanged.emit()

    def _set_error(self, message: str) -> None:
        if message != self._error_text:
            self._error_text = message
            self.errorTextChanged.emit()
        if message:
            self._set_hint("")
        if message == "":
            if self._notice_level != "info":
                self._notice_level = "info"
                self.noticeLevelChanged.emit()
        elif self._notice_level == "info":
            self._notice_level = "error"
            self.noticeLevelChanged.emit()

    # ── destroy-confirmation gate (FR-1.1) ───────────────────────────────────

    def _can_apply_now(self) -> bool:
        """True when the change may run without discarding a live render."""
        return not self.rendered or self._rendering

    def _loss_message(self, action: str) -> str:
        return self.tr(
            "{action} sẽ xóa bản lồng tiếng đã tạo và tệp phụ đề đã căn chỉnh. Tiếp tục?"
        ).format(action=action)

    def _stage_invalidation(
        self,
        reason: str,
        action: str,
        apply: Callable[[], None],
        revert_signals: tuple[Any, ...] = (),
        *,
        when: bool | None = None,
    ) -> bool:
        """Stage ``apply`` behind a confirmation; True when it was staged.

        With nothing to lose the change applies immediately (and returns
        False), so callers can use one linear code path. ``when`` overrides
        the default "a matching render exists" test.
        """
        should_gate = (not self._can_apply_now()) if when is None else when
        if not should_gate:
            apply()
            return False
        self._pending_invalidation = _PendingInvalidation(
            reason=reason,
            message=self._loss_message(action),
            apply=apply,
            revert_signals=revert_signals,
        )
        for signal in revert_signals:
            signal.emit()
        self.pendingInvalidationChanged.emit()
        self.renderInvalidationRequested.emit(reason, self._pending_invalidation.message)
        return True

    @Property(bool, notify=pendingInvalidationChanged)
    def invalidationPending(self) -> bool:
        return self._pending_invalidation is not None

    @Property(str, notify=pendingInvalidationChanged)
    def invalidationReason(self) -> str:
        pending = self._pending_invalidation
        return "" if pending is None else pending.reason

    @Property(str, notify=pendingInvalidationChanged)
    def invalidationMessage(self) -> str:
        pending = self._pending_invalidation
        return "" if pending is None else pending.message

    @Slot()
    def confirmRenderInvalidation(self) -> None:
        """Apply the staged change (the user accepted the loss)."""
        pending = self._pending_invalidation
        if pending is None:
            return
        self._pending_invalidation = None
        self.pendingInvalidationChanged.emit()
        self._stale_reason = pending.reason
        pending.apply()
        self.staleChanged.emit()

    @Slot()
    def rejectRenderInvalidation(self) -> None:
        """Drop the staged change; controls re-read their unchanged values."""
        pending = self._pending_invalidation
        if pending is None:
            return
        self._pending_invalidation = None
        self.pendingInvalidationChanged.emit()
        for signal in pending.revert_signals:
            signal.emit()

    def _discard_pending(self) -> None:
        if self._pending_invalidation is not None:
            self._pending_invalidation = None
            self.pendingInvalidationChanged.emit()

    # ── staleness (FR-1.2) ───────────────────────────────────────────────────

    def _capture_stats(self) -> None:
        """Retain the last measured stats across an invalidation."""
        project = self._project
        if project is None:
            return
        if project.stats is not None:
            self._last_stats = project.stats
            self._last_stats_project_id = project.id
        elif project.id != self._last_stats_project_id:
            self._last_stats = None
            self._last_stats_project_id = ""

    def _reset_staleness(self) -> None:
        changed = bool(self._stale_reason) or self._last_stats is not None
        self._stale_reason = ""
        self._last_stats = None
        self._last_stats_project_id = ""
        if changed:
            self.staleChanged.emit()

    @Property(bool, notify=staleChanged)
    def renderStale(self) -> bool:
        """A rendered track exists on disk but no longer matches the inputs."""
        project = self._project
        if project is None:
            return False
        return self._store.has_track(project.id) and not self.rendered

    @Property(str, notify=staleChanged)
    def renderStaleReason(self) -> str:
        if not self.renderStale:
            return ""
        if self._stale_reason:
            return self.tr(
                "Bản lồng tiếng và phụ đề đã căn chỉnh không còn khớp với thay đổi "
                "({reason}). Hãy tạo lại để xuất."
            ).format(reason=self._stale_action_text(self._stale_reason))
        return self.tr(
            "Bản lồng tiếng đã lưu không khớp với cấu hình hiện tại. Hãy tạo lại để xuất."
        )

    def _stale_action_text(self, reason: str) -> str:
        labels = {
            "mode": self.tr("đổi chế độ"),
            "rate_cap": self.tr("đổi mức nén"),
            "max_gap_ms": self.tr("đổi giới hạn khoảng lặng"),
            "offset_ms": self.tr("đổi độ lệch"),
            "merge_sentences": self.tr("đổi cách gộp câu"),
            "voice": self.tr("đổi giọng đọc"),
            "cue_edit": self.tr("sửa phụ đề"),
            "diarization": self.tr("phân tách người nói"),
            "llm": self.tr("tinh chỉnh bằng AI"),
            "asr": self.tr("nhận diện lại phụ đề"),
        }
        return labels.get(reason, reason)

    @Property(bool, notify=renderedChanged)
    def canExportTrack(self) -> bool:
        return self.rendered

    @Property(bool, notify=renderedChanged)
    def canExportSrt(self) -> bool:
        project = self._project
        return self.rendered and project is not None and bool(project.adjusted or project.cues)

    @Property(str, notify=renderedChanged)
    def exportBlockedReason(self) -> str:
        """Why the export actions are unavailable ("" when they are available)."""
        if self._project is None:
            return self.tr("Chưa có dự án phụ đề nào. Hãy nhập tệp để bắt đầu.")
        if self.renderStale:
            return self.renderStaleReason
        if not self.rendered:
            return self.tr("Chưa có bản lồng tiếng để xuất. Hãy tạo âm thanh trước.")
        return ""

    # ── derived state (FR-2.2) ───────────────────────────────────────────────

    def _compute_state(self) -> str:
        if self._exporting:
            return "exporting"
        if self._rendering:
            return "rendering"
        if self._ai_busy:
            return "ai_busy"
        if self._error_text:
            return "error"
        if self.renderStale:
            return "stale"
        if self.rendered:
            return "rendered"
        if self._project is not None:
            return "cues_ready"
        if self.videoPath:
            return "source_only"
        return "empty"

    def _compute_busy_owner(self) -> str:
        if self._exporting:
            return "export"
        if self._rendering:
            return "render"
        if self._ai_busy:
            return "ai"
        return ""

    def _refresh_state(self) -> None:
        """Recompute the derived state; emit only when it actually changed."""
        state = self._compute_state()
        if state != self._last_state:
            self._last_state = state
            self.stateChanged.emit()
        owner = self._compute_busy_owner()
        if owner != self._last_busy_owner:
            self._last_busy_owner = owner
            self.busyOwnerChanged.emit()

    @Property(str, notify=stateChanged)
    def state(self) -> str:
        """One of ``empty|source_only|cues_ready|ai_busy|rendering|rendered|stale|error``.

        ``exporting`` is its own state on the same busy axis; :attr:`busyOwner`
        names exactly one of ``ai``/``render``/``export`` at a time (FR-2.2).
        """
        return self._compute_state()

    @Property(str, notify=busyOwnerChanged)
    def busyOwner(self) -> str:
        """``"" | "ai" | "render" | "export"`` — never two at once."""
        return self._compute_busy_owner()

    # ── properties ───────────────────────────────────────────────────────────

    @Property(bool, notify=loadedChanged)
    def loaded(self) -> bool:
        return self._project is not None

    @Property(str, notify=loadedChanged)
    def projectId(self) -> str:
        return "" if self._project is None else self._project.id

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

    @Property(QObject, constant=True)
    def cueModel(self) -> DictListModel:
        """Row-level model of :attr:`cues` for QML views."""
        return self._cue_list

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
        if stats is None and self.renderStale:
            stats = self._last_stats
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

        def apply() -> None:
            self._mode = value
            if value == MODE_TRANSCRIPT and not self._merge_sentences:
                # Transcript mode has no rate knob; keep the effective policy honest.
                self._merge_sentences = True
                self.mergeSentencesChanged.emit()
            self.modeChanged.emit()
            self.maxGapMsChanged.emit()
            self.policyChanged.emit()
            self._rebuild_for_policy()

        self._stage_invalidation(
            "mode", self.tr("Đổi chế độ khớp thời gian"), apply, (self.modeChanged,)
        )

    @Property(float, notify=rateCapChanged)
    def rateCap(self) -> float:
        return self._rate_cap

    @rateCap.setter
    def rateCap(self, value: float) -> None:  # noqa: F811
        value = clamp_rate_cap(value)
        if value == self._rate_cap:
            return

        def apply() -> None:
            self._rate_cap = value
            self.rateCapChanged.emit()
            self.policyChanged.emit()
            self._rebuild_for_policy()

        self._stage_invalidation("rate_cap", self.tr("Đổi mức nén"), apply, (self.rateCapChanged,))

    @Property(int, notify=maxGapMsChanged)
    def maxGapMs(self) -> int:
        if self._max_gap_ms_set:
            return self._max_gap_ms
        # Transcript mode preserves pauses but caps them; dub mode keeps the
        # original pauses exactly (0 == uncapped, align.py documents it).
        return TRANSCRIPT_DEFAULT_MAX_GAP_MS if self._mode == MODE_TRANSCRIPT else 0

    @maxGapMs.setter
    def maxGapMs(self, value: int) -> None:  # noqa: F811
        try:
            value = max(0, min(60_000, int(value)))
        except (TypeError, ValueError):
            return
        if value == self.maxGapMs and self._max_gap_ms_set:
            return

        def apply() -> None:
            self._max_gap_ms = value
            self._max_gap_ms_set = True
            self.maxGapMsChanged.emit()
            self.policyChanged.emit()
            self._rebuild_for_policy()

        self._stage_invalidation(
            "max_gap_ms", self.tr("Đổi giới hạn khoảng lặng"), apply, (self.maxGapMsChanged,)
        )

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

        def apply() -> None:
            self._offset_ms = value
            self.offsetMsChanged.emit()
            self.policyChanged.emit()
            self._rebuild_for_policy()

        self._stage_invalidation(
            "offset_ms", self.tr("Đổi độ lệch thời gian"), apply, (self.offsetMsChanged,)
        )

    @Property(bool, notify=mergeSentencesChanged)
    def mergeSentences(self) -> bool:
        return self._merge_sentences

    @mergeSentences.setter
    def mergeSentences(self, value: bool) -> None:  # noqa: F811
        value = bool(value)
        if value == self._merge_sentences:
            return

        def apply() -> None:
            self._merge_sentences = value
            self.mergeSentencesChanged.emit()
            self.policyChanged.emit()
            self._rebuild_for_policy()

        self._stage_invalidation(
            "merge_sentences", self.tr("Đổi cách gộp câu"), apply, (self.mergeSentencesChanged,)
        )

    @Property(str, notify=voiceChanged)
    def voice(self) -> str:
        return self._voice

    @voice.setter
    def voice(self, value: str) -> None:  # noqa: F811
        value = value or ""
        if value == self._voice:
            return

        def apply() -> None:
            self._voice = value
            self.voiceChanged.emit()
            self.policyChanged.emit()
            self._rebuild_for_policy()

        self._stage_invalidation("voice", self.tr("Đổi giọng đọc"), apply, (self.voiceChanged,))

    @Property("QVariantMap", notify=policyChanged)
    def policySummary(self) -> dict[str, Any]:
        return policy_summary(self._fit_policy())

    @Property(bool, notify=exportingChanged)
    def exporting(self) -> bool:
        return self._exporting

    @Property(float, notify=exportProgressChanged)
    def exportProgress(self) -> float:
        """0.0–1.0 progress of the running video export (burn-in is slow)."""
        return self._export_progress

    @Property(str, notify=errorTextChanged)
    def errorText(self) -> str:
        return self._error_text

    @Property(str, notify=noticeLevelChanged)
    def noticeLevel(self) -> str:
        """Severity of the current ``errorText``: ``info|warning|error``."""
        return self._notice_level

    @Property(str, notify=hintTextChanged)
    def hintText(self) -> str:
        """Non-fatal guidance for an action that could not run (FR-3.6)."""
        return self._hint_text

    @Slot()
    def clearNotice(self) -> None:
        """Dismiss the banner without touching the project."""
        self._set_error("")

    def _set_hint(self, message: str) -> None:
        if message != self._hint_text:
            self._hint_text = message
            self.hintTextChanged.emit()

    def _set_warning(self, message: str) -> None:
        self._notice_level = "warning"
        self.noticeLevelChanged.emit()
        self._set_error(message)

    @Property(str, notify=videoChanged)
    def videoPath(self) -> str:
        if self._project is not None and self._project.video_path:
            return self._project.video_path
        return self._pending_video_path

    @Property(str, notify=videoChanged)
    def videoUrl(self) -> str:
        vp = self.videoPath
        return QUrl.fromLocalFile(vp).toString() if vp else ""

    @Property(bool, notify=videoChanged)
    def hasVideo(self) -> bool:
        return bool(self.videoPath)

    @Property(str, notify=videoChanged)
    def videoName(self) -> str:
        vp = self.videoPath
        return Path(vp).name if vp else ""

    @Property(str, notify=videoChanged)
    def videoMixMode(self) -> str:
        if self._project is not None:
            return self._project.video_mix_mode
        return self._video_mix_mode

    @videoMixMode.setter
    def videoMixMode(self, value: str) -> None:  # noqa: F811
        mode = "replace" if str(value or "") == "replace" else "duck"
        if mode == self.videoMixMode:
            return
        self._video_mix_mode = mode
        if self._project is not None:
            self._project = replace(self._project, video_mix_mode=mode)
            with contextlib.suppress(SubtitleProjectError):
                self._store.save(self._project)
        self.videoChanged.emit()

    @Property(float, notify=videoChanged)
    def videoBgGainDb(self) -> float:
        if self._project is not None:
            return float(self._project.video_bg_gain_db)
        return float(self._video_bg_gain_db)

    @videoBgGainDb.setter
    def videoBgGainDb(self, value: float) -> None:  # noqa: F811
        try:
            gain = max(-36.0, min(0.0, float(value)))
        except (TypeError, ValueError):
            return
        if abs(gain - self.videoBgGainDb) < 1e-6:
            return
        self._video_bg_gain_db = gain
        if self._project is not None:
            self._project = replace(self._project, video_bg_gain_db=gain)
            with contextlib.suppress(SubtitleProjectError):
                self._store.save(self._project)
        self.videoChanged.emit()

    @Property(bool, notify=videoChanged)
    def embedSubtitles(self) -> bool:
        if self._project is not None:
            return bool(self._project.embed_subtitles)
        return bool(self._embed_subtitles)

    @embedSubtitles.setter
    def embedSubtitles(self, value: bool) -> None:  # noqa: F811
        embed = bool(value)
        if embed == self.embedSubtitles:
            return
        self._embed_subtitles = embed
        if self._project is not None:
            self._project = replace(self._project, embed_subtitles=embed)
            with contextlib.suppress(SubtitleProjectError):
                self._store.save(self._project)
        self.videoChanged.emit()

    @Property(str, notify=videoChanged)
    def subtitleEmbedMode(self) -> str:
        """``"soft"`` (player-toggleable track) or ``"burn"`` (drawn into the picture)."""
        if self._project is not None:
            return self._project.subtitle_embed_mode
        return self._subtitle_embed_mode

    @subtitleEmbedMode.setter
    def subtitleEmbedMode(self, value: str) -> None:  # noqa: F811
        mode = "burn" if str(value or "") == "burn" else "soft"
        if mode == self.subtitleEmbedMode:
            return
        self._subtitle_embed_mode = mode
        if self._project is not None:
            self._project = replace(self._project, subtitle_embed_mode=mode)
            with contextlib.suppress(SubtitleProjectError):
                self._store.save(self._project)
        self.videoChanged.emit()

    @Property(bool, notify=videoChanged)
    def softSubtitlesSupported(self) -> bool:
        """False when the attached video's container cannot carry a soft track (AVI)."""
        vp = self.videoPath
        return supports_soft_subtitles(vp) if vp else True

    @Property(int, notify=overrunChanged)
    def videoOverrunMs(self) -> int:
        """How far the rendered dub runs past the video (0 when it fits).

        The exporter deliberately keeps every spoken word, so an overrun shows
        as a frozen tail; the card warns instead of shipping a surprise.
        """
        project = self._project
        vp = self.videoPath
        if project is None or not vp or not project.rendered:
            return 0
        wav = self._store.wav_path(project.id)
        if not wav.is_file():
            return 0
        with contextlib.suppress(OSError):
            stat = wav.stat()
            key = (str(vp), str(wav), stat.st_mtime_ns, stat.st_size)
            if self._overrun_cache is not None and self._overrun_cache[0] == key:
                return self._overrun_cache[1]
            with contextlib.suppress(Exception):
                value = dub_overrun_ms(vp, wav, data_dir=self._data_dir)
                self._overrun_cache = (key, value)
                return value
        return 0

    def _invalidate_overrun(self) -> None:
        if self._overrun_cache is None:
            return
        self._overrun_cache = None
        self.overrunChanged.emit()

    @Property("QVariantList", notify=speakersChanged)
    def speakers(self) -> list[dict[str, Any]]:
        project = self._project
        if project is None:
            return []
        counts: dict[str, int] = {}
        dur_by_spk: dict[str, int] = {}
        for cue in project.cues:
            if not cue.speaker:
                continue
            counts[cue.speaker] = counts.get(cue.speaker, 0) + 1
            dur_by_spk[cue.speaker] = dur_by_spk.get(cue.speaker, 0) + cue.duration_ms
        default_v = self._effective_voice()
        return [
            {
                "id": spk,
                "label": spk,
                "cueCount": count,
                "voice": project.speaker_voices.get(spk, default_v),
                "sampleDurationMs": dur_by_spk.get(spk, 0),
            }
            for spk, count in counts.items()
        ]

    @Property(bool, notify=speakersChanged)
    def hasSpeakers(self) -> bool:
        return len(self.speakers) > 0

    @Property(bool, notify=aiBusyChanged)
    def aiBusy(self) -> bool:
        return self._ai_busy

    @Property(str, notify=aiBusyChanged)
    def aiStageText(self) -> str:
        return self._ai_stage_text

    @Property(float, notify=aiProgressChanged)
    def aiProgress(self) -> float:
        return self._ai_progress

    # ── policy ───────────────────────────────────────────────────────────────

    def _fit_policy(self) -> FitPolicy:
        if self._mode == MODE_TRANSCRIPT:
            return FitPolicy.transcript(
                offset_ms=self._offset_ms,
                max_gap_ms=self.maxGapMs,
                merge_sentences=self._merge_sentences,
            )
        return FitPolicy.dub(
            rate_cap=self._rate_cap,
            offset_ms=self._offset_ms,
            max_gap_ms=self.maxGapMs,
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
            return replace(
                project,
                fingerprint=render_fingerprint(
                    project.cues,
                    project.policy,
                    project.sample_rate,
                    self._effective_voice(),
                    None,
                    speaker_voices=project.speaker_voices,
                ),
            )
        return replace(
            project,
            context=context,
            fingerprint=render_fingerprint(
                project.cues,
                project.policy,
                project.sample_rate,
                self._effective_voice(),
                context,
                speaker_voices=project.speaker_voices,
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
        self._capture_stats()
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
            video_path=self._project.video_path,
            speaker_voices=self._project.speaker_voices,
            video_mix_mode=self._project.video_mix_mode,
            video_bg_gain_db=self._project.video_bg_gain_db,
            embed_subtitles=self._project.embed_subtitles,
            subtitle_embed_mode=self._project.subtitle_embed_mode,
            project_id=self._project.id,
        )
        # A refused combination adopts nothing: its cache belongs to an engine
        # the active profile cannot vouch for.
        cached = None if refused else self._store.cached_render(candidate)
        if cached is not None:
            cached = replace(
                cached,
                video_path=self._project.video_path or cached.video_path,
                speaker_voices=self._project.speaker_voices or cached.speaker_voices,
                video_mix_mode=self._project.video_mix_mode,
                video_bg_gain_db=self._project.video_bg_gain_db,
                embed_subtitles=self._project.embed_subtitles,
                subtitle_embed_mode=self._project.subtitle_embed_mode,
            )
        previous_cues = self._project.cues
        self._project = cached or candidate
        self._load_reader()  # restores the measured timeline on a cache hit
        if self._render_progress != 0.0:
            self._render_progress = 0.0
            self.renderProgressChanged.emit()
        # Rate/gap knobs re-plan timing, not the cue list: skip the QML list
        # rebuild (1,500 delegates) when the cues QML renders are unchanged.
        if self._project.cues != previous_cues:
            self._emit_cues()
        self.renderedChanged.emit()
        self.durationChanged.emit()
        self.statsChanged.emit()
        self.staleChanged.emit()

    # ── import ───────────────────────────────────────────────────────────────

    def _video_path_digest(self, video: Path) -> str:
        try:
            resolved = str(video.resolve())
        except OSError:
            resolved = str(video)
        return hashlib.sha256(resolved.encode("utf-8")).hexdigest()[:8]

    def _managed_srt_path(self, video: Path) -> Path:
        stem = sanitize_filename(video.stem, fallback="video")
        return self._store.root / f"{stem}_{self._video_path_digest(video)}.srt"

    def _media_cache_dir(self, video: Path) -> Path:
        stem = sanitize_filename(video.stem, fallback="video")
        cache_dir = self._store.root / "_media_cache" / f"{stem}_{self._video_path_digest(video)}"
        with contextlib.suppress(OSError):
            cache_dir.mkdir(parents=True, exist_ok=True)
        return cache_dir

    def _activate_cues_project(
        self,
        source: Path,
        cues: list[Cue] | tuple[Cue, ...],
        *,
        video_path: str = "",
        speaker_voices: dict[str, str] | None = None,
        title: str | None = None,
    ) -> bool:
        try:
            context, refused = self._probe_context()
            eff_video = video_path or self._pending_video_path
            if not eff_video:
                comp_vid = find_companion_video(source)
                if comp_vid is not None:
                    eff_video = str(comp_vid)
            if title is None and eff_video:
                with contextlib.suppress(OSError):
                    if source.parent.resolve() == self._store.root.resolve():
                        title = Path(eff_video).stem
            # Re-importing the SAME file must not reset the user's saved embed
            # choices: the freshly built project starts from in-memory defaults,
            # so adopt the stored project's video settings when one exists.
            stored = self._store.load(
                build_project(
                    source,
                    cues,
                    self._fit_policy(),
                    sample_rate=DEFAULT_SAMPLE_RATE,
                    voice_key=self._effective_voice(),
                    title=title,
                    context=context,
                ).id
            )
            embed_subs = self._embed_subtitles
            embed_mode = self._subtitle_embed_mode
            mix_mode = self._video_mix_mode
            bg_gain = self._video_bg_gain_db
            if stored is not None:
                embed_subs = stored.embed_subtitles
                embed_mode = stored.subtitle_embed_mode
                mix_mode = stored.video_mix_mode
                bg_gain = stored.video_bg_gain_db
                eff_video = eff_video or stored.video_path
                if speaker_voices is None and stored.speaker_voices:
                    speaker_voices = dict(stored.speaker_voices)
            self._embed_subtitles = embed_subs
            self._subtitle_embed_mode = embed_mode
            self._video_mix_mode = mix_mode
            self._video_bg_gain_db = bg_gain
            base_project = build_project(
                source,
                cues,
                self._fit_policy(),
                sample_rate=DEFAULT_SAMPLE_RATE,
                voice_key=self._effective_voice(),
                title=title,
                context=context,
                video_path=eff_video,
                speaker_voices=speaker_voices,
                video_mix_mode=mix_mode,
                video_bg_gain_db=bg_gain,
                embed_subtitles=embed_subs,
                subtitle_embed_mode=embed_mode,
            )
            cached = None if refused else self._store.cached_render(base_project)
            if cached is None and eff_video:
                wav_16k = self._media_cache_dir(Path(eff_video)) / "source_16k.wav"
                if wav_16k.is_file() and any(
                    c.speech_start_ms is None or c.speech_end_ms is None for c in cues
                ):
                    cues = refine_cues_speech_bounds(cues, wav_16k)
                    base_project = build_project(
                        source,
                        cues,
                        self._fit_policy(),
                        sample_rate=DEFAULT_SAMPLE_RATE,
                        voice_key=self._effective_voice(),
                        title=title,
                        context=context,
                        video_path=eff_video,
                        speaker_voices=speaker_voices,
                        video_mix_mode=mix_mode,
                        video_bg_gain_db=bg_gain,
                        embed_subtitles=embed_subs,
                        subtitle_embed_mode=embed_mode,
                    )
                    cached = None if refused else self._store.cached_render(base_project)
            project = base_project
            if cached is not None:
                project = replace(
                    cached,
                    video_path=eff_video or cached.video_path,
                    speaker_voices=speaker_voices or cached.speaker_voices,
                    video_mix_mode=self._video_mix_mode,
                    video_bg_gain_db=self._video_bg_gain_db,
                    embed_subtitles=self._embed_subtitles,
                    subtitle_embed_mode=self._subtitle_embed_mode,
                )
            elif not refused:
                self._store.save(project)
        except SubtitleProjectError as exc:
            self._set_error(str(exc))
            return False
        self._pending_video_path = project.video_path
        self._project = project
        self._discard_pending()
        self._reset_staleness()
        self._cues_dirty = False
        self._load_reader()
        self._reset_active_span()
        self._position_ms = 0
        self.positionMsChanged.emit()
        self._set_error("")
        self.loadedChanged.emit()
        self.titleChanged.emit()
        self.sourcePathChanged.emit()
        self.videoChanged.emit()
        self.speakersChanged.emit()
        self._emit_cues()
        self.renderedChanged.emit()
        self.durationChanged.emit()
        self.statsChanged.emit()
        self.renderProgressChanged.emit()
        return True

    @Slot(str, result=bool)
    def importSrt(self, path: str) -> bool:  # type: ignore[override]
        """Parse an .srt (or video) into a workspace; returns False with ``errorText`` set."""
        source = normalize_local_path(path)
        if is_video_path(source):
            return bool(self.importVideo(str(source)))
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
        return self._activate_cues_project(source, cues)

    @Slot(str, result=bool)
    def importVideo(self, path: str) -> bool:  # type: ignore[override]
        """Import a video file: auto-pairs a companion `.srt` if present, or stages for ASR."""
        source = normalize_local_path(path)
        if not str(source) or not source.is_file():
            self._set_error(self.tr("Không tìm thấy tệp video: {name}").format(name=source.name))
            return False
        self._stop_render()
        self._stop_playback()
        self._pending_video_path = str(source)
        companion = find_companion_srt(source)
        if companion is None:
            managed_srt = self._managed_srt_path(source)
            if managed_srt.is_file():
                companion = managed_srt
        if companion is not None:
            try:
                cues = read_cues(companion)
            except (SubtitleError, OSError):
                cues = []
            if cues:
                return self._activate_cues_project(companion, cues, video_path=str(source))
        if self._project is not None:
            updated_cues = self._project.cues
            wav_16k = self._media_cache_dir(source) / "source_16k.wav"
            if not self.rendered and wav_16k.is_file():
                updated_cues = tuple(refine_cues_speech_bounds(updated_cues, wav_16k))
            self._project = replace(
                self._project,
                cues=updated_cues,
                video_path=str(source),
                fingerprint=render_fingerprint(
                    updated_cues,
                    self._project.policy,
                    self._project.sample_rate,
                    self._project.voice_key,
                    self._project.context,
                    speaker_voices=self._project.speaker_voices,
                ),
            )
            with contextlib.suppress(SubtitleProjectError):
                self._store.save(self._project)
            if updated_cues != self._project.cues or wav_16k.is_file():
                self._emit_cues()
        self.videoChanged.emit()
        return True

    @Slot(str, result=bool)
    def attachVideo(self, path: str) -> bool:  # type: ignore[override]
        """Attach a video file to the current subtitle workspace."""
        return bool(self.importVideo(path))

    @Slot()
    def clearVideo(self) -> None:
        """Detach the video file from the current subtitle workspace."""
        self.videoReleaseRequested.emit()
        self._pending_video_path = ""
        if self._project is not None and self._project.video_path:
            self._project = replace(self._project, video_path="")
            with contextlib.suppress(SubtitleProjectError):
                self._store.save(self._project)
        self.videoChanged.emit()

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

    @Slot(result=bool)
    def reloadCurrentProject(self) -> bool:
        """Reload the active subtitle project and timeline from disk after Studio save-back."""
        if self._project is None:
            return False
        loaded = self._store.load(self._project.id)
        if loaded is None:
            return False
        self._stop_playback()
        self._project = loaded
        self._load_reader()
        self._reset_stale_span_after_reload()
        self._emit_cues()
        self.renderedChanged.emit()
        self.durationChanged.emit()
        self.statsChanged.emit()
        return True

    def _reset_stale_span_after_reload(self) -> None:
        """Bookkeeping for a project reloaded from disk (shared with openProject)."""
        self._discard_pending()
        self._reset_staleness()
        self._cues_dirty = False
        self._reset_active_span()
        self._position_ms = 0
        self.positionMsChanged.emit()

    # ── project lifecycle (FR-1.3) ───────────────────────────────────────────

    @Property("QVariantList", notify=projectsChanged)
    def projects(self) -> list[dict[str, Any]]:
        """Every stored workspace, newest first (FR-1.3 picker)."""
        rows: list[dict[str, Any]] = []
        for project in self._store.list_projects():
            modified_at = project.created_at
            with contextlib.suppress(OSError):
                modified_at = (
                    _dt.datetime.fromtimestamp(self._store.project_path(project.id).stat().st_mtime)
                    .astimezone()
                    .isoformat(timespec="seconds")
                )
            rows.append(
                {
                    "id": project.id,
                    "title": project.title,
                    "cueCount": project.cue_count,
                    "durationMs": project.total_ms,
                    "rendered": project.rendered,
                    "hasVideo": bool(project.video_path),
                    "createdAt": project.created_at,
                    "modifiedAt": modified_at,
                    "active": self._project is not None and self._project.id == project.id,
                }
            )
        return rows

    @Property(int, notify=projectsChanged)
    def projectCount(self) -> int:
        return len(self._store.list_projects())

    @Slot(str, result=bool)
    def openProject(self, project_id: str) -> bool:
        """Reopen a stored workspace, restoring its knobs and measured timeline."""
        pid = str(project_id or "")
        loaded = self._store.load(pid)
        if loaded is None:
            self._set_error(self.tr("Không mở được dự án phụ đề đã chọn."))
            return False
        self.videoReleaseRequested.emit()
        self._stop_render()
        self._stop_playback()
        self.cancelAiTask()
        # Invalidated like clear(): a stale export callback must not touch it.
        self._export_generation += 1
        policy = loaded.policy
        self._mode = policy.mode
        self._rate_cap = policy.rate_cap
        self._max_gap_ms = policy.max_gap_ms
        self._max_gap_ms_set = True
        self._offset_ms = policy.offset_ms
        self._merge_sentences = policy.merge_sentences
        self._voice = loaded.voice_key
        self._video_mix_mode = loaded.video_mix_mode
        self._video_bg_gain_db = loaded.video_bg_gain_db
        self._embed_subtitles = loaded.embed_subtitles
        self._subtitle_embed_mode = loaded.subtitle_embed_mode
        self._project = loaded
        self._pending_video_path = loaded.video_path
        self._set_error("")
        self._load_reader()
        self._reset_stale_span_after_reload()
        for signal in (
            self.modeChanged,
            self.rateCapChanged,
            self.maxGapMsChanged,
            self.offsetMsChanged,
            self.mergeSentencesChanged,
            self.voiceChanged,
            self.videoChanged,
            self.policyChanged,
            self.loadedChanged,
            self.titleChanged,
            self.sourcePathChanged,
        ):
            signal.emit()
        self.speakersChanged.emit()
        self._emit_cues()
        self.renderedChanged.emit()
        self.durationChanged.emit()
        self.statsChanged.emit()
        self.renderProgressChanged.emit()
        self.projectsChanged.emit()
        return True

    @Slot(str, result=bool)
    def removeProject(self, project_id: str) -> bool:
        """Delete a stored workspace; removing the active one returns to empty."""
        pid = str(project_id or "")
        self.videoReleaseRequested.emit()
        try:
            self._store.remove(pid)
        except SubtitleProjectError as exc:
            self._set_error(str(exc))
            return False
        if self._project is not None and self._project.id == pid:
            self.clear()
        self.projectsChanged.emit()
        return True

    @Slot()
    def refreshProjects(self) -> None:
        self.projectsChanged.emit()

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
        probe = getattr(self._app, "submission_context_for", None)
        if callable(probe):
            used_speakers = {c.speaker for c in project.cues if c.speaker}
            for spk in sorted(used_speakers):
                spk_voice = (project.speaker_voices.get(spk) or "").strip()
                if spk_voice:
                    try:
                        spk_ctx = probe(spk_voice)
                    except TypeError:
                        break
                    if spk_ctx is None:
                        self._set_error(
                            str(getattr(self._app, "errorText", "") or "")
                            or self.tr("Giọng đọc '{voice}' của '{speaker}' không hợp lệ.").format(
                                voice=spk_voice, speaker=spk
                            )
                        )
                        return
        project = self._project_with_context(project, context)
        self._project = project
        cached = self._store.cached_render(project)
        if cached is not None:
            # A matching track exists: adopt the stored project (stats,
            # adjusted cues, measured timeline), never re-synthesize.
            self._project = cached
            self._reset_staleness()
            self._cues_dirty = False
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
            self._capture_stats()
            if not self._stale_reason:
                self._stale_reason = "voice"
            project = replace(project, total_ms=0, stats=None, adjusted=())
            self._project = project
            self._load_reader()  # drops the previous track's timeline
            self.renderedChanged.emit()
            self.durationChanged.emit()
            self.statsChanged.emit()
            self.staleChanged.emit()
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
        self._render_generation += 1
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

    @Slot()
    def cancelExport(self) -> None:
        """Cancel the running export (FR-1.4): unlatch the tab immediately.

        The generation bump makes the worker's eventual callback a no-op, and
        ``_export_cancel`` terminates a running ffmpeg. Cancelling while idle
        is a no-op.
        """
        if not self._exporting:
            return
        self._export_cancel = True
        self._export_generation += 1
        self._exporting = False
        self._export_progress = 0.0
        self.exportingChanged.emit()
        self.exportProgressChanged.emit()
        self.exportFinished.emit("", self.tr("Đã hủy xuất."))

    def _stop_render(self) -> None:
        self._play_after_render = False
        if self._job_id is not None:
            with contextlib.suppress(Exception):
                self._app.cancel_job(self._job_id)
            self._job_id = None
        self._abort_renderer()
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
        first_cue = project.cues[unit[0]] if unit else None
        unit_voice = (
            project.speaker_voices.get(first_cue.speaker, "")
            if first_cue is not None and first_cue.speaker
            else ""
        ) or self._effective_voice()
        submit = getattr(self._app, "submit_stream_for_listener", None)
        if not callable(submit):
            self._fail_render(self.tr("Không thể tạo tác vụ tổng hợp."))
            return
        context = project.context
        if unit_voice and unit_voice != self._effective_voice():
            probe = getattr(self._app, "submission_context_for", None)
            if callable(probe):
                try:
                    alt_ctx = probe(unit_voice, report=False)
                except TypeError:
                    alt_ctx = None
                else:
                    if alt_ctx is None:
                        self._fail_render(
                            self.tr("Giọng đọc '{voice}' không hợp lệ cho hồ sơ hiện tại.").format(
                                voice=unit_voice
                            )
                        )
                        return
                    context = alt_ctx
        try:
            if context is None:
                job_id = submit(text, unit_voice or None, self, kind="bulk")
            else:
                job_id = submit(text, unit_voice or None, self, kind="bulk", context=context)
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
        """Hand a synthesized unit to the ordered worker, then synthesize the next.

        Placement (WAV read → per-cue split → time-stretch → track write) runs
        on ``_units_exec`` in submission order while the engine already works
        on the following unit; failures come back here and fail the render.
        """
        project = self._project
        renderer = self._renderer
        if project is None or renderer is None:
            self._release_artifact(artifact)
            return
        unit = self._units[self._unit_index]
        if not isinstance(artifact, SynthesisArtifact) or artifact.job_id != expected_job_id:
            self._release_artifact(artifact)
            self._fail_render(self.tr("Tệp âm thanh vừa tạo không hợp lệ."))
            return
        generation = self._render_generation

        def work() -> None:
            try:
                if generation == self._render_generation:  # else: dropped
                    self._place_unit(renderer, project, unit, artifact)
            finally:
                self._release_artifact(artifact)

        def placed(_result: None) -> None:
            return None

        def failed(exc: BaseException) -> None:
            if generation == self._render_generation:
                self._fail_render(str(exc))

        self._units_exec.submit(work, placed, failed)
        self._unit_index += 1
        self._unit_progress = 0.0
        self._emit_progress()
        self._submit_next_unit()

    def _place_unit(
        self,
        renderer: SubtitleTrackRenderer,
        project: SubtitleProject,
        unit: tuple[int, ...],
        artifact: SynthesisArtifact,
    ) -> None:
        """Worker thread: place one unit's audio; raises with the banner text."""
        if not artifact.path.is_file() or artifact.samples <= 0:
            raise SubtitleProjectError(self.tr("Tệp âm thanh vừa tạo không hợp lệ."))
        try:
            audio, sample_rate = read_wav(artifact.path)
        except Exception as exc:
            logger.exception("reading synthesized subtitle unit failed")
            raise SubtitleProjectError(
                self.tr("Không đọc được âm thanh vừa tạo: {error}").format(error=exc)
            ) from exc
        if sample_rate != project.sample_rate:
            raise SubtitleProjectError(
                self.tr("Âm thanh vừa tạo có tần số lấy mẫu không hỗ trợ ({rate} Hz).").format(
                    rate=sample_rate
                )
            )
        try:
            renderer.add_unit_clip(unit, audio, split_fn=split_unit_audio)
        except SubtitleProjectError:
            raise
        except Exception as exc:
            logger.exception("placing a synthesized subtitle unit failed")
            raise SubtitleProjectError(
                self.tr("Không ghép được âm thanh vào phụ đề: {error}").format(error=exc)
            ) from exc

    def _emit_progress(self) -> None:
        total = len(self._units) or 1
        progress = min(1.0, (self._unit_index + self._unit_progress) / total)
        if progress != self._render_progress:
            self._render_progress = progress
            self.renderProgressChanged.emit()

    def _finish_render(self) -> None:
        """Queue the track finish behind every placement (ordered worker)."""
        renderer = self._renderer
        project = self._project
        if renderer is None or project is None:
            self._fail_render(self.tr("Tổng hợp thất bại."))
            return
        generation = self._render_generation
        store = self._store
        promoted: list[bool] = []

        def work() -> SubtitleProject | None:
            if generation != self._render_generation:
                return None  # cancelled while queued: the abort follows
            # Promote track.wav, save the timeline and the measured project.
            renderer.finish()
            promoted.append(True)
            return store.require(project.id)

        def finished(loaded: SubtitleProject | None) -> None:
            if generation != self._render_generation or loaded is None:
                return
            # The renderer is done: the promoted track is live.
            self._renderer = None
            self._project = loaded
            self._reset_staleness()
            self._cues_dirty = False
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

        def failed(exc: BaseException) -> None:
            if generation != self._render_generation:
                return
            if promoted:
                # A reload failure after finish(): the promoted track is
                # live, so the finished renderer must NOT be aborted.
                self._renderer = None
            if not isinstance(exc, SubtitleProjectError):
                logger.error("finishing the subtitle track failed", exc_info=exc)
                message = self.tr("Không thể hoàn tất tệp phụ đề âm thanh: {error}").format(
                    error=exc
                )
            else:
                message = str(exc)
            self._fail_render(message)

        self._units_exec.submit(work, finished, failed)

    def _abort_renderer(self) -> None:
        """Discard the partial track on the ordered worker (after queued work).

        Bumping the generation first makes every queued placement/finish of
        this render a no-op, so the abort never races a write in flight.
        """
        renderer = self._renderer
        self._renderer = None
        self._render_generation += 1
        if renderer is None:
            return

        def ignore(_result: Any) -> None:
            return None

        def report(exc: BaseException) -> None:
            logger.error("subtitle render abort failed", exc_info=exc)

        self._units_exec.submit(renderer.abort, ignore, report)

    def _fail_render(self, message: str) -> None:
        self._abort_renderer()
        if self._job_id is not None:
            # Synthesis overlaps placement: a unit may be in flight when an
            # earlier one fails to place.
            with contextlib.suppress(Exception):
                self._app.cancel_job(self._job_id)
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
            self._set_hint(self.tr("Chưa có bản âm thanh — đang tạo trước khi phát."))
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

    @Slot()
    def notifyVideoPreviewPlaying(self) -> None:
        """Stop active dub or speaker-sample playback when source video playback begins."""
        if (
            self._player_state != "stopped"
            or str(getattr(self._player, "state", "stopped") or "stopped") != "stopped"
        ):
            self._stop_playback()

    @Slot(int)
    def syncActiveCueFromVideo(self, ms: int) -> None:
        """Highlight the cue spanning ``ms`` during source-video playback/scrubbing."""
        project = self._project
        if project is None or self._player_state == "playing":
            return
        pos = max(0, int(ms))
        if pos != self._position_ms:
            self._position_ms = pos
            self.positionMsChanged.emit()
        matched = -1
        for i, cue in enumerate(project.cues):
            if cue.start_ms <= pos < cue.end_ms:
                matched = i
                break
        if matched != self._active_cue:
            self._active_cue = matched
            self.activeCueChanged.emit()

    @Slot(int)
    def seek(self, ms: int) -> None:
        with contextlib.suppress(Exception):
            self._player.seek(int(ms))

    @Slot(int)
    def seekToCue(self, index: int) -> None:
        project = self._project
        if project is None:
            return
        if self._timeline is None:
            if not self.hasVideo:
                self._set_hint(
                    self.tr("Chưa có bản âm thanh — hãy tạo âm thanh trước khi tua phụ đề.")
                )
                return
            if not 0 <= index < len(project.cues):
                return
            target_ms = int(project.cues[index].start_ms)
            self._set_hint("")
            if index != self._active_cue:
                self._active_cue = index
                self.activeCueChanged.emit()
            if target_ms != self._position_ms:
                self._position_ms = target_ms
                self.positionMsChanged.emit()
            self.videoSeekRequested.emit(target_ms)
            return
        if not 0 <= index < len(project.cues):
            return
        if index >= len(self._timeline.segments):
            return
        segment = self._timeline.segments[index]
        if segment.end_ms <= segment.start_ms:
            return
        self.seek(segment.start_ms)
        if self.hasVideo:
            self.videoSeekRequested.emit(int(project.cues[index].start_ms))

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
            self._set_error(self.exportBlockedReason or self.tr("Chưa có tệp âm thanh để xuất."))
            return ""
        target = self._export_target(dest_dir, project.title, self._export_extension())
        source = self._store.wav_path(project.id)
        self._export_cancel = False
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
            self._set_error(self.exportBlockedReason or self.tr("Chưa có phụ đề đã chỉnh để xuất."))
            return ""
        cues = project.adjusted
        include_speakers = bool(getattr(self._app, "subtitleExportSpeakerLabels", False))
        target = self._export_target(dest_dir, project.title, ".srt")
        self._export_cancel = False
        self._export_generation += 1
        generation = self._export_generation
        self._exporting = True
        self.exportingChanged.emit()

        def work() -> str:
            export_srt_file(target, cues, include_speakers=include_speakers)
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

    @Slot(str, result=str)
    def exportVideo(self, dest_dir: str) -> str:  # type: ignore[override]
        """Mux ``track.wav`` onto ``videoPath`` off-thread; emits ``exportFinished``."""
        project = self._project
        if project is None or self._rendering or self._exporting:
            return ""
        if not self.rendered:
            self._set_error(
                self.exportBlockedReason or self.tr("Chưa có tệp âm thanh để ghép video.")
            )
            return ""
        vpath_str = self.videoPath
        if not vpath_str or not Path(vpath_str).is_file():
            self._set_error(self.tr("Chưa chọn tệp video nguồn hợp lệ để ghép."))
            return ""
        self.videoReleaseRequested.emit()
        vpath = Path(vpath_str)
        ext = vpath.suffix.lower() or ".mp4"
        target = self._export_target(dest_dir, f"{project.title}_dubbed", ext)
        source_wav = self._store.wav_path(project.id)
        adjusted_cues = project.adjusted or project.cues
        placed_spans = (
            self._timeline.segments
            if self._timeline is not None and len(self._timeline.segments) == len(project.cues)
            else adjusted_cues
        )
        duck_spans = build_duck_spans(
            project.cues,
            placed_spans,
            offset_ms=int(project.policy.offset_ms),
        )
        mix_mode = self.videoMixMode
        bg_gain = self.videoBgGainDb
        embed_subs = self.embedSubtitles
        embed_mode = self.subtitleEmbedMode
        subtitle_language = self._subtitle_language()
        include_speakers = bool(getattr(self._app, "subtitleExportSpeakerLabels", False))
        proj_dir = self._store.project_dir(project.id)
        self._export_cancel = False
        self._export_generation += 1
        generation = self._export_generation
        self._exporting = True
        self._export_progress = 0.0
        self.exportingChanged.emit()
        self.exportProgressChanged.emit()

        def report(fraction: float) -> None:
            """Marshal ffmpeg progress from the worker thread to the GUI thread."""
            QMetaObject.invokeMethod(
                self,
                "_apply_export_progress",
                Qt.QueuedConnection,
                Q_ARG(float, float(fraction)),
            )

        def work() -> str:
            srt_temp: Path | None = None
            try:
                if self._export_cancel:
                    raise VideoDubError(self.tr("Đã hủy xuất."))
                if embed_subs and adjusted_cues:
                    srt_temp = proj_dir / ".retimed_mux.srt"
                    export_srt_file(srt_temp, adjusted_cues, include_speakers=include_speakers)
                mux_dubbed_video(
                    vpath,
                    source_wav,
                    target,
                    srt_path=srt_temp,
                    mix_mode=mix_mode,
                    bg_gain_db=bg_gain,
                    embed_subtitles=embed_subs,
                    subtitle_mode=embed_mode,
                    subtitle_language=subtitle_language,
                    duck_spans=duck_spans,
                    progress=report,
                    data_dir=self._data_dir,
                    cancel_check=lambda: self._export_cancel,
                )
                return str(target)
            finally:
                if srt_temp is not None:
                    with contextlib.suppress(OSError):
                        srt_temp.unlink(missing_ok=True)

        def done(path: str) -> None:
            if not self._end_export(generation):
                return
            self.exportFinished.emit(path, "")

        def failed(exc: BaseException) -> None:
            if not self._end_export(generation):
                return
            self._set_error(self.tr("Không thể xuất video lồng tiếng: {error}").format(error=exc))
            self.exportFinished.emit("", str(exc))

        try:
            self._run_bg(work, done, self, on_error=failed)
        except Exception as exc:  # noqa: BLE001
            failed(exc)
            return ""
        return str(target)

    @Slot(float)
    def _apply_export_progress(self, fraction: float) -> None:
        """Record export progress on the GUI thread (worker → queued call)."""
        value = max(0.0, min(1.0, float(fraction)))
        if abs(value - self._export_progress) < 1e-3:
            return
        self._export_progress = value
        self.exportProgressChanged.emit()

    def _subtitle_language(self) -> str:
        """Language code for the soft subtitle track's metadata.

        Prefers what ASR actually used; ``auto`` means "unknown", which players
        treat as ``und`` anyway, so it is passed through as empty.
        """
        raw = str(getattr(self._app, "asrLanguage", "auto") or "").strip()
        if raw.lower() in ("", "auto", "und"):
            return ""
        return raw

    # ── AI ASR, Diarization, Speaker Mapping & LLM Refinement ────────────────

    def _asr_config(self) -> AsrConfig:
        backend = str(getattr(self._app, "asrBackend", "auto") or "auto")
        model = str(getattr(self._app, "asrModel", "base") or "base")
        lang = str(getattr(self._app, "asrLanguage", "auto") or "auto")
        device = str(getattr(self._app, "asrDevice", "auto") or "auto")
        try:
            threads = int(getattr(self._app, "asrThreads", 0) or 0)
        except (TypeError, ValueError):
            threads = 0
        base_url = str(getattr(self._app, "llmBaseUrl", "") or "")
        api_key = str(getattr(self._app, "llmApiKey", "") or "")
        return AsrConfig(
            backend=backend,
            model=model,
            language=lang,
            api_base_url=base_url,
            api_key=api_key,
            device=device,
            threads=threads,
        )

    def _diarization_config(self) -> DiarizationConfig:
        backend = str(getattr(self._app, "diarizationBackend", "auto") or "auto")
        model = str(
            getattr(self._app, "diarizationModel", "nemotron-3-diarization-q8_0")
            or "nemotron-3-diarization-q8_0"
        )
        try:
            max_spk = int(getattr(self._app, "diarizationMaxSpeakers", 0) or 0)
        except (TypeError, ValueError):
            max_spk = 0
        try:
            threads = int(getattr(self._app, "diarizationThreads", 0) or 0)
        except (TypeError, ValueError):
            threads = 0
        device = str(getattr(self._app, "diarizationDevice", "auto") or "auto")
        return DiarizationConfig(
            backend=backend,
            max_speakers=max_spk,
            model=model,
            device=device,
            threads=threads,
        )

    def _llm_config(self) -> LlmConfig:
        base_url = str(
            getattr(self._app, "llmBaseUrl", "http://localhost:11434/v1")
            or "http://localhost:11434/v1"
        )
        api_key = str(getattr(self._app, "llmApiKey", "") or "")
        model = str(getattr(self._app, "llmModel", "qwen2.5:7b") or "qwen2.5:7b")
        return LlmConfig(base_url=base_url, api_key=api_key, model=model)

    def _set_ai_state(self, busy: bool, stage_text: str = "", progress: float = 0.0) -> None:
        busy_changed = (busy != self._ai_busy) or (stage_text != self._ai_stage_text)
        prog_changed = abs(progress - self._ai_progress) > 1e-4
        self._ai_busy = busy
        self._ai_stage_text = stage_text
        self._ai_progress = progress
        if busy_changed:
            self.aiBusyChanged.emit()
        if prog_changed:
            self.aiProgressChanged.emit()

    @Slot()
    def cancelAiTask(self) -> None:
        """Cancel an in-flight ASR, diarization, or LLM refinement task."""
        self._ai_cancel_flag = True
        self._ai_generation += 1
        if self._ai_busy:
            self._set_ai_state(False, "", 0.0)

    @Slot(bool, result=bool)
    @Slot(result=bool)
    def transcribeVideo(self, with_diarization: bool = True) -> bool:  # type: ignore[override]
        """Run Whisper ASR (and optional speaker diarization) on ``videoPath`` off-thread."""
        vpath_str = self.videoPath
        if not vpath_str or not Path(vpath_str).is_file():
            self._set_error(self.tr("Chưa có tệp video để nhận diện phụ đề."))
            return False
        if self._rendering or self._ai_busy:
            self._set_hint(self.tr("Đang bận — hãy chờ tác vụ hiện tại hoàn tất."))
            return False
        self._set_hint("")
        # FR-1.6: a re-run overwrites the managed SRT — never silently discard
        # a live render or cue edits the user made on top of it.
        self._stage_invalidation(
            "asr",
            self.tr("Nhận diện lại phụ đề"),
            lambda: self._transcribe_video_now(with_diarization),
            when=self.rendered or self._cues_dirty,
        )
        return True

    def _transcribe_video_now(self, with_diarization: bool) -> None:
        vpath_str = self.videoPath
        if not vpath_str or not Path(vpath_str).is_file():
            self._set_error(self.tr("Chưa có tệp video để nhận diện phụ đề."))
            return
        vpath = Path(vpath_str)
        asr_cfg = self._asr_config()
        diar_cfg = self._diarization_config()
        self._ai_generation += 1
        gen = self._ai_generation
        self._ai_cancel_flag = False
        self._set_error("")
        self._set_ai_state(True, self.tr("Đang nhận diện giọng nói (ASR)…"), 0.05)

        def report_asr(progress: float, msg: str) -> None:
            # A cancelled generation must not repaint the progress bar: the
            # queued call can land after cancelAiTask() reset the UI.
            if gen != self._ai_generation:
                return
            self._set_ai_state(
                True,
                msg or self.tr("Đang nhận diện giọng nói (ASR)…"),
                progress * (0.7 if with_diarization else 1.0),
            )

        def report_diar(progress: float) -> None:
            if gen != self._ai_generation:
                return
            self._set_ai_state(True, self.tr("Đang phân tách người nói…"), 0.7 + 0.3 * progress)

        def work() -> tuple[list[Cue], str]:
            work_dir = self._media_cache_dir(vpath)
            asr_notes: list[str] = []
            cues = transcribe_media_to_cues(
                vpath,
                asr_cfg,
                work_dir=work_dir,
                data_dir=self._data_dir,
                progress_cb=report_asr,
                cancel_check=lambda: self._ai_cancel_flag or gen != self._ai_generation,
                notes=asr_notes,
            )
            diar_warn = ""
            notes: list[str] = []
            if with_diarization and not self._ai_cancel_flag and gen == self._ai_generation:
                try:
                    cues = diarize_cues(
                        vpath,
                        cues,
                        diar_cfg,
                        work_dir=work_dir,
                        data_dir=self._data_dir,
                        progress_cb=report_diar,
                        cancel_check=lambda: self._ai_cancel_flag or gen != self._ai_generation,
                        notes=notes,
                    )
                except DiarizationCancelled:
                    diar_warn = self.tr("Đã hủy phân tách người nói; phụ đề vẫn được giữ nguyên.")
                except Exception as exc:  # noqa: BLE001 - ASR output must survive
                    # Anything other than a clean success used to propagate
                    # out of work() and discard a finished transcription.
                    logger.warning("Diarization failed during transcribe: %s", exc)
                    diar_warn = self.tr("Không phân tách được người nói: {error}").format(error=exc)
                else:
                    if notes:
                        diar_warn = "\n".join(notes)
            warnings = [w for w in (*asr_notes, diar_warn) if w]
            return cues, "\n".join(warnings)

        def done(res: tuple[list[Cue], str]) -> None:
            if gen != self._ai_generation:
                return
            cues, warn = res
            self._set_ai_state(False, "", 1.0)
            if not cues:
                self._set_error(self.tr("Không nhận diện được phụ đề nào trong video này."))
                return
            srt_target = self._managed_srt_path(vpath)
            with contextlib.suppress(OSError):
                srt_target.parent.mkdir(parents=True, exist_ok=True)
                srt_target.write_text(format_srt(cues, include_speakers=True), encoding="utf-8")
            self._activate_cues_project(srt_target, cues, video_path=str(vpath), title=vpath.stem)
            if warn:
                self._set_warning(warn)

        def failed(exc: BaseException) -> None:
            if gen != self._ai_generation:
                return
            self._set_ai_state(False, "", 0.0)
            if not isinstance(exc, AsrCancelled):
                self._set_error(str(exc))

        try:
            self._run_bg(work, done, self, on_error=failed)
        except Exception as exc:  # noqa: BLE001
            failed(exc)

    @Slot(result=bool)
    def runDiarization(self) -> bool:  # type: ignore[override]
        """Run speaker diarization on the loaded project's video or rendered track."""
        project = self._project
        if project is None or self._rendering or self._ai_busy:
            if project is not None and (self._rendering or self._ai_busy):
                self._set_hint(self.tr("Đang bận — hãy chờ tác vụ hiện tại hoàn tất."))
            return False
        media = self.videoPath
        if not media or not Path(media).is_file():
            self._set_error(self.tr("Cần đính kèm tệp video gốc trước khi phân tách người nói."))
            return False
        self._set_hint("")
        self._stage_invalidation(
            "diarization", self.tr("Phân tách người nói"), self._run_diarization_now
        )
        return True

    def _run_diarization_now(self) -> None:
        project = self._project
        if project is None:
            return
        media = self.videoPath
        if not media or not Path(media).is_file():
            self._set_error(self.tr("Cần đính kèm tệp video gốc trước khi phân tách người nói."))
            return

        diar_cfg = self._diarization_config()
        cues_in = project.cues
        self._ai_generation += 1
        gen = self._ai_generation
        self._ai_cancel_flag = False
        self._set_error("")
        self._set_ai_state(True, self.tr("Đang phân tách người nói…"), 0.1)

        def report_diar(progress: float) -> None:
            if gen != self._ai_generation:
                return
            self._set_ai_state(True, self.tr("Đang phân tách người nói…"), 0.1 + 0.9 * progress)

        def work() -> tuple[list[Cue], str]:
            notes: list[str] = []
            tagged = diarize_cues(
                media,
                cues_in,
                diar_cfg,
                work_dir=self._media_cache_dir(Path(media)),
                data_dir=self._data_dir,
                progress_cb=report_diar,
                cancel_check=lambda: self._ai_cancel_flag or gen != self._ai_generation,
                notes=notes,
            )
            # Re-diarizing used to renumber speakers from scratch and silently
            # move cloned voices; keep the project's existing label identity.
            tagged = remap_speakers_to_previous(cues_in, tagged)
            return tagged, "\n".join(notes)

        def done(res: tuple[list[Cue], str]) -> None:
            if gen != self._ai_generation or self._project is None:
                return
            tagged, warn = res
            self._set_ai_state(False, "", 1.0)
            self._update_project_cues(tagged)
            if warn:
                self._set_warning(warn)
            else:
                self._set_error("")

        def failed(exc: BaseException) -> None:
            if gen != self._ai_generation:
                return
            self._set_ai_state(False, "", 0.0)
            self._set_error(str(exc))

        try:
            self._run_bg(work, done, self, on_error=failed)
        except Exception as exc:  # noqa: BLE001
            failed(exc)

    @Slot(str, str, result=bool)
    def setSpeakerVoice(self, speaker: str, voice_key: str) -> bool:  # type: ignore[override]
        """Assign ``voice_key`` to ``speaker`` in ``project.speaker_voices``."""
        project = self._project
        spk = (speaker or "").strip()
        if project is None or not spk or self._rendering:
            return False
        updated_map = dict(project.speaker_voices)
        vkey = (voice_key or "").strip()
        if vkey:
            updated_map[spk] = vkey
        else:
            updated_map.pop(spk, None)
        self._project = replace(project, speaker_voices=updated_map)
        self._rebuild_for_policy()
        with contextlib.suppress(SubtitleProjectError):
            self._store.save(self._project)
        self._emit_cues()
        self.speakersChanged.emit()
        return True

    @Slot(int, str, str, result=bool)
    @Slot(int, str, result=bool)
    def updateCue(self, index: int, text: str, speaker: str | None = None) -> bool:
        """Update one cue's ``text`` (and optionally ``speaker``) in place."""
        project = self._project
        if project is None or self._rendering or self._ai_busy:
            return False
        idx = int(index)
        if not (0 <= idx < len(project.cues)):
            return False
        cleaned = clean_cue_text(text or "")
        if not cleaned:
            self._set_error(self.tr("Nội dung phụ đề không được để trống."))
            return False
        orig = project.cues[idx]
        new_spk = orig.speaker if speaker is None else str(speaker).strip()
        cues_list = list(project.cues)
        cues_list[idx] = replace(orig, text=cleaned, speaker=new_spk)

        def apply() -> None:
            self._set_error("")
            self._update_project_cues(cues_list)

        self._stage_invalidation("cue_edit", self.tr("Sửa phụ đề"), apply)
        return True

    @Slot(str, result=bool)
    def playSpeakerSample(self, speaker: str) -> bool:
        """Extract (or reuse) a short reference clip for ``speaker`` and audition it."""
        project = self._project
        spk = (speaker or "").strip()
        vpath = self.videoPath
        if project is None or not spk or not vpath or not Path(vpath).is_file():
            self._set_error(self.tr("Cần video nguồn để nghe mẫu giọng của người nói."))
            return False
        ref_out = self._store.project_dir(project.id) / f"ref_{sanitize_filename(spk)}.wav"
        self._set_error("")
        if ref_out.is_file() and ref_out.stat().st_size > 0:
            self._stop_playback()
            self._player.play(str(ref_out))
            return True
        project_id = project.id
        cues = project.cues
        self._ai_generation += 1
        gen = self._ai_generation

        def work() -> Path:
            # ffmpeg extraction used to block the GUI thread on first audition.
            extract_speaker_reference_wav(vpath, cues, spk, ref_out, data_dir=self._data_dir)
            return ref_out

        def done(path: Path) -> None:
            if gen != self._ai_generation:
                return
            if self._project is None or self._project.id != project_id:
                return
            self._stop_playback()
            self._player.play(str(path))

        def failed(exc: BaseException) -> None:
            if gen != self._ai_generation:
                return
            self._set_error(str(exc))

        try:
            self._run_bg(work, done, self, on_error=failed)
        except Exception as exc:  # noqa: BLE001 - a rejected pool must not wedge playback
            failed(exc)
            return False
        return True

    @Slot(str, str, result=bool)
    @Slot(str, result=bool)
    def cloneSpeakerFromVideo(self, speaker: str, clone_name: str = "") -> bool:  # type: ignore[override]
        """Extract reference clip for ``speaker`` from ``videoPath`` and enroll a cloned voice."""
        project = self._project
        spk = (speaker or "").strip()
        vpath = self.videoPath
        if project is None or not spk or not vpath or not Path(vpath).is_file():
            self._set_error(self.tr("Cần video nguồn và phân tách người nói để lấy mẫu giọng."))
            return False
        if getattr(self._app, "supportsCloning", True) is False:
            self._set_error(
                self.tr("Hồ sơ mô hình hiện tại không hỗ trợ nhân bản giọng nói từ video.")
            )
            return False
        if getattr(self._app, "consentGiven", True) is False:
            self._set_error(
                self.tr(
                    "Vui lòng xác nhận cam kết sử dụng giọng nhân bản "
                    "trong thẻ Nhân bản giọng trước khi lấy mẫu."
                )
            )
            return False
        if self._rendering or self._ai_busy:
            return False

        name = (clone_name or "").strip() or f"{project.title}_{spk}"
        ref_out = self._store.project_dir(project.id) / f"ref_{sanitize_filename(spk)}.wav"
        cues_in = project.cues
        self._ai_generation += 1
        gen = self._ai_generation
        self._set_error("")
        self._set_ai_state(True, self.tr("Đang trích xuất mẫu giọng {spk}…").format(spk=spk), 0.2)

        def work() -> tuple[Path, str]:
            return extract_speaker_reference_wav(
                vpath, cues_in, spk, ref_out, data_dir=self._data_dir
            )

        def done(res: tuple[Path, str]) -> None:
            if gen != self._ai_generation:
                return
            wav_path, transcript = res
            self._set_ai_state(False, "", 1.0)
            add_voice = getattr(self._app, "addVoice", None)
            denoise = bool(
                getattr(self._app, "activeProfile", "vieneu") == "vieneu"
                and getattr(self._app, "denoiseRef", True)
            )
            if callable(add_voice):
                try:
                    add_voice(
                        name,
                        str(wav_path),
                        denoise,
                        transcript,
                        on_enrolled=lambda enrolled_id: self.setSpeakerVoice(
                            spk, enrolled_id or name
                        ),
                    )
                except TypeError:
                    with contextlib.suppress(Exception):
                        add_voice(name, str(wav_path), denoise, transcript)
                    self.setSpeakerVoice(spk, name)
                except Exception as exc:  # noqa: BLE001
                    self._set_error(str(exc))
            else:
                self.setSpeakerVoice(spk, name)

        def failed(exc: BaseException) -> None:
            if gen != self._ai_generation:
                return
            self._set_ai_state(False, "", 0.0)
            self._set_error(str(exc))

        try:
            self._run_bg(work, done, self, on_error=failed)
        except Exception as exc:  # noqa: BLE001
            failed(exc)
            return False
        return True

    @Slot(str, str, result=bool)
    @Slot(str, result=bool)
    @Slot(result=bool)
    def refineWithLlm(self, task: str = "refine_asr", target_language: str = "") -> bool:  # type: ignore[override]
        """Refine project cues with the configured OpenAI-compatible LLM off-thread."""
        project = self._project
        if project is None or self._rendering or self._ai_busy:
            if project is not None and (self._rendering or self._ai_busy):
                self._set_hint(self.tr("Đang bận — hãy chờ tác vụ hiện tại hoàn tất."))
            return False
        self._set_hint("")
        self._stage_invalidation(
            "llm",
            self.tr("Tinh chỉnh phụ đề bằng AI"),
            lambda: self._refine_with_llm_now(task, target_language),
        )
        return True

    def _refine_with_llm_now(self, task: str, target_language: str) -> None:
        project = self._project
        if project is None:
            return
        llm_cfg = self._llm_config()
        cues_in = project.cues
        raw_lang = (target_language or "").strip() or str(
            getattr(self._app, "synthesisLanguage", "") or "vi"
        )
        lang = "vi" if raw_lang.lower() == "auto" else raw_lang
        rate_cap = float(project.policy.rate_cap)

        self._ai_generation += 1
        gen = self._ai_generation
        self._ai_cancel_flag = False
        self._set_error("")
        stage_map = {
            "refine_asr": self.tr("Đang làm sạch phụ đề & người nói (LLM)…"),
            "fit_timing": self.tr("Đang rút gọn phụ đề khớp thời gian (LLM)…"),
            "translate_dub": self.tr("Đang dịch phụ đề khớp thời gian (LLM)…"),
        }
        stage_text = stage_map.get(task, stage_map["refine_asr"])
        self._set_ai_state(True, stage_text, 0.1)

        def work() -> list[Cue]:
            return refine_cues_with_llm(
                cues_in,
                llm_cfg,
                task=task,
                target_language=lang,
                rate_cap=rate_cap,
                progress_cb=lambda p: self._set_ai_state(True, stage_text, 0.1 + 0.9 * p),
                cancel_check=lambda: self._ai_cancel_flag or gen != self._ai_generation,
            )

        def done(refined: list[Cue]) -> None:
            if gen != self._ai_generation or self._project is None:
                return
            self._set_ai_state(False, "", 1.0)
            self._update_project_cues(refined)

        def failed(exc: BaseException) -> None:
            if gen != self._ai_generation:
                return
            self._set_ai_state(False, "", 0.0)
            self._set_error(str(exc))

        try:
            self._run_bg(work, done, self, on_error=failed)
        except Exception as exc:  # noqa: BLE001
            failed(exc)

    def _update_project_cues(self, new_cues: list[Cue] | tuple[Cue, ...]) -> None:
        if self._project is None or not new_cues:
            return
        self._capture_stats()
        self._cues_dirty = True
        self._project = replace(
            self._project,
            cues=tuple(new_cues),
            total_ms=0,
            stats=None,
            adjusted=(),
        )
        self._rebuild_for_policy()
        with contextlib.suppress(SubtitleProjectError):
            self._store.save(self._project)
        src_path = Path(self._project.source_path)
        with contextlib.suppress(OSError):
            if src_path.parent.resolve() == self._store.root.resolve():
                src_path.write_text(
                    format_srt(self._project.cues, include_speakers=True), encoding="utf-8"
                )
        self._emit_cues()
        self.speakersChanged.emit()

    def _end_export(self, generation: int) -> bool:
        """Unlatch ``_exporting``; False when the callback belongs to a cleared project."""
        if generation != self._export_generation:
            if self._exporting:
                self._exporting = False
                self._export_progress = 0.0
                self.exportingChanged.emit()
                self.exportProgressChanged.emit()
            return False
        self._export_cancel = False
        self._exporting = False
        self._export_progress = 0.0
        self.exportingChanged.emit()
        self.exportProgressChanged.emit()
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
        self.videoReleaseRequested.emit()
        self._stop_render()
        self._stop_playback()
        self.cancelAiTask()
        self._discard_pending()
        self._reset_staleness()
        self._cues_dirty = False
        # Invalidate any in-flight export's callbacks; the latch itself stays
        # held until that stale callback lands, so a new export cannot race
        # the old one's target. A running ffmpeg is also stopped.
        self._export_cancel = True
        self._export_generation += 1
        self._pending_video_path = ""
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
        self._set_hint("")
        self.loadedChanged.emit()
        self.titleChanged.emit()
        self.sourcePathChanged.emit()
        self.videoChanged.emit()
        self.speakersChanged.emit()
        self._emit_cues()
        self.renderedChanged.emit()
        self.durationChanged.emit()
        self.statsChanged.emit()

    @Slot()
    def shutdown(self) -> None:
        self.videoReleaseRequested.emit()
        self._stop_render()
        self._stop_playback()
        # Queued placements are already no-ops; let the abort run (bounded).
        self._units_exec.flush()
