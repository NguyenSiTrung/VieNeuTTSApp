"""Narrow-path bytes handed to qwen.dll (Windows ANSI-fopen contract).

The Windows build of qwentts.cpp opens model paths through the ANSI codepage,
so bytes it receives must be pure ASCII — the 8.3 spelling when the path has
one, the original spelling when it is already ASCII, and an actionable error
instead of an opaque load failure when neither is representable.
"""

from __future__ import annotations

import ctypes
import gc
import os
import weakref
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from vienetts_app.core import paths as paths_module
from vienetts_app.workers.qwen_gguf_abi import (
    QT_ABI_VERSION,
    QT_STATUS_OK,
    NativeAbiError,
    NativeQwenSession,
    VoiceRefData,
    _native_path_bytes,
    _QtAudio,
    _QtChunkCb,
    _QtInitParams,
    _QtLogCb,
    _QtTtsParams,
    _QtVoiceRef,
)


def test_posix_paths_go_through_as_utf8(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(os, "name", "posix")
    assert _native_path_bytes("/mô hình/model.gguf", "talker") == "/mô hình/model.gguf".encode()


@pytest.mark.skipif(os.name != "nt", reason="the ANSI contract only binds on Windows")
class TestWindowsPathBytes:
    def test_ascii_path_passes_unchanged(self) -> None:
        assert _native_path_bytes("C:\\models\\model.gguf", "talker") == b"C:\\models\\model.gguf"

    def test_an_8_3_spelling_is_used_when_ascii(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            paths_module,
            "to_short_path",
            lambda p: Path("C:\\Users\\NGUYEN~1\\model.gguf"),
        )
        assert _native_path_bytes("C:\\Users\\Nguyễn\\model.gguf", "talker") == (
            b"C:\\Users\\NGUYEN~1\\model.gguf"
        )

    def test_no_ascii_spelling_is_an_actionable_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(paths_module, "to_short_path", lambda p: Path(p))
        with pytest.raises(NativeAbiError, match="ASCII-only"):
            _native_path_bytes("C:\\Users\\Nguyễn\\model.gguf", "codec")

    def test_a_partial_spelling_is_not_silently_sent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Per-component conversion can leave the unicode component in place.
        monkeypatch.setattr(
            paths_module,
            "to_short_path",
            lambda p: Path("C:\\Users\\NGUYEN~1\\Nhạc Việt\\model.gguf"),
        )
        with pytest.raises(NativeAbiError, match="ASCII-only"):
            _native_path_bytes("C:\\Users\\Nguyễn\\Nhạc Việt\\model.gguf", "codec")


class _CtypesBoundFakeLib:
    """In-process C-ABI library whose symbols are real ``ctypes.CFUNCTYPE``s.

    When injected via ``monkeypatch.setattr(ctypes, "CDLL", ...)``,
    ``NativeQwenSession(path)`` runs the real ``_bind_signatures(lib)`` and
    enforces real ctypes ``argtypes`` / ``restype`` marshalling on every call.
    """

    def __init__(self) -> None:
        self._version_buf = ctypes.create_string_buffer(b"qwentts-abi-test-5")
        self._empty_buf = ctypes.create_string_buffer(b"")
        self._model_type_buf = ctypes.create_string_buffer(b"base")
        self.log_set_calls: list[int | None] = []
        self.current_log_addr: int | None = None
        self.on_log_set: Any = None
        self.on_synthesize: Any = None
        self.freed_ctxs: list[int] = []
        self.audio_frees = 0
        # Keep every CFUNCTYPE trampoline rooted for the fake's lifetime.
        self._pinned: list[Any] = []

        def _pin(cfn: Any) -> Any:
            self._pinned.append(cfn)
            return cfn

        def _qt_version() -> int:
            return ctypes.addressof(self._version_buf)

        def _qt_last_error() -> int:
            return ctypes.addressof(self._empty_buf)

        def _qt_init_default_params(params: Any) -> None:
            p = params.contents
            p.abi_version = QT_ABI_VERSION
            p.use_fa = True
            p.clamp_fp16 = False
            p.max_batch = 1
            p.codec_chunk_sec = 24.0

        def _qt_init(_params: Any) -> int:
            return 0x1001

        def _qt_free(ctx: int) -> None:
            self.freed_ctxs.append(ctx)

        def _qt_audio_free(_audio: Any) -> None:
            self.audio_frees += 1

        def _qt_extract_voice_ref(_ctx: Any, _pcm: Any, _n: int, _out: Any) -> int:
            return QT_STATUS_OK

        def _qt_voice_ref_free(_ref: Any) -> None:
            return None

        def _qt_log_set(cb_addr: int | None, _ud: Any) -> None:
            if self.on_log_set is not None:
                self.on_log_set(cb_addr)
            self.log_set_calls.append(cb_addr)
            self.current_log_addr = cb_addr

        def _qt_tts_default_params(params: Any) -> None:
            p = params.contents
            p.abi_version = QT_ABI_VERSION
            p.ref_codes = None
            p.ref_T = 0
            p.ref_spk_emb = None
            p.ref_spk_dim = 0

        def _qt_synthesize(_ctx: Any, params: Any, _out: Any) -> int:
            if self.on_synthesize is not None:
                return int(self.on_synthesize(params.contents))
            return QT_STATUS_OK

        def _qt_num_codebooks(_ctx: Any) -> int:
            return 16

        def _qt_duration_sec_to_tokens(_ctx: Any, seconds: float) -> int:
            return max(1, int(seconds * 12.5))

        def _qt_n_speakers(_ctx: Any) -> int:
            return 0

        def _qt_speaker_name(_ctx: Any, _idx: int) -> int:
            return ctypes.addressof(self._empty_buf)

        def _qt_n_languages(_ctx: Any) -> int:
            return 0

        def _qt_language_name(_ctx: Any, _idx: int) -> int:
            return ctypes.addressof(self._empty_buf)

        def _qt_model_type(_ctx: Any) -> int:
            return ctypes.addressof(self._model_type_buf)

        self.qt_version = _pin(ctypes.CFUNCTYPE(ctypes.c_void_p)(_qt_version))
        self.qt_last_error = _pin(ctypes.CFUNCTYPE(ctypes.c_void_p)(_qt_last_error))
        self.qt_init_default_params = _pin(
            ctypes.CFUNCTYPE(None, ctypes.POINTER(_QtInitParams))(_qt_init_default_params)
        )
        self.qt_init = _pin(
            ctypes.CFUNCTYPE(ctypes.c_void_p, ctypes.POINTER(_QtInitParams))(_qt_init)
        )
        self.qt_free = _pin(ctypes.CFUNCTYPE(None, ctypes.c_void_p)(_qt_free))
        self.qt_audio_free = _pin(ctypes.CFUNCTYPE(None, ctypes.POINTER(_QtAudio))(_qt_audio_free))
        self.qt_extract_voice_ref = _pin(
            ctypes.CFUNCTYPE(
                ctypes.c_int,
                ctypes.c_void_p,
                ctypes.POINTER(ctypes.c_float),
                ctypes.c_int,
                ctypes.POINTER(_QtVoiceRef),
            )(_qt_extract_voice_ref)
        )
        self.qt_voice_ref_free = _pin(
            ctypes.CFUNCTYPE(None, ctypes.POINTER(_QtVoiceRef))(_qt_voice_ref_free)
        )
        # Underlying C parameter is raw function pointer address; _bind_signatures
        # overrides .argtypes = [_QtLogCb, ctypes.c_void_p] so bare None fails
        # with ctypes.ArgumentError while _QtLogCb(0) marshals as NULL.
        self.qt_log_set = _pin(
            ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_void_p)(_qt_log_set)
        )
        self.qt_tts_default_params = _pin(
            ctypes.CFUNCTYPE(None, ctypes.POINTER(_QtTtsParams))(_qt_tts_default_params)
        )
        self.qt_synthesize = _pin(
            ctypes.CFUNCTYPE(
                ctypes.c_int,
                ctypes.c_void_p,
                ctypes.POINTER(_QtTtsParams),
                ctypes.POINTER(_QtAudio),
            )(_qt_synthesize)
        )
        self.qt_num_codebooks = _pin(
            ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p)(_qt_num_codebooks)
        )
        self.qt_duration_sec_to_tokens = _pin(
            ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_double)(
                _qt_duration_sec_to_tokens
            )
        )
        self.qt_n_speakers = _pin(ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p)(_qt_n_speakers))
        self.qt_speaker_name = _pin(
            ctypes.CFUNCTYPE(ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int)(_qt_speaker_name)
        )
        self.qt_n_languages = _pin(ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p)(_qt_n_languages))
        self.qt_language_name = _pin(
            ctypes.CFUNCTYPE(ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int)(_qt_language_name)
        )
        self.qt_model_type = _pin(
            ctypes.CFUNCTYPE(ctypes.c_void_p, ctypes.c_void_p)(_qt_model_type)
        )


def _bound_session(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[NativeQwenSession, _CtypesBoundFakeLib]:
    lib = _CtypesBoundFakeLib()
    monkeypatch.setattr(ctypes, "CDLL", lambda _path: lib)
    session = NativeQwenSession("/fake/libqwen.so")
    # Prove _bind_signatures actually ran and pinned _QtLogCb on qt_log_set.
    assert lib.qt_log_set.argtypes == [_QtLogCb, ctypes.c_void_p]
    return session, lib


class TestGgufAbiSafety:
    """Real-ctypes ABI safety: log detachment, swap lifetime, NULL guards, empty refs."""

    def test_close_and_set_none_detach_log_callback_with_typed_null(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        session, lib = _bound_session(monkeypatch)
        seen: list[str] = []
        session.set_log_callback(seen.append)
        assert len(lib.log_set_calls) == 1
        assert lib.log_set_calls[0]  # non-NULL trampoline address

        # Close detaches via _QtLogCb(0) through real _bind_signatures argtypes
        # (bare None would raise ctypes.ArgumentError and skip detachment).
        session.close()
        assert len(lib.log_set_calls) == 2
        assert lib.log_set_calls[1] is None  # marshalled as C NULL
        assert session._log_cb is None

        # Explicit set_log_callback(None) also detaches via _QtLogCb(0).
        session.set_log_callback(seen.append)
        assert len(lib.log_set_calls) == 3 and lib.log_set_calls[2]
        session.set_log_callback(None)
        assert len(lib.log_set_calls) == 4 and lib.log_set_calls[3] is None
        assert session._log_cb is None

    def test_close_keeps_log_callback_rooted_when_detach_fails(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        session, lib = _bound_session(monkeypatch)
        # Swap qt_log_set on a Python-injected session where detach raises.
        fail_detach = False
        calls: list[Any] = []

        def flaky_log_set(cb: Any, ud: Any) -> None:
            calls.append((cb, ud))
            if fail_detach:
                raise RuntimeError("detach failed")

        lib.qt_log_set = flaky_log_set  # type: ignore[assignment]
        session.set_log_callback(lambda _msg: None)
        rooted_cb = session._log_cb
        assert rooted_cb is not None

        fail_detach = True
        session.close()
        # Detachment failed: callback must remain rooted so native code cannot UAF.
        assert session._log_cb is rooted_cb
        assert len(calls) == 2

    def test_set_log_callback_retains_previous_callback_across_c_call(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        session, lib = _bound_session(monkeypatch)
        first_seen: list[str] = []
        second_seen: list[str] = []

        class _Sink:
            def __init__(self, out: list[str]) -> None:
                self.out = out

            def __call__(self, line: str) -> None:
                self.out.append(line)

        sink1 = _Sink(first_seen)
        sink1_ref = weakref.ref(sink1)
        session.set_log_callback(sink1)
        first_cb_id = id(session._log_cb)
        first_addr = lib.current_log_addr
        assert session._log_cb is not None and first_addr

        # Drop caller's strong references so ONLY NativeQwenSession roots sink1.
        del sink1
        gc.collect()
        assert sink1_ref() is not None

        observed_during_swap: dict[str, Any] = {}

        def _during_swap(new_addr: int | None) -> None:
            gc.collect()
            sink1_alive = sink1_ref() is not None
            observed_during_swap["sink1_alive"] = sink1_alive
            observed_during_swap["session_still_holds_first"] = id(session._log_cb) == first_cb_id
            observed_during_swap["new_addr_nonzero"] = bool(new_addr)
            if sink1_alive:
                # Native thread may still invoke the previous callback until qt_log_set finishes.
                ctypes.cast(first_addr, _QtLogCb)(1, b"mid-swap", None)

        lib.on_log_set = _during_swap
        session.set_log_callback(_Sink(second_seen))

        assert observed_during_swap == {
            "sink1_alive": True,
            "session_still_holds_first": True,
            "new_addr_nonzero": True,
        }
        assert first_seen == ["mid-swap"]
        assert id(session._log_cb) != first_cb_id
        # After qt_log_set returns, the superseded callback is released.
        gc.collect()
        assert sink1_ref() is None
        assert lib.current_log_addr and lib.current_log_addr != first_addr
        ctypes.cast(lib.current_log_addr, _QtLogCb)(1, b"after-swap", None)
        assert second_seen == ["after-swap"]

    def test_chunk_relay_guards_null_pointer_and_non_positive_counts(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        session, lib = _bound_session(monkeypatch)
        session.load("/t/talker.gguf", "/c/codec.gguf")
        verdicts: list[bool] = []

        def _synth(params: _QtTtsParams) -> int:
            chunk_cb = ctypes.cast(params.on_chunk, _QtChunkCb)
            null_ptr = ctypes.POINTER(ctypes.c_float)()
            valid_buf = (ctypes.c_float * 3)(0.25, -0.5, 0.75)
            verdicts.append(bool(chunk_cb(null_ptr, 0, None)))
            verdicts.append(bool(chunk_cb(null_ptr, 16, None)))
            verdicts.append(bool(chunk_cb(valid_buf, 0, None)))
            verdicts.append(bool(chunk_cb(valid_buf, -4, None)))
            verdicts.append(bool(chunk_cb(valid_buf, 3, None)))
            return QT_STATUS_OK

        lib.on_synthesize = _synth
        received: list[np.ndarray] = []
        session.synthesize(text="hi", lang="english", on_chunk=received.append)

        assert verdicts == [True, True, True, True, True]
        assert len(received) == 1
        assert np.allclose(received[0], np.array([0.25, -0.5, 0.75], dtype=np.float32))

    def test_synthesize_passes_null_ref_codes_when_codes_array_is_empty(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        session, lib = _bound_session(monkeypatch)
        session.load("/t/talker.gguf", "/c/codec.gguf")
        captured: list[dict[str, Any]] = []

        def _synth(params: _QtTtsParams) -> int:
            captured.append(
                {
                    "has_emb": bool(params.ref_spk_emb),
                    "ref_spk_dim": int(params.ref_spk_dim),
                    "has_codes": bool(params.ref_codes),
                    "ref_T": int(params.ref_T),
                }
            )
            return QT_STATUS_OK

        lib.on_synthesize = _synth
        emb = np.array([0.1, 0.2, 0.3], dtype=np.float32)

        for empty_shape in ((0, 0), (16, 0), (0, 3)):
            ref = VoiceRefData(
                spk_emb=emb,
                codes=np.zeros(empty_shape, dtype=np.int32),
                ref_text_usable=False,
            )
            session.synthesize(text="hi", lang="english", ref=ref)

        non_empty = VoiceRefData(
            spk_emb=emb,
            codes=np.arange(6, dtype=np.int32).reshape(2, 3),
            ref_text_usable=True,
        )
        session.synthesize(text="hi", lang="english", ref=non_empty)

        assert captured == [
            {"has_emb": True, "ref_spk_dim": 3, "has_codes": False, "ref_T": 0},
            {"has_emb": True, "ref_spk_dim": 3, "has_codes": False, "ref_T": 0},
            {"has_emb": True, "ref_spk_dim": 3, "has_codes": False, "ref_T": 0},
            {"has_emb": True, "ref_spk_dim": 3, "has_codes": True, "ref_T": 3},
        ]
