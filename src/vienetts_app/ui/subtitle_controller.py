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
from vienetts_app.core.asr import AsrConfig, transcribe_media_to_cues
from vienetts_app.core.audio import DEFAULT_SAMPLE_RATE, export_audio_file, read_wav
from vienetts_app.core.diarization import (
    DiarizationConfig,
    DiarizationError,
    diarize_cues,
    extract_speaker_reference_wav,
)
from vienetts_app.core.llm_refine import LlmConfig, refine_cues_with_llm
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
from vienetts_app.core.video_dub import (
    find_companion_srt,
    find_companion_video,
    is_video_path,
    mux_dubbed_video,
)
from vienetts_app.ui.bg_ops import OrderedExecutor, SyncOrderedExecutor, run_on_thread_pool
from vienetts_app.ui.list_models import DictListModel
from vienetts_app.ui.playback import PlaybackController

logger = logging.getLogger(__name__)


def _default_player_factory() -> PlaybackController:
    return PlaybackController()


def _default_store_factory(data_dir: Path) -> SubtitleProjectStore:
    return SubtitleProjectStore(Path(data_dir) / "subtitles")


# Keys of one `cues` row — the roles of `cueModel`.
CUE_ROLES = ("index", "startMs", "endMs", "startLabel", "endLabel", "text", "speaker", "voice")


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
    videoChanged = Signal()
    speakersChanged = Signal()
    aiBusyChanged = Signal()
    aiProgressChanged = Signal()

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
        self._pending_video_path = ""
        self._video_mix_mode = "replace"
        self._video_bg_gain_db = -16.0
        self._embed_subtitles = True
        self._ai_busy = False
        self._ai_stage_text = ""
        self._ai_progress = 0.0
        self._ai_generation = 0
        self._ai_cancel_flag = False

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
        # Row-level mirror of `cues` for the QML cue list (perf 6.4): a
        # re-import diffs by cue index instead of rebuilding every row.
        self._cue_list = DictListModel(CUE_ROLES, key="index", parent=self)

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
            default_v = self._effective_voice()
            self._cues_cache = [
                {
                    "index": cue.index,
                    "startMs": cue.start_ms,
                    "endMs": cue.end_ms,
                    "startLabel": format_timestamp(cue.start_ms),
                    "endLabel": format_timestamp(cue.end_ms),
                    "text": cue.text,
                    "speaker": cue.speaker,
                    "voice": spk_voices.get(cue.speaker, default_v) if cue.speaker else default_v,
                }
                for cue in cues
            ]
        return self._cues_cache

    def _emit_cues(self) -> None:
        self._cues_cache = None
        self._cue_list.sync(self._cues_model())
        self.cuesChanged.emit()

    def _set_error(self, message: str) -> None:
        if message != self._error_text:
            self._error_text = message
            self.errorTextChanged.emit()

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

    @Property(str, notify=videoChanged)
    def videoPath(self) -> str:
        if self._project is not None and self._project.video_path:
            return self._project.video_path
        return self._pending_video_path

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
        mode = "duck" if str(value or "") == "duck" else "replace"
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

    # ── import ───────────────────────────────────────────────────────────────

    def _activate_cues_project(
        self,
        source: Path,
        cues: list[Cue] | tuple[Cue, ...],
        *,
        video_path: str = "",
        speaker_voices: dict[str, str] | None = None,
    ) -> bool:
        try:
            context, refused = self._probe_context()
            eff_video = video_path or self._pending_video_path
            if not eff_video:
                comp_vid = find_companion_video(source)
                if comp_vid is not None:
                    eff_video = str(comp_vid)
            project = build_project(
                source,
                cues,
                self._fit_policy(),
                sample_rate=DEFAULT_SAMPLE_RATE,
                voice_key=self._effective_voice(),
                context=context,
                video_path=eff_video,
                speaker_voices=speaker_voices,
                video_mix_mode=self._video_mix_mode,
                video_bg_gain_db=self._video_bg_gain_db,
                embed_subtitles=self._embed_subtitles,
            )
            cached = None if refused else self._store.cached_render(project)
            if cached is not None:
                project = replace(
                    cached,
                    video_path=eff_video or cached.video_path,
                    speaker_voices=speaker_voices or cached.speaker_voices,
                    video_mix_mode=self._video_mix_mode,
                    video_bg_gain_db=self._video_bg_gain_db,
                    embed_subtitles=self._embed_subtitles,
                )
            elif not refused:
                self._store.save(project)
        except SubtitleProjectError as exc:
            self._set_error(str(exc))
            return False
        self._pending_video_path = project.video_path
        self._project = project
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
        if companion is not None:
            try:
                cues = read_cues(companion)
            except (SubtitleError, OSError):
                cues = []
            if cues:
                return self._activate_cues_project(companion, cues, video_path=str(source))
        if self._project is not None:
            self._project = replace(self._project, video_path=str(source))
            with contextlib.suppress(SubtitleProjectError):
                self._store.save(self._project)
        self._set_error("")
        self.videoChanged.emit()
        return True

    @Slot(str, result=bool)
    def attachVideo(self, path: str) -> bool:  # type: ignore[override]
        """Attach a video file to the current subtitle workspace."""
        return bool(self.importVideo(path))

    @Slot()
    def clearVideo(self) -> None:
        """Detach the video file from the current subtitle workspace."""
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
        self._reset_active_span()
        self._emit_cues()
        self.renderedChanged.emit()
        self.durationChanged.emit()
        self.statsChanged.emit()
        return True

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
                with contextlib.suppress(Exception):
                    alt_ctx = probe(unit_voice, report=False)
                    if alt_ctx is not None:
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
            pieces = split_unit_audio(audio, unit, project.cues, sample_rate)
            for cue_index, piece in zip(unit, pieces, strict=True):
                renderer.add_clip(cue_index, piece)
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

    @Slot(str, result=str)
    def exportVideo(self, dest_dir: str) -> str:  # type: ignore[override]
        """Mux ``track.wav`` onto ``videoPath`` off-thread; emits ``exportFinished``."""
        project = self._project
        if project is None or self._rendering or self._exporting:
            return ""
        if not self.rendered:
            self._set_error(self.tr("Chưa có tệp âm thanh để ghép video. Hãy tạo trước."))
            return ""
        vpath_str = self.videoPath
        if not vpath_str or not Path(vpath_str).is_file():
            self._set_error(self.tr("Chưa chọn tệp video nguồn hợp lệ để ghép."))
            return ""
        vpath = Path(vpath_str)
        ext = vpath.suffix.lower() or ".mp4"
        target = self._export_target(dest_dir, f"{project.title}_dubbed", ext)
        source_wav = self._store.wav_path(project.id)
        adjusted_cues = project.adjusted or project.cues
        mix_mode = self.videoMixMode
        bg_gain = self.videoBgGainDb
        embed_subs = self.embedSubtitles
        proj_dir = self._store.project_dir(project.id)

        self._export_generation += 1
        generation = self._export_generation
        self._exporting = True
        self.exportingChanged.emit()

        def work() -> str:
            srt_temp: Path | None = None
            try:
                if embed_subs and adjusted_cues:
                    srt_temp = proj_dir / ".retimed_mux.srt"
                    export_srt_file(srt_temp, adjusted_cues)
                mux_dubbed_video(
                    vpath,
                    source_wav,
                    target,
                    srt_path=srt_temp,
                    mix_mode=mix_mode,
                    bg_gain_db=bg_gain,
                    embed_subtitles=embed_subs,
                    data_dir=self._data_dir,
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

    # ── AI ASR, Diarization, Speaker Mapping & LLM Refinement ────────────────

    def _asr_config(self) -> AsrConfig:
        backend = str(getattr(self._app, "asrBackend", "auto") or "auto")
        model = str(getattr(self._app, "asrModel", "base") or "base")
        lang = str(getattr(self._app, "asrLanguage", "auto") or "auto")
        base_url = str(getattr(self._app, "llmBaseUrl", "") or "")
        api_key = str(getattr(self._app, "llmApiKey", "") or "")
        return AsrConfig(
            backend=backend,
            model=model,
            language=lang,
            api_base_url=base_url,
            api_key=api_key,
        )

    def _diarization_config(self) -> DiarizationConfig:
        backend = str(getattr(self._app, "diarizationBackend", "auto") or "auto")
        try:
            max_spk = int(getattr(self._app, "diarizationMaxSpeakers", 0) or 0)
        except (TypeError, ValueError):
            max_spk = 0
        return DiarizationConfig(backend=backend, max_speakers=max_spk)

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
            return False

        vpath = Path(vpath_str)
        asr_cfg = self._asr_config()
        diar_cfg = self._diarization_config()
        self._ai_generation += 1
        gen = self._ai_generation
        self._ai_cancel_flag = False
        self._set_error("")
        self._set_ai_state(True, self.tr("Đang nhận diện giọng nói (ASR)…"), 0.05)

        def work() -> list[Cue]:
            cues = transcribe_media_to_cues(
                vpath,
                asr_cfg,
                data_dir=self._data_dir,
                cancel_check=lambda: self._ai_cancel_flag or gen != self._ai_generation,
            )
            if with_diarization and not self._ai_cancel_flag and gen == self._ai_generation:
                with contextlib.suppress(DiarizationError):
                    cues = diarize_cues(vpath, cues, diar_cfg, data_dir=self._data_dir)
            return cues

        def done(cues: list[Cue]) -> None:
            if gen != self._ai_generation:
                return
            self._set_ai_state(False, "", 1.0)
            srt_target = self._store.root / f"{sanitize_filename(vpath.stem, fallback='video')}.srt"
            self._activate_cues_project(srt_target, cues, video_path=str(vpath))

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

    @Slot(result=bool)
    def runDiarization(self) -> bool:  # type: ignore[override]
        """Run speaker diarization on the loaded project's video or rendered track."""
        project = self._project
        if project is None or self._rendering or self._ai_busy:
            return False
        media = self.videoPath
        if not media or not Path(media).is_file():
            wav = self._store.wav_path(project.id)
            if wav.is_file():
                media = str(wav)
            else:
                self._set_error(
                    self.tr("Cần đính kèm video hoặc tạo âm thanh trước khi phân tách người nói.")
                )
                return False

        diar_cfg = self._diarization_config()
        cues_in = project.cues
        self._ai_generation += 1
        gen = self._ai_generation
        self._ai_cancel_flag = False
        self._set_error("")
        self._set_ai_state(True, self.tr("Đang phân tách người nói…"), 0.1)

        def work() -> list[Cue]:
            return diarize_cues(media, cues_in, diar_cfg, data_dir=self._data_dir)

        def done(tagged: list[Cue]) -> None:
            if gen != self._ai_generation or self._project is None:
                return
            self._set_ai_state(False, "", 1.0)
            self._update_project_cues(tagged)

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
            if callable(add_voice):
                with contextlib.suppress(Exception):
                    add_voice(name, str(wav_path), True, transcript)
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
            return False
        llm_cfg = self._llm_config()
        cues_in = project.cues
        lang = (target_language or "").strip() or str(
            getattr(self._app, "synthesisLanguage", "") or "vi"
        )
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
        self._set_ai_state(True, stage_map.get(task, stage_map["refine_asr"]), 0.1)

        def work() -> list[Cue]:
            return refine_cues_with_llm(
                cues_in,
                llm_cfg,
                task=task,
                target_language=lang,
                rate_cap=rate_cap,
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
            return False
        return True

    def _update_project_cues(self, new_cues: list[Cue] | tuple[Cue, ...]) -> None:
        if self._project is None or not new_cues:
            return
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
        self._emit_cues()
        self.speakersChanged.emit()

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
        self.cancelAiTask()
        # Invalidate any in-flight export's callbacks; the latch itself stays
        # held until that stale callback lands, so a new export cannot race
        # the old one's target.
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
        self._stop_render()
        self._stop_playback()
        # Queued placements are already no-ops; let the abort run (bounded).
        self._units_exec.flush()
