"""Unit tests for core/onnx_audio.py: Silero VAD framing and hysteresis, speaker embeddings.

Inference runs through fake ``session_factory`` sessions; only the last test
touches real model files, and it skips unless they are installed.
"""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest

from vienetts_app.core import onnx_audio
from vienetts_app.core.ai_models import (
    SILERO_VAD_MODEL_ID,
    SPEAKER_EMBEDDING_MODEL_ID,
    audio_model_path,
)


class ScriptedSilero:
    """Returns scripted speech probabilities and adds 1 to the state each frame."""

    def __init__(self, probs: list[float]) -> None:
        self.probs = probs
        self.calls: list[dict[str, np.ndarray]] = []

    def run(self, _outputs: Any, feeds: dict[str, np.ndarray]) -> list[np.ndarray]:
        self.calls.append({name: np.array(value, copy=True) for name, value in feeds.items()})
        prob = self.probs[len(self.calls) - 1]
        return [np.array([[prob]], dtype=np.float32), feeds["state"] + 1.0]


class FixedEmbedding:
    def __init__(self) -> None:
        self.calls: list[np.ndarray] = []

    def get_inputs(self) -> list[Any]:
        return [SimpleNamespace(name="x")]

    def get_modelmeta(self) -> Any:
        return SimpleNamespace(
            custom_metadata_map={"normalize_samples": "1", "feature_normalize_type": "global-mean"}
        )

    def run(self, _outputs: Any, feeds: dict[str, np.ndarray]) -> list[np.ndarray]:
        self.calls.append(feeds["x"])
        vec = np.zeros((1, 512), dtype=np.float32)
        vec[0, :2] = (3.0, 4.0)
        return [vec]


def test_silero_vad_carries_state_and_context_and_applies_hysteresis(tmp_path: Path) -> None:
    n = 10 * 512 + 100  # 11 frames, the last one zero-padded
    samples = (np.arange(n, dtype=np.float32) + 1.0) / n
    # Speech from frame 1; 0.4 stays speech (>= 0.35 offset), three low frames
    # end it at frame 4; frame 8 starts again and one low frame is bridged.
    session = ScriptedSilero([0.1, 0.6, 0.4, 0.4, 0.2, 0.1, 0.1, 0.1, 0.7, 0.2, 0.9])
    model = tmp_path / "silero_vad.onnx"
    model.write_bytes(b"onnx")
    vad = onnx_audio.SileroVad(model, session_factory=lambda _path, _threads: session)

    spans = vad.speech_spans(samples, 16_000, min_speech_ms=32, min_silence_ms=64)

    assert spans == [(512, 2048), (4096, n)]
    assert len(session.calls) == 11
    padded = np.concatenate([samples, np.zeros(11 * 512 - n, dtype=np.float32)])
    for i, call in enumerate(session.calls):
        assert call["input"].shape == (1, 576)
        context = np.zeros(64, np.float32) if i == 0 else session.calls[i - 1]["input"][0, -64:]
        np.testing.assert_array_equal(call["input"][0, :64], context)
        np.testing.assert_array_equal(call["input"][0, 64:], padded[i * 512 : (i + 1) * 512])
        assert call["state"].shape == (2, 1, 128) and np.all(call["state"] == float(i))
        assert call["sr"].dtype == np.int64 and int(call["sr"]) == 16_000

    # Other rates are resampled to 16 kHz and the spans mapped back.
    session.calls.clear()
    tripled = np.repeat(samples, 3)
    assert vad.speech_spans(tripled, 48_000, min_speech_ms=32, min_silence_ms=64) == [
        (1536, 6144),
        (12288, 3 * n),
    ]


def test_speaker_embedding_feeds_80_dim_fbank_and_l2_normalizes(tmp_path: Path) -> None:
    session = FixedEmbedding()
    made: list[tuple[Path, int]] = []

    def factory(path: Path, threads: int) -> FixedEmbedding:
        made.append((path, threads))
        return session

    model = tmp_path / "campplus.onnx"
    model.write_bytes(b"onnx")
    embedder = onnx_audio.SpeakerEmbedder(model, session_factory=factory)
    rng = np.random.default_rng(0)
    vec = embedder.embed((0.1 * rng.standard_normal(16_000)).astype(np.float32), 16_000)

    assert vec.dtype == np.float32 and vec.shape == (512,)
    np.testing.assert_allclose(vec[:2], [0.6, 0.8], rtol=1e-6)
    assert float(np.linalg.norm(vec)) == pytest.approx(1.0)
    (x,) = session.calls
    assert x.shape == (1, 98, 80) and x.dtype == np.float32  # 25 ms / 10 ms frames
    np.testing.assert_allclose(x[0].mean(axis=0), 0.0, atol=1e-4)  # global-mean
    # A clip shorter than one frame is tiled up to something the model can embed.
    assert embedder.embed(np.full(100, 0.1, np.float32), 16_000).shape == (512,)
    # One shared session per model file.
    onnx_audio.SpeakerEmbedder(model, session_factory=factory)
    assert len(made) == 1


def _real_models_dir() -> Path | None:
    raw = os.environ.get("VIENETTS_AUDIO_MODELS_DIR", "")
    if raw:
        return Path(raw)
    from vienetts_app.core.settings import default_data_dir

    return default_data_dir()


def test_real_onnx_models_run_when_installed() -> None:
    data_dir = _real_models_dir()
    vad_path = audio_model_path(SILERO_VAD_MODEL_ID, data_dir)
    emb_path = audio_model_path(SPEAKER_EMBEDDING_MODEL_ID, data_dir)
    if vad_path is None or emb_path is None:
        pytest.skip("Silero VAD / speaker-embedding models are not installed")
    pytest.importorskip("onnxruntime")

    vad = onnx_audio.SileroVad(vad_path)
    probs = vad.frame_probabilities(np.zeros(16_000, dtype=np.float32))
    assert probs.shape == (32,) and np.all((probs >= 0.0) & (probs <= 1.0))
    assert vad.speech_spans(np.zeros(16_000, dtype=np.float32), 16_000) == []

    embedder = onnx_audio.SpeakerEmbedder(emb_path)
    t = np.arange(32_000, dtype=np.float32) / 16_000
    voiced = (0.3 * np.sin(2 * np.pi * 140.0 * t) * (1 + np.sin(2 * np.pi * 3 * t))).astype(
        np.float32
    )
    first = embedder.embed(voiced, 16_000)
    assert first.shape == (512,) and float(np.linalg.norm(first)) == pytest.approx(1.0, abs=1e-4)
    np.testing.assert_allclose(embedder.embed(voiced, 16_000), first, atol=1e-5)
