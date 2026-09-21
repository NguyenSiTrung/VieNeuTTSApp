"""Release validation for the real Qwen model (Phase 7 Task 7.3).

The validator itself is opt-in and needs packs plus weights, so these tests pin
its CONTRACT instead: usage validation, the checksum-verified install path, the
metrics it records, both cancellation terminals, the crash restart, the 48 kHz
gate, and the workflow that runs it. Every engine interaction goes through the
scripted fake host, so nothing here needs torch or a checkpoint.
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
from scripts import qwen_release_smoke as smoke
from tests.unit import qwen_host_fake as host_fake

from vienetts_app.core.audio import write_wav_file
from vienetts_app.core.engine_profiles import QWEN_CUSTOM
from vienetts_app.core.qwen_engine import QwenEngine

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "qwen-runtime-smoke.yml"

#: The locked matrix: every cell the compatibility document promises to prove.
EXPECTED_CELLS = {
    "windows-x64-cpu": ("windows-latest", "cpu"),
    "windows-x64-cuda": ("self-hosted", "cuda"),
    "linux-x64-cpu": ("ubuntu-22.04", "cpu"),
    "linux-x64-cuda": ("self-hosted", "cuda"),
    "macos-arm64-cpu": ("macos-latest", "cpu"),
    "macos-arm64-mps": ("macos-latest", "mps"),
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
    """Run the plan script for one dispatch, the way the workflow runs it."""
    return subprocess.run(
        [sys.executable, str(script), cells], capture_output=True, text=True, check=False
    )


def pack_args(tmp_path: Path) -> list[str]:
    """A valid `--packs` root: the two subdirectories the validator expects."""
    packs = tmp_path / "packs"
    (packs / "runtime").mkdir(parents=True, exist_ok=True)
    (packs / "models").mkdir(parents=True, exist_ok=True)
    return ["--packs", str(packs), "--out", str(tmp_path / "out")]


def request_for(tmp_path: Path, *extra: str) -> smoke.SmokeRequest:
    # `run()` resolves an unset language; the streaming helpers need it already
    # resolved, so the fixture names one explicitly (later args win).
    return smoke.validate_request(
        smoke.parse_args(
            ["--profile", "customvoice", "--language", "zh", *pack_args(tmp_path), *extra]
        )
    )


def engine_with_switchable_mode(
    tmp_path: Path, mode: str, **overrides: object
) -> tuple[QwenEngine, Path]:
    """A fake host whose mode the test can change between restarts.

    The engine re-spawns the SAME command after a cancel or a crash, so a
    scenario that needs a different behaviour from the fresh host (a real
    restart, a recovery run) has to change what that command reads.
    """
    host = tmp_path / "fake_qwen_host.py"
    host.write_text(host_fake.FAKE_HOST_SOURCE, encoding="utf-8")
    mode_file = tmp_path / "mode.txt"
    mode_file.write_text(mode, encoding="utf-8")
    wrapper = tmp_path / "fake_qwen_host_mode.py"
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
                smoke.parse_args(["--profile", "qwen1_7b", *pack_args(tmp_path)])
            )

    def test_an_unknown_device_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(smoke.ReleaseSmokeUsageError, match="unknown device"):
            request_for(tmp_path, "--device", "tpu")

    def test_base_requires_a_reference_clip_and_transcript(self, tmp_path: Path) -> None:
        args = ["--profile", "base", *pack_args(tmp_path)]
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
                smoke.parse_args(["--profile", "customvoice", "--packs", str(tmp_path / "nope")])
            )

    def test_a_language_the_profile_cannot_serve_is_refused(self, tmp_path: Path) -> None:
        # Vietnamese is VieNeu's: the Qwen profiles must refuse it before loading.
        with pytest.raises(smoke.ReleaseSmokeUsageError, match="does not support language"):
            request_for(tmp_path, "--language", "vi")

    def test_defaults_derive_from_the_packs_root(self, tmp_path: Path) -> None:
        request = request_for(tmp_path)
        assert request.runtime_pack == tmp_path / "packs" / "runtime"
        assert request.model_pack == tmp_path / "packs" / "models"
        assert request.data_dir == tmp_path / "out" / "data"
        assert request.engine_profile == QWEN_CUSTOM
        assert request.wav_path.name == "qwen-customvoice-cpu.wav"

    def test_a_usage_error_exits_two_without_a_traceback(self, capsys) -> None:
        assert smoke.main(["--profile", "nope"]) == 2
        assert "usage error:" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# host pid and installs
# --------------------------------------------------------------------------- #


class _StubEngine:
    def __init__(self, tail: str) -> None:
        self._tail = tail

    def stderr_tail(self) -> str:
        return self._tail


class TestHostPid:
    def test_the_pid_comes_from_the_last_starting_line(self) -> None:
        tail = "\n".join(
            [
                '{"event": "starting", "pid": 111}',
                "library chatter, not JSON",
                '{"event": "loaded", "profile": "customvoice"}',
                '{"event": "starting", "pid": 222}',
            ]
        )
        assert smoke.host_pid(_StubEngine(tail)) == 222

    def test_without_a_starting_line_it_is_an_actionable_error(self) -> None:
        with pytest.raises(smoke.ReleaseSmokeError, match="never logged its pid"):
            smoke.host_pid(_StubEngine('{"event": "loaded"}'))


class TestInstallPacks:
    def test_an_empty_runtime_pack_fails_against_the_pinned_manifest(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from vienetts_app.core import qwen_runtime_manifest

        monkeypatch.setattr(
            qwen_runtime_manifest, "host_platform_key", lambda _device: "linux-x64-cpu"
        )
        request = request_for(tmp_path)
        with pytest.raises(smoke.ReleaseSmokeError) as excinfo:
            smoke.install_packs(request)
        message = str(excinfo.value)
        # The real manifest is consulted: the first pinned wheel is reported
        # missing, so a pack that is not the verified one can never load.
        assert "did not install" in message
        assert ".whl" in message

    def test_a_platform_without_a_pinned_runtime_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from vienetts_app.core import qwen_runtime_manifest

        monkeypatch.setattr(
            qwen_runtime_manifest, "host_platform_key", lambda _device: "linux-arm64-cpu"
        )
        with pytest.raises(smoke.ReleaseSmokeError, match="no pinned Qwen runtime"):
            smoke.install_packs(request_for(tmp_path))


# --------------------------------------------------------------------------- #
# metrics and the 48 kHz artifact
# --------------------------------------------------------------------------- #


class TestStreamSegment:
    def test_metrics_and_a_48khz_wav(self, tmp_path: Path) -> None:
        engine = host_fake.engine_for(tmp_path, "ok")
        request = request_for(tmp_path)
        engine.initialize()
        pid = host_fake.host_pid(tmp_path)
        audio, metrics = smoke.stream_segment(
            engine, request, job_id="t", pid=pid, rss_fn=lambda _pid: 12_345
        )
        engine.close()

        assert audio.size == 24_000  # two 12 000-sample chunks at 48 kHz
        assert metrics.chunks == 2
        assert metrics.audio_seconds == pytest.approx(0.5)
        assert 0 < metrics.ttfr_seconds <= metrics.total_seconds
        assert metrics.rtf > 0
        assert metrics.peak_rss_bytes == 12_345
        assert metrics.peak_rss_source == "sampled-host"

        # The scripted host emits silence, and the release gate must not be
        # fooled by it: the artifact is checked at 48 kHz AND for audible level
        # (a real tone at the same rate passes in TestWavCheckerRateGate).
        write_wav_file(audio, request.wav_path, smoke.APP_SAMPLE_RATE)
        report = smoke.validate_wav(request)
        assert report["sampleRate"] == 48_000
        assert report["seconds"] == pytest.approx(0.5)
        assert any("silent or near-silent" in problem for problem in report["problems"])

    def test_without_rss_samples_the_source_says_unavailable(self, tmp_path: Path) -> None:
        engine = host_fake.engine_for(tmp_path, "ok")
        request = request_for(tmp_path)
        engine.initialize()
        pid = host_fake.host_pid(tmp_path)
        _audio, metrics = smoke.stream_segment(
            engine, request, job_id="t", pid=pid, rss_fn=lambda _pid: None
        )
        engine.close()
        assert metrics.peak_rss_bytes == 0
        assert metrics.peak_rss_source == "unavailable"


# --------------------------------------------------------------------------- #
# cancellation and crash recovery
# --------------------------------------------------------------------------- #


class TestCancellation:
    def test_a_settled_cancel_keeps_the_host_and_needs_no_recovery(self, tmp_path: Path) -> None:
        engine = host_fake.engine_for(tmp_path, "graceful_cancel", cancel_timeout=2.0)
        request = request_for(tmp_path, "--cancel-after-ms", "10")
        engine.initialize()
        pid = host_fake.host_pid(tmp_path)
        report = smoke.check_cancellation(engine, request, pid=pid)
        alive = smoke.process_alive(pid)
        engine.close()

        assert report["terminal"] == "cancelled"
        assert report["latencySeconds"] >= 0
        assert report["hostAliveAfterCancel"] is True
        assert "recovered" not in report
        assert alive is True

    def test_a_host_that_will_not_stop_is_terminated_and_the_engine_recovers(
        self, tmp_path: Path
    ) -> None:
        engine, mode_file = engine_with_switchable_mode(
            tmp_path, "hang_synthesize", cancel_timeout=0.3
        )
        request = request_for(tmp_path, "--cancel-after-ms", "10")
        engine.initialize()
        pid = host_fake.host_pid(tmp_path)
        mode_file.write_text("ok", encoding="utf-8")  # the NEXT host streams
        report = smoke.check_cancellation(engine, request, pid=pid)
        engine.close()

        # A host that ignores the cancel is terminated, and the engine still
        # reports a cancellation (the force-cancel path) instead of a crash.
        assert report["terminal"] == "cancelled"
        assert report["hostAliveAfterCancel"] is False
        assert report["hostReaped"] is True
        assert report["recovered"] is True
        assert report["recoverySeconds"] > 0


class TestRestart:
    def test_a_killed_host_is_restarted_by_the_next_job(self, tmp_path: Path) -> None:
        engine, mode_file = engine_with_switchable_mode(tmp_path, "slow_pcm")
        # 300 ms lands while the scripted host is holding its first PCM chunk.
        request = request_for(tmp_path, "--cancel-after-ms", "300")
        engine.initialize()
        pid = host_fake.host_pid(tmp_path)
        mode_file.write_text("ok", encoding="utf-8")  # the NEXT host streams
        report = smoke.check_restart(engine, request, lambda: host_fake.host_pid(tmp_path))
        engine.close()

        assert report["killed"] is True
        assert report["hostPid"] == pid
        assert report["recovered"] is True
        assert report["audioSeconds"] > 0
        assert report["recoverySeconds"] > 0
        assert "error" not in report


# --------------------------------------------------------------------------- #
# the report contract
# --------------------------------------------------------------------------- #


class TestEmit:
    def test_a_finding_fails_the_run_and_the_json_is_mirrored(self, tmp_path: Path, capsys) -> None:
        report = {
            "schemaVersion": smoke.SCHEMA_VERSION,
            "kind": smoke.KIND,
            "profile": "customvoice",
            "device": "cpu",
            "synthesis": {"ttfrSeconds": 1.0},
            "problems": ["the host produced no audio"],
        }
        target = tmp_path / "metrics" / "report.json"
        assert smoke.emit(report, str(target)) == 1
        captured = capsys.readouterr()
        assert json.loads(captured.out) == report
        assert "FAIL the host produced no audio" in captured.err
        assert json.loads(target.read_text(encoding="utf-8")) == report

    def test_a_clean_report_exits_zero(self, capsys) -> None:
        report = {
            "kind": smoke.KIND,
            "profile": "base",
            "device": "mps",
            "synthesis": {"ttfrSeconds": 2.0, "totalSeconds": 4.0, "rtf": 0.5},
            "problems": [],
        }
        assert smoke.emit(report, "") == 0
        captured = capsys.readouterr()
        assert json.loads(captured.out) == report
        assert "ok: base on mps" in captured.err


class TestWavCheckerRateGate:
    def test_the_expected_rate_passes_and_a_wrong_one_fails(self, tmp_path: Path) -> None:
        path = tmp_path / "tone.wav"
        tone = 0.4 * np.sin(np.linspace(0.0, 400.0 * np.pi, 48_000, dtype=np.float32))
        write_wav_file(tone, path, 48_000)

        assert check_smoke_wav.check(path, 0.5, expect_rate=48_000) == []
        assert check_smoke_wav.check(path, 0.5, expect_rate=24_000) == [
            "sample rate 48000 != required 24000"
        ]
        assert check_smoke_wav.main([str(path), "--expect-rate", "24000"]) == 1
        assert check_smoke_wav.main([str(path), "--expect-rate", "48000"]) == 0


# --------------------------------------------------------------------------- #
# the opt-in workflow that runs it
# --------------------------------------------------------------------------- #


class TestWorkflowContract:
    def test_the_workflow_is_opt_in_only(self) -> None:
        text = WORKFLOW.read_text(encoding="utf-8")
        assert "workflow_dispatch:" in text
        # Ordinary CI must never run this: it downloads multi-GB packs.
        assert "\n  push:" not in text
        assert "pull_request" not in text
        assert "schedule" not in text

    def test_the_matrix_covers_every_locked_cell(self) -> None:
        # The cells are declared once, in the plan job's table; `validate` builds
        # its matrix from it, so that table IS the locked contract.
        table = cell_table()
        assert set(table) == set(EXPECTED_CELLS)
        for cell, (runner, device) in EXPECTED_CELLS.items():
            spec = table[cell]
            assert spec["device"] == device
            runs_on = spec["runs_on"]
            if device == "cuda":
                # GitHub-hosted runners have no GPU: CUDA cells need self-hosted ones.
                assert "self-hosted" in runs_on
            else:
                assert "self-hosted" not in runs_on
                assert runner.split("-")[0] in runs_on

    def test_the_cells_input_shrinks_the_matrix_not_a_job_level_if(self) -> None:
        # A job-level `if:` cannot read `matrix` — GitHub rejects the whole
        # workflow file — so `cells` filters the matrix in the plan job instead.
        # That is also what keeps an unselected cell from creating a job: a CUDA
        # cell would otherwise wait on a self-hosted runner that is not online.
        text = WORKFLOW.read_text(encoding="utf-8")
        assert all(
            "matrix" not in condition
            for condition in re.findall(r"^    if: (?P<condition>.+)$", text, re.M)
        )
        assert "matrix: ${{ fromJSON(needs.plan.outputs.matrix) }}" in text
        assert "needs: plan" in text

    def test_the_plan_step_selects_exactly_the_requested_cells(self, tmp_path: Path) -> None:
        script = plan_script(tmp_path)

        default = selected_matrix(script, "windows-x64-cpu,linux-x64-cpu,macos-arm64-cpu")
        assert default.returncode == 0
        assert [
            entry["cell"] for entry in json.loads(default.stdout.removeprefix("matrix="))["include"]
        ] == ["windows-x64-cpu", "linux-x64-cpu", "macos-arm64-cpu"]

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
        # The message names the choices, so a typo costs one dispatch to fix.
        assert all(cell in refused.stderr for cell in EXPECTED_CELLS)

    def test_the_packs_are_consumed_offline(self) -> None:
        text = WORKFLOW.read_text(encoding="utf-8")
        assert "scripts/qwen_release_smoke.py" in text
        assert "qwen-models.zip" in text
        assert "--packs" in text
        assert 'HF_HUB_OFFLINE: "1"' in text
        assert 'TRANSFORMERS_OFFLINE: "1"' in text
        # The app environment never gains the stack: the runtime comes from the pack.
        assert "qwen-tts" not in text
        assert "download.pytorch.org" not in text

    def test_every_artifact_is_gated_at_48khz(self) -> None:
        text = WORKFLOW.read_text(encoding="utf-8")
        assert "scripts/check_smoke_wav.py" in text
        assert "--expect-rate 48000" in text

    def test_the_metrics_and_audio_are_uploaded(self) -> None:
        text = WORKFLOW.read_text(encoding="utf-8")
        assert "actions/upload-artifact@v4" in text
        assert "qwen-metrics/*.json" in text
        assert "qwen-smoke/*.wav" in text
        assert "$GITHUB_STEP_SUMMARY" in text
