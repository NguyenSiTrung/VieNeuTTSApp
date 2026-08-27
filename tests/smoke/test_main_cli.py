"""--smoke CLI: end-to-end synthesis through the worker, exit codes, WAV output."""

from pathlib import Path

import numpy as np
import pytest
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
    def test_exit_zero_and_valid_wav(self, tmp_path: Path, capsys) -> None:
        out = tmp_path / "out.wav"
        rc = main(
            ["--smoke", "Xin chào", "--voice", "Adam", "-o", str(out)], engine_factory=factory
        )
        assert rc == 0
        data, sr = sf.read(str(out), dtype="float32")
        assert sr == 48_000
        assert len(data) == 24_000
        assert float(np.abs(data).max()) > 0.1
        printed = capsys.readouterr().out
        assert str(out) in printed
        assert "engine" in printed.lower()

    def test_default_output_is_out_wav(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.chdir(tmp_path)
        rc = main(["--smoke", "hi"], engine_factory=factory)
        assert rc == 0
        assert (tmp_path / "out.wav").is_file()

    def test_stream_mode_writes_wav(self, tmp_path: Path) -> None:
        out = tmp_path / "s.wav"
        rc = main(["--smoke", "hi", "--stream", "-o", str(out)], engine_factory=factory)
        assert rc == 0
        data, sr = sf.read(str(out), dtype="float32")
        assert sr == 48_000 and len(data) == 24_000

    def test_unicode_text_accepted(self, tmp_path: Path) -> None:
        rc = main(
            ["--smoke", "Xin chào thế giới 🌏", "-o", str(tmp_path / "u.wav")],
            engine_factory=factory,
        )
        assert rc == 0


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

    def test_no_args_routes_to_gui_not_smoke(self) -> None:
        # FR-2.1 superseded the Phase 1 "missing --smoke is a usage error"
        # contract: no args now opens the GUI shell (see test_app_entry.py).
        rc = main([], gui_runner=lambda: 0)
        assert rc == 0

    def test_blank_smoke_text_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(SystemExit):
            main(["--smoke", "   ", "-o", str(tmp_path / "y.wav")], engine_factory=factory)
