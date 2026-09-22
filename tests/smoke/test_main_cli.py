"""--smoke CLI: end-to-end synthesis through the worker, exit codes, WAV output.

``--qwen-host`` (Phase 7 Task 7.1) is the other headless entry: the packaged
app re-dispatching ITSELF as the isolated Qwen model host. It needs no models
and no runtime, so it is exercised here end-to-end through a real process.
"""

import json
import subprocess
import sys
from io import BytesIO
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from vienetts_app.__main__ import main
from vienetts_app.core.engine import TTSEngineError
from vienetts_app.core.qwen_protocol import EndOfStream, read_frame

pytestmark = pytest.mark.smoke


def tone(samples: int = 48_000) -> np.ndarray:
    t = np.arange(samples, dtype=np.float32) / 48_000.0
    return (0.4 * np.sin(2 * np.pi * 440.0 * t)).astype(np.float32)


class CliEngine:
    """Real-ish fake: infer returns a sine; stream yields two chunks."""

    sample_rate = 48_000
    backend = "onnx"

    def infer(self, text, voice=None, **kw) -> np.ndarray:
        return tone(24_000)

    def infer_stream(self, text, voice=None, **kw):
        yield tone(12_000)
        yield tone(12_000)

    def close(self) -> None:
        pass


def factory(**kwargs) -> CliEngine:
    return CliEngine()


class TestSmokeHappyPath:
    def test_smoke_cli_happy_paths(self, tmp_path: Path, capsys, monkeypatch) -> None:
        # 1. Custom out path + soundfile read-back
        out = tmp_path / "out.wav"
        rc = main(
            ["--smoke", "Xin chào thế giới 🌏", "--voice", "Adam", "-o", str(out)],
            engine_factory=factory,
        )
        assert rc == 0
        data, sr = sf.read(str(out), dtype="float32")
        assert sr == 48_000
        assert len(data) == 24_000
        assert float(np.abs(data).max()) > 0.1
        printed = capsys.readouterr().out
        assert str(out) in printed
        assert "engine" in printed.lower()

        # 2. Default output is out.wav
        monkeypatch.chdir(tmp_path)
        rc2 = main(["--smoke", "hi"], engine_factory=factory)
        assert rc2 == 0
        assert (tmp_path / "out.wav").is_file()

        # 3. Stream mode
        s_out = tmp_path / "s.wav"
        rc3 = main(["--smoke", "hi", "--stream", "-o", str(s_out)], engine_factory=factory)
        assert rc3 == 0
        s_data, s_sr = sf.read(str(s_out), dtype="float32")
        assert s_sr == 48_000 and len(s_data) == 24_000


class TestSmokeFailures:
    def test_engine_error_exits_nonzero(self, tmp_path: Path, capsys) -> None:
        class Boom(CliEngine):
            def infer_stream(self, text, voice=None, **kw):
                raise TTSEngineError("Voice 'Nope' not found")
                yield  # pragma: no cover - makes this a generator

        rc = main(
            ["--smoke", "hi", "--voice", "Nope", "-o", str(tmp_path / "x.wav")],
            engine_factory=lambda **kw: Boom(),
        )
        assert rc == 1
        assert "Nope" in capsys.readouterr().err
        # argv-dispatch siblings (no-args → GUI, blank-text → usage error) are
        # pinned by tests/unit/test_app_entry.py::TestArgvDispatch.


class TestQwenHostEntry:
    """``--qwen-host``: the packaged app re-dispatching itself as the model host.

    A frozen build has no ``python -m <module>`` to hand the host half to
    (``qwen_engine.host_command``), so the CLI must route the flag to the host
    before any GUI/stdio setup. No runtime and no checkpoint are involved: the
    host announces itself with its hello frame and exits when stdin closes.
    """

    def _run_host(self, tmp_path: Path, *, importtime: bool = False) -> subprocess.CompletedProcess:
        argv = [sys.executable]
        if importtime:
            argv += ["-X", "importtime"]
        argv += ["-m", "vienetts_app", "--qwen-host"]
        cwd = tmp_path / "thư mục có dấu cách"
        cwd.mkdir(parents=True, exist_ok=True)
        return subprocess.run(
            argv, cwd=cwd, input=b"", capture_output=True, timeout=120, check=False
        )

    def test_the_host_half_serves_the_frame_channel_and_never_imports_qt(
        self, tmp_path: Path
    ) -> None:
        proc = self._run_host(tmp_path, importtime=True)
        stderr = proc.stderr.decode("utf-8", "replace")
        assert proc.returncode == 0, stderr
        # stdout is the frame channel: the hello frame, then nothing at all —
        # a stray print would corrupt the parent's reader.
        stream = BytesIO(proc.stdout)
        hello = read_frame(stream)
        assert hello.type == "hello"
        assert hello.get("sampleRate") == 48_000
        assert hello.get("host") == "vienetts-qwen-host"
        with pytest.raises(EndOfStream):
            read_frame(stream)
        events = [
            json.loads(line)["event"]
            for line in stderr.splitlines()
            if line.strip().startswith("{")
        ]
        assert events == ["starting", "peer_closed"]
        # …and the GUI stack stays out of the host process entirely.
        assert "PySide6" not in stderr

    def test_the_check_half_prints_one_verdict_and_never_imports_qt(self, tmp_path: Path) -> None:
        """``--qwen-host-check``: the same re-dispatch, in import-check mode.

        The runtime manager starts this through ``host_check_command`` to prove
        a promoted install can import before committing it. With no runtime on
        the import path the verdict names the missing module — which is the
        whole point: the user is told what is missing, not just that a load
        failed.
        """
        argv = [
            sys.executable,
            "-m",
            "vienetts_app",
            "--qwen-host",
            "--qwen-host-check",
        ]
        proc = subprocess.run(argv, cwd=tmp_path, capture_output=True, timeout=180, check=False)
        stderr = proc.stderr.decode("utf-8", "replace")
        assert proc.returncode == 1, stderr
        # stdout carries exactly one JSON verdict and no protocol frames.
        lines = proc.stdout.decode("utf-8", "replace").strip().splitlines()
        assert len(lines) == 1
        verdict = json.loads(lines[0])
        assert verdict["ok"] is False
        assert "is missing" in verdict["detail"]
        assert "Settings" not in verdict["detail"]  # the detail stays technical
        assert '"event": "import_check"' in stderr
        assert "PySide6" not in stderr
