"""Isolated Qwen model host: load view, capabilities, synthesis, and frame loop."""

from __future__ import annotations

import contextlib
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
from vienetts_app.core.qwen_protocol import (
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
    StreamingResampler,
    build_load_view,
    describe_capabilities,
    hello_frame,
    log_to_stderr,
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
    ) -> None:
        self.audio_samples = tone(RESAMPLE_CHUNK_SAMPLES * 2 + 123) if audio is None else audio
        self.wavs = wavs
        self.sample_rate = sample_rate
        self.failure = failure
        self.speakers = speakers
        self.languages = languages
        self.gate = gate
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

    def _ready(self) -> None:
        if self.gate is not None:
            assert self.gate.wait(timeout=5.0), "test never released the model gate"
        if self.failure is not None:
            raise self.failure

    def _batch_result(self, count: int) -> Any:
        if self.wavs is not None:
            return self.wavs
        return [self.audio_samples] * count

    def generate_custom_voice(self, *, text: Any, language: str, speaker: str, **kwargs: Any):
        texts = text if isinstance(text, list) else [text]
        self.custom_calls.append({"text": text, "language": language, "speaker": speaker, **kwargs})
        self._ready()
        return self._batch_result(len(texts)), self.sample_rate

    def generate_voice_clone(
        self, *, text: Any, language: str, voice_clone_prompt: Any, **kwargs: Any
    ):
        texts = text if isinstance(text, list) else [text]
        self.clone_calls.append(
            {"text": text, "language": language, "voice_clone_prompt": voice_clone_prompt, **kwargs}
        )
        self._ready()
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
    def test_rejects_invalid_rates_and_chunks(self) -> None:
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

    def test_chunked_upsample_matches_one_shot(self) -> None:
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

    def test_equal_rates_pass_through_and_downsample_is_bounded(self) -> None:
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
    def test_merges_both_trees_into_one_load_path(self, tmp_path: Path) -> None:
        profile_dir, shared_dir = model_tree(tmp_path)
        view = build_load_view(profile_dir, shared_dir, tmp_path / ".load" / "customvoice")
        for relative in ("config.json", "model.safetensors", "vocab.json", "merges.txt"):
            assert (view / relative).is_file(), relative
        assert (view / "speech_tokenizer" / "config.json").is_file()
        assert not (view / "install.json").exists()
        assert os.path.samefile(view / "model.safetensors", profile_dir / "model.safetensors")
        assert os.path.samefile(view / "vocab.json", shared_dir / "vocab.json")

    def test_rebuild_reuses_valid_links_and_replaces_stale_ones(self, tmp_path: Path) -> None:
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

    def test_link_fallback_uses_symlinks_when_hard_links_are_unavailable(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        profile_dir, shared_dir = model_tree(tmp_path)

        def refuse_link(*_args: Any, **_kwargs: Any) -> None:
            raise OSError("hard links are not supported here")

        monkeypatch.setattr(os, "link", refuse_link)
        view = build_load_view(profile_dir, shared_dir, tmp_path / ".load" / "customvoice")
        assert (view / "vocab.json").is_symlink()
        assert os.path.samefile(view / "vocab.json", shared_dir / "vocab.json")

    def test_link_fallback_copies_when_the_filesystem_links_nothing(
        self, tmp_path: Path, monkeypatch
    ) -> None:
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

    def test_empty_profile_tree_is_actionable(self, tmp_path: Path) -> None:
        profile_dir = tmp_path / "customvoice"
        profile_dir.mkdir()
        (profile_dir / "install.json").write_text("{}", encoding="utf-8")
        shared_dir = tmp_path / "shared"
        shared_dir.mkdir()
        with pytest.raises(QwenLoadError, match="empty"):
            build_load_view(profile_dir, shared_dir, tmp_path / "view")

    def test_missing_trees_are_actionable(self, tmp_path: Path) -> None:
        with pytest.raises(QwenLoadError, match="model directory"):
            build_load_view(tmp_path / "absent", tmp_path / "also-absent", tmp_path / "view")
        profile_dir, shared_dir = model_tree(tmp_path)
        assert shared_dir.is_dir()
        with pytest.raises(QwenLoadError, match="shared"):
            build_load_view(profile_dir, tmp_path / "gone", tmp_path / "view")

    def test_removal_keeps_the_verified_trees(self, tmp_path: Path) -> None:
        profile_dir, shared_dir = model_tree(tmp_path)
        view = build_load_view(profile_dir, shared_dir, tmp_path / ".load" / "customvoice")
        remove_load_view(view)
        assert not view.exists()
        assert (profile_dir / "model.safetensors").is_file()
        assert (shared_dir / "vocab.json").is_file()
        remove_load_view(view)  # idempotent

    def test_removal_never_follows_a_symlinked_view(self, tmp_path: Path) -> None:
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
    def test_pinned_table_is_the_fallback(self) -> None:
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

    def test_model_reported_names_map_onto_app_ids(self) -> None:
        model = FakeQwenModel(
            speakers=["ryan", "vivian", "sohee"], languages=["auto", "english", "chinese"]
        )
        custom = describe_capabilities("customvoice", model)
        assert custom.speakers == ("Vivian", "Ryan", "Sohee")  # pinned display order
        assert custom.languages == ("auto", "zh", "en")  # pinned order, narrowed to the report

    def test_unusable_report_falls_back_to_the_pinned_table(self) -> None:
        events: list[str] = []
        model = FakeQwenModel(speakers=["nobody"], languages=["klingon"])
        caps = describe_capabilities(
            "customvoice", model, log=lambda event, **_fields: events.append(event)
        )
        assert "Vivian" in caps.speakers
        assert "zh" in caps.languages
        assert events == ["capabilities_fallback", "capabilities_fallback"]

    def test_model_without_reporting_methods(self) -> None:
        model = FakeQwenModel(report_speakers=False, report_languages=False)
        caps = describe_capabilities("base", model)
        assert caps.speakers == ()
        assert "zh" in caps.languages

    def test_reporting_failure_falls_back_to_the_pinned_table(self) -> None:
        events: list[str] = []
        model = FakeQwenModel(speaker_report_error=RuntimeError("no speaker table"))
        caps = describe_capabilities(
            "customvoice", model, log=lambda event, **_fields: events.append(event)
        )
        assert "Vivian" in caps.speakers
        assert events == []  # an absent report is normal, not a fallback warning

    def test_unknown_profile_rejected(self) -> None:
        with pytest.raises(QwenHostError, match="profile"):
            describe_capabilities("vieneu")


# --------------------------------------------------------------------------- #
# load
# --------------------------------------------------------------------------- #


class TestLoad:
    def test_loads_the_merged_view_with_explicit_runtime_flags(self, tmp_path: Path) -> None:
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

    def test_failed_load_is_actionable_and_leaves_the_host_unloaded(self, tmp_path: Path) -> None:
        def loader(*_args: Any, **_kwargs: Any) -> Any:
            raise RuntimeError("no qwen runtime installed")

        host = QwenModelHost(loader=loader)
        profile_dir, shared_dir = model_tree(tmp_path)
        with pytest.raises(QwenLoadError, match="no qwen runtime installed"):
            host.load(load_fields(profile_dir, shared_dir))
        assert host.loaded is False
        assert not (tmp_path / ".load" / "customvoice").exists()

    def test_unknown_profile_and_missing_shared_tree_are_rejected(self, tmp_path: Path) -> None:
        host = QwenModelHost(loader=lambda *_args, **_kwargs: FakeQwenModel())
        profile_dir, shared_dir = model_tree(tmp_path)
        with pytest.raises(QwenLoadError, match="unsupported Qwen profile"):
            host.load(load_fields(profile_dir, shared_dir, profile="vieneu"))
        with pytest.raises(QwenLoadError, match="shared tokenizer"):
            host.load(load_fields(profile_dir, tmp_path / "gone"))
        assert host.loaded is False

    def test_loader_errors_pass_through_unwrapped(self, tmp_path: Path) -> None:
        def loader(*_args: Any, **_kwargs: Any) -> Any:
            raise QwenLoadError("the managed runtime is not installed")

        host = QwenModelHost(loader=loader)
        profile_dir, shared_dir = model_tree(tmp_path)
        with pytest.raises(QwenLoadError, match="^the managed runtime is not installed$"):
            host.load(load_fields(profile_dir, shared_dir))

    def test_reload_swaps_the_model_and_drops_cached_clone_prompts(self, tmp_path: Path) -> None:
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


# --------------------------------------------------------------------------- #
# synthesis
# --------------------------------------------------------------------------- #


class TestSynthesize:
    def test_custom_voice_forwards_language_and_speaker_without_instruct(
        self, tmp_path: Path
    ) -> None:
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

    def test_base_builds_and_caches_the_clone_prompt(self, tmp_path: Path) -> None:
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

    def test_pcm_frames_are_bounded_sequential_and_final(self, tmp_path: Path) -> None:
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

    def test_pcm_continues_one_resample_across_chunks(self, tmp_path: Path) -> None:
        host, model = loaded_host(tmp_path)
        try:
            run = run_synth(host)
        finally:
            host.close()
        assert model.audio_samples.size > RESAMPLE_CHUNK_SAMPLES  # forces several pushes
        np.testing.assert_allclose(
            run.pcm, one_shot_resample(model.audio_samples), rtol=1e-6, atol=1e-6
        )

    def test_progress_frames_stay_inside_the_contract(self, tmp_path: Path) -> None:
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

    def test_sample_rate_drift_fails_the_job(self, tmp_path: Path) -> None:
        host, _model = loaded_host(tmp_path, FakeQwenModel(sample_rate=16000))
        try:
            run = run_synth(host)
        finally:
            host.close()
        assert run.terminal.get("status") == "failed"
        (error,) = run.errors()
        assert error.get("code") == "sample_rate_drift"
        assert run.pcm_frames == []

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

    def test_base_without_clone_prompt_support_fails_the_job(self, tmp_path: Path) -> None:
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

    def test_generation_error_is_non_fatal_and_keeps_the_model(self, tmp_path: Path) -> None:
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

    def test_device_error_is_fatal_and_unloads_the_model(self, tmp_path: Path) -> None:
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

    def test_cancel_stops_emission_and_settles_cancelled(self, tmp_path: Path) -> None:
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

    def test_synthesize_without_a_loaded_model_fails_cleanly(self, tmp_path: Path) -> None:
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


class TestLogging:
    def test_log_lines_are_json_on_stderr(self, capsys) -> None:
        log_to_stderr("loaded", profile="customvoice", device="cpu")
        captured = capsys.readouterr()
        assert captured.out == ""
        assert f'"host": "{HOST_NAME}"' in captured.err
        assert '"event": "loaded"' in captured.err
        assert '"profile": "customvoice"' in captured.err

    def test_a_broken_stderr_never_kills_the_host(self, monkeypatch) -> None:
        class BrokenStderr:
            def write(self, _text: str) -> int:
                raise OSError("stderr is gone")

            def flush(self) -> None:
                raise OSError("stderr is gone")

        monkeypatch.setattr(sys, "stderr", BrokenStderr())
        log_to_stderr("loaded")
        monkeypatch.setattr(sys, "stderr", None)
        log_to_stderr("loaded")  # a windowed build has no stderr at all

    def test_main_without_stdio_exits_with_an_error(self, monkeypatch) -> None:
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

    def test_inter_op_is_pinned_and_the_knob_sets_intra_op(self, monkeypatch) -> None:
        from vienetts_app.workers.qwen_host import configure_torch_threads

        calls: list[tuple[str, int]] = []
        monkeypatch.setitem(sys.modules, "torch", self._stub_torch(calls))
        monkeypatch.setenv("QWEN_NUM_THREADS", "6")
        assert configure_torch_threads() == {"intra": 6, "inter": 1}
        assert calls == [("inter", 1), ("intra", 6)]

    def test_an_unset_knob_keeps_the_runtime_intra_op_default(self, monkeypatch) -> None:
        from vienetts_app.workers.qwen_host import configure_torch_threads

        calls: list[tuple[str, int]] = []
        monkeypatch.setitem(sys.modules, "torch", self._stub_torch(calls))
        monkeypatch.delenv("QWEN_NUM_THREADS", raising=False)
        assert configure_torch_threads() == {"intra": 8, "inter": 1}
        assert calls == [("inter", 1)]

    def test_an_invalid_knob_keeps_the_runtime_intra_op_default(self, monkeypatch) -> None:
        from vienetts_app.workers.qwen_host import configure_torch_threads

        calls: list[tuple[str, int]] = []
        monkeypatch.setitem(sys.modules, "torch", self._stub_torch(calls))
        monkeypatch.setenv("QWEN_NUM_THREADS", "junk")
        assert configure_torch_threads() == {"intra": 8, "inter": 1}
        assert calls == [("inter", 1)]

    def test_without_torch_the_posture_is_a_no_op(self, monkeypatch) -> None:
        from vienetts_app.workers.qwen_host import configure_torch_threads

        monkeypatch.setitem(sys.modules, "torch", None)  # makes `import torch` fail
        assert configure_torch_threads() == {}

    def test_an_inter_op_refusal_is_swallowed_and_read_back(self, monkeypatch) -> None:
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

    def test_the_loaded_log_reports_the_thread_posture(self, tmp_path: Path, monkeypatch) -> None:
        events: list[tuple[str, dict[str, Any]]] = []
        calls: list[tuple[str, int]] = []
        monkeypatch.setitem(sys.modules, "torch", self._stub_torch(calls))
        monkeypatch.delenv("QWEN_NUM_THREADS", raising=False)
        loaded_host(tmp_path, log=lambda event, **fields: events.append((event, fields)))
        loaded = [fields for event, fields in events if event == "loaded"]
        assert loaded and loaded[0]["threads"] == {"intra": 8, "inter": 1}


class TestBatchSynthesize:
    """``synthesize_batch``: one generate call, segment-tagged stream."""

    def test_one_generate_call_streams_segment_tagged_pcm(self, tmp_path: Path) -> None:
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
    def test_close_releases_the_accelerator_cache(self, tmp_path: Path, monkeypatch) -> None:
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

    def test_close_releases_the_mps_cache_as_well(self, tmp_path: Path, monkeypatch) -> None:
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

    def test_mps_cache_failures_are_swallowed(self, tmp_path: Path, monkeypatch) -> None:
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

    def test_accelerator_failures_are_swallowed(self, tmp_path: Path, monkeypatch) -> None:
        def boom() -> bool:
            raise RuntimeError("no CUDA context")

        monkeypatch.setitem(
            sys.modules,
            "torch",
            types.SimpleNamespace(cuda=types.SimpleNamespace(is_available=boom)),
        )
        host, _model = loaded_host(tmp_path)
        host.close()  # must not raise

    def test_unknown_runtime_dtype_is_actionable(self, monkeypatch) -> None:
        from vienetts_app.workers.qwen_host import _torch_dtype

        monkeypatch.setitem(sys.modules, "torch", types.SimpleNamespace(float32="float32"))
        assert _torch_dtype("float32") == "float32"
        with pytest.raises(QwenLoadError, match="bfloat16"):
            _torch_dtype("bfloat16")


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
    def test_handshake_load_synthesize_and_shutdown(self, tmp_path: Path, harness_factory) -> None:
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

    def test_cancel_frame_stops_a_running_job(self, tmp_path: Path, harness_factory) -> None:
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

    def test_load_failure_reports_an_error_and_keeps_serving(
        self, tmp_path: Path, harness_factory
    ) -> None:
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

    def test_transition_violation_exits_without_loading(
        self, tmp_path: Path, harness_factory
    ) -> None:
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

    def test_the_host_leaves_the_harness_pipes_to_the_harness(
        self, tmp_path: Path, harness_factory
    ) -> None:
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

    def test_peer_close_exits_cleanly(self, tmp_path: Path, harness_factory) -> None:
        profile_dir, shared_dir = model_tree(tmp_path)
        harness = harness_factory(loader=lambda *_args, **_kwargs: FakeQwenModel())
        harness.wait_for(has("hello"))
        harness.send(Frame(type="load", fields=load_fields(profile_dir, shared_dir)))
        harness.wait_for(has("capabilities"))
        harness.close_input()
        assert harness.finish() == 0

    def test_fatal_error_stops_the_host(self, tmp_path: Path, harness_factory) -> None:
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

    def test_stale_cancel_for_a_settled_job_is_dropped(
        self, tmp_path: Path, harness_factory
    ) -> None:
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

    def test_malformed_bytes_exit_with_a_protocol_error(self, harness_factory) -> None:
        harness = harness_factory(loader=lambda *_args, **_kwargs: FakeQwenModel())
        harness.wait_for(has("hello"))
        harness.send_raw(b"\x00\x01\x00\x01")  # a header length past the protocol bound
        assert harness.finish() == 2
