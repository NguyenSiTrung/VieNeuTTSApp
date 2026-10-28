"""Optional ONNX audio models: Silero VAD and a speaker-embedding network (FR-4.5).

Both are managed downloads (``ai_models.AUDIO_MODELS``). When one is
installed, diarization uses it in place of the built-in heuristic: Silero for
speech bounds, acoustic segmentation and reference-clip scoring, the speaker
embedding for acoustic clustering. Without them the energy VAD and the 24-dim
embedding run unchanged.

``onnxruntime`` and ``kaldi-native-fbank`` are imported lazily, so importing
this module never loads either. Sessions run on the CPU provider, and each
model file gets one shared session (``InferenceSession.run`` is thread-safe).
Inference goes through a ``session_factory(path, threads)`` seam that tests
replace with fakes.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import numpy as np

logger = logging.getLogger(__name__)

SAMPLE_RATE = 16_000
#: Silero v5+ at 16 kHz: 512-sample frames, each fed with the previous
#: frame's last 64 samples in front (the upstream ``OnnxWrapper`` contract).
SILERO_FRAME = 512
SILERO_CONTEXT = 64
#: Shortest clip worth embedding; shorter speech is tiled up to it.
MIN_EMBED_SAMPLES = SAMPLE_RATE // 2

SessionFactory = Callable[[Path, int], Any]


class OnnxAudioError(RuntimeError):
    """An optional ONNX model could not be loaded or run."""


def default_session_factory(path: Path, threads: int) -> Any:
    """A CPU ``onnxruntime.InferenceSession`` for ``path``; ``threads`` 0 = ORT's default."""
    try:
        import onnxruntime as ort
    except ImportError as exc:  # pragma: no cover - a dependency of the app
        raise OnnxAudioError(f"onnxruntime is not available: {exc}") from exc
    options = ort.SessionOptions()
    if threads > 0:
        options.intra_op_num_threads = threads
        options.inter_op_num_threads = 1
    try:
        return ort.InferenceSession(str(path), options, providers=["CPUExecutionProvider"])
    except Exception as exc:  # noqa: BLE001 - ORT raises its own exception types
        raise OnnxAudioError(f"Cannot load {Path(path).name}: {exc}") from exc


_sessions: dict[tuple[Any, ...], Any] = {}
_sessions_lock = threading.Lock()


def get_session(
    path: str | Path, *, threads: int = 0, session_factory: SessionFactory | None = None
) -> Any:
    """The shared session for ``path``, created on first use.

    Keyed by the factory and the file's size and mtime too, so a replaced
    model file (or a test's fake factory) never reuses another session.
    """
    factory = session_factory or default_session_factory
    model = Path(path)
    try:
        stat = model.stat()
    except OSError as exc:
        raise OnnxAudioError(f"Model file is missing: {model}") from exc
    key = (factory, str(model.resolve()), stat.st_size, stat.st_mtime_ns, int(threads))
    with _sessions_lock:
        session = _sessions.get(key)
        if session is None:
            session = factory(model, int(threads))
            _sessions[key] = session
        return session


def to_16k(samples: np.ndarray, sample_rate: int) -> np.ndarray:
    """``samples`` as contiguous float32 at 16 kHz."""
    mono = np.ascontiguousarray(samples, dtype=np.float32).reshape(-1)
    if int(sample_rate) == SAMPLE_RATE or mono.size == 0:
        return mono
    import soxr

    return np.ascontiguousarray(soxr.resample(mono, int(sample_rate), SAMPLE_RATE), np.float32)


def hysteresis_spans(
    probs: np.ndarray,
    total_samples: int,
    *,
    threshold: float = 0.5,
    neg_threshold: float = 0.35,
    min_speech_samples: int = 4_000,
    min_silence_samples: int = 1_600,
    frame: int = SILERO_FRAME,
) -> list[tuple[int, int]]:
    """Speech ``(start, end)`` sample spans from per-frame speech probabilities.

    Upstream ``get_speech_timestamps`` without padding: speech starts at a
    frame >= ``threshold`` and ends only after ``min_silence_samples`` below
    ``neg_threshold``, so a probability between the two never flips the state.
    """
    spans: list[tuple[int, int]] = []
    triggered = False
    start = 0
    temp_end = 0
    for i, prob in enumerate(np.asarray(probs, dtype=np.float64).reshape(-1)):
        pos = i * frame
        if prob >= threshold and temp_end:
            temp_end = 0
        if prob >= threshold and not triggered:
            triggered = True
            start = pos
            continue
        if prob < neg_threshold and triggered:
            if not temp_end:
                temp_end = pos
            if pos - temp_end < min_silence_samples:
                continue
            if temp_end - start > min_speech_samples:
                spans.append((start, temp_end))
            triggered = False
            temp_end = 0
    if triggered and total_samples - start > min_speech_samples:
        spans.append((start, int(total_samples)))
    return spans


class SileroVad:
    """Silero VAD (v5/v6 ONNX) over 16 kHz audio."""

    def __init__(
        self, model_path: str | Path, *, session_factory: SessionFactory | None = None
    ) -> None:
        # One thread: a 512-sample frame is far too small to split, and extra
        # threads only add synchronisation to every call.
        self._session = get_session(model_path, threads=1, session_factory=session_factory)

    def frame_probabilities(self, samples16k: np.ndarray) -> np.ndarray:
        """Speech probability of each 512-sample frame (the last one zero-padded)."""
        audio = np.ascontiguousarray(samples16k, dtype=np.float32).reshape(-1)
        n_frames = -(-audio.size // SILERO_FRAME)
        probs = np.zeros(n_frames, dtype=np.float32)
        if n_frames == 0:
            return probs
        padded = np.zeros(n_frames * SILERO_FRAME + SILERO_CONTEXT, dtype=np.float32)
        padded[SILERO_CONTEXT : SILERO_CONTEXT + audio.size] = audio
        state = np.zeros((2, 1, 128), dtype=np.float32)
        sr = np.array(SAMPLE_RATE, dtype=np.int64)
        for i in range(n_frames):
            # The window starts 64 samples early: the previous frame's tail
            # (zeros before the first frame) is the model's context.
            window = padded[i * SILERO_FRAME : (i + 1) * SILERO_FRAME + SILERO_CONTEXT]
            out, state = self._session.run(
                None, {"input": window[None, :], "state": state, "sr": sr}
            )[:2]
            probs[i] = float(np.asarray(out).reshape(-1)[0])
        return probs

    def speech_spans(
        self,
        samples: np.ndarray,
        sample_rate: int,
        *,
        threshold: float = 0.5,
        min_speech_ms: int = 250,
        min_silence_ms: int = 100,
    ) -> list[tuple[int, int]]:
        """Speech ``(start, end)`` spans of ``samples``, in samples at ``sample_rate``."""
        rate = int(sample_rate)
        audio16 = to_16k(samples, rate)
        probs = self.frame_probabilities(audio16)
        spans = hysteresis_spans(
            probs,
            audio16.size,
            threshold=threshold,
            neg_threshold=max(0.01, threshold - 0.15),
            min_speech_samples=SAMPLE_RATE * int(min_speech_ms) // 1000,
            min_silence_samples=SAMPLE_RATE * int(min_silence_ms) // 1000,
        )
        if rate == SAMPLE_RATE:
            return spans
        total = int(np.asarray(samples).size)
        scale = rate / SAMPLE_RATE
        return [
            (min(total, int(round(s * scale))), min(total, int(round(e * scale)))) for s, e in spans
        ]


def fbank_features(samples16k: np.ndarray, *, num_bins: int = 80) -> np.ndarray:
    """Kaldi 80-dim log-mel filterbank (25 ms / 10 ms, no dither) as ``[T, num_bins]``."""
    import kaldi_native_fbank as knf

    options = knf.FbankOptions()
    options.frame_opts.dither = 0.0
    options.frame_opts.samp_freq = float(SAMPLE_RATE)
    options.frame_opts.snip_edges = True
    options.mel_opts.num_bins = int(num_bins)
    fbank = knf.OnlineFbank(options)
    fbank.accept_waveform(float(SAMPLE_RATE), np.ascontiguousarray(samples16k, dtype=np.float32))
    fbank.input_finished()
    n = int(fbank.num_frames_ready)
    feats = np.empty((n, int(num_bins)), dtype=np.float32)
    for i in range(n):
        feats[i] = fbank.get_frame(i)
    return feats


class SpeakerEmbedder:
    """Speaker-embedding ONNX model fed Kaldi fbank features (``x`` [N, T, 80]).

    The model's own metadata (sherpa-onnx convention) says whether samples
    are scaled to the int16 range (``normalize_samples`` "0", WeSpeaker) and
    how features are normalised (``feature_normalize_type``).
    """

    def __init__(
        self,
        model_path: str | Path,
        *,
        session_factory: SessionFactory | None = None,
        threads: int = 0,
    ) -> None:
        self._session = get_session(model_path, threads=threads, session_factory=session_factory)
        meta: dict[str, str] = {}
        try:
            meta = dict(self._session.get_modelmeta().custom_metadata_map or {})
        except Exception:  # noqa: BLE001 - metadata is optional
            meta = {}
        self._scale = 1.0 if str(meta.get("normalize_samples", "1")) != "0" else 32768.0
        self._normalize = str(meta.get("feature_normalize_type", "global-mean"))
        try:
            self._input = str(self._session.get_inputs()[0].name)
        except Exception:  # noqa: BLE001
            self._input = "x"

    def embed(self, samples: np.ndarray, sample_rate: int) -> np.ndarray | None:
        """L2-normalised embedding of ``samples``; ``None`` for empty audio."""
        audio = to_16k(samples, sample_rate)
        if audio.size == 0:
            return None
        if audio.size < MIN_EMBED_SAMPLES:
            audio = np.tile(audio, -(-MIN_EMBED_SAMPLES // audio.size))[:MIN_EMBED_SAMPLES]
        feats = fbank_features(audio * np.float32(self._scale))
        if feats.shape[0] == 0:
            return None
        if self._normalize == "global-mean":
            feats = feats - feats.mean(axis=0, keepdims=True)
        out = self._session.run(None, {self._input: feats[None, :, :].astype(np.float32)})[0]
        vec = np.asarray(out, dtype=np.float32).reshape(-1)
        norm = float(np.linalg.norm(vec))
        return vec / norm if norm > 1e-12 else vec


def installed_silero_vad(data_dir: Path | None) -> SileroVad | None:
    """Silero VAD when its model is installed and loads; otherwise ``None``."""
    from vienetts_app.core.ai_models import SILERO_VAD_MODEL_ID, audio_model_path

    path = audio_model_path(SILERO_VAD_MODEL_ID, data_dir)
    if path is None:
        return None
    try:
        return SileroVad(path)
    except OnnxAudioError as exc:
        logger.warning("Silero VAD unavailable, using the energy VAD: %s", exc)
        return None


def installed_speaker_embedder(data_dir: Path | None) -> SpeakerEmbedder | None:
    """The speaker-embedding model when installed and loadable; otherwise ``None``."""
    from vienetts_app.core.ai_models import SPEAKER_EMBEDDING_MODEL_ID, audio_model_path

    path = audio_model_path(SPEAKER_EMBEDDING_MODEL_ID, data_dir)
    if path is None:
        return None
    try:
        return SpeakerEmbedder(path)
    except OnnxAudioError as exc:
        logger.warning("Speaker embedding unavailable, using the 24-dim fallback: %s", exc)
        return None
