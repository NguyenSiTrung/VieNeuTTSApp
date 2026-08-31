"""Reference-clip codec capability per platform (bead VieNeuTTSApp-vis).

CloningTab's picker accepts ``*.wav *.mp3``; decode goes through
soundfile/libsndfile, whose mp3 support is a BUILD property of the bundled
library (added in libsndfile 1.1.0 — pysoundfile wheels ship 1.2.x, but a
distro or custom build can differ). This suite turns "mp3 decode works on
this OS" into CI evidence on every release platform (the Release workflow
runs pytest on windows/macos/ubuntu): if a runner's libsndfile loses mp3,
cloning from mp3 breaks for users of that artifact — far better to fail the
build than ship breakage discovered at enrollment time.
"""

import io

import numpy as np
import pytest
import soundfile as sf

SAMPLE_RATE = 24_000  # reference clips are 3-8 s voice; rate is incidental here


def _tone(seconds: float = 0.5) -> np.ndarray:
    t = np.arange(int(SAMPLE_RATE * seconds)) / SAMPLE_RATE
    return (0.4 * np.sin(2 * np.pi * 440.0 * t)).astype(np.float32)


@pytest.mark.parametrize("fmt", ["WAV", "MP3"])
def test_reference_clip_format_decodes(fmt: str) -> None:
    data = _tone()
    buf = io.BytesIO()
    sf.write(buf, data, SAMPLE_RATE, format=fmt)
    buf.seek(0)
    decoded, sample_rate = sf.read(buf, dtype="float32")
    assert sample_rate == SAMPLE_RATE
    # mp3 is lossy with codec padding: assert real energy and comparable
    # length, never sample equality.
    assert decoded.size >= int(data.size * 0.9)
    assert 0.2 < float(np.abs(decoded).max()) < 0.6


def test_soundfile_reports_libsndfile() -> None:
    # Pin the build identity in failure output: the mp3 result above is a
    # property of THIS libsndfile build, so its version belongs in any
    # failure triage.
    assert sf.__libsndfile_version__
