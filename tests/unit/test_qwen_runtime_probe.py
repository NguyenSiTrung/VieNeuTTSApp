"""Qwen runtime compatibility probe: CLI validation and JSON schema.

The probe exists to produce ONE machine-readable evidence record per platform
(Task 0.2). These tests never download weights: every model interaction goes
through an injectable loader, and every environment probe through injectables.
"""

from __future__ import annotations

import json
import time

import numpy as np
import pytest
from scripts.spike import qwen_runtime_probe as probe


class FakeModel:
    """Minimal qwen-tts model double with configurable behavior."""

    def __init__(
        self,
        *,
        rate: int = 24000,
        languages: tuple[str, ...] | None = None,
        speakers: tuple[str, ...] | None = None,
        instruct_affects: bool = False,
        instruct_raises: bool = False,
        stream_method: bool = False,
        generate_seconds: float = 0.0,
        close_raises: bool = False,
    ) -> None:
        self.rate = rate
        self.languages = languages
        self.speakers = speakers
        self.instruct_affects = instruct_affects
        self.instruct_raises = instruct_raises
        self.generate_seconds = generate_seconds
        self.close_raises = close_raises
        self.calls: list[dict[str, object]] = []
        self.closed = False
        if stream_method:
            self.generate_custom_voice_stream = lambda **kwargs: iter([np.zeros(10, np.float32)])

    def generate_custom_voice(self, **kwargs: object):
        self.calls.append(dict(kwargs))
        if self.generate_seconds:
            time.sleep(self.generate_seconds)
        if self.instruct_raises and kwargs.get("instruct"):
            raise TypeError("unexpected keyword argument 'instruct'")
        tone = 0.5 if (self.instruct_affects and kwargs.get("instruct")) else 0.1
        samples = np.full(int(self.rate * 0.25), tone, dtype=np.float32)
        return [samples], self.rate

    def generate_voice_clone(self, **kwargs: object):
        self.calls.append(dict(kwargs))
        return [np.full(int(self.rate * 0.25), 0.2, dtype=np.float32)], self.rate

    def close(self) -> None:
        if self.close_raises:
            raise RuntimeError("device teardown failed")
        self.closed = True


def run_with(model: FakeModel, **kwargs) -> dict:
    """Run the probe against ``model`` with environment probes stubbed."""
    request = probe.ProbeRequest(
        profile=kwargs.pop("profile", "customvoice"),
        model_dir=str(kwargs.pop("model_dir", "/models/qwen")),
        **kwargs,
    )
    return probe.run_probe(
        request,
        loader=lambda _request: model,
        rss_fn=lambda: 1024 * 1024,  # 1 GiB in KB (Linux posture)
        vram_fn=lambda: 0,
        runtime_info_fn=lambda: {"qwenTts": "0.1.1", "torch": "2.8.0", "transformers": "4.57.3"},
        device_info_fn=lambda _device: ("cpu", "float32", "sdpa", "no CUDA device"),
    )


class TestCliValidation:
    def test_profile_inputs_are_required_before_any_load(self, tmp_path) -> None:
        model_dir = tmp_path / "model"
        model_dir.mkdir()
        base = ["--profile", "customvoice", "--model-dir", str(model_dir)]

        with pytest.raises(probe.ProbeUsageError, match="speaker"):
            probe.validate_request(probe.parse_args(base))
        with pytest.raises(probe.ProbeUsageError, match="ref-audio"):
            probe.validate_request(
                probe.parse_args(["--profile", "base", "--model-dir", str(model_dir)])
            )
        with pytest.raises(probe.ProbeUsageError, match="unknown profile"):
            probe.validate_request(
                probe.parse_args(["--profile", "qwen9", "--model-dir", str(model_dir)])
            )
        with pytest.raises(probe.ProbeUsageError, match="model-dir"):
            probe.validate_request(
                probe.parse_args(
                    [
                        "--profile",
                        "customvoice",
                        "--speaker",
                        "Ryan",
                        "--model-dir",
                        str(tmp_path / "nope"),
                    ]
                )
            )

    def test_valid_request_carries_defaults(self, tmp_path) -> None:
        model_dir = tmp_path / "model"
        model_dir.mkdir()
        request = probe.validate_request(
            probe.parse_args(
                ["--profile", "customvoice", "--model-dir", str(model_dir), "--speaker", "Ryan"]
            )
        )
        assert request.profile == "customvoice"
        assert request.language == "English"
        assert request.check_instructions is False
        assert request.check_incremental is True

    def test_main_reports_usage_errors_as_json(self, tmp_path, capsys) -> None:
        exit_code = probe.main(["--profile", "base", "--model-dir", str(tmp_path)])
        payload = json.loads(capsys.readouterr().out.strip())
        assert exit_code == 2
        assert payload["schemaVersion"] == probe.SCHEMA_VERSION
        assert payload["errors"] and "ref-audio" in payload["errors"][0]


class TestResultSchema:
    def test_result_has_every_required_field_and_finite_metrics(self) -> None:
        payload = run_with(FakeModel(languages=("English",), speakers=("Ryan",)))

        assert payload["schemaVersion"] == probe.SCHEMA_VERSION
        assert payload["profile"] == "customvoice"
        assert payload["runtime"]["qwenTts"] == "0.1.1"
        assert payload["device"]["resolved"] == "cpu"
        assert payload["device"]["dtype"] == "float32"
        assert payload["device"]["attention"] == "sdpa"
        assert payload["capabilities"]["sampleRate"] == 24000
        assert payload["capabilities"]["languages"] == ["English"]
        assert payload["capabilities"]["speakers"] == ["Ryan"]
        metrics = payload["metrics"]
        assert metrics["audioSeconds"] == pytest.approx(0.25)
        assert metrics["totalMs"] > 0
        assert metrics["ttfrMs"] > 0
        assert metrics["rtf"] == pytest.approx(
            metrics["totalMs"] / 1000 / metrics["audioSeconds"], rel=0.05
        )
        assert metrics["peakRssMb"] == pytest.approx(1024.0)
        assert metrics["peakVramMb"] is None
        assert payload["errors"] == []
        json.dumps(payload, allow_nan=False)  # no NaN/Infinity anywhere

    def test_cli_emits_exactly_one_json_object(self, tmp_path, capsys, monkeypatch) -> None:
        model_dir = tmp_path / "model"
        model_dir.mkdir()
        monkeypatch.setattr(probe, "load_model", lambda request: FakeModel())
        exit_code = probe.main(
            ["--profile", "customvoice", "--model-dir", str(model_dir), "--speaker", "Ryan"]
        )
        out = capsys.readouterr().out
        assert exit_code == 0
        assert out.count("\n") == 1
        assert json.loads(out)["profile"] == "customvoice"

    def test_loader_failure_is_one_error_json_and_nonzero_exit(
        self, tmp_path, capsys, monkeypatch
    ) -> None:
        model_dir = tmp_path / "model"
        model_dir.mkdir()

        def broken_loader(_request):
            raise RuntimeError("qwen_tts is not installed")

        monkeypatch.setattr(probe, "load_model", broken_loader)
        exit_code = probe.main(
            ["--profile", "customvoice", "--model-dir", str(model_dir), "--speaker", "Ryan"]
        )
        payload = json.loads(capsys.readouterr().out.strip())
        assert exit_code == 1
        assert payload["errors"] == ["qwen_tts is not installed"]
        assert payload["metrics"]["totalMs"] is None


class TestCapabilityReporting:
    def test_model_reported_languages_win_and_incremental_audio_is_detected(self) -> None:
        payload = run_with(
            FakeModel(languages=("English", "Chinese"), speakers=("Ryan",), stream_method=True)
        )
        caps = payload["capabilities"]
        assert caps["languages"] == ["English", "Chinese"]
        assert caps["languagesSource"] == "model"
        assert caps["speakers"] == ["Ryan"]
        assert caps["incrementalAudio"] is True
        assert "stream" in caps["incrementalDetail"]

    def test_official_fallback_is_labeled_when_the_model_reports_nothing(self) -> None:
        payload = run_with(FakeModel())
        caps = payload["capabilities"]
        assert caps["languages"] == list(probe.OFFICIAL_LANGUAGES)
        assert caps["languagesSource"] == "official-model-card"
        assert caps["speakersSource"] == "official-model-card"
        assert caps["incrementalAudio"] is False

    def test_base_profile_reports_the_clone_call_and_no_fixed_speakers(self) -> None:
        payload = run_with(
            FakeModel(speakers=()),
            profile="base",
            ref_audio="/clips/ref.wav",
            ref_text="hello there",
        )
        assert payload["capabilities"]["speakers"] == []
        assert payload["request"]["cloneCall"] == "generate_voice_clone"
        assert payload["request"]["speaker"] == ""


class TestInstructionProbe:
    def test_ignored_instruction_is_reported_as_no_audio_effect(self) -> None:
        payload = run_with(
            FakeModel(instruct_affects=False), check_instructions=True, instruct="whisper"
        )
        assert payload["instructions"]["accepted"] is True
        assert payload["instructions"]["affectsAudio"] is False

    def test_effective_instruction_is_reported_as_audio_effect(self) -> None:
        payload = run_with(
            FakeModel(instruct_affects=True), check_instructions=True, instruct="whisper"
        )
        assert payload["instructions"]["affectsAudio"] is True

    def test_rejected_instruct_kwarg_is_reported_as_not_accepted(self) -> None:
        payload = run_with(
            FakeModel(instruct_raises=True), check_instructions=True, instruct="whisper"
        )
        assert payload["instructions"]["accepted"] is False
        assert "instruct" in payload["instructions"]["detail"]


class TestCancellationAndShutdown:
    def test_blocking_generation_reports_not_interruptible_with_latency(self) -> None:
        payload = run_with(FakeModel(generate_seconds=0.15), check_cancel=True, cancel_after_ms=20)
        cancellation = payload["cancellation"]
        assert cancellation["interruptible"] is False
        assert cancellation["latencyMs"] >= 20
        assert cancellation["terminal"] == "completed"

    def test_clean_close_is_recorded_and_close_errors_are_reported(self) -> None:
        assert run_with(FakeModel())["shutdown"]["clean"] is True

        payload = run_with(FakeModel(close_raises=True))
        assert payload["shutdown"]["clean"] is False
        assert "device teardown failed" in payload["shutdown"]["detail"]


class TestRssNormalization:
    def test_ru_maxrss_units_differ_by_platform(self, monkeypatch) -> None:
        monkeypatch.setattr(probe.sys, "platform", "darwin")
        assert probe.peak_rss_mb(lambda: 3 * 1024 * 1024) == pytest.approx(3.0)
        monkeypatch.setattr(probe.sys, "platform", "linux")
        assert probe.peak_rss_mb(lambda: 3 * 1024 * 1024) == pytest.approx(3072.0)
