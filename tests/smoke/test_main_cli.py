"""--smoke CLI: end-to-end synthesis through the worker, exit codes, WAV output."""

from pathlib import Path

import numpy as np
import soundfile as sf

from vienetts_app.__main__ import main
from vienetts_app.core.engine import TTSEngineError


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
            def infer(self, text, voice=None, **kw):
                raise TTSEngineError("Voice 'Nope' not found")

        rc = main(
            ["--smoke", "hi", "--voice", "Nope", "-o", str(tmp_path / "x.wav")],
            engine_factory=lambda **kw: Boom(),
        )
        assert rc == 1
        assert "Nope" in capsys.readouterr().err
        # argv-dispatch siblings (no-args → GUI, blank-text → usage error) are
        # pinned by tests/unit/test_app_entry.py::TestArgvDispatch.
