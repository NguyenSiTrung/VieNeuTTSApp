"""Release validation for the real GGUF Qwen model (track Task 6.2).

The validator itself is opt-in and needs the verified packs plus weights, so
these tests pin its CONTRACT instead: usage gates (a run must name its profile,
quantization and native device), the checksum-verified install path, the exact
identities the report carries, the backend refusal and segment-limit refusals,
cancellation/crash recovery, the 48 kHz non-silent finite-audio gate, and the
opt-in workflow matrix. Every engine interaction goes through the scripted fake
host, so nothing here needs a native library or a GGUF weight.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import textwrap
from pathlib import Path

import numpy as np
import pytest
from scripts import check_smoke_wav
from scripts import qwen_gguf_release_smoke as smoke
from tests.unit import qwen_gguf_host_fake as host_fake

from vienetts_app.core.audio import write_wav_file
from vienetts_app.core.qwen_gguf_engine import QwenGgufEngine

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "qwen-gguf-runtime-smoke.yml"

#: The locked matrix: every cell the compatibility document promises to prove.
EXPECTED_CELLS = {
    "windows-x64-cpu": ("windows-latest", "cpu"),
    "windows-x64-cuda": ("self-hosted", "cuda"),
    "linux-x64-cpu": ("ubuntu-22.04", "cpu"),
    "linux-x64-cuda": ("self-hosted", "cuda"),
    "macos-arm64-cpu": ("macos-latest", "cpu"),
    "macos-arm64-metal": ("macos-latest", "metal"),
}


def cell_table() -> dict[str, dict[str, object]]:
    """The plan job's JSON table: the one declaration of the locked cells."""
    text = WORKFLOW.read_text(encoding="utf-8")
    match = re.search(r'CELLS = json\.loads\(\s*"""(?P<table>.*?)"""', text, re.S)
    assert match, "the plan job no longer declares the cell table"
    return json.loads(match.group("table"))


def plan_script(tmp_path: Path) -> Path:
    """The plan job's heredoc as a runnable script: the workflow's own selection."""
    text = WORKFLOW.read_text(encoding="utf-8")
    match = re.search(
        r"python3 - \"\$\{\{ inputs\.cells \}\}\" <<'PY' >> \"\$GITHUB_OUTPUT\"\n"
        r"(?P<body>.*?)\n\s+PY\n",
        text,
        re.S,
    )
    assert match, "the plan job no longer runs a cell-selection script"
    script = tmp_path / "plan_cells.py"
    script.write_text(textwrap.dedent(match.group("body")), encoding="utf-8")
    return script


def selected_matrix(script: Path, cells: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(script), cells], capture_output=True, text=True, check=False
    )


def pack_args(tmp_path: Path) -> list[str]:
    """A valid `--packs` root: the two subdirectories the validator expects."""
    packs = tmp_path / "packs"
    (packs / "runtime").mkdir(parents=True, exist_ok=True)
    (packs / "models").mkdir(parents=True, exist_ok=True)
    return ["--packs", str(packs), "--out", str(tmp_path / "out")]


def request_for(tmp_path: Path, *extra: str) -> smoke.GgufSmokeRequest:
    return smoke.validate_request(
        smoke.parse_args(
            [
                "--profile",
                "customvoice",
                "--quantization",
                "Q8_0",
                "--language",
                "zh",
                *pack_args(tmp_path),
                *extra,
            ]
        )
    )


def engine_with_switchable_mode(
    tmp_path: Path, mode: str, **overrides: object
) -> tuple[QwenGgufEngine, Path]:
    """A fake host whose mode the test can change between restarts."""
    host = tmp_path / "fake_qwen_gguf_host.py"
    host.write_text(host_fake.FAKE_GGUF_HOST_SOURCE, encoding="utf-8")
    mode_file = tmp_path / "mode.txt"
    mode_file.write_text(mode, encoding="utf-8")
    wrapper = tmp_path / "fake_qwen_gguf_host_mode.py"
    wrapper.write_text(
        "import pathlib\n"
        "import runpy\n"
        "import sys\n"
        "\n"
        "mode_file, host = sys.argv[1], sys.argv[2]\n"
        "sys.argv = [host, pathlib.Path(mode_file).read_text(encoding='utf-8').strip()]\n"
        "runpy.run_path(host, run_name='__main__')\n",
        encoding="utf-8",
    )
    engine = host_fake.engine_for(
        tmp_path,
        mode,
        command=[sys.executable, str(wrapper), str(mode_file), str(host)],
        **overrides,
    )
    return engine, mode_file


# --------------------------------------------------------------------------- #
# CLI validation
# --------------------------------------------------------------------------- #


class TestUsage:
    def test_an_unknown_profile_names_the_choices(self, tmp_path: Path) -> None:
        with pytest.raises(smoke.ReleaseSmokeUsageError, match="customvoice, base"):
            smoke.validate_request(
                smoke.parse_args(
                    ["--profile", "qwen1_7b", "--quantization", "Q8_0", *pack_args(tmp_path)]
                )
            )

    def test_the_quantization_is_required(self, tmp_path: Path) -> None:
        """An omitted quantization must never silently pick one: evidence
        names exactly what ran."""
        with pytest.raises(smoke.ReleaseSmokeUsageError, match="--quantization"):
            smoke.validate_request(
                smoke.parse_args(["--profile", "customvoice", *pack_args(tmp_path)])
            )

    def test_an_unknown_quantization_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(smoke.ReleaseSmokeUsageError, match="unknown quantization"):
            request_for(tmp_path, "--quantization", "Q5_K_M")

    def test_the_pytorch_spelling_is_refused(self, tmp_path: Path) -> None:
        """`mps` is the official host's vocabulary; the native host is metal."""
        with pytest.raises(smoke.ReleaseSmokeUsageError, match="cpu, cuda, metal"):
            request_for(tmp_path, "--device", "mps")

    def test_auto_is_not_evidence(self, tmp_path: Path) -> None:
        """`auto` is a UI selection, not a validated device — a run must name
        the backend it actually proves."""
        with pytest.raises(smoke.ReleaseSmokeUsageError, match="cpu, cuda, metal"):
            request_for(tmp_path, "--device", "auto")

    def test_base_requires_a_reference_clip_and_transcript(self, tmp_path: Path) -> None:
        args = ["--profile", "base", "--quantization", "Q4_K_M", *pack_args(tmp_path)]
        with pytest.raises(smoke.ReleaseSmokeUsageError, match="--ref-audio"):
            smoke.validate_request(smoke.parse_args(args))
        clip = tmp_path / "ref.wav"
        clip.write_bytes(b"RIFF")
        with pytest.raises(smoke.ReleaseSmokeUsageError, match="--ref-text"):
            smoke.validate_request(smoke.parse_args([*args, "--ref-audio", str(clip)]))

    def test_customvoice_requires_a_fixed_speaker(self, tmp_path: Path) -> None:
        with pytest.raises(smoke.ReleaseSmokeUsageError, match="--speaker"):
            request_for(tmp_path, "--speaker", "")

    def test_a_missing_pack_directory_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(smoke.ReleaseSmokeUsageError, match="runtime pack"):
            smoke.validate_request(
                smoke.parse_args(
                    [
                        "--profile",
                        "customvoice",
                        "--quantization",
                        "Q8_0",
                        "--packs",
                        str(tmp_path / "nope"),
                    ]
                )
            )

    def test_a_language_the_profile_cannot_serve_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(smoke.ReleaseSmokeUsageError, match="does not support language"):
            request_for(tmp_path, "--language", "vi")

    def test_defaults_derive_from_the_packs_root(self, tmp_path: Path) -> None:
        request = request_for(tmp_path)
        assert request.runtime_pack == tmp_path / "packs" / "runtime"
        assert request.model_pack == tmp_path / "packs" / "models"
        assert request.data_dir == tmp_path / "out" / "data"
        assert request.engine_profile == "qwen_custom_0_6b"
        assert request.quantization == "Q8_0"
        assert request.wav_path.name == "qwen-gguf-customvoice-Q8_0-cpu.wav"

    def test_a_usage_error_exits_two_without_a_traceback(self, capsys) -> None:
        assert smoke.main(["--profile", "nope"]) == 2
        assert "usage error:" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# installs and identities
# --------------------------------------------------------------------------- #


class TestInstallPacks:
    def test_a_cell_without_a_published_pack_is_explicitly_blocked(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A cell in the locked matrix without a shipped manifest is a blocked
        cell, never a silently-passing run."""
        from vienetts_app.core import qwen_gguf_runtime_manifest

        monkeypatch.setattr(qwen_gguf_runtime_manifest, "manifest_for_cell", lambda _cell: None)
        with pytest.raises(smoke.ReleaseSmokeError, match="no published pack"):
            smoke.install_packs(request_for(tmp_path))

    def test_a_host_with_no_cell_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from vienetts_app.core import qwen_gguf_runtime_manifest

        monkeypatch.setattr(qwen_gguf_runtime_manifest, "host_cell_key", lambda _device: None)
        with pytest.raises(smoke.ReleaseSmokeError, match="no managed qwentts.cpp cell"):
            smoke.install_packs(request_for(tmp_path))

    def test_an_empty_runtime_pack_fails_against_the_pinned_manifest(self, tmp_path: Path) -> None:
        request = request_for(tmp_path)
        with pytest.raises(smoke.ReleaseSmokeError) as excinfo:
            smoke.install_packs(request)
        message = str(excinfo.value)
        assert "did not install" in message

    def test_an_installed_pack_with_the_wrong_identity_is_rejected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The checksum gate is the manager's; the runner double-checks the
        promoted identity so a mismatched pack can never mint evidence."""
        from vienetts_app.core import qwen_gguf_runtime_manifest
        from vienetts_app.core.qwen_gguf_runtime import (
            QwenGgufRuntimeLocation,
            QwenGgufRuntimeStatus,
        )

        pack = qwen_gguf_runtime_manifest.manifest_for_cell("linux-x64-cpu")
        assert pack is not None
        location = QwenGgufRuntimeLocation(
            root=tmp_path / "promoted",
            library_path=tmp_path / "promoted" / "libqwen.so",
            format_version=pack.format_version,
            cell="linux-x64-cpu",
            device="cpu",
            abi_version=pack.abi_version,
            runtime_identity="qwentts.cpp@forged+ggml@forged:linux-x64-cpu:00",
            backends=("CPU",),
            dependencies=(),
            deployment_floor="test",
        )
        status = QwenGgufRuntimeStatus(
            state="ready", cell="linux-x64-cpu", installed_bytes=1, location=location
        )
        monkeypatch.setattr(
            smoke,
            "_runtime_manager_for",
            lambda _root, _pack: _StubRuntimeManager(status),
        )
        with pytest.raises(smoke.ReleaseSmokeError, match="identity"):
            smoke.install_packs(request_for(tmp_path))

    def test_a_model_pack_whose_variant_is_wrong_is_rejected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A staged Q4_K_M tree must never mint Q8_0 evidence."""
        from vienetts_app.core import (
            qwen_gguf_model_manifest,
            qwen_gguf_runtime_manifest,
        )
        from vienetts_app.core.qwen_gguf_models import (
            QwenGgufModelLocation,
            QwenGgufModelStatus,
        )
        from vienetts_app.core.qwen_gguf_runtime import (
            QwenGgufRuntimeLocation,
            QwenGgufRuntimeStatus,
        )

        pack = qwen_gguf_runtime_manifest.manifest_for_cell("linux-x64-cpu")
        recipe = qwen_gguf_model_manifest.recipe_for("customvoice", "Q8_0")
        other = qwen_gguf_model_manifest.recipe_for("customvoice", "Q4_K_M")
        assert pack is not None and recipe is not None and other is not None
        # The provisioned models root holds every variant's files; the runner
        # stages the requested recipe's view, so the source files must exist.
        request = request_for(tmp_path)
        for relative in (
            Path(recipe.key) / recipe.talker.path,
            Path("shared") / recipe.tokenizer.path,
        ):
            source = request.model_pack / relative
            source.parent.mkdir(parents=True, exist_ok=True)
            source.write_bytes(b"GGUF")
        runtime_location = QwenGgufRuntimeLocation(
            root=tmp_path / "promoted",
            library_path=tmp_path / "promoted" / "libqwen.so",
            format_version=pack.format_version,
            cell="linux-x64-cpu",
            device="cpu",
            abi_version=pack.abi_version,
            runtime_identity=pack.identity,
            backends=("CPU",),
            dependencies=(),
            deployment_floor="test",
        )
        runtime_status = QwenGgufRuntimeStatus(
            state="ready",
            cell="linux-x64-cpu",
            installed_bytes=1,
            location=runtime_location,
        )
        monkeypatch.setattr(
            smoke,
            "_runtime_manager_for",
            lambda _root, _pack: _StubRuntimeManager(runtime_status),
        )
        # The staged model claims the Q4_K_M recipe while the run asked for Q8_0.
        model_location = QwenGgufModelLocation(
            root=tmp_path / "models",
            talker_path=tmp_path / "models" / "talker.gguf",
            tokenizer_path=tmp_path / "models" / "shared" / "codec.gguf",
            format_version="qwen-gguf-model-v1",
            variant_key=other.key,
            model_identity=other.model_identity,
            revision=other.revision,
        )
        model_status = QwenGgufModelStatus(
            state="ready",
            variant_key=other.key,
            installed_bytes=1,
            location=model_location,
        )
        monkeypatch.setattr(
            smoke,
            "_model_manager_for",
            lambda _root, _variant: _StubModelManager(model_status),
        )
        with pytest.raises(smoke.ReleaseSmokeError, match="identity"):
            smoke.install_packs(request)


class _StubRuntimeManager:
    def __init__(self, status: object) -> None:
        self._status = status

    def install_from_offline_pack(self, *_a: object, **_kw: object) -> object:
        return self._status


class _StubModelManager:
    def __init__(self, status: object) -> None:
        self._status = status

    def install_offline(self, *_a: object, **_kw: object) -> object:
        return self._status


# --------------------------------------------------------------------------- #
# streaming metrics and the audio gate
# --------------------------------------------------------------------------- #


class TestStreamSegment:
    def test_metrics_and_a_48khz_finite_wav(self, tmp_path: Path) -> None:
        engine = host_fake.engine_for(tmp_path, "ok")
        request = request_for(tmp_path)
        engine.initialize()
        pid = host_fake.host_pid(tmp_path)
        audio, metrics = smoke.stream_segment(
            engine, request, job_id="t", pid=pid, rss_fn=lambda _pid: 12_345
        )
        engine.close()

        assert audio.size == 24_000
        assert metrics.chunks == 2
        assert metrics.audio_seconds == pytest.approx(0.5)
        assert metrics.peak_rss_bytes == 12_345
        assert np.isfinite(audio).all()

        write_wav_file(audio, request.wav_path, smoke.APP_SAMPLE_RATE)
        report = smoke.validate_wav(request)
        assert report["sampleRate"] == 48_000
        # The scripted host emits silence; the gate must not be fooled.
        assert any("silent or near-silent" in p for p in report["problems"])

    def test_non_finite_audio_is_a_finding(self, tmp_path: Path) -> None:
        """A native buffer overrun can yield NaN/Inf — silence-checking alone
        would pass a constant NaN stream as 'non-silent'."""
        audio = np.full(24_000, np.nan, dtype=np.float32)
        problems = smoke.audio_problems(audio)
        assert problems and "non-finite" in problems[0]
        # And a legit tone passes both gates.
        tone = 0.4 * np.sin(np.linspace(0.0, 400.0 * np.pi, 48_000, dtype=np.float32))
        assert smoke.audio_problems(tone) == []


# --------------------------------------------------------------------------- #
# the GGUF-specific probes
# --------------------------------------------------------------------------- #


class TestBackendRefusal:
    def test_a_pack_refuses_a_backend_it_does_not_ship(self, tmp_path: Path) -> None:
        """On a CPU-only pack, asking for a GPU backend must fail cleanly —
        a structured load error, never a crash or a silent CPU fallback."""
        install = smoke.GgufPackInstall(
            cell="linux-x64-cpu",
            runtime_dir=tmp_path / "pack",
            runtime_identity="test-runtime",
            backends=("CPU",),
            talker_path=tmp_path / "talker.gguf",
            codec_path=tmp_path / "codec.gguf",
            model_identity="test-model",
            tokenizer_identity="test-codec",
        )
        (tmp_path / "pack").mkdir(parents=True, exist_ok=True)
        install.talker_path.write_bytes(b"GGUF")
        install.codec_path.write_bytes(b"GGUF")
        request = request_for(tmp_path)

        def fake_engine(req, inst, *, device: str, **_kw: object) -> QwenGgufEngine:
            # The fake refuses devices outside FAKE_HOST_BACKENDS the way ggml
            # backend_init refuses a backend module the pack does not ship.
            return host_fake.engine_for(
                tmp_path,
                "ok",
                device=device,
                environment={"FAKE_HOST_BACKENDS": "cpu"},
            )

        report = smoke.check_backend_refusal(request, install, engine_factory=fake_engine)
        assert report["refused"] is True
        assert report["device"] in ("cuda", "metal")
        assert report["code"]  # a structured code, not a bare crash

    def test_an_engine_level_spelling_refusal_is_recorded(self, tmp_path: Path) -> None:
        install = smoke.GgufPackInstall(
            cell="linux-x64-cpu",
            runtime_dir=tmp_path / "pack",
            runtime_identity="test-runtime",
            backends=("CPU",),
            talker_path=tmp_path / "talker.gguf",
            codec_path=tmp_path / "codec.gguf",
            model_identity="test-model",
            tokenizer_identity="test-codec",
        )
        (tmp_path / "pack").mkdir(parents=True, exist_ok=True)
        install.talker_path.write_bytes(b"GGUF")
        install.codec_path.write_bytes(b"GGUF")
        report = smoke.check_backend_refusal(
            request_for(tmp_path), install, engine_factory=smoke.build_engine
        )
        # The vocabulary refusal never reaches a spawn: 'mps'/'tpu' are rejected
        # by the engine constructor itself, and a backend the pack does not
        # ship fails the load with a structured code — the real host reports
        # the pack's missing library as runtime_incomplete here.
        assert all(entry["refused"] for entry in report["spellings"])
        assert report["refused"] is True
        assert report["code"]


class TestSegmentation:
    def test_an_overlong_segment_is_refused_before_ipc(self, tmp_path: Path) -> None:
        engine = host_fake.engine_for(tmp_path, "ok")
        engine.initialize()
        try:
            report = smoke.check_segment_limit(engine, request_for(tmp_path))
        finally:
            engine.close()
        assert report["refused"] is True
        assert report["limit"] > 0


class TestRepeatedJobs:
    def test_a_second_job_streams_on_the_same_host(self, tmp_path: Path) -> None:
        engine = host_fake.engine_for(tmp_path, "ok")
        request = request_for(tmp_path)
        engine.initialize()
        pid = host_fake.host_pid(tmp_path)
        report = smoke.check_repeated_job(engine, request, pid=pid, rss_fn=lambda _pid: 4096)
        alive = smoke.process_alive(pid)
        engine.close()
        assert report["audioSeconds"] > 0
        assert report["sameHostAlive"] is True
        assert alive is True


# --------------------------------------------------------------------------- #
# cancellation and crash recovery
# --------------------------------------------------------------------------- #


class TestCancellation:
    @pytest.mark.slow
    def test_a_settled_cancel_keeps_the_host(self, tmp_path: Path) -> None:
        engine = host_fake.engine_for(tmp_path, "graceful_cancel", cancel_timeout=2.0)
        request = request_for(tmp_path, "--cancel-after-ms", "1000")
        engine.initialize()
        pid = host_fake.host_pid(tmp_path)
        report = smoke.check_cancellation(engine, request, pid=pid)
        alive = smoke.process_alive(pid)
        engine.close()

        assert report["terminal"] == "cancelled"
        assert report["hostAliveAfterCancel"] is True
        assert alive is True

    def test_a_host_that_will_not_stop_is_terminated_and_recovers(self, tmp_path: Path) -> None:
        engine, mode_file = engine_with_switchable_mode(
            tmp_path, "hang_synthesize", cancel_timeout=0.3
        )
        request = request_for(tmp_path, "--cancel-after-ms", "1000")
        engine.initialize()
        pid = host_fake.host_pid(tmp_path)
        mode_file.write_text("ok", encoding="utf-8")
        report = smoke.check_cancellation(engine, request, pid=pid)
        engine.close()

        assert report["terminal"] == "cancelled"
        assert report["hostAliveAfterCancel"] is False
        assert report["hostReaped"] is True
        assert report["recovered"] is True


class TestRestart:
    def test_a_killed_host_is_restarted_by_the_next_job(self, tmp_path: Path) -> None:
        engine, mode_file = engine_with_switchable_mode(tmp_path, "slow_pcm")
        request = request_for(tmp_path, "--cancel-after-ms", "300")
        engine.initialize()
        pid = host_fake.host_pid(tmp_path)
        mode_file.write_text("ok", encoding="utf-8")
        report = smoke.check_restart(engine, request, lambda: host_fake.host_pid(tmp_path))
        engine.close()

        assert report["killed"] is True
        assert report["hostPid"] == pid
        assert report["recovered"] is True
        assert report["audioSeconds"] > 0


# --------------------------------------------------------------------------- #
# the whole run through the fake host
# --------------------------------------------------------------------------- #


def stubbed_install(tmp_path: Path) -> smoke.GgufPackInstall:
    pack = tmp_path / "promoted"
    models = tmp_path / "models"
    pack.mkdir(parents=True, exist_ok=True)
    models.mkdir(parents=True, exist_ok=True)
    talker = models / "talker.gguf"
    codec = models / "codec.gguf"
    talker.write_bytes(b"GGUF")
    codec.write_bytes(b"GGUF")
    return smoke.GgufPackInstall(
        cell="linux-x64-cpu",
        runtime_dir=pack,
        runtime_identity="test-runtime@rev:linux-x64-cpu:00",
        backends=("CPU",),
        talker_path=talker,
        codec_path=codec,
        model_identity="test-model@rev:customvoice-Q8_0:aa+bb",
        tokenizer_identity="test-model@rev:tokenizer-Q8_0:bb",
    )


class TestRun:
    def test_a_fake_host_run_records_every_check_and_fails_on_silence(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The deepest contract: a scripted fixture drives every probe, and the
        silence gate is what keeps fake audio from EVER minting a pass."""
        install = stubbed_install(tmp_path)
        monkeypatch.setattr(smoke, "install_packs", lambda _request: install)
        monkeypatch.setattr(smoke, "host_pid", lambda _engine: host_fake.host_pid(tmp_path))

        def fake_build(req, inst, *, device=None, environment=None, **_kw):
            env = {"FAKE_HOST_LOG": str(tmp_path / "host.log"), "FAKE_HOST_BACKENDS": "cpu"}
            env.update(environment or {})
            return host_fake.engine_for(
                tmp_path,
                "ok",
                profile=req.engine_profile,
                runtime_dir=inst.runtime_dir,
                talker_path=inst.talker_path,
                codec_path=inst.codec_path,
                quantization=req.quantization,
                device=device or req.device,
                environment=env,
            )

        monkeypatch.setattr(smoke, "build_engine", fake_build)
        request = request_for(tmp_path, "--data-dir", str(tmp_path / "data"))
        report = smoke.run(request)

        for section in smoke.REQUIRED_SECTIONS:
            assert section in report, f"section {section} never ran"
        assert report["identities"] == {
            "runtime": "test-runtime@rev:linux-x64-cpu:00",
            "model": "test-model@rev:customvoice-Q8_0:aa+bb",
            "tokenizer": "test-model@rev:tokenizer-Q8_0:bb",
        }
        assert report["install"]["cell"] == "linux-x64-cpu"
        assert report["quantization"] == "Q8_0"
        assert report["segmentation"]["refused"] is True
        assert report["backendRefusal"]["refused"] is True
        assert all(entry["refused"] for entry in report["backendRefusal"]["spellings"])
        assert report["repeatedJob"]["audioSeconds"] > 0
        assert report["shutdown"]["reaped"] is True
        # Scripted PCM is zeros: the WAV gate reports silence, and the instant
        # job makes the cancel land too late — both are findings, not passes.
        assert any("silent" in p for p in report["problems"])
        assert any("cancellation did not stop" in p for p in report["problems"])
        assert smoke.emit(report, "") == 1

    def test_skipped_lifecycle_checks_can_never_pass(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        install = stubbed_install(tmp_path)
        monkeypatch.setattr(smoke, "install_packs", lambda _request: install)
        monkeypatch.setattr(smoke, "host_pid", lambda _engine: host_fake.host_pid(tmp_path))
        monkeypatch.setattr(
            smoke,
            "build_engine",
            lambda req, inst, **_kw: host_fake.engine_for(
                tmp_path,
                "ok",
                profile=req.engine_profile,
                runtime_dir=inst.runtime_dir,
                talker_path=inst.talker_path,
                codec_path=inst.codec_path,
                quantization=req.quantization,
                environment={
                    "FAKE_HOST_LOG": str(tmp_path / "host.log"),
                    "FAKE_HOST_BACKENDS": "cpu",
                },
            ),
        )
        request = request_for(
            tmp_path,
            "--data-dir",
            str(tmp_path / "data"),
            "--no-check-cancel",
            "--no-check-restart",
        )
        report = smoke.run(request)
        assert report["cancellation"] == {"skipped": "--no-check-cancel"}
        assert report["restart"] == {"skipped": "--no-check-restart"}
        assert any("skipped" in p for p in report["problems"])
        assert smoke.emit(report, "") == 1


# --------------------------------------------------------------------------- #
# the report contract
# --------------------------------------------------------------------------- #


class TestEmit:
    def test_a_finding_fails_the_run_and_the_json_is_mirrored(self, tmp_path: Path, capsys) -> None:
        report = {
            "schemaVersion": smoke.SCHEMA_VERSION,
            "kind": smoke.KIND,
            "profile": "customvoice",
            "quantization": "Q8_0",
            "device": "cpu",
            "cell": "linux-x64-cpu",
            "identities": {"runtime": "r", "model": "m", "tokenizer": "t"},
            "synthesis": {"ttfrSeconds": 1.0},
            "problems": ["the host produced no audio"],
        }
        target = tmp_path / "metrics" / "report.json"
        assert smoke.emit(report, str(target)) == 1
        captured = capsys.readouterr()
        emitted = json.loads(captured.out)
        # The artifact records why it failed: the run's own finding plus the
        # evidence sections this hand-built report never produced.
        assert "the host produced no audio" in emitted["problems"]
        assert any("missing evidence section" in p for p in emitted["problems"])
        assert "FAIL the host produced no audio" in captured.err
        assert json.loads(target.read_text(encoding="utf-8")) == emitted

    def test_an_unrun_report_can_never_pass(self, capsys) -> None:
        """A report that never ran the checks must not exit 0 — the absence of
        the required sections IS the finding."""
        report = {
            "schemaVersion": smoke.SCHEMA_VERSION,
            "kind": smoke.KIND,
            "profile": "customvoice",
            "quantization": "Q8_0",
            "device": "cpu",
            "problems": [],
        }
        assert smoke.emit(report, "") == 1
        assert "missing evidence" in capsys.readouterr().err

    def test_a_clean_report_exits_zero(self, capsys) -> None:
        report = {
            "schemaVersion": smoke.SCHEMA_VERSION,
            "kind": smoke.KIND,
            "profile": "base",
            "quantization": "Q4_K_M",
            "device": "metal",
            "cell": "macos-arm64-metal",
            "identities": {"runtime": "r", "model": "m", "tokenizer": "t"},
            "install": {"cell": "macos-arm64-metal"},
            "deviceEvidence": {"requested": "metal", "device": "metal"},
            "synthesis": {"ttfrSeconds": 2.0, "totalSeconds": 4.0, "rtf": 0.5},
            "wav": {"seconds": 4.0},
            "segmentation": {"refused": True},
            "backendRefusal": {"refused": True},
            "repeatedJob": {"audioSeconds": 0.5},
            "cancellation": {"terminal": "cancelled"},
            "restart": {"killed": True, "recovered": True},
            "shutdown": {"reaped": True, "clean": True},
            "problems": [],
        }
        assert smoke.emit(report, "") == 0
        captured = capsys.readouterr()
        assert "ok: base Q4_K_M on metal" in captured.err


class TestWavCheckerRateGate:
    def test_the_expected_rate_passes_and_a_wrong_one_fails(self, tmp_path: Path) -> None:
        path = tmp_path / "tone.wav"
        tone = 0.4 * np.sin(np.linspace(0.0, 400.0 * np.pi, 48_000, dtype=np.float32))
        write_wav_file(tone, path, 48_000)

        assert check_smoke_wav.check(path, 0.5, expect_rate=48_000) == []
        assert check_smoke_wav.check(path, 0.5, expect_rate=24_000) == [
            "sample rate 48000 != required 24000"
        ]


# --------------------------------------------------------------------------- #
# the opt-in workflow that runs it
# --------------------------------------------------------------------------- #


class TestWorkflowContract:
    def test_the_workflow_is_opt_in_only(self) -> None:
        text = WORKFLOW.read_text(encoding="utf-8")
        assert "workflow_dispatch:" in text
        assert "\n  push:" not in text
        assert "pull_request" not in text
        assert "schedule" not in text

    def test_the_matrix_covers_every_locked_cell(self) -> None:
        table = cell_table()
        assert set(table) == set(EXPECTED_CELLS)
        for cell, (runner, device) in EXPECTED_CELLS.items():
            spec = table[cell]
            assert spec["device"] == device
            runs_on = spec["runs_on"]
            if device == "cuda":
                assert "self-hosted" in runs_on
            else:
                assert "self-hosted" not in runs_on
                assert runner.split("-")[0] in runs_on

    def test_every_cell_runs_both_profiles_and_quantizations(self) -> None:
        """The 24-combination sweep: 6 cells × 2 profiles × 2 quantizations —
        the workflow must iterate all four variants per cell, never collapse
        to one default quantization."""
        text = WORKFLOW.read_text(encoding="utf-8")
        assert re.search(r'for quantization in ["\']?Q8_0 Q4_K_M', text) or re.search(
            r"Q8_0 Q4_K_M", text
        ), "the workflow must sweep both quantizations"
        for profile in ("customvoice", "base"):
            assert profile in text
        assert "qwen_gguf_release_smoke.py" in text

    def test_the_cells_input_shrinks_the_matrix_not_a_job_level_if(self) -> None:
        text = WORKFLOW.read_text(encoding="utf-8")
        assert all(
            "matrix" not in condition
            for condition in re.findall(r"^    if: (?P<condition>.+)$", text, re.M)
        )
        assert "matrix: ${{ fromJSON(needs.plan.outputs.matrix) }}" in text
        assert "needs: plan" in text

    def test_the_plan_step_selects_exactly_the_requested_cells(self, tmp_path: Path) -> None:
        script = plan_script(tmp_path)

        default = selected_matrix(
            script, "windows-x64-cpu,linux-x64-cpu,macos-arm64-cpu,macos-arm64-metal"
        )
        assert default.returncode == 0
        assert [
            entry["cell"] for entry in json.loads(default.stdout.removeprefix("matrix="))["include"]
        ] == ["windows-x64-cpu", "linux-x64-cpu", "macos-arm64-cpu", "macos-arm64-metal"]

        cuda = selected_matrix(script, "linux-x64-cuda")
        assert cuda.returncode == 0
        assert json.loads(cuda.stdout.removeprefix("matrix=")) == {
            "include": [
                {
                    "cell": "linux-x64-cuda",
                    "runs_on": ["self-hosted", "linux", "x64", "cuda"],
                    "device": "cuda",
                }
            ]
        }

        refused = selected_matrix(script, "linux-x64-gpu")
        assert refused.returncode != 0
        assert "unknown cells: linux-x64-gpu" in refused.stderr
        assert all(cell in refused.stderr for cell in EXPECTED_CELLS)

    def test_the_packs_are_consumed_offline(self) -> None:
        text = WORKFLOW.read_text(encoding="utf-8")
        assert "scripts/qwen_gguf_release_smoke.py" in text
        assert "qwen-gguf-models.zip" in text
        assert "--packs" in text
        assert 'HF_HUB_OFFLINE: "1"' in text
        assert 'TRANSFORMERS_OFFLINE: "1"' in text
        # The app environment never gains the stack: the runtime comes from the pack.
        assert "qwen-tts" not in text
        assert "download.pytorch.org" not in text
        assert "huggingface.co" not in text

    def test_every_artifact_is_gated_at_48khz(self) -> None:
        text = WORKFLOW.read_text(encoding="utf-8")
        assert "scripts/check_smoke_wav.py" in text
        assert "--expect-rate 48000" in text

    def test_the_metrics_and_audio_are_uploaded(self) -> None:
        text = WORKFLOW.read_text(encoding="utf-8")
        assert "actions/upload-artifact@v4" in text
        assert "qwen-gguf-metrics/*.json" in text
        assert "qwen-gguf-smoke/*.wav" in text
        assert "$GITHUB_STEP_SUMMARY" in text
