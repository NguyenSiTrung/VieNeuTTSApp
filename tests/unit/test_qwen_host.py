"""Isolated Qwen model host: load view, capabilities, synthesis, and frame loop."""

from __future__ import annotations

import contextlib
import json
import os
import sys
import threading
import time
import types
from pathlib import Path
from typing import IO, Any

import numpy as np
import pytest

from vienetts_app.core.engine_profiles import APP_SAMPLE_RATE, QWEN_SOURCE_RATE
from vienetts_app.core.qwen_engine import HOST_CHECK_FLAG
from vienetts_app.core.qwen_protocol import (
    RUNTIME_INCOMPLETE_CODE,
    EndOfStream,
    Frame,
    ProtocolError,
    pcm_from_bytes,
    read_frame,
    write_frame,
)
from vienetts_app.workers.qwen_host import (
    HOST_NAME,
    PCM_FRAME_SAMPLES,
    RESAMPLE_CHUNK_SAMPLES,
    QwenHostError,
    QwenLoadError,
    QwenModelHost,
    QwenRuntimeIncompleteError,
    StreamingResampler,
    build_load_view,
    check_main,
    check_runtime_imports,
    describe_capabilities,
    describe_import_failure,
    hello_frame,
    log_to_stderr,
    lower_process_priority,
    remove_load_view,
    serve,
)

# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def tone(samples: int, rate: int = QWEN_SOURCE_RATE, freq: float = 220.0) -> np.ndarray:
    t = np.arange(samples, dtype=np.float64) / rate
    return (0.4 * np.sin(2.0 * np.pi * freq * t)).astype(np.float32)


def one_shot_resample(
    audio: np.ndarray, src: int = QWEN_SOURCE_RATE, dst: int = APP_SAMPLE_RATE
) -> np.ndarray:
    resampler = StreamingResampler(src, dst)
    return np.concatenate([resampler.push(audio), resampler.flush()])


def model_tree(root: Path, profile: str = "customvoice") -> tuple[Path, Path]:
    """Minimal verified trees: profile weights plus the shared tokenizer."""
    profile_dir = root / profile
    shared_dir = root / "shared"
    profile_dir.mkdir(parents=True)
    (profile_dir / "config.json").write_text("{}", encoding="utf-8")
    (profile_dir / "model.safetensors").write_bytes(b"weights")
    (profile_dir / "install.json").write_text("{}", encoding="utf-8")
    (shared_dir / "speech_tokenizer").mkdir(parents=True)
    (shared_dir / "vocab.json").write_text("{}", encoding="utf-8")
    (shared_dir / "merges.txt").write_text("", encoding="utf-8")
    (shared_dir / "speech_tokenizer" / "config.json").write_text("{}", encoding="utf-8")
    return profile_dir, shared_dir


def load_fields(profile_dir: Path, shared_dir: Path, **overrides: Any) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "profile": "customvoice",
        "modelDir": str(profile_dir),
        "sharedDir": str(shared_dir),
        "device": "cpu",
        "dtype": "float32",
        "attention": "sdpa",
    }
    fields.update(overrides)
    return fields


class FakeQwenModel:
    """Duck type of ``qwen_tts.Qwen3TTSModel`` using the official card signatures."""

    def __init__(
        self,
        *,
        audio: np.ndarray | None = None,
        wavs: Any = None,
        sample_rate: int = QWEN_SOURCE_RATE,
        failure: Exception | None = None,
        speakers: list[str] | None = None,
        languages: list[str] | None = None,
        report_speakers: bool = True,
        report_languages: bool = True,
        speaker_report_error: Exception | None = None,
        gate: threading.Event | None = None,
        batch_default_mode_failure: Exception | None = None,
    ) -> None:
        self.audio_samples = tone(RESAMPLE_CHUNK_SAMPLES * 2 + 123) if audio is None else audio
        self.wavs = wavs
        self.sample_rate = sample_rate
        self.failure = failure
        self.speakers = speakers
        self.languages = languages
        self.gate = gate
        self.batch_default_mode_failure = batch_default_mode_failure
        if report_speakers:
            self.get_supported_speakers = self._report_speakers
        if report_languages:
            self.get_supported_languages = lambda: self.languages
        self.speaker_report_error = speaker_report_error
        self.custom_calls: list[dict[str, Any]] = []
        self.clone_calls: list[dict[str, Any]] = []
        self.prompt_calls: list[dict[str, Any]] = []

    def _report_speakers(self) -> list[str] | None:
        if self.speaker_report_error is not None:
            raise self.speaker_report_error
        return self.speakers

    def _ready(self, *, texts: list[Any], kwargs: dict[str, Any]) -> None:
        if self.gate is not None:
            assert self.gate.wait(timeout=5.0), "test never released the model gate"
        if self.failure is not None:
            raise self.failure
        # qwen_tts 0.1.1 NaNs a multi-item batch in its default non-streaming
        # mode whenever the items need padding (unequal lengths). The knob lets
        # tests prove the host steers around that default.
        if (
            self.batch_default_mode_failure is not None
            and len(texts) > 1
            and kwargs.get("non_streaming_mode", True)
        ):
            raise self.batch_default_mode_failure

    def _batch_result(self, count: int) -> Any:
        if self.wavs is not None:
            return self.wavs
        return [self.audio_samples] * count

    def generate_custom_voice(self, *, text: Any, language: str, speaker: str, **kwargs: Any):
        texts = text if isinstance(text, list) else [text]
        self.custom_calls.append({"text": text, "language": language, "speaker": speaker, **kwargs})
        self._ready(texts=texts, kwargs=kwargs)
        return self._batch_result(len(texts)), self.sample_rate

    def generate_voice_clone(
        self, *, text: Any, language: str, voice_clone_prompt: Any, **kwargs: Any
    ):
        texts = text if isinstance(text, list) else [text]
        self.clone_calls.append(
            {"text": text, "language": language, "voice_clone_prompt": voice_clone_prompt, **kwargs}
        )
        self._ready(texts=texts, kwargs=kwargs)
        return self._batch_result(len(texts)), self.sample_rate

    def create_voice_clone_prompt(self, *, ref_audio: str, ref_text: str) -> dict[str, Any]:
        self.prompt_calls.append({"ref_audio": ref_audio, "ref_text": ref_text})
        return {"ref_audio": ref_audio, "ref_text": ref_text}


class SynthRun:
    """Recorded frames of one ``synthesize`` call, with convenience views."""

    def __init__(self, frames: list[Frame]) -> None:
        self.frames = frames

    def types(self) -> list[str]:
        return [frame.type for frame in self.frames]

    @property
    def pcm_frames(self) -> list[Frame]:
        return [frame for frame in self.frames if frame.type == "pcm"]

    @property
    def pcm(self) -> np.ndarray:
        samples: list[float] = []
        for frame in self.pcm_frames:
            samples.extend(pcm_from_bytes(frame.payload))
        return np.asarray(samples, dtype=np.float32)

    @property
    def terminal(self) -> Frame:
        terminals = [frame for frame in self.frames if frame.type == "terminal"]
        assert len(terminals) == 1, f"expected one terminal, got {self.types()}"
        return terminals[0]

    def errors(self) -> list[Frame]:
        return [frame for frame in self.frames if frame.type == "error"]


def loaded_host(
    tmp_path: Path,
    model: FakeQwenModel | None = None,
    *,
    profile: str = "customvoice",
    log: Any = None,
) -> tuple[QwenModelHost, FakeQwenModel]:
    model = FakeQwenModel() if model is None else model
    profile_dir, shared_dir = model_tree(tmp_path, profile)
    host = QwenModelHost(loader=lambda *_args, **_kwargs: model, log=log)
    host.load(load_fields(profile_dir, shared_dir, profile=profile))
    return host, model


def run_synth(
    host: QwenModelHost,
    fields: dict[str, Any] | None = None,
    *,
    cancelled: Any = lambda: False,
    job: str = "job-1",
) -> SynthRun:
    frames: list[Frame] = []
    request: dict[str, Any] = {"text": "hello", "language": "en", "speaker": "Ryan"}
    request.update(fields or {})
    frames.append(host.synthesize(job, request, frames.append, cancelled=cancelled))
    return SynthRun(frames)


# --------------------------------------------------------------------------- #
# resampler (ported from the prior Qwen branch)
# --------------------------------------------------------------------------- #


class TestStreamingResampler:
    def test_resampler_contract(self) -> None:
        # The GGUF host shares this resampler; qwen_host re-exports it so the
        # old import surface keeps working.
        from vienetts_app.core import streaming_resampler

        assert StreamingResampler is streaming_resampler.StreamingResampler

        for bad in (0, -24000, 24.5, "24000", True, None):
            with pytest.raises(ValueError, match="src_rate"):
                StreamingResampler(bad, APP_SAMPLE_RATE)  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="dst_rate"):
            StreamingResampler(QWEN_SOURCE_RATE, 0)
        resampler = StreamingResampler(QWEN_SOURCE_RATE, APP_SAMPLE_RATE)
        assert (resampler.src_rate, resampler.dst_rate) == (QWEN_SOURCE_RATE, APP_SAMPLE_RATE)
        with pytest.raises(ValueError, match="1-D mono"):
            resampler.push(np.zeros((100, 2), dtype=np.float32))
        with pytest.raises(ValueError, match="numpy array"):
            resampler.push([0.0, 0.1])  # type: ignore[arg-type]
        non_finite = np.zeros(100, dtype=np.float32)
        non_finite[10] = np.nan
        with pytest.raises(ValueError, match="finite"):
            resampler.push(non_finite)
        assert resampler.push(np.zeros(10, dtype=np.float64)).dtype == np.float32

    def test_resampling_math(self) -> None:
        data = tone(QWEN_SOURCE_RATE)
        want = one_shot_resample(data)
        assert abs(want.size - 2 * data.size) <= 1
        for sizes in ([data.size], [6000] * 4, [7, 999, 3, data.size - 1009]):
            resampler = StreamingResampler(QWEN_SOURCE_RATE, APP_SAMPLE_RATE)
            parts: list[np.ndarray] = []
            offset = 0
            for size in sizes:
                parts.append(resampler.push(data[offset : offset + size]))
                offset += size
            parts.append(resampler.flush())
            got = np.concatenate(parts)
            assert got.size == want.size
            np.testing.assert_allclose(got, want, rtol=1e-5, atol=1e-6)
        ideal = tone(want.size, APP_SAMPLE_RATE)
        correlation = float(np.corrcoef(want.astype(np.float64), ideal.astype(np.float64))[0, 1])
        assert correlation > 0.999
        # The carried state is what keeps a chunked stream identical to a one-shot
        # resample, so a boundary phase reset cannot introduce a click.
        stateful = StreamingResampler(QWEN_SOURCE_RATE, APP_SAMPLE_RATE)
        joined = np.concatenate(
            [stateful.push(data[:12000]), stateful.push(data[12000:]), stateful.flush()]
        )
        np.testing.assert_allclose(joined, want, rtol=1e-5, atol=1e-6)

        data = tone(48000, APP_SAMPLE_RATE)
        resampler = StreamingResampler(APP_SAMPLE_RATE, APP_SAMPLE_RATE)
        assert np.array_equal(
            np.concatenate(
                [resampler.push(data[:10000]), resampler.push(data[10000:]), resampler.flush()]
            ),
            data,
        )
        down = one_shot_resample(tone(48000, APP_SAMPLE_RATE), APP_SAMPLE_RATE, QWEN_SOURCE_RATE)
        assert abs(down.size - 24000) <= 1

    def test_empty_flush_and_reset(self) -> None:
        resampler = StreamingResampler(QWEN_SOURCE_RATE, APP_SAMPLE_RATE)
        assert resampler.push(np.zeros(0, dtype=np.float32)).size == 0
        assert resampler.flush().size == 0
        data = tone(24000)
        resampler.push(data)
        resampler.reset()
        got = np.concatenate([resampler.push(data), resampler.flush()])
        np.testing.assert_allclose(got, one_shot_resample(data), rtol=1e-5, atol=1e-6)


# --------------------------------------------------------------------------- #
# load view
# --------------------------------------------------------------------------- #


class TestLoadView:
    def test_view_construction(self, tmp_path: Path, monkeypatch) -> None:
        profile_dir, shared_dir = model_tree(tmp_path)
        view = build_load_view(profile_dir, shared_dir, tmp_path / ".load" / "customvoice")
        for relative in ("config.json", "model.safetensors", "vocab.json", "merges.txt"):
            assert (view / relative).is_file(), relative
        assert (view / "speech_tokenizer" / "config.json").is_file()
        assert not (view / "install.json").exists()
        assert os.path.samefile(view / "model.safetensors", profile_dir / "model.safetensors")
        assert os.path.samefile(view / "vocab.json", shared_dir / "vocab.json")

        tmp_path = tmp_path / "case-b"
        tmp_path.mkdir(parents=True, exist_ok=True)

        profile_dir, shared_dir = model_tree(tmp_path)
        view = tmp_path / ".load" / "customvoice"
        build_load_view(profile_dir, shared_dir, view)
        (view / "vocab.json").unlink()
        build_load_view(profile_dir, shared_dir, view)
        assert os.path.samefile(view / "vocab.json", shared_dir / "vocab.json")
        (view / "vocab.json").unlink()
        (view / "vocab.json").write_text("stale copy", encoding="utf-8")
        (view / "merges.txt").unlink()
        (view / "merges.txt").symlink_to(tmp_path / "gone")
        build_load_view(profile_dir, shared_dir, view)
        assert os.path.samefile(view / "vocab.json", shared_dir / "vocab.json")
        assert os.path.samefile(view / "merges.txt", shared_dir / "merges.txt")

        tmp_path = tmp_path / "case-b"
        tmp_path.mkdir(parents=True, exist_ok=True)

        profile_dir, shared_dir = model_tree(tmp_path)

        def refuse_link(*_args: Any, **_kwargs: Any) -> None:
            raise OSError("hard links are not supported here")

        monkeypatch.setattr(os, "link", refuse_link)
        view = build_load_view(profile_dir, shared_dir, tmp_path / ".load" / "customvoice")
        assert (view / "vocab.json").is_symlink()
        assert os.path.samefile(view / "vocab.json", shared_dir / "vocab.json")

        tmp_path = tmp_path / "case-b"
        tmp_path.mkdir(parents=True, exist_ok=True)

        profile_dir, shared_dir = model_tree(tmp_path)

        def refuse(*_args: Any, **_kwargs: Any) -> None:
            raise OSError("links are unavailable")

        monkeypatch.setattr(os, "link", refuse)
        monkeypatch.setattr(os, "symlink", refuse)
        view = build_load_view(profile_dir, shared_dir, tmp_path / ".load" / "customvoice")
        assert not (view / "vocab.json").is_symlink()
        assert (view / "vocab.json").read_text(encoding="utf-8") == (
            shared_dir / "vocab.json"
        ).read_text(encoding="utf-8")

    def test_view_rejections(self, tmp_path: Path) -> None:
        profile_dir = tmp_path / "customvoice"
        profile_dir.mkdir()
        (profile_dir / "install.json").write_text("{}", encoding="utf-8")
        shared_dir = tmp_path / "shared"
        shared_dir.mkdir()
        with pytest.raises(QwenLoadError, match="empty"):
            build_load_view(profile_dir, shared_dir, tmp_path / "view")

        tmp_path = tmp_path / "case-b"
        tmp_path.mkdir(parents=True, exist_ok=True)

        with pytest.raises(QwenLoadError, match="model directory"):
            build_load_view(tmp_path / "absent", tmp_path / "also-absent", tmp_path / "view")
        profile_dir, shared_dir = model_tree(tmp_path)
        assert shared_dir.is_dir()
        with pytest.raises(QwenLoadError, match="shared"):
            build_load_view(profile_dir, tmp_path / "gone", tmp_path / "view")

    def test_removal_safety(self, tmp_path: Path) -> None:
        profile_dir, shared_dir = model_tree(tmp_path)
        view = build_load_view(profile_dir, shared_dir, tmp_path / ".load" / "customvoice")
        remove_load_view(view)
        assert not view.exists()
        assert (profile_dir / "model.safetensors").is_file()
        assert (shared_dir / "vocab.json").is_file()
        remove_load_view(view)  # idempotent

        tmp_path = tmp_path / "case-b"
        tmp_path.mkdir(parents=True, exist_ok=True)

        real = tmp_path / "real"
        real.mkdir()
        (real / "keep.txt").write_text("keep", encoding="utf-8")
        view = tmp_path / ".load" / "customvoice"
        view.parent.mkdir(parents=True)
        view.symlink_to(real, target_is_directory=True)
        remove_load_view(view)
        assert (real / "keep.txt").is_file()


# --------------------------------------------------------------------------- #
# capabilities
# --------------------------------------------------------------------------- #


class TestCapabilities:
    def test_capability_mapping(self) -> None:
        custom = describe_capabilities("customvoice")
        assert custom.profile == "customvoice"
        assert len(custom.speakers) == 9
        assert "Vivian" in custom.speakers
        assert custom.languages[0] == "auto"
        assert {"zh", "en", "ja", "ko", "de", "fr", "ru", "pt", "es", "it"} <= set(custom.languages)
        assert custom.supports_clone is False
        assert custom.sample_rate == APP_SAMPLE_RATE
        base = describe_capabilities("base")
        assert base.speakers == ()
        assert base.supports_clone is True

        model = FakeQwenModel(
            speakers=["ryan", "vivian", "sohee"], languages=["auto", "english", "chinese"]
        )
        custom = describe_capabilities("customvoice", model)
        assert custom.speakers == ("Vivian", "Ryan", "Sohee")  # pinned display order
        assert custom.languages == ("auto", "zh", "en")  # pinned order, narrowed to the report

        events: list[str] = []
        model = FakeQwenModel(speakers=["nobody"], languages=["klingon"])
        caps = describe_capabilities(
            "customvoice", model, log=lambda event, **_fields: events.append(event)
        )
        assert "Vivian" in caps.speakers
        assert "zh" in caps.languages
        assert events == ["capabilities_fallback", "capabilities_fallback"]

    def test_capability_fallbacks(self) -> None:
        model = FakeQwenModel(report_speakers=False, report_languages=False)
        caps = describe_capabilities("base", model)
        assert caps.speakers == ()
        assert "zh" in caps.languages

        events: list[str] = []
        model = FakeQwenModel(speaker_report_error=RuntimeError("no speaker table"))
        caps = describe_capabilities(
            "customvoice", model, log=lambda event, **_fields: events.append(event)
        )
        assert "Vivian" in caps.speakers
        assert events == []  # an absent report is normal, not a fallback warning

        with pytest.raises(QwenHostError, match="profile"):
            describe_capabilities("vieneu")


# --------------------------------------------------------------------------- #
# load
# --------------------------------------------------------------------------- #


class TestLoad:
    def test_load_and_reload(self, tmp_path: Path) -> None:
        calls: list[dict[str, Any]] = []
        model = FakeQwenModel()

        def loader(path: str, *, device: str, dtype: str, attention: str, profile: str):
            calls.append(
                {
                    "path": Path(path),
                    "device": device,
                    "dtype": dtype,
                    "attention": attention,
                    "profile": profile,
                }
            )
            return model

        profile_dir, shared_dir = model_tree(tmp_path)
        host = QwenModelHost(loader=loader)
        try:
            caps = host.load(
                load_fields(
                    profile_dir,
                    shared_dir,
                    device="cuda",
                    dtype="bfloat16",
                    attention="flash_attention_2",
                )
            )
            (call,) = calls
            assert call["path"] == tmp_path / ".load" / "customvoice"
            assert (call["path"] / "vocab.json").is_file()
            assert (call["path"] / "config.json").is_file()
            assert host.loaded is True
            assert host.profile == "customvoice"
            assert host.capabilities is not None
            assert caps.sample_rate == APP_SAMPLE_RATE
        finally:
            host.close()
        assert call["device"] == "cuda"
        assert call["dtype"] == "bfloat16"
        assert call["attention"] == "flash_attention_2"
        assert call["profile"] == "customvoice"

        tmp_path = tmp_path / "case-b"
        tmp_path.mkdir(parents=True, exist_ok=True)

        reference = tmp_path / "reference.wav"
        reference.write_bytes(b"RIFF")
        models: list[FakeQwenModel] = []

        def loader(*_args: Any, **_kwargs: Any) -> FakeQwenModel:
            model = FakeQwenModel()
            models.append(model)
            return model

        profile_dir, shared_dir = model_tree(tmp_path, "base")
        host = QwenModelHost(loader=loader)
        try:
            for _ in range(2):
                host.load(load_fields(profile_dir, shared_dir, profile="base"))
                run_synth(
                    host,
                    {
                        "language": "en",
                        "speaker": "",
                        "voicePrompt": str(reference),
                        "refText": "hi",
                    },
                )
        finally:
            host.close()
        assert len(models) == 2
        assert len(models[0].prompt_calls) == 1
        assert len(models[1].prompt_calls) == 1  # the second load rebuilt it, not reused it
        assert models[0].clone_calls[0]["voice_clone_prompt"] is not None

    @pytest.mark.parametrize(
        ("override", "message"),
        [
            ({"dtype": "float64"}, "dtype"),
            ({"attention": "magic"}, "attention"),
            ({"device": "tpu"}, "device"),
            ({"modelDir": "/nonexistent/qwen"}, "model directory"),
        ],
    )
    def test_rejects_unsupported_selection_before_loading(
        self, tmp_path: Path, override: dict[str, str], message: str
    ) -> None:
        loaded = FakeQwenModel()
        host = QwenModelHost(loader=lambda *_a, **_k: loaded)
        profile_dir, shared_dir = model_tree(tmp_path)
        fields = load_fields(profile_dir, shared_dir, **override)
        if "modelDir" in override:
            fields["sharedDir"] = str(shared_dir)
        with pytest.raises(QwenLoadError, match=message):
            host.load(fields)
        assert host.loaded is False

    def test_load_rejections(self, tmp_path: Path) -> None:
        def loader(*_args: Any, **_kwargs: Any) -> Any:
            raise RuntimeError("no qwen runtime installed")

        host = QwenModelHost(loader=loader)
        profile_dir, shared_dir = model_tree(tmp_path)
        with pytest.raises(QwenLoadError, match="no qwen runtime installed"):
            host.load(load_fields(profile_dir, shared_dir))
        assert host.loaded is False
        assert not (tmp_path / ".load" / "customvoice").exists()

        tmp_path = tmp_path / "case-b"
        tmp_path.mkdir(parents=True, exist_ok=True)

        host = QwenModelHost(loader=lambda *_args, **_kwargs: FakeQwenModel())
        profile_dir, shared_dir = model_tree(tmp_path)
        with pytest.raises(QwenLoadError, match="unsupported Qwen profile"):
            host.load(load_fields(profile_dir, shared_dir, profile="vieneu"))
        with pytest.raises(QwenLoadError, match="shared tokenizer"):
            host.load(load_fields(profile_dir, tmp_path / "gone"))
        assert host.loaded is False

        tmp_path = tmp_path / "case-b"
        tmp_path.mkdir(parents=True, exist_ok=True)

        def loader(*_args: Any, **_kwargs: Any) -> Any:
            raise QwenLoadError("the managed runtime is not installed")

        host = QwenModelHost(loader=loader)
        profile_dir, shared_dir = model_tree(tmp_path)
        with pytest.raises(QwenLoadError, match="^the managed runtime is not installed$"):
            host.load(load_fields(profile_dir, shared_dir))


# --------------------------------------------------------------------------- #
# synthesis
# --------------------------------------------------------------------------- #


class TestSynthesize:
    def test_voice_prompt_paths(self, tmp_path: Path) -> None:
        host, model = loaded_host(tmp_path)
        try:
            run = run_synth(
                host, {"text": "你好世界", "language": "zh", "speaker": "Sohee", "instruct": "calm"}
            )
        finally:
            host.close()
        (call,) = model.custom_calls
        assert call["text"] == "你好世界"
        assert call["language"] == "Chinese"
        assert call["speaker"] == "Sohee"
        assert "instruct" not in call  # the 0.6B checkpoint ignores it
        assert run.terminal.get("status") == "ok"

        tmp_path = tmp_path / "case-b"
        tmp_path.mkdir(parents=True, exist_ok=True)

        reference = tmp_path / "reference.wav"
        reference.write_bytes(b"RIFF")
        host, model = loaded_host(tmp_path, profile="base")
        request = {
            "language": "en",
            "speaker": "",
            "voicePrompt": str(reference),
            "refText": "hello there",
        }
        try:
            first = run_synth(host, request, job="job-1")
            second = run_synth(host, request, job="job-2")
        finally:
            host.close()
        assert first.terminal.get("status") == "ok"
        assert second.terminal.get("status") == "ok"
        (prompt_call,) = model.prompt_calls
        assert Path(prompt_call["ref_audio"]) == reference.resolve()
        assert prompt_call["ref_text"] == "hello there"
        assert len(model.clone_calls) == 2
        assert (
            model.clone_calls[0]["voice_clone_prompt"] == model.clone_calls[1]["voice_clone_prompt"]
        )
        assert model.clone_calls[0]["language"] == "English"

        tmp_path = tmp_path / "case-b"
        tmp_path.mkdir(parents=True, exist_ok=True)

        reference = tmp_path / "reference.wav"
        reference.write_bytes(b"RIFF")
        model = FakeQwenModel()
        model.create_voice_clone_prompt = None  # type: ignore[method-assign]
        host, _model = loaded_host(tmp_path, model, profile="base")
        try:
            run = run_synth(
                host,
                {"language": "en", "speaker": "", "voicePrompt": str(reference), "refText": "hi"},
            )
        finally:
            host.close()
        assert run.terminal.get("status") == "failed"
        assert run.errors()[0].get("code") == "clone_prompt_unsupported"

    @pytest.mark.parametrize(
        ("fields", "message"),
        [
            ({"language": "vi"}, "Vietnamese"),
            ({"language": "xx"}, "language"),
            ({"speaker": "Nobody"}, "Nobody"),
            ({"speaker": ""}, "speaker"),
        ],
    )
    def test_unsupported_selection_fails_the_job_before_generating(
        self, tmp_path: Path, fields: dict[str, str], message: str
    ) -> None:
        host, model = loaded_host(tmp_path)
        try:
            run = run_synth(host, fields)
        finally:
            host.close()
        assert model.custom_calls == []
        assert run.terminal.get("status") == "failed"
        assert message in run.terminal.get("error", "")
        (error,) = run.errors()
        assert error.get("code") == "unsupported_selection"
        assert error.get("fatal") is False

    @pytest.mark.parametrize(
        ("overrides", "message"),
        [
            ({"voicePrompt": "", "refText": "hi"}, "reference clip"),
            ({"voicePrompt": "/tmp/x.wav", "refText": "  "}, "transcript"),
            ({"voicePrompt": "/tmp/absent.wav", "refText": "hi"}, "reference clip is missing"),
        ],
    )
    def test_base_requires_a_reference_clip_and_transcript(
        self, tmp_path: Path, overrides: dict[str, str], message: str
    ) -> None:
        host, model = loaded_host(tmp_path, profile="base")
        try:
            run = run_synth(host, {"language": "en", "speaker": "", **overrides})
        finally:
            host.close()
        assert model.clone_calls == []
        assert run.terminal.get("status") == "failed"
        assert message in run.terminal.get("error", "")

    def test_pcm_contract(self, tmp_path: Path) -> None:
        host, model = loaded_host(tmp_path)
        try:
            run = run_synth(host)
        finally:
            host.close()
        frames = run.pcm_frames
        assert len(frames) > 1
        assert [frame.get("seq") for frame in frames] == list(range(len(frames)))
        assert all(frame.get("sampleRate") == APP_SAMPLE_RATE for frame in frames)
        assert all(len(frame.payload) <= PCM_FRAME_SAMPLES * 4 for frame in frames)
        assert [frame.get("final") for frame in frames] == [False] * (len(frames) - 1) + [True]
        assert run.terminal.get("frames") == len(frames)
        expected_seconds = model.audio_samples.size * 2 / APP_SAMPLE_RATE
        assert run.terminal.get("audioSeconds") == pytest.approx(expected_seconds, abs=0.001)

        tmp_path = tmp_path / "case-b"
        tmp_path.mkdir(parents=True, exist_ok=True)

        host, model = loaded_host(tmp_path)
        try:
            run = run_synth(host)
        finally:
            host.close()
        assert model.audio_samples.size > RESAMPLE_CHUNK_SAMPLES  # forces several pushes
        np.testing.assert_allclose(
            run.pcm, one_shot_resample(model.audio_samples), rtol=1e-6, atol=1e-6
        )

        tmp_path = tmp_path / "case-b"
        tmp_path.mkdir(parents=True, exist_ok=True)

        host, _model = loaded_host(tmp_path)
        try:
            run = run_synth(host)
        finally:
            host.close()
        progress = [frame for frame in run.frames if frame.type == "progress"]
        assert progress
        fractions = [frame.get("fraction") for frame in progress]
        assert all(0.0 <= value <= 1.0 for value in fractions)
        assert fractions == sorted(fractions)
        assert all(frame.get("stage") for frame in progress)

    def test_synthesize_failures(self, tmp_path: Path) -> None:
        host, _model = loaded_host(tmp_path, FakeQwenModel(sample_rate=16000))
        try:
            run = run_synth(host)
        finally:
            host.close()
        assert run.terminal.get("status") == "failed"
        (error,) = run.errors()
        assert error.get("code") == "sample_rate_drift"
        assert run.pcm_frames == []

        tmp_path = tmp_path / "case-b"
        tmp_path.mkdir(parents=True, exist_ok=True)

        host, _model = loaded_host(tmp_path, FakeQwenModel(failure=ValueError("bad prompt")))
        try:
            run = run_synth(host)
            assert host.loaded is True
            assert host.fatal is False
        finally:
            host.close()
        assert run.terminal.get("status") == "failed"
        (error,) = run.errors()
        assert error.get("code") == "generation_failed"
        assert error.get("fatal") is False

        tmp_path = tmp_path / "case-b"
        tmp_path.mkdir(parents=True, exist_ok=True)

        host, _model = loaded_host(
            tmp_path, FakeQwenModel(failure=RuntimeError("CUDA out of memory. Tried to allocate"))
        )
        run = run_synth(host)
        assert run.terminal.get("status") == "failed"
        (error,) = run.errors()
        assert error.get("fatal") is True
        assert host.fatal is True
        assert host.loaded is False
        assert not (tmp_path / ".load" / "customvoice").exists()

    @pytest.mark.parametrize(
        "audio",
        [
            np.zeros(0, dtype=np.float32),
            np.zeros((64, 2), dtype=np.float32),
        ],
    )
    def test_invalid_audio_fails_the_job(self, tmp_path: Path, audio: np.ndarray) -> None:
        host, _model = loaded_host(tmp_path, FakeQwenModel(audio=audio))
        try:
            run = run_synth(host)
        finally:
            host.close()
        assert run.terminal.get("status") == "failed"
        assert run.errors()[0].get("code") == "generation_failed"

    @pytest.mark.parametrize(
        "wavs",
        [
            [],
            [np.array([0.0, np.nan, 0.1], dtype=np.float32)],
        ],
    )
    def test_malformed_model_output_fails_the_job(self, tmp_path: Path, wavs: list) -> None:
        host, _model = loaded_host(tmp_path, FakeQwenModel(wavs=wavs))
        try:
            run = run_synth(host)
        finally:
            host.close()
        assert run.terminal.get("status") == "failed"
        assert run.errors()[0].get("code") == "generation_failed"

    def test_cancel_and_unloaded(self, tmp_path: Path) -> None:
        host, _model = loaded_host(tmp_path)
        checks = {"count": 0}

        def cancelled() -> bool:
            checks["count"] += 1
            return checks["count"] > 2  # let the first resampled chunk out

        try:
            cancelled_run = run_synth(host, cancelled=cancelled)
            full_run = run_synth(host, job="job-2")
        finally:
            host.close()
        assert cancelled_run.terminal.get("status") == "cancelled"
        assert 0 < len(cancelled_run.pcm_frames) < len(full_run.pcm_frames)
        assert not any(frame.get("final") for frame in cancelled_run.pcm_frames)
        assert cancelled_run.terminal.get("frames") == len(cancelled_run.pcm_frames)

        tmp_path = tmp_path / "case-b"
        tmp_path.mkdir(parents=True, exist_ok=True)

        host = QwenModelHost()
        run = run_synth(host)
        assert run.terminal.get("status") == "failed"
        assert run.errors()[0].get("code") == "not_loaded"


# --------------------------------------------------------------------------- #
# loader + logging
# --------------------------------------------------------------------------- #


class TestDefaultLoader:
    def test_forces_offline_local_only_loading(self, tmp_path: Path, monkeypatch) -> None:
        recorded: dict[str, Any] = {}

        class FakeRuntimeModel:
            @staticmethod
            def from_pretrained(path: str, **kwargs: Any) -> object:
                recorded.update({"path": path, **kwargs})
                return object()

        monkeypatch.setitem(
            sys.modules,
            "qwen_tts",
            types.SimpleNamespace(Qwen3TTSModel=FakeRuntimeModel),
        )
        monkeypatch.setitem(sys.modules, "torch", types.SimpleNamespace(float32="float32"))
        monkeypatch.delenv("HF_HUB_OFFLINE", raising=False)
        monkeypatch.delenv("TRANSFORMERS_OFFLINE", raising=False)

        from vienetts_app.workers.qwen_host import default_model_loader

        default_model_loader(
            tmp_path, device="cpu", dtype="float32", attention="sdpa", profile="customvoice"
        )
        assert recorded["path"] == str(tmp_path)
        assert recorded["local_files_only"] is True
        assert recorded["trust_remote_code"] is False
        assert recorded["device_map"] == "cpu"
        assert recorded["attn_implementation"] == "sdpa"
        assert recorded["dtype"] == "float32"
        assert os.environ["HF_HUB_OFFLINE"] == "1"
        assert os.environ["TRANSFORMERS_OFFLINE"] == "1"


class TestRuntimeImportCheck:
    """A runtime that cannot import is a runtime problem, not a model problem."""

    def test_import_failure_kinds(self) -> None:
        missing = ModuleNotFoundError("No module named 'sox'", name="sox")
        assert describe_import_failure(missing) == (
            "the managed Qwen runtime is incomplete: Python module 'sox' is missing"
        )

        broken = ImportError("dlopen(libomp.dylib, 0x0005): tried: 'libomp.dylib'")
        assert describe_import_failure(broken) == (
            "the managed Qwen runtime cannot import its stack: "
            "dlopen(libomp.dylib, 0x0005): tried: 'libomp.dylib'"
        )

    def test_load_failure_blame(self, tmp_path: Path, monkeypatch) -> None:
        """The exact defect: ``qwen_tts`` imports a module the closure lacks."""
        package = tmp_path / "site-packages" / "qwen_tts"
        package.mkdir(parents=True)
        (package / "__init__.py").write_text("import vienetts_missing_probe\n", encoding="utf-8")
        monkeypatch.syspath_prepend(str(tmp_path / "site-packages"))
        monkeypatch.setitem(sys.modules, "torch", types.SimpleNamespace(float32="float32"))
        monkeypatch.delitem(sys.modules, "qwen_tts", raising=False)  # a fresh import

        from vienetts_app.workers.qwen_host import default_model_loader

        with pytest.raises(QwenRuntimeIncompleteError) as failure:
            default_model_loader(
                tmp_path, device="cpu", dtype="float32", attention="sdpa", profile="customvoice"
            )
        assert failure.value.code == RUNTIME_INCOMPLETE_CODE
        assert "Python module 'vienetts_missing_probe' is missing" in str(failure.value)
        assert "Settings" in str(failure.value)

        tmp_path = tmp_path / "case-b"
        tmp_path.mkdir(parents=True, exist_ok=True)

        """A missing module is the runtime's fault; a missing tree is not."""
        monkeypatch.setitem(sys.modules, "torch", types.SimpleNamespace(float32="float32"))
        monkeypatch.setitem(sys.modules, "qwen_tts", None)  # `import qwen_tts` fails

        from vienetts_app.workers.qwen_host import default_model_loader

        with pytest.raises(QwenRuntimeIncompleteError) as failure:
            default_model_loader(
                tmp_path, device="cpu", dtype="float32", attention="sdpa", profile="customvoice"
            )
        assert failure.value.code == RUNTIME_INCOMPLETE_CODE

    def test_check_mode_contract(self, tmp_path: Path, monkeypatch, capsys) -> None:
        package = tmp_path / "site-packages" / "qwen_tts"
        package.mkdir(parents=True)
        (package / "__init__.py").write_text("Qwen3TTSModel = object\n", encoding="utf-8")
        monkeypatch.syspath_prepend(str(tmp_path / "site-packages"))
        monkeypatch.setitem(sys.modules, "torch", types.SimpleNamespace(float32="float32"))
        monkeypatch.delitem(sys.modules, "qwen_tts", raising=False)  # a fresh import

        assert check_runtime_imports() == (True, "")

        tmp_path = tmp_path / "case-b"
        tmp_path.mkdir(parents=True, exist_ok=True)
        monkeypatch.undo()

        import vienetts_app.workers.qwen_host as host_module

        def noisy_import():
            print("third-party startup banner")
            return types.SimpleNamespace(Qwen3TTSModel=object)

        monkeypatch.setattr(host_module, "import_qwen_sdk", noisy_import)

        assert check_runtime_imports() == (True, "")
        assert capsys.readouterr().out == ""

        tmp_path = tmp_path / "case-b"
        tmp_path.mkdir(parents=True, exist_ok=True)
        monkeypatch.undo()

        package = tmp_path / "site-packages" / "qwen_tts"
        package.mkdir(parents=True)
        (package / "__init__.py").write_text("import vienetts_missing_probe\n", encoding="utf-8")
        monkeypatch.syspath_prepend(str(tmp_path / "site-packages"))
        monkeypatch.setitem(sys.modules, "torch", types.SimpleNamespace(float32="float32"))
        monkeypatch.delitem(sys.modules, "qwen_tts", raising=False)  # a fresh import

        ok, detail = check_runtime_imports()

        assert ok is False
        assert "Python module 'vienetts_missing_probe' is missing" in detail

    def test_check_mode_output(self, monkeypatch, capsys: pytest.CaptureFixture[str]) -> None:
        import vienetts_app.workers.qwen_host as host_module

        monkeypatch.setattr(host_module, "check_runtime_imports", lambda: (False, "no module"))
        assert check_main() == 1
        out = capsys.readouterr().out.strip().splitlines()
        assert json.loads(out[-1]) == {"ok": False, "detail": "no module"}

        monkeypatch.setattr(host_module, "check_runtime_imports", lambda: (True, ""))
        assert check_main() == 0
        out = capsys.readouterr().out.strip().splitlines()
        assert json.loads(out[-1]) == {"ok": True, "detail": ""}

        monkeypatch.undo()

        import vienetts_app.workers.qwen_host as host_module

        monkeypatch.setattr(host_module, "check_runtime_imports", lambda: (True, ""))
        monkeypatch.setattr(sys, "stdin", None)  # a check never speaks the protocol

        assert host_module.main([HOST_CHECK_FLAG]) == 0
        assert json.loads(capsys.readouterr().out.strip().splitlines()[-1])["ok"] is True


class TestLogging:
    def test_logging_contract(self, capsys, monkeypatch) -> None:
        log_to_stderr("loaded", profile="customvoice", device="cpu")
        captured = capsys.readouterr()
        assert captured.out == ""
        assert f'"host": "{HOST_NAME}"' in captured.err
        assert '"event": "loaded"' in captured.err
        assert '"profile": "customvoice"' in captured.err

        monkeypatch.undo()

        class BrokenStderr:
            def write(self, _text: str) -> int:
                raise OSError("stderr is gone")

            def flush(self) -> None:
                raise OSError("stderr is gone")

        monkeypatch.setattr(sys, "stderr", BrokenStderr())
        log_to_stderr("loaded")
        monkeypatch.setattr(sys, "stderr", None)
        log_to_stderr("loaded")  # a windowed build has no stderr at all

        monkeypatch.undo()

        from vienetts_app.workers.qwen_host import main

        monkeypatch.setattr(sys, "stdin", None)
        assert main([]) == 2

    def test_hello_frame_reports_the_host_contract(self) -> None:
        frame = hello_frame()
        assert frame.type == "hello"
        assert frame.job == ""
        assert frame.get("host") == HOST_NAME
        assert frame.get("sampleRate") == APP_SAMPLE_RATE
        assert frame.get("platform")
        assert frame.get("python").startswith(f"{sys.version_info.major}.")


class TestThreadPosture:
    """The host's torch thread posture: inter-op pinned, intra-op tunable."""

    def _stub_torch(self, calls: list[tuple[str, int]]) -> types.SimpleNamespace:
        state = {"intra": 8, "inter": 1}

        def set_inter(n: int) -> None:
            calls.append(("inter", n))
            state["inter"] = n

        def set_intra(n: int) -> None:
            calls.append(("intra", n))
            state["intra"] = n

        return types.SimpleNamespace(
            set_num_interop_threads=set_inter,
            set_num_threads=set_intra,
            get_num_threads=lambda: state["intra"],
            get_num_interop_threads=lambda: state["inter"],
        )

    def test_knob_contract(self, monkeypatch) -> None:
        from vienetts_app.workers.qwen_host import configure_torch_threads

        calls: list[tuple[str, int]] = []
        monkeypatch.setitem(sys.modules, "torch", self._stub_torch(calls))
        monkeypatch.setenv("QWEN_NUM_THREADS", "6")
        assert configure_torch_threads() == {"intra": 6, "inter": 1}
        assert calls == [("inter", 1), ("intra", 6)]

        monkeypatch.undo()

        from vienetts_app.workers.qwen_host import configure_torch_threads

        calls: list[tuple[str, int]] = []
        monkeypatch.setitem(sys.modules, "torch", self._stub_torch(calls))
        monkeypatch.delenv("QWEN_NUM_THREADS", raising=False)
        assert configure_torch_threads() == {"intra": 8, "inter": 1}
        assert calls == [("inter", 1)]

        monkeypatch.undo()

        from vienetts_app.workers.qwen_host import configure_torch_threads

        calls: list[tuple[str, int]] = []
        monkeypatch.setitem(sys.modules, "torch", self._stub_torch(calls))
        monkeypatch.setenv("QWEN_NUM_THREADS", "junk")
        assert configure_torch_threads() == {"intra": 8, "inter": 1}
        assert calls == [("inter", 1)]

    def test_posture_fallbacks(self, monkeypatch, tmp_path: Path) -> None:
        from vienetts_app.workers.qwen_host import configure_torch_threads

        monkeypatch.setitem(sys.modules, "torch", None)  # makes `import torch` fail
        assert configure_torch_threads() == {}

        tmp_path = tmp_path / "case-b"
        tmp_path.mkdir(parents=True, exist_ok=True)
        monkeypatch.undo()

        from vienetts_app.workers.qwen_host import configure_torch_threads

        def refuse(_n: int) -> None:
            raise RuntimeError("cannot set after parallel work has started")

        stub = types.SimpleNamespace(
            set_num_interop_threads=refuse,
            set_num_threads=lambda _n: None,
            get_num_threads=lambda: 8,
            get_num_interop_threads=lambda: 16,
        )
        monkeypatch.setitem(sys.modules, "torch", stub)
        monkeypatch.delenv("QWEN_NUM_THREADS", raising=False)
        assert configure_torch_threads() == {"intra": 8, "inter": 16}

        tmp_path = tmp_path / "case-b"
        tmp_path.mkdir(parents=True, exist_ok=True)
        monkeypatch.undo()

        events: list[tuple[str, dict[str, Any]]] = []
        calls: list[tuple[str, int]] = []
        monkeypatch.setitem(sys.modules, "torch", self._stub_torch(calls))
        monkeypatch.delenv("QWEN_NUM_THREADS", raising=False)
        loaded_host(tmp_path, log=lambda event, **fields: events.append((event, fields)))
        loaded = [fields for event, fields in events if event == "loaded"]
        assert loaded and loaded[0]["threads"] == {"intra": 8, "inter": 1}


class TestBatchSynthesize:
    """``synthesize_batch``: one generate call, segment-tagged stream."""

    def test_batch_pcm(self, tmp_path: Path) -> None:
        host, model = loaded_host(tmp_path)
        frames: list[Frame] = []
        terminal = host.synthesize_batch(
            "job-1",
            {"texts": ["one.", "two."], "language": "en", "speaker": "Ryan"},
            frames.append,
        )
        assert terminal.get("status") == "ok"
        assert terminal.get("segments") == 2
        assert len(model.custom_calls) == 1
        assert model.custom_calls[0]["text"] == ["one.", "two."]
        tags = [frame.get("segment") for frame in frames if frame.type == "pcm"]
        assert tags == sorted(tags), "segments must stream in order"
        assert set(tags) == {0, 1}
        for tag in (0, 1):
            own = [frame for frame in frames if frame.type == "pcm" and frame.get("segment") == tag]
            assert own[-1].get("final") is True
            assert all(frame.get("final") is False for frame in own[:-1])

        tmp_path = tmp_path / "case-b"
        tmp_path.mkdir(parents=True, exist_ok=True)

        """qwen_tts NaNs a padded multi-item batch in its default mode.

        The real 0.6B checkpoint raises "probability tensor contains either
        ``inf``, ``nan`` or element < 0" for two segments of different lengths
        unless the batch runs with the SDK's streaming text mode. An export job
        must still produce ordered audio for every segment.
        """
        failure = RuntimeError("probability tensor contains either `inf`, `nan` or element < 0")
        host, _model = loaded_host(tmp_path, FakeQwenModel(batch_default_mode_failure=failure))
        frames: list[Frame] = []
        terminal = host.synthesize_batch(
            "job-1",
            {
                "texts": ["Một câu ngắn.", "Một câu dài hơn hẳn để lệch độ dài."],
                "language": "en",
                "speaker": "Ryan",
            },
            frames.append,
        )
        assert terminal.get("status") == "ok"
        assert terminal.get("segments") == 2
        tags = [frame.get("segment") for frame in frames if frame.type == "pcm"]
        assert set(tags) == {0, 1}
        assert tags == sorted(tags), "segments must stream in order"

        tmp_path = tmp_path / "case-b"
        tmp_path.mkdir(parents=True, exist_ok=True)

        failure = RuntimeError("probability tensor contains either `inf`, `nan` or element < 0")
        reference = tmp_path / "reference.wav"
        reference.write_bytes(b"RIFF")
        host, _model = loaded_host(
            tmp_path,
            FakeQwenModel(batch_default_mode_failure=failure),
            profile="base",
        )
        frames: list[Frame] = []
        terminal = host.synthesize_batch(
            "job-1",
            {
                "texts": ["Một câu ngắn.", "Một câu dài hơn hẳn để lệch độ dài."],
                "language": "en",
                "speaker": "",
                "voicePrompt": str(reference),
                "refText": "hello there",
            },
            frames.append,
        )
        assert terminal.get("status") == "ok"
        assert terminal.get("segments") == 2

    def test_a_cancel_mid_batch_stops_before_later_segments(self, tmp_path: Path) -> None:
        host, _model = loaded_host(tmp_path)
        frames: list[Frame] = []

        def cancelled() -> bool:
            return any(
                frame.type == "pcm" and frame.get("segment") == 0 and frame.get("final")
                for frame in frames
            )

        terminal = host.synthesize_batch(
            "job-1",
            {"texts": ["one.", "two."], "language": "en", "speaker": "Ryan"},
            frames.append,
            cancelled=cancelled,
        )
        assert terminal.get("status") == "cancelled"
        tags = {frame.get("segment") for frame in frames if frame.type == "pcm"}
        assert tags == {0}, "a cancelled batch must not stream later segments"

    def test_a_single_segment_batch_keeps_the_sdk_default_mode(self, tmp_path: Path) -> None:
        # The interactive path must not change shape: one segment is one string.
        failure = RuntimeError("probability tensor contains either `inf`, `nan` or element < 0")
        host, model = loaded_host(tmp_path, FakeQwenModel(batch_default_mode_failure=failure))
        frames: list[Frame] = []
        terminal = host.synthesize_batch(
            "job-1", {"texts": ["one."], "language": "en", "speaker": "Ryan"}, frames.append
        )
        assert terminal.get("status") == "ok"
        assert model.custom_calls[0]["text"] == "one."
        assert "non_streaming_mode" not in model.custom_calls[0]


class TestGenerationLiveness:
    """Heartbeats while a blocking ``generate_*`` call owns the thread."""

    def test_a_blocking_generate_emits_fractionless_heartbeats(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        import vienetts_app.workers.qwen_host as host_module

        monkeypatch.setattr(host_module, "HEARTBEAT_SECONDS", 0.05)
        gate = threading.Event()
        host, _model = loaded_host(tmp_path, FakeQwenModel(gate=gate))
        frames: list[Frame] = []
        threading.Timer(0.25, gate.set).start()
        terminal = host.synthesize(
            "job-1", {"text": "hi", "language": "en", "speaker": "Ryan"}, frames.append
        )
        assert terminal.get("status") == "ok"
        beats = [frame for frame in frames if frame.get("stage") == "generating"]
        assert beats  # ~5 heartbeats while the gate held the generate
        assert all("fraction" not in frame.fields for frame in beats)
        # Heartbeats stop before the pcm stream: writes never interleave.
        last_beat = max(i for i, frame in enumerate(frames) if frame.get("stage") == "generating")
        assert [frame.type for frame in frames].index("pcm") > last_beat


class TestAcceleratorRelease:
    def test_close_releases(self, tmp_path: Path, monkeypatch) -> None:
        calls: list[str] = []
        stub = types.SimpleNamespace(
            cuda=types.SimpleNamespace(
                is_available=lambda: True, empty_cache=lambda: calls.append("empty")
            )
        )
        monkeypatch.setitem(sys.modules, "torch", stub)
        host, _model = loaded_host(tmp_path)
        calls.clear()  # the load path also closes the previous owner
        host.close()
        assert calls == ["empty"]

        tmp_path = tmp_path / "case-b"
        tmp_path.mkdir(parents=True, exist_ok=True)
        monkeypatch.undo()

        calls: list[str] = []
        stub = types.SimpleNamespace(
            cuda=types.SimpleNamespace(is_available=lambda: False),
            backends=types.SimpleNamespace(mps=types.SimpleNamespace(is_available=lambda: True)),
            mps=types.SimpleNamespace(empty_cache=lambda: calls.append("mps")),
        )
        monkeypatch.setitem(sys.modules, "torch", stub)
        host, _model = loaded_host(tmp_path)
        calls.clear()
        host.close()
        assert calls == ["mps"]

        tmp_path = tmp_path / "case-b"
        tmp_path.mkdir(parents=True, exist_ok=True)
        monkeypatch.undo()

        def boom() -> None:
            raise RuntimeError("no MPS context")

        stub = types.SimpleNamespace(
            cuda=types.SimpleNamespace(is_available=lambda: False),
            backends=types.SimpleNamespace(mps=types.SimpleNamespace(is_available=lambda: True)),
            mps=types.SimpleNamespace(empty_cache=boom),
        )
        monkeypatch.setitem(sys.modules, "torch", stub)
        host, _model = loaded_host(tmp_path)
        host.close()  # must not raise

        tmp_path = tmp_path / "case-b"
        tmp_path.mkdir(parents=True, exist_ok=True)
        monkeypatch.undo()

        def boom() -> bool:
            raise RuntimeError("no CUDA context")

        monkeypatch.setitem(
            sys.modules,
            "torch",
            types.SimpleNamespace(cuda=types.SimpleNamespace(is_available=boom)),
        )
        host, _model = loaded_host(tmp_path)
        host.close()  # must not raise

    def test_release_edges(self, monkeypatch, tmp_path: Path, harness_factory) -> None:
        from vienetts_app.workers.qwen_host import _torch_dtype

        monkeypatch.setitem(sys.modules, "torch", types.SimpleNamespace(float32="float32"))
        assert _torch_dtype("float32") == "float32"
        with pytest.raises(QwenLoadError, match="bfloat16"):
            _torch_dtype("bfloat16")

        tmp_path = tmp_path / "case-b"
        tmp_path.mkdir(parents=True, exist_ok=True)
        monkeypatch.undo()

        """A long export runs hundreds of jobs in one host: its generation
        caches must be given back periodically, or the resident footprint only
        ratchets up until the OS's memory manager intervenes
        (VieNeuTTSApp-mbzv) — but not after every job, which made each next
        job re-grow its allocator caches."""
        calls: list[str] = []
        stub = types.SimpleNamespace(
            cuda=types.SimpleNamespace(
                is_available=lambda: True, empty_cache=lambda: calls.append("empty")
            )
        )
        monkeypatch.setitem(sys.modules, "torch", stub)
        model = FakeQwenModel(speakers=["ryan"], languages=["auto", "english"])
        profile_dir, shared_dir = model_tree(tmp_path)
        harness = harness_factory(
            loader=lambda *_args, **_kwargs: model,
            release_every_jobs=2,
            footprint=lambda: None,
        )
        harness.wait_for(has("hello"))
        harness.send(Frame(type="load", fields=load_fields(profile_dir, shared_dir)))
        harness.wait_for(has("capabilities"))
        calls.clear()

        for job in ("job-1", "job-2", "job-3", "job-4"):
            harness.send(
                Frame(
                    type="synthesize",
                    job=job,
                    fields={"text": "hello", "language": "en", "speaker": "Ryan"},
                )
            )
            harness.wait_for(
                lambda frames, job=job: any(
                    frame.type == "terminal" and frame.job == job for frame in frames
                )
            )
        harness.send(Frame(type="shutdown"))
        assert harness.finish() == 0
        # Every second settled job (2 and 4), plus the shutdown close.
        assert calls == ["empty", "empty", "empty"]


class TestReleasePolicy:
    def _policy(self, footprints: list[int | None], **kwargs: Any) -> tuple[Any, list[str]]:
        from vienetts_app.workers.qwen_host import AcceleratorReleasePolicy

        released: list[str] = []
        samples = iter(footprints)
        policy = AcceleratorReleasePolicy(
            footprint=lambda: next(samples),
            release=lambda: released.append("release"),
            **kwargs,
        )
        return policy, released

    def test_releases_every_n_jobs_and_never_in_between(self) -> None:
        policy, released = self._policy([0] * 20, every_jobs=3, growth_bytes=1 << 30)
        policy.rebaseline()
        outcomes = [policy.after_job() for _ in range(7)]
        assert outcomes == [False, False, True, False, False, True, False]
        assert released == ["release", "release"]

    def test_rss_growth_over_the_threshold_releases_early(self) -> None:
        # baseline 1000 · job1 1050 (+50) · job2 1200 (+200: release, then the
        # post-release sample 1100 is the new baseline) · job3 1150 (+50).
        policy, released = self._policy(
            [1000, 1050, 1200, 1100, 1150], every_jobs=100, growth_bytes=200
        )
        policy.rebaseline()
        assert [policy.after_job() for _ in range(3)] == [False, True, False]
        assert released == ["release"]

    def test_an_unknown_footprint_only_counts_jobs(self) -> None:
        policy, released = self._policy([None] * 10, every_jobs=4, growth_bytes=1)
        policy.rebaseline()
        assert [policy.after_job() for _ in range(4)] == [False, False, False, True]
        assert released == ["release"]


class TestTorchPosture:
    def test_threads_are_configured_before_the_loader_runs(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        import vienetts_app.workers.qwen_host as qwen_host

        order: list[str] = []
        monkeypatch.setattr(
            qwen_host, "configure_torch_threads", lambda: order.append("threads") or {}
        )
        model = FakeQwenModel()

        def loader(*_args: Any, **_kwargs: Any) -> FakeQwenModel:
            order.append("load")
            return model

        profile_dir, shared_dir = model_tree(tmp_path)
        host = QwenModelHost(loader=loader)
        host.load(load_fields(profile_dir, shared_dir))
        # set_num_interop_threads is refused once parallel work has started,
        # and loading a checkpoint is parallel work.
        assert order == ["threads", "load"]

    def test_generation_runs_under_inference_mode_when_torch_has_it(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        state = {"active": False}
        seen: list[bool] = []

        @contextlib.contextmanager
        def inference_mode() -> Any:
            state["active"] = True
            try:
                yield
            finally:
                state["active"] = False

        monkeypatch.setitem(
            sys.modules,
            "torch",
            types.SimpleNamespace(
                inference_mode=inference_mode,
                cuda=types.SimpleNamespace(is_available=lambda: False),
            ),
        )
        model = FakeQwenModel()
        original = model.generate_custom_voice

        def generate(**kwargs: Any) -> Any:
            seen.append(state["active"])
            return original(**kwargs)

        model.generate_custom_voice = generate  # type: ignore[method-assign]
        host, _ = loaded_host(tmp_path, model)
        assert run_synth(host).terminal.fields["status"] == "ok"
        assert seen == [True]
        assert state["active"] is False

    def test_generation_without_torch_runs_plain(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.delitem(sys.modules, "torch", raising=False)
        host, model = loaded_host(tmp_path)
        assert run_synth(host).terminal.fields["status"] == "ok"
        assert len(model.custom_calls) == 1


# --------------------------------------------------------------------------- #
# process priority
# --------------------------------------------------------------------------- #


class TestProcessPriority:
    def test_priority_contract(self, monkeypatch) -> None:
        if os.name == "nt":
            pytest.skip("POSIX nice path")
        recorded: list[int] = []
        monkeypatch.setattr(os, "nice", recorded.append)
        assert lower_process_priority() is True
        assert recorded == [5]

        monkeypatch.undo()

        if os.name == "nt":
            pytest.skip("POSIX nice path")

        def refuse(_value: int) -> int:
            raise PermissionError("cannot lower priority")

        monkeypatch.setattr(os, "nice", refuse)
        assert lower_process_priority() is False


# --------------------------------------------------------------------------- #
# frame loop
# --------------------------------------------------------------------------- #


class _HostReadEnd:
    """The host's read end, closed by the thread that consumes it.

    ``serve`` never closes the reader it is handed (production hands it
    ``sys.stdin.buffer``), so this harness owns its fd: it closes the pipe as
    soon as the reader sees end-of-stream, in that thread. Closing a pipe read
    end while another thread is blocked reading it hangs on Windows — which is
    exactly what a shutdown-frame exit leaves behind (the loop exits on a frame,
    not on EOF), so the close can never come from the host thread.
    """

    def __init__(self, stream: IO[bytes]) -> None:
        self._stream = stream

    @property
    def closed(self) -> bool:
        """Whether the pipe is closed (file-object protocol, and the ownership pin)."""
        return self._stream.closed

    def read(self, count: int = -1) -> bytes:
        chunk = self._stream.read(count)
        if not chunk:
            with contextlib.suppress(OSError, ValueError):
                self._stream.close()
        return chunk


class HostHarness:
    """Drive ``serve`` in a thread over real pipes (no subprocess, no torch)."""

    def __init__(self, **serve_kwargs: Any) -> None:
        to_host_read, to_host_write = os.pipe()
        from_host_read, from_host_write = os.pipe()
        self._in = _HostReadEnd(os.fdopen(to_host_read, "rb", buffering=0))
        self._to_host = os.fdopen(to_host_write, "wb", buffering=0)
        self._from_host = os.fdopen(from_host_read, "rb", buffering=0)
        self._host_out = os.fdopen(from_host_write, "wb", buffering=0)
        self.frames: list[Frame] = []
        self.exit_code: int | None = None
        self.error: BaseException | None = None
        self._lock = threading.Lock()
        self._serve_kwargs = serve_kwargs
        self._host_thread = threading.Thread(target=self._run_host, daemon=True)
        self._reader_thread = threading.Thread(target=self._read_frames, daemon=True)
        self._host_thread.start()
        self._reader_thread.start()

    def _run_host(self) -> None:
        try:
            self.exit_code = serve(self._in, self._host_out, **self._serve_kwargs)
        except BaseException as exc:  # noqa: BLE001 — surfaced by wait_for/finish
            self.error = exc
        finally:
            # Only the write end: closing the read end from here would sit on
            # top of the reader thread's blocked read (see `_HostReadEnd`).
            with contextlib.suppress(OSError, ValueError):
                self._host_out.close()

    def _read_frames(self) -> None:
        while True:
            try:
                frame = read_frame(self._from_host)
            except (EndOfStream, ProtocolError, OSError, ValueError):
                return
            with self._lock:
                self.frames.append(frame)

    def send(self, frame: Frame) -> None:
        write_frame(self._to_host, frame)

    def send_raw(self, payload: bytes) -> None:
        self._to_host.write(payload)

    def snapshot(self) -> list[Frame]:
        with self._lock:
            return list(self.frames)

    def wait_for(self, predicate: Any, timeout: float = 5.0) -> list[Frame]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.error is not None:
                raise AssertionError(f"host raised: {self.error!r}")
            frames = self.snapshot()
            if predicate(frames):
                return frames
            time.sleep(0.01)
        raise AssertionError(f"timed out; frames={[frame.type for frame in self.snapshot()]}")

    def finish(self, timeout: float = 5.0) -> int:
        self._host_thread.join(timeout)
        assert not self._host_thread.is_alive(), "the host loop did not exit"
        if self.error is not None:
            raise AssertionError(f"host raised: {self.error!r}")
        assert self.exit_code is not None
        return self.exit_code

    def close_input(self) -> None:
        with contextlib.suppress(OSError, ValueError):
            self._to_host.close()

    def close(self) -> None:
        self.close_input()
        # Join first, close second: a close under a blocked read hangs on
        # Windows, and the host's reader only sees EOF (above) or a frame.
        self._host_thread.join(2.0)
        self._reader_thread.join(2.0)
        with contextlib.suppress(OSError, ValueError):
            self._from_host.close()
        with contextlib.suppress(OSError, ValueError):
            self._to_host.close()


def has(frame_type: str) -> Any:
    return lambda frames: any(frame.type == frame_type for frame in frames)


def has_terminal(status: str) -> Any:
    return lambda frames: any(
        frame.type == "terminal" and frame.get("status") == status for frame in frames
    )


@pytest.fixture()
def harness_factory():
    created: list[HostHarness] = []

    def make(**kwargs: Any) -> HostHarness:
        harness = HostHarness(**kwargs)
        created.append(harness)
        return harness

    yield make
    for harness in created:
        harness.close()


class TestFrameLoop:
    def test_session_lifecycle(self, tmp_path: Path, harness_factory) -> None:
        events: list[str] = []
        model = FakeQwenModel(speakers=["ryan"], languages=["auto", "english"])
        profile_dir, shared_dir = model_tree(tmp_path)
        harness = harness_factory(
            loader=lambda *_args, **_kwargs: model,
            log=lambda event, **_fields: events.append(event),
        )
        harness.wait_for(has("hello"))
        hello = harness.snapshot()[0]
        assert hello.get("host") == HOST_NAME
        assert hello.get("sampleRate") == APP_SAMPLE_RATE

        harness.send(Frame(type="load", fields=load_fields(profile_dir, shared_dir)))
        harness.wait_for(has("capabilities"))
        capabilities = harness.snapshot()[1]
        assert capabilities.get("speakers") == ["Ryan"]
        assert capabilities.get("languages") == ["auto", "en"]
        assert capabilities.get("supportsClone") is False
        assert capabilities.get("sampleRate") == APP_SAMPLE_RATE

        harness.send(
            Frame(
                type="synthesize",
                job="job-7",
                fields={"text": "hello", "language": "en", "speaker": "Ryan"},
            )
        )
        frames = harness.wait_for(has_terminal("ok"))
        pcm = [frame for frame in frames if frame.type == "pcm"]
        assert pcm and pcm[-1].get("final") is True
        assert [frame.get("seq") for frame in pcm] == list(range(len(pcm)))
        assert all(frame.job == "job-7" for frame in pcm)

        harness.send(Frame(type="shutdown"))
        assert harness.finish() == 0
        assert not (tmp_path / ".load" / "customvoice").exists()
        assert "shutdown" in events

        tmp_path = tmp_path / "case-b"
        tmp_path.mkdir(parents=True, exist_ok=True)

        profile_dir, shared_dir = model_tree(tmp_path)
        harness = harness_factory(loader=lambda *_args, **_kwargs: FakeQwenModel())
        harness.wait_for(has("hello"))
        harness.send(Frame(type="load", fields=load_fields(tmp_path / "absent", shared_dir)))
        frames = harness.wait_for(has("error"))
        error = next(frame for frame in frames if frame.type == "error")
        assert error.get("code") == "load_failed"
        assert error.get("fatal") is False
        assert error.job == ""

        harness.send(Frame(type="load", fields=load_fields(profile_dir, shared_dir)))
        harness.wait_for(has("capabilities"))
        harness.send(Frame(type="shutdown"))
        assert harness.finish() == 0

        tmp_path = tmp_path / "case-b"
        tmp_path.mkdir(parents=True, exist_ok=True)

        """The parent tells a runtime problem from a model problem by this code."""

        def loader(*_args: Any, **_kwargs: Any) -> Any:
            raise QwenRuntimeIncompleteError(
                "the managed Qwen runtime is incomplete: Python module 'sox' is missing"
            )

        profile_dir, shared_dir = model_tree(tmp_path)
        harness = harness_factory(loader=loader)
        harness.wait_for(has("hello"))
        harness.send(Frame(type="load", fields=load_fields(profile_dir, shared_dir)))
        frames = harness.wait_for(has("error"))
        error = next(frame for frame in frames if frame.type == "error")
        assert error.get("code") == RUNTIME_INCOMPLETE_CODE
        assert "sox" in str(error.get("message"))
        assert error.get("fatal") is False

    def test_cancel_frames(self, tmp_path: Path, harness_factory) -> None:
        gate = threading.Event()
        events: list[str] = []
        model = FakeQwenModel(gate=gate)
        profile_dir, shared_dir = model_tree(tmp_path)
        harness = harness_factory(
            loader=lambda *_args, **_kwargs: model,
            log=lambda event, **_fields: events.append(event),
        )
        harness.wait_for(has("hello"))
        harness.send(Frame(type="load", fields=load_fields(profile_dir, shared_dir)))
        harness.wait_for(has("capabilities"))
        harness.send(
            Frame(
                type="synthesize",
                job="job-9",
                fields={"text": "hello", "language": "en", "speaker": "Ryan"},
            )
        )
        harness.wait_for(lambda _frames: bool(model.custom_calls))
        harness.send(Frame(type="cancel", job="job-9"))
        harness.wait_for(lambda _frames: "cancel_requested" in events)
        gate.set()
        harness.wait_for(has_terminal("cancelled"))
        harness.send(Frame(type="shutdown"))
        assert harness.finish() == 0

        tmp_path = tmp_path / "case-b"
        tmp_path.mkdir(parents=True, exist_ok=True)

        model = FakeQwenModel()
        profile_dir, shared_dir = model_tree(tmp_path)
        harness = harness_factory(loader=lambda *_args, **_kwargs: model)
        harness.wait_for(has("hello"))
        harness.send(Frame(type="load", fields=load_fields(profile_dir, shared_dir)))
        harness.wait_for(has("capabilities"))
        harness.send(
            Frame(
                type="synthesize",
                job="job-5",
                fields={"text": "hello", "language": "en", "speaker": "Ryan"},
            )
        )
        harness.wait_for(has_terminal("ok"))
        harness.send(Frame(type="cancel", job="job-5"))
        harness.send(Frame(type="shutdown"))
        assert harness.finish() == 0

    def test_transition_and_pipes(self, tmp_path: Path, harness_factory) -> None:
        calls: list[str] = []
        harness = harness_factory(loader=lambda *_args, **_kwargs: calls.append("load"))
        harness.wait_for(has("hello"))
        harness.send(
            Frame(
                type="synthesize",
                job="job-1",
                fields={"text": "hello", "language": "en", "speaker": "Ryan"},
            )
        )
        assert harness.finish() == 2
        assert calls == []

        tmp_path = tmp_path / "case-b"
        tmp_path.mkdir(parents=True, exist_ok=True)

        # The host thread must close only the end it writes: closing the read
        # end while its reader thread is still blocked in it hangs on Windows,
        # and a shutdown-frame exit leaves exactly that behind.
        profile_dir, shared_dir = model_tree(tmp_path)
        harness = harness_factory(loader=lambda *_args, **_kwargs: FakeQwenModel())
        harness.wait_for(has("hello"))
        harness.send(Frame(type="load", fields=load_fields(profile_dir, shared_dir)))
        harness.wait_for(has("capabilities"))
        harness.send(Frame(type="shutdown"))
        assert harness.finish() == 0
        assert harness._in.closed is False

        tmp_path = tmp_path / "case-b"
        tmp_path.mkdir(parents=True, exist_ok=True)

        profile_dir, shared_dir = model_tree(tmp_path)
        harness = harness_factory(loader=lambda *_args, **_kwargs: FakeQwenModel())
        harness.wait_for(has("hello"))
        harness.send(Frame(type="load", fields=load_fields(profile_dir, shared_dir)))
        harness.wait_for(has("capabilities"))
        harness.close_input()
        assert harness.finish() == 0

    def test_fatal_frames(self, tmp_path: Path, harness_factory) -> None:
        model = FakeQwenModel(failure=RuntimeError("CUDA error: device-side assert triggered"))
        profile_dir, shared_dir = model_tree(tmp_path)
        harness = harness_factory(loader=lambda *_args, **_kwargs: model)
        harness.wait_for(has("hello"))
        harness.send(Frame(type="load", fields=load_fields(profile_dir, shared_dir)))
        harness.wait_for(has("capabilities"))
        harness.send(
            Frame(
                type="synthesize",
                job="job-3",
                fields={"text": "hello", "language": "en", "speaker": "Ryan"},
            )
        )
        harness.wait_for(has_terminal("failed"))
        assert harness.finish() == 1
        assert not (tmp_path / ".load" / "customvoice").exists()

        tmp_path = tmp_path / "case-b"
        tmp_path.mkdir(parents=True, exist_ok=True)

        harness = harness_factory(loader=lambda *_args, **_kwargs: FakeQwenModel())
        harness.wait_for(has("hello"))
        harness.send_raw(b"\x00\x01\x00\x01")  # a header length past the protocol bound
        assert harness.finish() == 2
