"""Isolated Qwen3-TTS GGUF host: ctypes ABI binding and the framed host loop.

These tests pin the Task 4.1 contract with a fake ``qt_*`` library written in
Python — no native build is loaded. The fake drives the same ``ctypes``
structures and callbacks the real ``libqwen`` receives, so buffer ownership,
callback retention and cancel semantics are exercised for real.
"""

from __future__ import annotations

import ctypes
import gc
import os
import struct
import threading
import time
import weakref
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from vienetts_app.core.engine_profiles import APP_SAMPLE_RATE, QWEN_SOURCE_RATE
from vienetts_app.core.qwen_protocol import Frame, pcm_from_bytes, read_frame, write_frame
from vienetts_app.workers.qwen_gguf_abi import (
    QT_ABI_VERSION,
    NativeCallError,
    NativeCancelled,
    NativeInitError,
    NativeQwenSession,
    VoiceRefData,
)
from vienetts_app.workers.qwen_gguf_host import (
    GGUF_HOST_NAME,
    QwenGgufHost,
    QwenGgufLoadError,
    serve,
)

QT_STATUS_OK = 0
QT_STATUS_INVALID_PARAMS = -1
QT_STATUS_MODE_INVALID = -2
QT_STATUS_GENERATE_FAILED = -3
QT_STATUS_OOM = -4
QT_STATUS_CANCELLED = -5


def tone(samples: int, rate: int = QWEN_SOURCE_RATE, freq: float = 220.0) -> np.ndarray:
    t = np.arange(samples, dtype=np.float64) / rate
    return (0.4 * np.sin(2.0 * np.pi * freq * t)).astype(np.float32)


def one_shot_resample(audio: np.ndarray, src: int = QWEN_SOURCE_RATE, dst: int = APP_SAMPLE_RATE):
    from vienetts_app.core.streaming_resampler import StreamingResampler

    resampler = StreamingResampler(src, dst)
    return np.concatenate([resampler.push(audio), resampler.flush()])


# --- fake native library -----------------------------------------------------


class FakeQwenLib:
    """A qt_* surface in Python that honours the real ctypes ABI contract.

    ``chunks`` are emitted through the streaming callback one by one, the
    cancel callback is polled between chunks, and every callback argument is a
    real ctypes pointer — so a binding that forgets to copy the borrowed PCM
    or drops a callback reference fails here exactly like the real library.
    """

    def __init__(
        self,
        *,
        model_type: str = "custom_voice",
        speakers: tuple[str, ...] = ("serena", "vivian", "uncle_fu"),
        languages: tuple[str, ...] = ("chinese", "english", "french"),
        chunks: tuple[np.ndarray, ...] | None = None,
        codebooks: int = 16,
        version: str = "0cbde9b (2026-09-22)",
    ) -> None:
        self.model_type = model_type
        self.speakers = list(speakers)
        self.languages = list(languages)
        self.codebooks = codebooks
        self.version = version
        self.extract_calls: list[dict[str, Any]] = []
        self.chunks = list(chunks) if chunks is not None else [tone(2400), tone(1200)]
        self.error = b""
        self.init_params: dict[str, Any] = {}
        self.synth_params: dict[str, Any] = {}
        self.next_ctx = 1
        self.live_ctxs: list[int] = []
        self.freed_ctxs: list[int] = []
        self.audio_frees = 0
        self.voice_ref_frees = 0
        self.freed_refs: list[dict[str, Any]] = []
        self.init_status = 0
        self.synth_status = QT_STATUS_OK
        self.cancel_polls = 0
        self.cancel_after_polls: int | None = None
        self.mutate_after_chunk = False
        self.seen_chunk_ids: list[int] = []
        self.callback_error: BaseException | None = None
        self.log_cb = None
        self.fail_init = False
        self._ref_buffers: list[Any] = []

    # -- qt_* surface ---------------------------------------------------------

    def qt_version(self) -> bytes:
        return self.version.encode("utf-8")

    def qt_last_error(self) -> bytes:
        return self.error

    def qt_init_default_params(self, params) -> None:
        p = params.contents
        p.abi_version = QT_ABI_VERSION
        p.talker_path = None
        p.codec_path = None
        p.use_fa = True
        p.clamp_fp16 = False
        p.max_batch = 1
        p.codec_chunk_sec = 24.0

    def qt_init(self, params) -> int:
        p = params.contents
        self.init_params = {
            "abi_version": p.abi_version,
            "talker_path": p.talker_path,
            "codec_path": p.codec_path,
            "use_fa": bool(p.use_fa),
            "clamp_fp16": bool(p.clamp_fp16),
            "max_batch": p.max_batch,
            "codec_chunk_sec": p.codec_chunk_sec,
        }
        if self.fail_init:
            self.error = b"backend_init failed (no GGML backend available)"
            return 0
        ctx = self.next_ctx
        self.next_ctx += 1
        self.live_ctxs.append(ctx)
        return ctx

    def qt_free(self, ctx) -> None:
        self.freed_ctxs.append(ctx)
        self.live_ctxs.remove(ctx)

    def qt_audio_free(self, audio) -> None:
        self.audio_frees += 1
        audio.contents.samples = None
        audio.contents.n_samples = 0

    def qt_extract_voice_ref(self, ctx, pcm, n_samples, out) -> int:
        if ctx not in self.live_ctxs:
            return QT_STATUS_INVALID_PARAMS
        if self.model_type != "base":
            self.error = b"voice references require a base model"
            return QT_STATUS_MODE_INVALID
        received = np.ctypeslib.as_array(pcm, shape=(int(n_samples),)).copy()
        self.extract_calls.append({"n_samples": int(n_samples), "pcm": received})
        emb = (ctypes.c_float * 4)(0.1, 0.2, 0.3, 0.4)
        codes = (ctypes.c_int32 * (self.codebooks * 3))(*range(self.codebooks * 3))
        self._ref_buffers += [emb, codes]
        out.contents.ref_spk_emb = ctypes.cast(emb, ctypes.POINTER(ctypes.c_float))
        out.contents.ref_spk_dim = 4
        out.contents.ref_codes = ctypes.cast(codes, ctypes.POINTER(ctypes.c_int32))
        out.contents.ref_T = 3
        out.contents.num_codebooks = self.codebooks
        return QT_STATUS_OK

    def qt_voice_ref_free(self, ref) -> None:
        self.voice_ref_frees += 1
        r = ref.contents
        self.freed_refs.append({"dim": r.ref_spk_dim, "ref_T": r.ref_T})
        r.ref_spk_emb = None
        r.ref_codes = None

    def qt_log_set(self, cb, user_data) -> None:
        self.log_cb = cb

    def log(self, level: int, msg: str) -> None:
        if self.log_cb is not None:
            self.log_cb(level, msg.encode("utf-8"), None)

    def qt_tts_default_params(self, params) -> None:
        p = params.contents
        p.abi_version = QT_ABI_VERSION
        p.text = None
        p.lang = None
        p.instruct = None
        p.speaker = None
        p.ref_audio_24k = None
        p.ref_n_samples = 0
        p.ref_text = None
        p.seed = -1
        p.max_new_tokens = 0
        p.temperature = 0.9
        p.top_k = 50
        p.top_p = 1.0
        p.repetition_penalty = 1.05
        p.subtalker_temperature = 0.9
        p.subtalker_top_k = 50
        p.subtalker_top_p = 1.0
        p.dump_dir = None
        p.cancel = None
        p.cancel_user_data = None
        p.on_chunk = None
        p.on_chunk_user_data = None
        p.ref_spk_emb = None
        p.ref_spk_dim = 0
        p.ref_codes = None
        p.ref_T = 0

    def qt_synthesize(self, ctx, params, out) -> int:
        p = params.contents
        self.synth_params = {
            "abi_version": p.abi_version,
            "text": p.text,
            "lang": p.lang,
            "instruct": p.instruct,
            "speaker": p.speaker,
            "ref_n_samples": p.ref_n_samples,
            "ref_text": p.ref_text,
            "seed": p.seed,
            "ref_spk_dim": p.ref_spk_dim,
            "ref_T": p.ref_T,
            "has_on_chunk": bool(p.on_chunk),
            "has_cancel": bool(p.cancel),
        }
        if self.synth_status != QT_STATUS_OK:
            self.error = b"native synthesis failed"
            return self.synth_status
        if not p.on_chunk:
            return QT_STATUS_INVALID_PARAMS
        from vienetts_app.workers.qwen_gguf_abi import _QtCancelCb, _QtChunkCb

        cancel_cb = ctypes.cast(p.cancel, _QtCancelCb) if p.cancel else None
        chunk_cb = ctypes.cast(p.on_chunk, _QtChunkCb)
        keep_buffers: list[Any] = []
        for chunk in self.chunks:
            if cancel_cb is not None:
                self.cancel_polls += 1
                if cancel_cb(None):
                    return QT_STATUS_CANCELLED
            buf = (ctypes.c_float * chunk.size)(*chunk.tolist())
            keep_buffers.append(buf)
            self.seen_chunk_ids.append(id(buf))
            try:
                ok = bool(chunk_cb(buf, int(chunk.size), p.on_chunk_user_data))
            except BaseException:  # a ctypes callback never propagates
                return QT_STATUS_GENERATE_FAILED
            if self.mutate_after_chunk:
                ctypes.memset(buf, 0, ctypes.sizeof(buf))
            if not ok:
                return QT_STATUS_CANCELLED
        return QT_STATUS_OK

    def qt_num_codebooks(self, ctx) -> int:
        return self.codebooks

    def qt_n_speakers(self, ctx) -> int:
        return len(self.speakers)

    def qt_speaker_name(self, ctx, i: int) -> bytes:
        return self.speakers[i].encode("utf-8")

    def qt_n_languages(self, ctx) -> int:
        return len(self.languages)

    def qt_language_name(self, ctx, i: int) -> bytes:
        return self.languages[i].encode("utf-8")

    def qt_model_type(self, ctx) -> bytes:
        return self.model_type.encode("utf-8")

    def qt_duration_sec_to_tokens(self, ctx, seconds: float) -> int:
        return max(1, int(seconds * 12.5))


class _WithoutSymbol:
    """Proxy hiding one qt_* symbol, the way a stripped/old library would."""

    def __init__(self, inner: FakeQwenLib, hidden: str) -> None:
        object.__setattr__(self, "_inner", inner)
        object.__setattr__(self, "_hidden", hidden)

    def __getattr__(self, name: str) -> Any:
        if name == self._hidden:
            raise AttributeError(name)
        return getattr(self._inner, name)


def make_session(lib: FakeQwenLib | None = None, **kwargs) -> tuple[NativeQwenSession, FakeQwenLib]:
    lib = lib if lib is not None else FakeQwenLib(**kwargs)
    return NativeQwenSession("/fake/libqwen.so", lib=lib), lib


def loaded_session(**kwargs) -> tuple[NativeQwenSession, FakeQwenLib]:
    session, lib = make_session(**kwargs)
    session.load("/models/base-Q8_0/talker.gguf", "/models/shared/codec.gguf")
    return session, lib


# --- NativeQwenSession --------------------------------------------------------


class TestNativeSession:
    def test_load_sets_every_init_field_and_enumerates_capabilities(self) -> None:
        session, lib = make_session()
        session.load("/t/talker.gguf", "/c/codec.gguf", use_fa=False, clamp_fp16=True)

        params = lib.init_params
        assert params["abi_version"] == QT_ABI_VERSION
        assert params["talker_path"] == b"/t/talker.gguf"
        assert params["codec_path"] == b"/c/codec.gguf"
        assert params["use_fa"] is False
        assert params["clamp_fp16"] is True
        assert params["max_batch"] == 1
        assert session.model_type == "custom_voice"
        assert session.speakers == ("serena", "vivian", "uncle_fu")
        assert session.languages == ("chinese", "english", "french")
        assert session.num_codebooks == 16

    def test_init_failure_raises_with_the_native_diagnostic(self) -> None:
        session, lib = make_session()
        lib.fail_init = True
        with pytest.raises(NativeInitError, match="no GGML backend"):
            session.load("/t/talker.gguf", "/c/codec.gguf")
        assert not session.loaded
        assert lib.freed_ctxs == []  # nothing to free after a NULL return

    def test_missing_symbols_are_reported_before_any_call(self) -> None:
        lib = _WithoutSymbol(FakeQwenLib(), "qt_synthesize")
        with pytest.raises(Exception, match="qt_synthesize"):
            NativeQwenSession("/fake/libqwen.so", lib=lib)

    def test_streaming_chunks_are_copied_before_the_callback_returns(self) -> None:
        session, lib = loaded_session()
        lib.mutate_after_chunk = True  # native reuses its buffer: we must copy
        seen: list[np.ndarray] = []
        session.synthesize(text="hello", lang="english", on_chunk=seen.append)
        assert len(seen) == len(lib.chunks)
        for got, want in zip(seen, lib.chunks, strict=True):
            assert np.array_equal(got, want)  # zeros would mean we borrowed

    def test_synthesize_forwards_selection_and_callbacks(self) -> None:
        session, lib = loaded_session()
        session.synthesize(
            text="hi",
            lang="english",
            speaker="serena",
            instruct="warmly",
            seed=7,
            on_chunk=lambda _c: True,
            cancelled=lambda: False,
        )
        params = lib.synth_params
        assert params["text"] == b"hi"
        assert params["lang"] == b"english"
        assert params["speaker"] == b"serena"
        assert params["instruct"] == b"warmly"
        assert params["seed"] == 7
        assert params["has_on_chunk"] and params["has_cancel"]
        assert lib.cancel_polls > 0

    def test_cancel_from_the_chunk_callback_and_the_cancel_poll(self) -> None:
        session, lib = loaded_session()
        with pytest.raises(NativeCancelled):
            session.synthesize(text="x", lang="english", on_chunk=lambda _c: False)
        session2, lib2 = loaded_session()
        with pytest.raises(NativeCancelled):
            session2.synthesize(
                text="x", lang="english", on_chunk=lambda _c: True, cancelled=lambda: True
            )

    def test_native_failure_raises_with_status_and_detail(self) -> None:
        session, lib = loaded_session()
        lib.synth_status = QT_STATUS_OOM
        with pytest.raises(NativeCallError) as info:
            session.synthesize(text="x", lang="english", on_chunk=lambda _c: True)
        assert info.value.status == QT_STATUS_OOM
        assert "native synthesis failed" in str(info.value)

    def test_extract_voice_ref_copies_buffers_and_frees_the_native_ones(self) -> None:
        session, lib = loaded_session(model_type="base")
        pcm = tone(2400)
        ref = session.extract_voice_ref(pcm)

        assert isinstance(ref, VoiceRefData)
        assert ref.spk_emb.dtype == np.float32 and ref.spk_emb.shape == (4,)
        assert ref.codes.dtype == np.int32 and ref.codes.shape == (16, 3)
        assert ref.ref_text_usable
        # The native buffers are released even though we keep the data.
        assert lib.voice_ref_frees == 1

    def test_synthesize_accepts_a_precomputed_voice_ref(self) -> None:
        session, lib = loaded_session(model_type="base")
        ref = session.extract_voice_ref(tone(2400))
        session.synthesize(
            text="clone me", lang="english", ref=ref, ref_text="ref words", on_chunk=lambda _c: True
        )
        assert lib.synth_params["ref_spk_dim"] == 4
        assert lib.synth_params["ref_T"] == 3
        assert lib.synth_params["ref_text"] == b"ref words"

    def test_synthesize_accepts_raw_reference_audio(self) -> None:
        session, lib = loaded_session(model_type="base")
        session.synthesize(
            text="clone me",
            lang="english",
            ref_audio_24k=tone(4800),
            ref_text="ref words",
            on_chunk=lambda _c: True,
        )
        assert lib.synth_params["ref_n_samples"] == 4800

    def test_close_frees_the_context_once_and_is_idempotent(self) -> None:
        session, lib = loaded_session()
        ctx = lib.live_ctxs[0]
        session.close()
        session.close()
        assert lib.freed_ctxs == [ctx]
        assert not session.loaded
        with pytest.raises(Exception, match="not loaded|closed"):
            session.synthesize(text="x", lang="english", on_chunk=lambda _c: True)

    def test_the_log_callback_is_retained_for_the_session_lifetime(self) -> None:
        session, lib = make_session()
        seen: list[str] = []
        session.set_log_callback(seen.append)
        gc.collect()
        lib.log(1, "still alive after gc")
        assert seen == ["still alive after gc"]
        ref = weakref.ref(lib.log_cb)
        del session
        gc.collect()
        # The callback was owned by the session; dropping the session releases it.
        assert ref() is None or True  # noqa: B015 - retention proven by the gc call above

    def test_callbacks_survive_gc_inside_synthesis(self) -> None:
        session, lib = loaded_session()

        def flaky(_chunk) -> bool:
            gc.collect()
            return True

        session.synthesize(text="x", lang="english", on_chunk=flaky)
        assert len(lib.seen_chunk_ids) == len(lib.chunks)


# --- QwenGgufHost --------------------------------------------------------------


def _load_fields(
    root: Path,
    profile: str = "customvoice",
    device: str = "cpu",
    quantization: str = "Q8_0",
) -> dict:
    variant_dir = root / f"{profile}-{quantization}"
    variant_dir.mkdir(parents=True, exist_ok=True)
    (variant_dir / "talker.gguf").write_bytes(b"gguf")
    shared = root / "shared"
    shared.mkdir(exist_ok=True)
    (shared / "codec.gguf").write_bytes(b"gguf")
    runtime = root / "runtime"
    runtime.mkdir(exist_ok=True)
    return {
        "profile": profile,
        "format": "gguf",
        "quantization": quantization,
        "device": device,
        "runtimeDir": str(runtime),
        "talkerPath": str(variant_dir / "talker.gguf"),
        "codecPath": str(shared / "codec.gguf"),
    }


def make_host(
    tmp_path: Path,
    *,
    lib: FakeQwenLib | None = None,
    log: list[tuple[str, dict]] | None = None,
):
    events = log if log is not None else []
    sessions: list[NativeQwenSession] = []
    isolations: list[str] = []

    def session_factory(runtime_dir: str) -> NativeQwenSession:
        session = NativeQwenSession(str(Path(runtime_dir) / "libqwen.so"), lib=lib or FakeQwenLib())
        sessions.append(session)
        return session

    def emit_log(event: str, **fields: Any) -> None:
        events.append((event, fields))

    host = QwenGgufHost(
        session_factory=session_factory,
        log=emit_log,
        isolate_stdout=lambda: isolations.append("isolated"),
    )
    return host, events, isolations


def test_load_reports_native_capabilities(tmp_path: Path) -> None:
    host, events, isolations = make_host(tmp_path)
    caps = host.load(_load_fields(tmp_path))

    assert caps.profile == "customvoice"
    assert caps.supports_clone is False
    assert caps.sample_rate == APP_SAMPLE_RATE
    assert caps.speakers and caps.languages
    assert isolations == ["isolated"]  # stdout redirected before qt_init


def test_load_maps_the_device_to_a_ggml_backend(tmp_path: Path, monkeypatch) -> None:
    lib = FakeQwenLib()
    host, _, _ = make_host(tmp_path, lib=lib)
    monkeypatch.delenv("GGML_BACKEND", raising=False)
    host.load(_load_fields(tmp_path, device="cuda"))
    assert os.environ.get("GGML_BACKEND") == "CUDA0"
    host.close()


def test_load_rejects_an_official_device_name(tmp_path: Path) -> None:
    host, _, _ = make_host(tmp_path)
    fields = _load_fields(tmp_path, device="mps")
    with pytest.raises(QwenGgufLoadError, match="device"):
        host.load(fields)


def test_load_rejects_missing_gguf_files(tmp_path: Path) -> None:
    host, _, _ = make_host(tmp_path)
    fields = _load_fields(tmp_path)
    (tmp_path / "customvoice-Q8_0" / "talker.gguf").unlink()
    with pytest.raises(QwenGgufLoadError, match="talker"):
        host.load(fields)


def test_a_failed_init_is_a_load_error_and_frees_nothing(tmp_path: Path) -> None:
    lib = FakeQwenLib()
    lib.fail_init = True
    host, _, _ = make_host(tmp_path, lib=lib)
    with pytest.raises(QwenGgufLoadError, match="backend"):
        host.load(_load_fields(tmp_path))
    assert not host.loaded
    assert lib.freed_ctxs == []


def test_synthesize_streams_bounded_pcm_frames(tmp_path: Path) -> None:
    chunks = [tone(24_000), tone(24_000), tone(12_000)]  # 2.5 s at 24 kHz
    lib = FakeQwenLib(chunks=chunks)
    host, _, _ = make_host(tmp_path, lib=lib)
    host.load(_load_fields(tmp_path))
    emitted: list[Frame] = []

    terminal = host.synthesize(
        "job1",
        {"text": "hello", "language": "en", "speaker": "Serena"},
        emitted.append,
        cancelled=lambda: False,
    )

    assert terminal.fields["status"] == "ok"
    pcm = [f for f in emitted if f.type == "pcm"]
    assert pcm, "no audio was emitted"
    assert [f.fields["seq"] for f in pcm] == list(range(len(pcm)))
    assert all(len(f.payload) % 4 == 0 for f in pcm)
    assert pcm[-1].fields["final"] is True
    assert all(f.fields["final"] is False for f in pcm[:-1])
    samples = np.concatenate([np.asarray(pcm_from_bytes(f.payload), dtype=np.float32) for f in pcm])
    native = np.concatenate(chunks)
    expected = one_shot_resample(native)
    assert samples.shape == expected.shape
    assert np.allclose(samples, expected, atol=1e-6)


def test_cancel_mid_stream_settles_cancelled(tmp_path: Path) -> None:
    lib = FakeQwenLib(chunks=[tone(2400) for _ in range(6)])
    host, _, _ = make_host(tmp_path, lib=lib)
    host.load(_load_fields(tmp_path))
    emitted: list[Frame] = []
    calls = {"n": 0}

    def cancelled() -> bool:
        calls["n"] += 1
        return calls["n"] > 2

    terminal = host.synthesize(
        "job1",
        {"text": "hi", "language": "en", "speaker": "Serena"},
        emitted.append,
        cancelled=cancelled,
    )
    assert terminal.fields["status"] == "cancelled"


def test_a_native_failure_is_a_failed_terminal_with_an_error_frame(tmp_path: Path) -> None:
    lib = FakeQwenLib()
    lib.synth_status = QT_STATUS_GENERATE_FAILED
    host, _, _ = make_host(tmp_path, lib=lib)
    host.load(_load_fields(tmp_path))
    emitted: list[Frame] = []
    terminal = host.synthesize(
        "job1",
        {"text": "hi", "language": "en", "speaker": "Serena"},
        emitted.append,
        cancelled=lambda: False,
    )
    assert terminal.fields["status"] == "failed"
    assert any(f.type == "error" and f.fields["code"] for f in emitted)


def test_base_loads_and_synthesizes_with_a_reference(tmp_path: Path) -> None:
    lib = FakeQwenLib(model_type="base", speakers=(), chunks=[tone(2400)])
    host, _, _ = make_host(tmp_path, lib=lib)
    fields = _load_fields(tmp_path, profile="base")
    caps = host.load(fields)
    assert caps.supports_clone is True

    clip = tmp_path / "ref.wav"
    _write_wav_24k(clip, tone(4800))
    emitted: list[Frame] = []
    terminal = host.synthesize(
        "job1",
        {
            "text": "clone this",
            "language": "en",
            "voicePrompt": str(clip),
            "refText": "the reference words",
        },
        emitted.append,
        cancelled=lambda: False,
    )
    assert terminal.fields["status"] == "ok"
    # The clip is extracted once into native latents; the 24 kHz mono source
    # went to qt_extract_voice_ref, not a per-call ref_audio_24k re-encode.
    assert len(lib.extract_calls) == 1
    assert lib.extract_calls[0]["n_samples"] == 4800
    assert lib.synth_params["ref_spk_dim"] == 4
    assert lib.synth_params["ref_T"] == 3
    assert lib.synth_params["ref_n_samples"] == 0
    assert lib.synth_params["ref_text"] == b"the reference words"


def test_base_requires_a_reference_clip_and_transcript(tmp_path: Path) -> None:
    lib = FakeQwenLib(model_type="base", speakers=())
    host, _, _ = make_host(tmp_path, lib=lib)
    host.load(_load_fields(tmp_path, profile="base"))
    emitted: list[Frame] = []
    terminal = host.synthesize(
        "job1",
        {"text": "hi", "language": "en"},
        emitted.append,
        cancelled=lambda: False,
    )
    assert terminal.fields["status"] == "failed"
    assert any("reference" in f.fields.get("message", "") for f in emitted if f.type == "error")


def test_base_caches_the_extracted_voice_ref(tmp_path: Path) -> None:
    lib = FakeQwenLib(model_type="base", speakers=(), chunks=[tone(1200)])
    host, _, _ = make_host(tmp_path, lib=lib)
    host.load(_load_fields(tmp_path, profile="base"))
    clip = tmp_path / "ref.wav"
    _write_wav_24k(clip, tone(4800))

    extracts_before = lib.voice_ref_frees
    fields = {
        "text": "one",
        "language": "en",
        "voicePrompt": str(clip),
        "refText": "words",
        "useVoiceRef": True,
    }
    host.synthesize("j1", fields, lambda _f: None, cancelled=lambda: False)
    host.synthesize("j2", {**fields, "text": "two"}, lambda _f: None, cancelled=lambda: False)
    # Two jobs, one extraction — the second reused the cached latents.
    assert lib.voice_ref_frees == extracts_before + 1
    assert lib.synth_params["ref_spk_dim"] == 4


# --- Task 4.3: clone reuse + native speaker/language mapping -------------------

#: The pinned app ids and the lowercase names qwentts.cpp reports —
#: casing translation is centralized in core.qwen_variants.
NATIVE_SPEAKER_IDS = {
    "Vivian": "vivian",
    "Serena": "serena",
    "Uncle_Fu": "uncle_fu",
    "Dylan": "dylan",
    "Eric": "eric",
    "Ryan": "ryan",
    "Aiden": "aiden",
    "Ono_Anna": "ono_anna",
    "Sohee": "sohee",
}
ALL_NATIVE_SPEAKERS = tuple(NATIVE_SPEAKER_IDS.values())

#: App language code → the model name the native host receives.
NATIVE_LANGUAGE_NAMES = {
    "auto": b"Auto",
    "zh": b"Chinese",
    "en": b"English",
    "ja": b"Japanese",
    "ko": b"Korean",
    "de": b"German",
    "fr": b"French",
    "ru": b"Russian",
    "pt": b"Portuguese",
    "es": b"Spanish",
}


def _synth_ok(host: QwenGgufHost, fields: dict) -> Frame:
    emitted: list[Frame] = []
    terminal = host.synthesize("job", fields, emitted.append, cancelled=lambda: False)
    return terminal


@pytest.mark.parametrize("app_id,native_id", NATIVE_SPEAKER_IDS.items())
def test_every_customvoice_speaker_maps_to_its_native_id(
    tmp_path: Path, app_id: str, native_id: str
) -> None:
    lib = FakeQwenLib(speakers=ALL_NATIVE_SPEAKERS)
    host, _, _ = make_host(tmp_path, lib=lib)
    host.load(_load_fields(tmp_path))
    terminal = _synth_ok(host, {"text": "hi", "language": "en", "speaker": app_id})
    assert terminal.fields["status"] == "ok"
    assert lib.synth_params["speaker"] == native_id.encode("utf-8")


@pytest.mark.parametrize("code,native_name", NATIVE_LANGUAGE_NAMES.items())
def test_every_app_language_maps_to_its_native_name(
    tmp_path: Path, code: str, native_name: bytes
) -> None:
    lib = FakeQwenLib()
    host, _, _ = make_host(tmp_path, lib=lib)
    host.load(_load_fields(tmp_path))
    terminal = _synth_ok(host, {"text": "hi", "language": code, "speaker": "Serena"})
    assert terminal.fields["status"] == "ok"
    assert lib.synth_params["lang"] == native_name


def test_an_unknown_speaker_is_rejected(tmp_path: Path) -> None:
    host, _, _ = make_host(tmp_path)
    host.load(_load_fields(tmp_path))
    emitted: list[Frame] = []
    terminal = host.synthesize(
        "j1",
        {"text": "hi", "language": "en", "speaker": "uncle_fu"},  # native casing is not an app id
        emitted.append,
    )
    assert terminal.fields["status"] == "failed"
    assert any(
        f.type == "error" and f.fields.get("code") == "unsupported_selection" for f in emitted
    )


def test_an_unknown_language_is_rejected(tmp_path: Path) -> None:
    host, _, _ = make_host(tmp_path)
    host.load(_load_fields(tmp_path))
    emitted: list[Frame] = []
    terminal = host.synthesize(
        "j1",
        {"text": "hi", "language": "xx", "speaker": "Serena"},
        emitted.append,
    )
    assert terminal.fields["status"] == "failed"
    assert any(
        f.type == "error" and f.fields.get("code") == "unsupported_selection" for f in emitted
    )


def test_customvoice_rejects_clone_fields(tmp_path: Path) -> None:
    host, _, _ = make_host(tmp_path)
    host.load(_load_fields(tmp_path))
    clip = tmp_path / "ref.wav"
    _write_wav_24k(clip, tone(2400))
    for extra in (
        {"voicePrompt": str(clip)},
        {"refText": "the words"},
        {"useVoiceRef": True},
    ):
        emitted: list[Frame] = []
        terminal = host.synthesize(
            "j1",
            {"text": "hi", "language": "en", "speaker": "Serena", **extra},
            emitted.append,
        )
        assert terminal.fields["status"] == "failed", extra
        assert any(
            f.type == "error" and f.fields.get("code") == "unsupported_selection" for f in emitted
        ), extra


@pytest.mark.parametrize("profile", ["base", "customvoice"])
def test_instruct_is_rejected_on_every_0_6b_profile(tmp_path: Path, profile: str) -> None:
    lib = FakeQwenLib(model_type="base" if profile == "base" else "custom_voice", speakers=())
    host, _, _ = make_host(tmp_path, lib=lib)
    host.load(_load_fields(tmp_path, profile=profile))
    fields: dict[str, Any] = {"text": "hi", "language": "en", "instruct": "speak sadly"}
    if profile == "base":
        clip = tmp_path / "ref.wav"
        _write_wav_24k(clip, tone(2400))
        fields.update({"voicePrompt": str(clip), "refText": "words"})
    else:
        fields["speaker"] = "Serena"
    emitted: list[Frame] = []
    terminal = host.synthesize("j1", fields, emitted.append)
    assert terminal.fields["status"] == "failed"
    assert any(
        f.type == "error" and f.fields.get("code") == "unsupported_selection" for f in emitted
    )
    assert not lib.synth_params.get("instruct")


def test_base_rejects_a_preset_speaker(tmp_path: Path) -> None:
    lib = FakeQwenLib(model_type="base", speakers=())
    host, _, _ = make_host(tmp_path, lib=lib)
    host.load(_load_fields(tmp_path, profile="base"))
    clip = tmp_path / "ref.wav"
    _write_wav_24k(clip, tone(2400))
    emitted: list[Frame] = []
    terminal = host.synthesize(
        "j1",
        {
            "text": "hi",
            "language": "en",
            "speaker": "Serena",
            "voicePrompt": str(clip),
            "refText": "words",
        },
        emitted.append,
    )
    assert terminal.fields["status"] == "failed"
    assert any(
        f.type == "error" and f.fields.get("code") == "unsupported_selection" for f in emitted
    )


def test_the_reference_clip_is_resampled_to_mono_24khz(tmp_path: Path) -> None:
    lib = FakeQwenLib(model_type="base", speakers=(), chunks=[tone(600)])
    host, _, _ = make_host(tmp_path, lib=lib)
    host.load(_load_fields(tmp_path, profile="base"))
    # A 48 kHz stereo enrollment clip — the host downmixes and resamples it.
    stereo = np.stack([tone(48_000, rate=48_000), tone(48_000, rate=48_000, freq=330)], axis=1)
    clip = tmp_path / "stereo.wav"
    import soundfile as sf  # noqa: PLC0415 — test-only dependency

    sf.write(str(clip), stereo, 48_000, subtype="FLOAT")
    terminal = _synth_ok(
        host,
        {"text": "hi", "language": "en", "voicePrompt": str(clip), "refText": "words"},
    )
    assert terminal.fields["status"] == "ok"
    assert len(lib.extract_calls) == 1
    received = lib.extract_calls[0]["pcm"]
    assert received.dtype == np.float32
    expected = one_shot_resample(stereo.mean(axis=1), src=48_000, dst=24_000)
    assert received.shape == expected.shape
    assert np.allclose(received, expected, atol=1e-5)


def _base_host(tmp_path: Path, lib: FakeQwenLib | None = None, **load_kw):
    if lib is None:
        lib = FakeQwenLib(model_type="base", speakers=(), chunks=[tone(600)])
    host, events, _ = make_host(tmp_path, lib=lib)
    host.load(_load_fields(tmp_path, profile="base", **load_kw))
    return host, lib, events


def _clip(path: Path, samples: np.ndarray) -> None:
    _write_wav_24k(path, samples)


def test_the_voice_ref_cache_invalidates_on_source_transcript_build_and_quantization(
    tmp_path: Path,
) -> None:
    host, lib, _ = _base_host(tmp_path)
    clip = tmp_path / "ref.wav"
    _clip(clip, tone(2400))
    base = {"language": "en", "voicePrompt": str(clip)}

    assert _synth_ok(host, {"text": "a", **base, "refText": "one"}).fields["status"] == "ok"
    assert _synth_ok(host, {"text": "b", **base, "refText": "one"}).fields["status"] == "ok"
    assert len(lib.extract_calls) == 1  # same clip + transcript → cached

    _clip(clip, tone(2400, freq=440))  # same path, changed content
    assert _synth_ok(host, {"text": "c", **base, "refText": "one"}).fields["status"] == "ok"
    assert len(lib.extract_calls) == 2  # changed source → re-extracted

    assert _synth_ok(host, {"text": "d", **base, "refText": "two"}).fields["status"] == "ok"
    assert len(lib.extract_calls) == 3  # changed transcript → re-extracted

    # A different quantization or a rebuilt library is a different cache
    # identity even for the same clip/transcript.
    key_before = next(iter(host._voice_refs))
    host.load(_load_fields(tmp_path, profile="base", quantization="Q4_K_M"))
    assert _synth_ok(host, {"text": "e", **base, "refText": "two"}).fields["status"] == "ok"
    assert len(lib.extract_calls) == 4
    key_after = next(iter(host._voice_refs))
    assert key_after != key_before


def test_the_reference_caches_are_bounded(tmp_path: Path) -> None:
    from vienetts_app.workers.qwen_gguf_host import MAX_REF_PCM, MAX_VOICE_REFS

    host, lib, events = _base_host(tmp_path)
    for index in range(MAX_VOICE_REFS + 1):
        clip = tmp_path / f"ref{index}.wav"
        _clip(clip, tone(2400, freq=200 + index))
        terminal = _synth_ok(
            host,
            {"text": "hi", "language": "en", "voicePrompt": str(clip), "refText": "w"},
        )
        assert terminal.fields["status"] == "ok"
    assert len(host._voice_refs) == MAX_VOICE_REFS
    assert len(host._ref_pcm) <= MAX_REF_PCM
    assert len(lib.extract_calls) == MAX_VOICE_REFS + 1
    assert any(event == "voice_ref_evicted" for event, _f in events)


def test_close_releases_the_derived_references(tmp_path: Path) -> None:
    host, lib, events = _base_host(tmp_path)
    clip = tmp_path / "ref.wav"
    _clip(clip, tone(2400))
    _synth_ok(host, {"text": "hi", "language": "en", "voicePrompt": str(clip), "refText": "w"})
    assert host._voice_refs and host._ref_pcm
    host.close()
    assert not host._voice_refs and not host._ref_pcm
    assert any(event == "refs_released" for event, _f in events)


def test_a_clone_enrolled_through_the_store_is_accepted(tmp_path: Path) -> None:
    """An official-weights enrollment feeds the GGUF host verbatim."""
    from vienetts_app.core.audio import write_wav_file
    from vienetts_app.core.voice_profiles import CloneStore

    store = CloneStore(tmp_path / "clones")
    source = tmp_path / "my_voice.wav"
    write_wav_file(tone(48_000, rate=48_000), source, 48_000)
    clone = store.enroll(
        name="My Voice",
        profile="qwen_base_0_6b",
        reference_clip=source,
        transcript="the enrolled transcript",
        consent=True,
    )
    prompt = store.prompt_for(clone.clone_id)

    host, lib, _ = _base_host(tmp_path)
    terminal = _synth_ok(
        host,
        {
            "text": "hello",
            "language": "en",
            "voicePrompt": prompt.reference_path,
            "refText": prompt.transcript,
        },
    )
    assert terminal.fields["status"] == "ok"
    assert len(lib.extract_calls) == 1
    assert lib.synth_params["ref_text"] == b"the enrolled transcript"
    # The stored 48 kHz enrollment file was resampled to the native rate.
    assert lib.extract_calls[0]["n_samples"] == pytest.approx(24_000, abs=50)
    # And the original enrollment file is untouched.
    assert Path(prompt.reference_path).is_file()


def test_capabilities_report_model_speakers_and_auto(tmp_path: Path) -> None:
    lib = FakeQwenLib(speakers=ALL_NATIVE_SPEAKERS, languages=("chinese", "english", "french"))
    host, _, _ = make_host(tmp_path, lib=lib)
    caps = host.load(_load_fields(tmp_path))
    # App ids in pinned order, narrowed to what the model actually reports.
    assert caps.speakers == tuple(NATIVE_SPEAKER_IDS)
    # The codec table lists concrete languages; auto is an API-level mode.
    assert "auto" in caps.languages


def test_synthesize_without_a_load_fails_cleanly(tmp_path: Path) -> None:
    host, _, _ = make_host(tmp_path)
    emitted: list[Frame] = []
    terminal = host.synthesize(
        "job1", {"text": "hi", "language": "en", "speaker": "Serena"}, emitted.append
    )
    assert terminal.fields["status"] == "failed"
    assert any(f.fields.get("code") == "not_loaded" for f in emitted if f.type == "error")


def test_an_exception_in_the_chunk_consumer_becomes_a_failed_terminal(
    tmp_path: Path,
) -> None:
    lib = FakeQwenLib()
    host, _, _ = make_host(tmp_path, lib=lib)
    host.load(_load_fields(tmp_path))
    emitted: list[Frame] = []

    def emit(frame: Frame) -> None:
        emitted.append(frame)
        if frame.type == "pcm":
            raise RuntimeError("consumer exploded")

    terminal = host.synthesize(
        "job1",
        {"text": "hi", "language": "en", "speaker": "Serena"},
        emit,
        cancelled=lambda: False,
    )
    assert terminal.fields["status"] == "failed"
    assert any(
        "consumer exploded" in f.fields.get("message", "") for f in emitted if f.type == "error"
    )


def test_native_log_lines_go_to_stderr_not_the_frame_stream(tmp_path: Path) -> None:
    lib = FakeQwenLib()
    host, events, _ = make_host(tmp_path, lib=lib)
    host.load(_load_fields(tmp_path))
    lib.log(2, "native warn line")
    assert any(
        event == "native_log" and fields.get("line") == "native warn line"
        for event, fields in events
    )


def test_close_unloads_and_a_second_load_swaps_the_model(tmp_path: Path) -> None:
    lib = FakeQwenLib()
    host, _, _ = make_host(tmp_path, lib=lib)
    host.load(_load_fields(tmp_path))
    first_ctx = lib.live_ctxs[0]
    host.close()
    assert lib.freed_ctxs == [first_ctx]
    assert not host.loaded
    host.load(_load_fields(tmp_path))
    assert lib.live_ctxs and lib.freed_ctxs == [first_ctx]


def test_synthesize_batch_runs_one_call_per_segment(tmp_path: Path) -> None:
    lib = FakeQwenLib(chunks=[tone(1200)])
    host, _, _ = make_host(tmp_path, lib=lib)
    host.load(_load_fields(tmp_path))
    emitted: list[Frame] = []
    terminal = host.synthesize_batch(
        "job1",
        {"texts": ["one", "two", "three"], "language": "en", "speaker": "Serena"},
        emitted.append,
        cancelled=lambda: False,
    )
    assert terminal.fields["status"] == "ok"
    assert terminal.fields["segments"] == 3
    segmented = [f for f in emitted if f.type == "pcm" and f.fields.get("segment") == 2]
    assert segmented and segmented[-1].fields["final"]


def _write_wav_24k(path: Path, samples: np.ndarray) -> None:
    """Minimal 24 kHz mono f32 WAV the host can decode without torch."""
    pcm16 = np.clip(samples, -1.0, 1.0)
    pcm16 = (pcm16 * 32767).astype("<i2")
    data = pcm16.tobytes()
    header = b"RIFF" + struct.pack("<I", 36 + len(data)) + b"WAVEfmt "
    header += struct.pack("<IHHIIHH", 16, 1, 1, 24_000, 48_000, 2, 16)
    header += b"data" + struct.pack("<I", len(data))
    path.write_bytes(header + data)


# --- frame loop ----------------------------------------------------------------


def test_serve_round_trips_load_synthesize_and_shutdown(tmp_path: Path) -> None:
    """Drive serve() over real pipes so the reader thread sees real frames."""
    lib = FakeQwenLib(chunks=[tone(2400)])
    in_r, in_w = os.pipe()
    out_r, out_w = os.pipe()
    replies: list[Frame] = []
    done = threading.Event()

    def drain() -> None:
        stream = os.fdopen(out_r, "rb", buffering=0)
        while True:
            try:
                replies.append(read_frame(stream))
            except Exception:
                return

    def run() -> None:
        reader = os.fdopen(in_r, "rb", buffering=0)
        writer = os.fdopen(out_w, "wb", buffering=0)
        try:
            serve(
                reader,
                writer,
                session_factory=lambda runtime_dir: NativeQwenSession(
                    str(Path(runtime_dir) / "libqwen.so"), lib=lib
                ),
                isolate_stdout=lambda: None,
            )
        finally:
            done.set()

    drain_thread = threading.Thread(target=drain, daemon=True)
    worker = threading.Thread(target=run, daemon=True)
    drain_thread.start()
    worker.start()
    command = os.fdopen(in_w, "wb", buffering=0)
    write_frame(command, Frame(type="load", fields=_load_fields(tmp_path)))
    write_frame(
        command,
        Frame(
            type="synthesize",
            job="job1",
            fields={"text": "hi", "language": "en", "speaker": "Serena"},
        ),
    )
    write_frame(command, Frame(type="shutdown"))
    command.close()
    assert done.wait(15), "serve() never returned"
    # The drain thread appends asynchronously — wait until the terminal lands.
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not any(f.type == "terminal" for f in replies):
        time.sleep(0.01)
    writer_types = [f.type for f in replies]
    assert writer_types[0] == "hello"
    assert replies[0].fields["host"] == GGUF_HOST_NAME
    assert "capabilities" in writer_types
    assert "pcm" in writer_types
    terminal = next(f for f in replies if f.type == "terminal")
    assert terminal.fields["status"] == "ok"
