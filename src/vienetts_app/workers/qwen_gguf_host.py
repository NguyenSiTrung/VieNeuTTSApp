"""Isolated Qwen3-TTS **GGUF** model host — the qwentts.cpp child process.

Sibling of ``workers/qwen_host.py``: same framed stdin/stdout protocol
(``core/qwen_protocol.py``), same one-job-at-a-time discipline, same bounded
48 kHz ``pcm`` frames — but inference is the managed ``libqwen`` native
library reached through :mod:`vienetts_app.workers.qwen_gguf_abi`, never torch.

Differences that shape the implementation:

* The native library can write to *process* stdout, not just ``sys.stdout`` —
  ``claim_frame_stdout`` reserves the frame pipe on a duplicated fd before
  ``isolate_stdout`` redirects fd 1 to stderr ahead of any native call.
* ``qt_synthesize`` streams 24 kHz chunks through a callback on *its* worker
  thread — frames are emitted from that thread (the shared ``emit`` wrapper
  holds the write lock), while the control reader stays responsive so a
  ``cancel`` lands through the native cancel callback.
* Every segment streams through one stateful resampler with a one-chunk
  lookahead, so ``final`` is exact and chunked output matches a one-shot
  resample.
* Native Metal is the explicit device name on the wire (``device="metal"``);
  the app-level ``mps`` id is translated by the parent, never guessed here.

Module import stays free of torch and the native library: ``CDLL`` runs inside
the injected session factory at ``load`` time only.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import os
import platform
import queue
import sys
import threading
import wave
from collections import OrderedDict
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import IO, Any, NamedTuple

import numpy as np

from vienetts_app.core.engine_profiles import (
    APP_SAMPLE_RATE,
    QWEN_BASE,
    QWEN_CUSTOM,
    QWEN_SOURCE_RATE,
    EngineProfileError,
    get_capabilities,
    language_model_name,
    validate_selection,
)
from vienetts_app.core.qwen_protocol import (
    GGUF_QUANTIZATIONS,
    RUNTIME_INCOMPLETE_CODE,
    EndOfStream,
    Frame,
    ProtocolError,
    SessionState,
    StaleFrameError,
    read_frame,
    write_frame,
)
from vienetts_app.core.qwen_variants import VariantError, native_speaker_id
from vienetts_app.core.streaming_resampler import StreamingResampler
from vienetts_app.workers.qwen_gguf_abi import (
    NATIVE_SAMPLE_RATE,
    QT_STATUS_OOM,
    NativeAbiError,
    NativeCallError,
    NativeCancelled,
    NativeQwenSession,
    VoiceRefData,
    shared_lib_name,
)

# Sibling-module reuse: the framing helpers, heartbeat beater, cancel registry
# and capability mapping are identical for both hosts — one implementation.
from vienetts_app.workers.qwen_host import (
    _DEVICE_ERROR_MARKERS,
    PROFILE_ENGINES,
    HostCapabilities,
    _CancelRequests,
    _emit_frames,
    _heartbeats,
    _message,
    describe_capabilities,
    log_to_stderr,
    lower_process_priority,
)

GGUF_HOST_NAME = "vienetts-qwen-gguf-host"

#: Application device id → the ``GGML_BACKEND`` name qwentts.cpp's backend_init
#: forces (src/backend.h). ggml names Metal devices MTL<N> (ggml-metal-device.m),
#: so ``metal`` resolves to MTL0 — "Metal" is not a device name on the wire.
GGUF_DEVICE_BACKENDS: Mapping[str, str] = {"cpu": "CPU", "cuda": "CUDA0", "metal": "MTL0"}

#: The native ``model_type`` each app profile must load — a CustomVoice GGUF
#: serving the Base profile (or vice versa) is a pairing bug, not a voice.
EXPECTED_MODEL_TYPE: Mapping[str, str] = {"base": "base", "customvoice": "custom_voice"}

#: Bounds for the derived native caches. The clips live on disk and each
#: ``VoiceRefData`` is a few KB of NumPy-owned latents — a handful of entries
#: is plenty; eviction only costs one decode/extract on the next use.
MAX_VOICE_REFS = 8
MAX_REF_PCM = 4


class _SourceClip(NamedTuple):
    """A decoded reference clip: resolved path, content digest, 24 kHz PCM."""

    resolved: str
    digest: str
    pcm: np.ndarray


def _default_log(event: str, **fields: Any) -> None:
    """stderr logging tagged with this host's name, not the official host's."""
    log_to_stderr(event, host=GGUF_HOST_NAME, **fields)


SessionFactory = Callable[[str], NativeQwenSession]


class QwenGgufHostError(RuntimeError):
    """A host-side failure; ``code`` travels in the error frame."""

    def __init__(self, message: str, *, code: str = "host_error", fatal: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.fatal = fatal


class QwenGgufLoadError(QwenGgufHostError):
    def __init__(self, message: str, *, code: str = "load_failed") -> None:
        super().__init__(message, code=code)


class QwenGgufGenerationError(QwenGgufHostError):
    """The loaded context could not produce usable audio for this segment."""

    def __init__(self, message: str, *, code: str = "generation_failed") -> None:
        super().__init__(message, code=code)


def _default_session_factory(runtime_dir: str) -> NativeQwenSession:
    """Bind the verified library inside one managed runtime pack directory."""
    return NativeQwenSession(str(Path(runtime_dir) / shared_lib_name()))


def claim_frame_stdout() -> tuple[IO[bytes], Callable[[], None]]:
    """Reserve fd 1 for frames BEFORE the native library can write to it.

    Returns ``(frame_writer, isolate)``: the writer holds a duplicated fd that
    keeps talking to the peer, and ``isolate()`` later redirects fd 1 (and
    ``sys.stdout``) onto stderr — the ordering Task 1.1's backend-discovery
    finding demands: ggml modules and the native logger must never interleave
    into the frame stream. ``isolate`` is safe to call more than once.
    """
    saved_fd = os.dup(1)
    writer = os.fdopen(saved_fd, "wb", buffering=0)
    isolated = False

    def isolate() -> None:
        nonlocal isolated
        if isolated:
            return
        isolated = True
        with contextlib.suppress(OSError):
            os.dup2(2, 1)
        with contextlib.suppress(Exception):
            sys.stdout = sys.stderr

    return writer, isolate


def _read_reference_clip(raw: bytes) -> tuple[np.ndarray, int]:
    """Decode reference WAV bytes to mono float32; returns ``(samples, rate)``.

    Prefers soundfile (the app's WAV stack, float/PCM/any rate); falls back to
    the stdlib ``wave`` module for plain PCM when soundfile is unavailable in
    the child interpreter.
    """
    try:
        import soundfile as sf  # noqa: PLC0415 — app dependency, lazy here

        data, rate = sf.read(io.BytesIO(raw), dtype="float32", always_2d=False)
        return np.asarray(data, dtype=np.float32), int(rate)
    except ImportError:
        pass
    with wave.open(io.BytesIO(raw), "rb") as wav:
        channels = wav.getnchannels()
        width = wav.getsampwidth()
        rate = wav.getframerate()
        pcm_bytes = wav.readframes(wav.getnframes())
    if width == 2:
        data = np.frombuffer(pcm_bytes, dtype="<i2").astype(np.float32) / 32768.0
    elif width == 4:
        data = np.frombuffer(pcm_bytes, dtype="<i4").astype(np.float32) / 2147483648.0
    elif width == 1:
        data = (np.frombuffer(pcm_bytes, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
    else:
        raise QwenGgufHostError(f"unsupported WAV sample width: {width} bytes")
    if channels > 1:
        data = data.reshape(-1, channels).mean(axis=1)
    return np.ascontiguousarray(data, dtype=np.float32), int(rate)


class QwenGgufHost:
    """Host-side state: one loaded native context, one job at a time."""

    def __init__(
        self,
        *,
        session_factory: SessionFactory | None = None,
        log: Callable[..., None] | None = None,
        isolate_stdout: Callable[[], None] | None = None,
        resampler_factory: Callable[..., StreamingResampler] = StreamingResampler,
    ) -> None:
        self._session_factory = session_factory or _default_session_factory
        self._log = log if log is not None else _default_log
        self._isolate_stdout = isolate_stdout if isolate_stdout is not None else (lambda: None)
        self._stdout_isolated = False
        self._resampler_factory = resampler_factory
        self._session: NativeQwenSession | None = None
        self._profile = ""
        self._engine_id = ""
        self._capabilities: HostCapabilities | None = None
        # (talker, codec, quantization, library build) captured at load —
        # part of every derived-reference cache key.
        self._model_identity: tuple[str, str, str, str] = ("", "", "", "")
        # Bounded LRU caches of data derived from on-disk enrollment clips:
        # decoded 24 kHz PCM keyed by (path, content hash) and extracted
        # native latents keyed by (path, hash, transcript, *model_identity).
        self._ref_pcm: OrderedDict[tuple[str, str], np.ndarray] = OrderedDict()
        self._voice_refs: OrderedDict[tuple[str, ...], VoiceRefData] = OrderedDict()
        self._fatal = False

    @property
    def loaded(self) -> bool:
        return self._session is not None and self._session.loaded

    @property
    def profile(self) -> str:
        return self._profile

    @property
    def capabilities(self) -> HostCapabilities | None:
        return self._capabilities

    @property
    def fatal(self) -> bool:
        """True once a device/OOM failure made this process unfit to continue."""
        return self._fatal

    # -- load / unload ------------------------------------------------------- #

    def load(self, fields: Mapping[str, Any]) -> HostCapabilities:
        """Validate a ``load`` frame, bind the native session, report capabilities."""
        profile = str(fields.get("profile", ""))
        fmt = str(fields.get("format", "gguf"))
        if fmt != "gguf":
            raise QwenGgufLoadError(
                f"the GGUF host serves format='gguf' loads, got {fmt!r} — "
                "this is a parent routing bug"
            )
        engine_id = PROFILE_ENGINES.get(profile)
        if engine_id is None:
            raise QwenGgufLoadError(
                f"unsupported Qwen profile {profile!r} — expected one of: "
                f"{', '.join(sorted(PROFILE_ENGINES))}"
            )
        quantization = str(fields.get("quantization", ""))
        if quantization not in GGUF_QUANTIZATIONS:
            raise QwenGgufLoadError(
                f"unsupported quantization {quantization!r} — "
                f"supported: {', '.join(sorted(GGUF_QUANTIZATIONS))}"
            )
        device = str(fields.get("device", ""))
        backend = GGUF_DEVICE_BACKENDS.get(device)
        if backend is None:
            raise QwenGgufLoadError(
                f"unsupported device {device!r} for the GGUF runtime — the native "
                f"backends are: {', '.join(sorted(GGUF_DEVICE_BACKENDS))} "
                "(the app's 'mps' id maps to 'metal')"
            )
        runtime_dir = Path(str(fields.get("runtimeDir", "")))
        if not runtime_dir.is_dir():
            # A missing pack dir IS an incomplete runtime — the parent routes
            # this code to the runtime card's Repair action, not a model fix.
            raise QwenGgufLoadError(
                f"GGUF runtime directory is missing: {runtime_dir}",
                code=RUNTIME_INCOMPLETE_CODE,
            )
        talker = Path(str(fields.get("talkerPath", "")))
        if not talker.is_file():
            raise QwenGgufLoadError(f"talker GGUF is missing: {talker}")
        codec = Path(str(fields.get("codecPath", "")))
        if not codec.is_file():
            raise QwenGgufLoadError(f"codec GGUF is missing: {codec}")

        self.close()  # exactly one large native context may be resident
        # fd 1 must already be a dead end before the shared library (and its
        # dlopen-ed ggml backends) can print into the frame stream.
        if not self._stdout_isolated:
            self._isolate_stdout()
            self._stdout_isolated = True
        # backend_init reads GGML_BACKEND inside qt_init — set it first.
        os.environ["GGML_BACKEND"] = backend
        try:
            session = self._session_factory(str(runtime_dir))
        except NativeAbiError as exc:
            raise QwenGgufLoadError(_message(str(exc)), code=RUNTIME_INCOMPLETE_CODE) from exc
        except Exception as exc:  # noqa: BLE001 — a factory may raise anything
            raise QwenGgufLoadError(_message(str(exc))) from exc
        session.set_log_callback(lambda line: self._log("native_log", line=line))
        try:
            session.load(str(talker), str(codec))
        except NativeAbiError as exc:
            session.close()
            raise QwenGgufLoadError(_message(str(exc))) from exc
        except Exception as exc:  # noqa: BLE001 — the binding reports anything
            session.close()
            raise QwenGgufLoadError(_message(f"could not load {talker.name}: {exc}")) from exc
        expected = EXPECTED_MODEL_TYPE.get(profile, "")
        if session.model_type != expected:
            reported = session.model_type or "unknown"
            session.close()
            raise QwenGgufLoadError(
                f"{talker.name} is a {reported!r} model — the {profile} profile needs {expected!r}"
            )
        self._session = session
        self._profile = profile
        self._engine_id = engine_id
        self._model_identity = (
            str(talker.resolve()),
            str(codec.resolve()),
            quantization,
            session.library_version,
        )
        self._capabilities = describe_capabilities(profile, session, log=self._log)
        self._log(
            "loaded",
            profile=profile,
            quantization=quantization,
            device=device,
            backend=backend,
            talker=str(talker),
            codec=str(codec),
            modelType=session.model_type,
            libraryVersion=session.library_version,
        )
        return self._capabilities

    def close(self) -> None:
        """Free the resident native context and every cached reference."""
        session, self._session = self._session, None
        if session is not None:
            session.close()
        self._profile = ""
        self._engine_id = ""
        self._capabilities = None
        self._model_identity = ("", "", "", "")
        self._release_reference_cache()

    def _release_reference_cache(self) -> None:
        """Drop every derived buffer — none may outlive the owning session."""
        if self._voice_refs or self._ref_pcm:
            self._log(
                "refs_released",
                voice_refs=len(self._voice_refs),
                pcm=len(self._ref_pcm),
            )
        self._voice_refs.clear()
        self._ref_pcm.clear()

    # -- selection ------------------------------------------------------------ #

    def _prepare(self, fields: Mapping[str, Any]) -> tuple[str, str, str, str]:
        """Validate one speaking selection: ``(lang_name, speaker, clip, ref_text)``.

        ``speaker`` comes back translated to the native lowercase id.
        """
        caps = get_capabilities(self._engine_id)
        language = str(fields.get("language", ""))
        speaker = str(fields.get("speaker", ""))
        prompt_path = str(fields.get("voicePrompt", ""))
        ref_text = str(fields.get("refText", ""))
        is_custom = self._engine_id == QWEN_CUSTOM
        try:
            # Same shared validator as the official host, so the messages the
            # parent already surfaces stay identical.
            validate_selection(
                self._engine_id,
                language=language,
                voice_id=speaker,
                clone_id="" if is_custom else prompt_path,
            )
            language_name = language_model_name(caps, language)
        except EngineProfileError as exc:
            raise QwenGgufHostError(str(exc), code="unsupported_selection") from exc
        # These 0.6B profiles expose no instruction control: the official host
        # drops the field because the PyTorch checkpoint ignores it, but the
        # native library would honour it — so it is rejected, never guessed.
        if str(fields.get("instruct", "")).strip():
            raise QwenGgufHostError(
                f"{caps.label} has no instruction controls — remove 'instruct'",
                code="unsupported_selection",
            )
        if is_custom:
            # Fixed-speaker profile: clone fields have no meaning here and
            # silently ignoring them would suggest cloning might happen.
            if prompt_path.strip() or ref_text.strip() or fields.get("useVoiceRef"):
                raise QwenGgufHostError(
                    f"{caps.label} uses fixed speakers — reference clips, "
                    "transcripts and voice references are only valid for "
                    "Qwen3-TTS Base",
                    code="unsupported_selection",
                )
            try:
                speaker = native_speaker_id(speaker)
            except VariantError as exc:
                raise QwenGgufHostError(str(exc), code="unsupported_selection") from exc
        else:
            if not ref_text.strip():
                raise QwenGgufHostError(
                    "Qwen3-TTS Base needs the reference transcript for its clone prompt"
                )
            if not Path(prompt_path).is_file():
                raise QwenGgufHostError(f"reference clip is missing: {prompt_path}")
            speaker = ""  # Base takes a clone, never a preset speaker
        return language_name, speaker, prompt_path, ref_text

    def _source_clip(self, prompt_path: str) -> _SourceClip:
        """Decode the reference clip to native-rate PCM; cached by content hash.

        The hash covers the file bytes — a rewritten clip at the same path is
        a different cache entry, so a changed source can never reuse stale
        latents.
        """
        resolved = str(Path(prompt_path).resolve())
        try:
            raw = Path(resolved).read_bytes()
        except OSError as exc:
            raise QwenGgufHostError(
                f"reference clip cannot be read: {exc}", code="unsupported_selection"
            ) from exc
        digest = hashlib.sha256(raw).hexdigest()
        key = (resolved, digest)
        cached = self._ref_pcm.get(key)
        if cached is not None:
            self._ref_pcm.move_to_end(key)
            return _SourceClip(resolved, digest, cached)
        try:
            data, rate = _read_reference_clip(raw)
        except QwenGgufHostError:
            raise
        except Exception as exc:  # noqa: BLE001 — corrupt/unsupported clip
            raise QwenGgufHostError(
                f"reference clip cannot be decoded: {exc}", code="unsupported_selection"
            ) from exc
        if data.ndim != 1:
            data = np.asarray(data.mean(axis=1), dtype=np.float32)
        if rate != NATIVE_SAMPLE_RATE:
            resampler = self._resampler_factory(rate, NATIVE_SAMPLE_RATE)
            data = np.concatenate([resampler.push(data), resampler.flush()])
        if data.size == 0 or not np.all(np.isfinite(data)):
            raise QwenGgufHostError(
                "reference clip decoded to no usable audio", code="unsupported_selection"
            )
        pcm = np.ascontiguousarray(data, dtype=np.float32)
        self._ref_pcm[key] = pcm
        self._ref_pcm.move_to_end(key)
        while len(self._ref_pcm) > MAX_REF_PCM:
            evicted, _ = self._ref_pcm.popitem(last=False)
            self._log("ref_pcm_evicted", reference=evicted[0])
        return _SourceClip(resolved, digest, pcm)

    def _voice_ref(self, prompt_path: str, ref_text: str) -> VoiceRefData:
        """Extract (once) and reuse the native voice reference for one clip.

        The cache key carries the source content hash, the transcript and the
        loaded model identity (talker, codec, quantization, native build) — a
        change to any of them must not reuse latents computed under another.
        Entries are NumPy-owned copies; the native buffers were already freed
        inside ``extract_voice_ref``.
        """
        clip = self._source_clip(prompt_path)
        key = (clip.resolved, clip.digest, ref_text, *self._model_identity)
        cached = self._voice_refs.get(key)
        if cached is not None:
            self._voice_refs.move_to_end(key)
            return cached
        session = self._session
        assert session is not None  # guarded by synthesize's load check
        try:
            ref = session.extract_voice_ref(clip.pcm)
        except (NativeAbiError, NativeCallError) as exc:
            raise QwenGgufHostError(_message(str(exc)), code="generation_failed") from exc
        self._voice_refs[key] = ref
        self._voice_refs.move_to_end(key)
        while len(self._voice_refs) > MAX_VOICE_REFS:
            evicted, _ = self._voice_refs.popitem(last=False)
            self._log("voice_ref_evicted", reference=evicted[0])
        self._log("voice_ref_built", reference=clip.resolved)
        return ref

    # -- synthesis ------------------------------------------------------------ #

    def synthesize(
        self,
        job: str,
        fields: Mapping[str, Any],
        emit: Callable[[Frame], None],
        *,
        cancelled: Callable[[], bool] = lambda: False,
    ) -> Frame:
        """Stream one bounded segment through the native callback."""
        if not self.loaded or self._capabilities is None:
            return self._failure(job, "not_loaded", "no GGUF model is loaded", emit=emit)
        try:
            selection = self._prepare(fields)
        except QwenGgufHostError as exc:
            return self._failure(job, exc.code, str(exc), emit=emit)
        return self._run_segments(
            job, [str(fields.get("text", ""))], selection, fields, emit, cancelled, segmented=False
        )

    def synthesize_batch(
        self,
        job: str,
        fields: Mapping[str, Any],
        emit: Callable[[Frame], None],
        *,
        cancelled: Callable[[], bool] = lambda: False,
    ) -> Frame:
        """One native call per segment — ``qt_synthesize`` has no batch entry."""
        if not self.loaded or self._capabilities is None:
            return self._failure(job, "not_loaded", "no GGUF model is loaded", emit=emit)
        texts = [str(item) for item in fields.get("texts", ())]
        try:
            selection = self._prepare(fields)
        except QwenGgufHostError as exc:
            return self._failure(job, exc.code, str(exc), emit=emit)
        if not texts:
            return self._failure(
                job, "generation_failed", "the batch carried no text segments", emit=emit
            )
        return self._run_segments(job, texts, selection, fields, emit, cancelled, segmented=True)

    # -- internals ------------------------------------------------------------ #

    def _run_segments(
        self,
        job: str,
        texts: list[str],
        selection: tuple[str, str, str, str],
        fields: Mapping[str, Any],
        emit: Callable[[Frame], None],
        cancelled: Callable[[], bool],
        *,
        segmented: bool,
    ) -> Frame:
        seq = 0
        emitted = 0
        for index, text in enumerate(texts):
            if cancelled():
                self._log("job_cancelled", job=job, frames=seq)
                return Frame(
                    type="terminal", job=job, fields={"status": "cancelled", "frames": seq}
                )
            result = self._stream_segment(
                job,
                text,
                selection,
                fields,
                emit,
                cancelled,
                segment=index if segmented else None,
                seq_start=seq,
            )
            if isinstance(result, Frame):
                return result
            seq, segment_emitted = result
            emitted += segment_emitted
        seconds = round(emitted / APP_SAMPLE_RATE, 3)
        terminal_fields: dict[str, Any] = {
            "status": "ok",
            "frames": seq,
            "audioSeconds": seconds,
        }
        if segmented:
            terminal_fields["segments"] = len(texts)
        self._log("job_ok", job=job, frames=seq, seconds=seconds, segments=len(texts))
        return Frame(type="terminal", job=job, fields=terminal_fields)

    def _stream_segment(
        self,
        job: str,
        text: str,
        selection: tuple[str, str, str, str, bool],
        fields: Mapping[str, Any],
        emit: Callable[[Frame], None],
        cancelled: Callable[[], bool],
        *,
        segment: int | None,
        seq_start: int,
    ) -> tuple[int, int] | Frame:
        """One ``qt_synthesize`` call, streamed; returns ``(seq, emitted)`` or a terminal.

        Native chunks arrive on the library's worker thread: each is resampled
        through this segment's one :class:`StreamingResampler`, and the previous
        resampled buffer is emitted as non-``final`` frames — a one-chunk
        lookahead is what keeps ``final`` exact when the stream ends.
        """
        language_name, speaker, prompt_path, ref_text = selection
        session = self._session
        assert session is not None
        resampler = self._resampler_factory(QWEN_SOURCE_RATE, APP_SAMPLE_RATE)
        seq = seq_start
        emitted = 0
        pending: np.ndarray | None = None

        def on_chunk(chunk: np.ndarray) -> bool:
            nonlocal seq, emitted, pending
            out = resampler.push(chunk)
            if pending is not None:
                seq, emitted = _emit_frames(
                    job, pending, seq, emitted, emit, final=False, segment=segment
                )
            pending = out
            return True

        try:
            kwargs = self._native_kwargs(fields, speaker, prompt_path, ref_text)
        except QwenGgufHostError as exc:
            return self._failure(job, exc.code, str(exc), emit=emit)
        try:
            with _heartbeats(job, emit):
                session.synthesize(
                    text=text,
                    lang=language_name,
                    on_chunk=on_chunk,
                    cancelled=cancelled,
                    **kwargs,
                )
        except NativeCancelled:
            resampler.reset()
            self._log("job_cancelled", job=job, frames=seq)
            return Frame(type="terminal", job=job, fields={"status": "cancelled", "frames": seq})
        except NativeCallError as exc:
            return self._failure(
                job,
                "generation_failed",
                str(exc),
                fatal=exc.status == QT_STATUS_OOM or _is_device_failure(exc),
                emit=emit,
            )
        except Exception as exc:  # noqa: BLE001 — callback exceptions land here
            detail = f"{type(exc).__name__}: {exc}"
            return self._failure(job, "generation_failed", detail, emit=emit)
        if cancelled():
            # The cancel landed after the last native poll — honour it anyway.
            resampler.reset()
            self._log("job_cancelled", job=job, frames=seq)
            return Frame(type="terminal", job=job, fields={"status": "cancelled", "frames": seq})
        tail = resampler.flush()
        if pending is None:
            pending = tail
        elif tail.size:
            pending = np.concatenate([pending, tail])
        if pending.size:
            seq, emitted = _emit_frames(
                job, pending, seq, emitted, emit, final=True, segment=segment
            )
        elif emitted == 0:
            return self._failure(
                job, "generation_failed", "the native runtime returned no audio", emit=emit
            )
        else:
            # The stream must always end on a ``final`` marker, even when the
            # resampler's tail produced no additional samples.
            fields_out: dict[str, Any] = {
                "sampleRate": APP_SAMPLE_RATE,
                "seq": seq,
                "final": True,
            }
            if segment is not None:
                fields_out["segment"] = segment
            emit(Frame(type="pcm", job=job, payload=b"", fields=fields_out))
            seq += 1
        return seq, emitted

    def _native_kwargs(
        self,
        fields: Mapping[str, Any],
        speaker: str,
        prompt_path: str,
        ref_text: str,
    ) -> dict[str, Any]:
        """Translate the validated selection into ``NativeQwenSession`` kwargs."""
        kwargs: dict[str, Any] = {}
        if self._engine_id == QWEN_BASE:
            # Upstream rejects --speaker/--instruct on base models outright.
            # The enrollment clip is extracted once into native latents and
            # reused for every segment — ``ref_audio_24k`` would re-run the
            # speaker encoder inside each qt_synthesize call.
            kwargs["ref"] = self._voice_ref(prompt_path, ref_text)
            kwargs["ref_text"] = ref_text
        else:
            # ``speaker`` is already the native lowercase id (``_prepare``
            # mapped it); ``instruct`` never reaches this line.
            kwargs["speaker"] = speaker or None
        seed = fields.get("seed")
        if isinstance(seed, int) and not isinstance(seed, bool):
            kwargs["seed"] = seed
        return kwargs

    def _failure(
        self,
        job: str,
        code: str,
        message: str,
        *,
        fatal: bool = False,
        emit: Callable[[Frame], None],
    ) -> Frame:
        detail = _message(message)
        self._log("job_failed", job=job, code=code, fatal=fatal, message=detail)
        emit(
            Frame(
                type="error",
                job=job,
                fields={"code": code, "message": detail, "fatal": fatal},
            )
        )
        if fatal:
            self._fatal = True
            self.close()
        return Frame(type="terminal", job=job, fields={"status": "failed", "error": detail})


def _is_device_failure(exc: Exception) -> bool:
    """Native OOM/backend failures poison the process; the parent restarts it."""
    text = f"{type(exc).__name__}: {exc}".lower()
    return any(marker in text for marker in _DEVICE_ERROR_MARKERS)


# --------------------------------------------------------------------------- #
# frame loop
# --------------------------------------------------------------------------- #


def hello_frame() -> Frame:
    """The first frame on the pipe: host identity, platform, frame sample rate."""
    return Frame(
        type="hello",
        fields={
            "host": GGUF_HOST_NAME,
            "platform": f"{sys.platform}-{platform.machine()}",
            "python": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
            "sampleRate": APP_SAMPLE_RATE,
        },
    )


def serve(
    reader: IO[bytes],
    writer: IO[bytes],
    *,
    session_factory: SessionFactory | None = None,
    log: Callable[..., None] | None = None,
    isolate_stdout: Callable[[], None] | None = None,
    resampler_factory: Callable[..., StreamingResampler] = StreamingResampler,
) -> int:
    """Run the GGUF host frame loop until ``shutdown`` or peer close.

    Same shape as the official host: a reader thread keeps ``cancel`` frames
    visible while the main thread is inside a blocking ``qt_synthesize`` call,
    and :class:`SessionState` still enforces the one-job-at-a-time contract.
    """
    emit_log = log if log is not None else _default_log
    host = QwenGgufHost(
        session_factory=session_factory,
        log=emit_log,
        isolate_stdout=isolate_stdout,
        resampler_factory=resampler_factory,
    )
    session = SessionState("host")
    state_lock = threading.Lock()
    cancelled = _CancelRequests()
    inbox: queue.Queue[object] = queue.Queue()

    def emit(frame: Frame) -> None:
        with state_lock:
            session.record_sent(frame)
            # Inside the lock: native chunk callbacks emit from the library's
            # own thread while the liveness beater emits from a third — frames
            # on the wire must never interleave.
            write_frame(writer, frame)

    def read_frames() -> None:
        while True:
            try:
                frame = read_frame(reader)
            except EndOfStream:
                inbox.put(None)
                return
            except ProtocolError as exc:
                inbox.put(exc)
                return
            # The reader validates but does NOT accept: SessionState ordering is
            # applied at dispatch so a pipelined load→synthesize can't mark the
            # job active before its capabilities frame is sent. Cancels are the
            # exception — they must reach the running job immediately.
            if frame.type == "cancel":
                cancelled.add(frame.job)
                emit_log("cancel_requested", job=frame.job)
            inbox.put(frame)

    threading.Thread(target=read_frames, name="qwen-gguf-host-reader", daemon=True).start()
    exit_code = 0
    try:
        emit(hello_frame())
        while True:
            item = inbox.get()
            if item is None:
                emit_log("peer_closed")
                break
            if isinstance(item, ProtocolError):
                emit_log("protocol_error", error=str(item))
                exit_code = 2
                break
            frame = item
            try:
                with state_lock:
                    session.accept(frame)
            except StaleFrameError as exc:
                # A late frame for a settled job — drop it, do not crash.
                emit_log("stale_frame", frame=frame.type, job=frame.job, error=str(exc))
                continue
            except ProtocolError as exc:
                emit_log("protocol_error", error=str(exc))
                exit_code = 2
                break
            if frame.type == "load":
                try:
                    capabilities = host.load(frame.fields)
                except (QwenGgufHostError, EngineProfileError) as exc:
                    emit_log("load_failed", error=str(exc))
                    emit(
                        Frame(
                            type="error",
                            fields={
                                "code": str(getattr(exc, "code", "") or "load_failed"),
                                "message": _message(str(exc)),
                                "fatal": False,
                            },
                        )
                    )
                else:
                    emit(Frame(type="capabilities", fields=capabilities.frame_fields()))
            elif frame.type in ("synthesize", "synthesize_batch"):
                handler = host.synthesize if frame.type == "synthesize" else host.synthesize_batch
                try:
                    terminal = handler(
                        frame.job,
                        frame.fields,
                        emit,
                        cancelled=lambda job=frame.job: cancelled.contains(job),
                    )
                finally:
                    cancelled.discard(frame.job)
                emit(terminal)
                if host.fatal:
                    emit_log("fatal_exit")
                    exit_code = 1
                    break
            elif frame.type == "shutdown":
                emit_log("shutdown")
                break
            # `cancel` needs no action here: the reader thread already recorded it.
    except (BrokenPipeError, OSError) as exc:
        emit_log("write_failed", error=str(exc))
        exit_code = 1
    finally:
        host.close()
    return exit_code


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point of the isolated GGUF interpreter.

    The frame pipe is claimed on a duplicated fd first; only then may the
    native library (and its dlopen-ed backends) write to fd 1, which by then
    points at stderr.
    """
    del argv  # no flags yet — the parent drives everything over frames
    stdin = getattr(sys.stdin, "buffer", None)
    if stdin is None or sys.stdout is None or sys.stderr is None:
        log_to_stderr("no_stdio", host=GGUF_HOST_NAME)
        return 2
    writer, isolate = claim_frame_stdout()
    log_to_stderr("starting", pid=os.getpid(), host=GGUF_HOST_NAME)
    log_to_stderr("priority", host=GGUF_HOST_NAME, lowered=lower_process_priority())
    code = serve(stdin, writer, isolate_stdout=isolate)
    # The daemon reader thread can still be parked inside a blocking stdin
    # read when `shutdown` wins the race — interpreter finalization would
    # abort the process trying to take that lock (_enter_buffered_busy), and
    # a clean shutdown would look like a crash. This process exists only to
    # serve frames: flush the pipes, then exit without finalizing.
    with contextlib.suppress(Exception):
        writer.flush()
    with contextlib.suppress(Exception):
        sys.stderr.flush()
    os._exit(code)
    return code  # unreachable — keeps the annotated contract honest


if __name__ == "__main__":  # pragma: no cover - process entry point
    raise SystemExit(main())
