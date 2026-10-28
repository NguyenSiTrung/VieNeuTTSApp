"""Unit tests for core/diarization.py: speaker clustering, RTTM alignment, and reference clips."""

from __future__ import annotations

import time
from pathlib import Path

import numpy as np
import pytest
import soundfile
from tests.unit.diarization_reference import (
    reference_cluster_embeddings,
    reference_refine_cues_speech_bounds,
)

from vienetts_app.core import onnx_audio
from vienetts_app.core.ai_models import (
    AUDIO_MODELS,
    SILERO_VAD_MODEL_ID,
    SPEAKER_EMBEDDING_MODEL_ID,
)
from vienetts_app.core.asr import RawSegment
from vienetts_app.core.audio import read_wav, write_wav_file
from vienetts_app.core.diarization import (
    DiarizationCancelled,
    DiarizationConfig,
    RttmTurn,
    _cluster_embeddings,
    _neural_cue_labels,
    assign_speakers_from_turns,
    diarize_cues,
    extract_speaker_reference_wav,
    parse_audiocpp_turns,
    parse_rttm_turns,
    refine_cues_speech_bounds,
    select_speaker_reference_clip,
    split_segments_at_turns,
)
from vienetts_app.core.subtitles import Cue


def _synth_voice_tone(sr: int, duration_s: float, f0_hz: float, formant_hz: float) -> np.ndarray:
    t = np.linspace(0.0, duration_s, int(sr * duration_s), endpoint=False, dtype=np.float32)
    sig = (
        0.5 * np.sin(2.0 * np.pi * f0_hz * t)
        + 0.3 * np.sin(2.0 * np.pi * (2.0 * f0_hz) * t)
        + 0.2 * np.sin(2.0 * np.pi * formant_hz * t)
    )
    return sig.astype(np.float32)


def test_parse_rttm_and_assign_speakers_from_turns() -> None:
    rttm = (
        "SPEAKER sample 1 0.000 2.100 <NA> <NA> speaker_0 <NA> <NA>\n"
        "SPEAKER sample 1 2.200 2.000 <NA> <NA> speaker_1 <NA> <NA>\n"
        "SPEAKER sample 1 4.500 1.800 <NA> <NA> speaker_0 <NA> <NA>\n"
    )
    turns = parse_rttm_turns(rttm)
    assert len(turns) == 3
    assert turns[0].speaker == "SPEAKER_01"
    assert turns[1].speaker == "SPEAKER_02"
    assert turns[2].speaker == "SPEAKER_01"

    cues = [
        Cue(1, 100, 1900, "Xin chào"),
        Cue(2, 2300, 4100, "Chào bạn"),
        Cue(3, 4600, 6100, "Hẹn gặp lại"),
    ]
    tagged = assign_speakers_from_turns(cues, turns)
    assert [c.speaker for c in tagged] == ["SPEAKER_01", "SPEAKER_02", "SPEAKER_01"]


def test_split_segments_at_turns_cuts_at_the_nearest_word_boundary() -> None:
    words = (
        (100, 400, "Xin"),
        (450, 800, "chào"),
        (850, 1300, "các"),
        (1350, 1900, "bạn."),  # the A→B change at 1700 is past this word's midpoint
        (1950, 2500, "Tôi"),
        (2550, 3000, "là"),
        (3050, 3600, "Lan."),
    )
    seg = RawSegment(0, 3700, "Xin chào các bạn. Tôi là Lan.", words=words)
    two = [RttmTurn(0, 1700, "SPEAKER_01"), RttmTurn(1700, 3700, "SPEAKER_02")]
    untouched = RawSegment(4000, 5000, "Không có từ.")

    assert split_segments_at_turns([seg, untouched], two) == [
        RawSegment(
            0,
            1900,
            "Xin chào các bạn.",
            "SPEAKER_01",
            words=words[:4],
            speech_start_ms=100,
            speech_end_ms=1900,
        ),
        RawSegment(
            1950,
            3700,
            "Tôi là Lan.",
            "SPEAKER_02",
            words=words[4:],
            speech_start_ms=1950,
            speech_end_ms=3600,
        ),
        untouched,
    ]

    # Three turns → three parts, each labelled and bounded by its own words.
    words3 = (
        (100, 400, "Một"),
        (450, 1100, "hai."),
        (1250, 1700, "Ba"),
        (1750, 2500, "bốn."),
        (2650, 3100, "Năm"),
        (3150, 3900, "sáu."),
    )
    seg3 = RawSegment(0, 4000, "Một hai. Ba bốn. Năm sáu.", words=words3)
    three = [
        RttmTurn(0, 1200, "SPEAKER_01"),
        RttmTurn(1200, 2600, "SPEAKER_02"),
        RttmTurn(2600, 4000, "SPEAKER_03"),
    ]
    parts = split_segments_at_turns([seg3], three)
    assert [(p.start_ms, p.end_ms, p.text, p.speaker) for p in parts] == [
        (0, 1100, "Một hai.", "SPEAKER_01"),
        (1250, 2500, "Ba bốn.", "SPEAKER_02"),
        (2650, 4000, "Năm sáu.", "SPEAKER_03"),
    ]
    assert [(p.speech_start_ms, p.speech_end_ms) for p in parts] == [
        (100, 1100),
        (1250, 2500),
        (2650, 3900),
    ]
    assert [len(p.words) for p in parts] == [2, 2, 2]


def test_split_segments_at_turns_keeps_segments_it_cannot_split_safely() -> None:
    words = (
        (100, 600, "Vâng"),
        (650, 1200, "tôi"),
        (1250, 1800, "hiểu"),
        (1850, 2400, "rồi"),
        (2450, 2700, "ạ"),
        (2750, 2990, "nhé."),
    )
    seg = RawSegment(0, 3000, "Vâng tôi hiểu rồi ạ nhé.", words=words)
    # The B side would be 2 words but only 550 ms long.
    short_tail = [RttmTurn(0, 2430, "SPEAKER_01"), RttmTurn(2430, 3000, "SPEAKER_02")]
    # The B side would be a single word.
    one_word = [RttmTurn(0, 2730, "SPEAKER_01"), RttmTurn(2730, 3000, "SPEAKER_02")]
    # A one-word interjection inside A's turn.
    interjection = [
        RttmTurn(0, 1220, "SPEAKER_01"),
        RttmTurn(1220, 1830, "SPEAKER_02"),
        RttmTurn(1830, 3000, "SPEAKER_01"),
    ]
    no_words = RawSegment(0, 3000, "Vâng tôi hiểu rồi ạ nhé.")
    for turns in (short_tail, one_word, interjection):
        assert split_segments_at_turns([seg], turns) == [seg]
    assert split_segments_at_turns([no_words], short_tail) == [no_words]
    assert split_segments_at_turns([seg], []) == [seg]


def test_acoustic_diarization_separates_two_distinct_speakers(tmp_path: Path) -> None:
    sr = 16_000
    # Build an 8-second WAV alternating between low-pitched voice A (110 Hz / 900 Hz)
    # and high-pitched voice B (240 Hz / 2600 Hz).
    v_a = _synth_voice_tone(sr, 1.8, 110.0, 900.0)
    v_b = _synth_voice_tone(sr, 1.8, 240.0, 2600.0)
    gap = np.zeros(int(sr * 0.2), dtype=np.float32)
    full = np.concatenate([v_a, gap, v_b, gap, v_a, gap, v_b])
    wav = write_wav_file(full, tmp_path / "dialog.wav", sample_rate=sr)

    cues = [
        Cue(1, 0, 1800, "Câu của người thứ nhất."),
        Cue(2, 2000, 3800, "Câu của người thứ hai."),
        Cue(3, 4000, 5800, "Người thứ nhất nói tiếp."),
        Cue(4, 6000, 7800, "Người thứ hai trả lời."),
    ]
    tagged = diarize_cues(wav, cues, DiarizationConfig(backend="acoustic", max_speakers=2))
    assert tagged[0].speaker == "SPEAKER_01"
    assert tagged[1].speaker == "SPEAKER_02"
    assert tagged[2].speaker == "SPEAKER_01"
    assert tagged[3].speaker == "SPEAKER_02"

    # Shared work_dir/source_16k.wav avoids re-extracting video audio
    import pytest

    from vienetts_app.core.diarization import DiarizationError

    fake_video = tmp_path / "shared_video.mp4"
    fake_video.write_bytes(b"not-a-real-video")
    shared_dir = tmp_path / "shared_work"
    shared_dir.mkdir(parents=True, exist_ok=True)
    write_wav_file(full, shared_dir / "source_16k.wav", sample_rate=sr)
    reused = diarize_cues(
        fake_video,
        cues,
        DiarizationConfig(backend="acoustic", max_speakers=2),
        work_dir=shared_dir,
    )
    assert [c.speaker for c in reused] == ["SPEAKER_01", "SPEAKER_02", "SPEAKER_01", "SPEAKER_02"]

    with pytest.raises(DiarizationError):
        diarize_cues(
            wav,
            cues,
            DiarizationConfig(backend="acoustic", max_speakers=2),
            cancel_check=lambda: True,
        )


def test_select_and_extract_speaker_reference_clip(tmp_path: Path) -> None:
    sr = 48_000
    full = np.full(sr * 10, 0.25, dtype=np.float32)
    wav = write_wav_file(full, tmp_path / "movie_audio.wav", sample_rate=sr)

    cues = [
        Cue(1, 200, 1000, "Ngắn.", speaker="SPEAKER_01"),
        Cue(2, 1500, 3200, "Câu khá dài đầu tiên,", speaker="SPEAKER_01"),
        Cue(3, 3400, 6200, "và câu tiếp theo rất rõ ràng để lấy mẫu giọng.", speaker="SPEAKER_01"),
        Cue(4, 6500, 9000, "Câu của người khác.", speaker="SPEAKER_02"),
    ]
    sel = select_speaker_reference_clip(cues, "SPEAKER_01")
    assert sel is not None
    start_ms, end_ms, transcript = sel
    # Should pick the 4.7s combined span of cues 2+3 (1500..6200 ms)
    assert start_ms == 1500
    assert end_ms == 6200
    assert "Câu khá dài đầu tiên" in transcript

    out_wav, out_text = extract_speaker_reference_wav(
        wav, cues, "SPEAKER_01", tmp_path / "ref_spk1.wav"
    )
    assert out_wav.is_file()
    samples, out_sr = read_wav(out_wav)
    assert out_sr == 48_000
    assert 4.0 <= (samples.size / out_sr) <= 5.5
    assert out_text == transcript


def test_nemotron_3_diarization_gguf_audiocpp_cpu_turns(tmp_path: Path) -> None:
    import json
    import os

    # 1. Verify exact audio.cpp --turns-out JSON schema (16 kHz sample offsets)
    turns_json = json.dumps(
        [
            {"start_sample": 0, "end_sample": 32000, "speaker_id": "speaker_0", "confidence": 1.0},
            {
                "start_sample": 35200,
                "end_sample": 67200,
                "speaker_id": "speaker_1",
                "confidence": 0.98,
            },
        ]
    )
    turns = parse_audiocpp_turns(turns_json, sample_rate=16_000)
    assert len(turns) == 2
    assert turns[0].start_ms == 0 and turns[0].end_ms == 2000 and turns[0].speaker == "SPEAKER_01"
    assert (
        turns[1].start_ms == 2200 and turns[1].end_ms == 4200 and turns[1].speaker == "SPEAKER_02"
    )

    # 2. End-to-end nemotron_gguf backend execution via audiocpp_cli on CPU
    bin_dir = tmp_path / "runtimes" / "audiocpp" / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    cli_name = "audiocpp_cli.exe" if os.name == "nt" else "audiocpp_cli"
    cli_path = bin_dir / cli_name
    cli_path.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        "from pathlib import Path\n"
        "args = sys.argv[1:]\n"
        "out = args[args.index('--turns-out') + 1]\n"
        "turns = [\n"
        "    {'start_sample': 0, 'end_sample': 32000, "
        "'speaker_id': 'speaker_0', 'confidence': 1.0},\n"
        "    {'start_sample': 35200, 'end_sample': 67200, "
        "'speaker_id': 'speaker_1', 'confidence': 0.95},\n"
        "]\n"
        "Path(out).write_text(json.dumps(turns), encoding='utf-8')\n",
        encoding="utf-8",
    )
    cli_path.chmod(0o755)

    model_dir = tmp_path / "models" / "diarization"
    model_dir.mkdir(parents=True, exist_ok=True)
    gguf_file = model_dir / "nemotron-3-diarization-q8_0.gguf"
    # The managed-model size guard rejects truncated catalog artifacts; a sparse
    # file keeps this fake at the real size without writing 102 MB.
    from vienetts_app.core.ai_models import DIARIZATION_GGUF_MODELS

    with gguf_file.open("wb") as fh:
        fh.truncate(int(DIARIZATION_GGUF_MODELS["nemotron-3-diarization-q8_0"].size_bytes))

    wav = write_wav_file(np.zeros(16_000 * 5, dtype=np.float32), tmp_path / "in.wav", 16_000)
    cues = [
        Cue(1, 100, 1900, "Người thứ nhất nói."),
        Cue(2, 2300, 4100, "Người thứ hai nói."),
    ]
    tagged = diarize_cues(
        wav,
        cues,
        DiarizationConfig(
            backend="nemotron_gguf",
            model="nemotron-3-diarization-q8_0",
            device="cpu",
        ),
        data_dir=tmp_path,
    )
    assert [c.speaker for c in tagged] == ["SPEAKER_01", "SPEAKER_02"]


def test_refine_cues_speech_bounds_vad_and_turn_intersection(tmp_path: Path) -> None:
    sr = 16_000
    # Cue 1 [0..2000 ms]: 250 ms leading silence, 1250 ms active speech (250..1500 ms),
    # 500 ms trailing silence. Cue 2 [2200..3500 ms]: completely silent.
    lead_sil = np.zeros(int(sr * 0.250), dtype=np.float32)
    active_speech = _synth_voice_tone(sr, 1.250, 140.0, 1100.0)
    trail_sil = np.zeros(int(sr * 0.500), dtype=np.float32)
    cue2_sil = np.zeros(int(sr * 1.500), dtype=np.float32)
    full = np.concatenate([lead_sil, active_speech, trail_sil, cue2_sil])
    wav_path = write_wav_file(full, tmp_path / "source_16k.wav", sample_rate=sr)

    cues = [
        Cue(1, 0, 2000, "Câu có khoảng lặng đầu và cuối.", speaker="SPEAKER_01"),
        Cue(2, 2200, 3400, "Câu im lặng.", speaker="SPEAKER_02"),
        Cue(
            3,
            2200,
            3400,
            "Câu giữ biên có sẵn khi âm thanh im lặng.",
            speaker="SPEAKER_02",
            speech_start_ms=2350,
            speech_end_ms=3100,
        ),
    ]
    refined = refine_cues_speech_bounds(cues, wav_path)
    # Caption window [0, 2000] stays intact; speech bounds tighten to ~[220, 1530] (30 ms pad)
    assert refined[0].start_ms == 0
    assert refined[0].end_ms == 2000
    assert refined[0].speech_start_ms is not None
    assert refined[0].speech_end_ms is not None
    assert 200 <= refined[0].speech_start_ms <= 250
    assert 1500 <= refined[0].speech_end_ms <= 1550
    assert refined[0].speech_duration_ms < refined[0].duration_ms

    # Silent WAV slice yields None and never overwrites existing bounds
    assert refined[1].speech_start_ms is None
    assert refined[1].speech_end_ms is None
    assert refined[2].speech_start_ms == 2350
    assert refined[2].speech_end_ms == 3100

    # Intersecting with RttmTurn tightens bounds to the intersection for matching speaker
    turns = [
        RttmTurn(320, 1420, "SPEAKER_01"),
        RttmTurn(100, 1900, "SPEAKER_02"),
    ]
    intersected = refine_cues_speech_bounds([cues[0]], wav_path, turns=turns)
    assert intersected[0].start_ms == 0
    assert intersected[0].end_ms == 2000
    assert intersected[0].speech_start_ms == 320
    assert intersected[0].speech_end_ms == 1420

    # Non-overlapping turn falls back to VAD bounds rather than producing an empty span
    disjoint_turns = [RttmTurn(1700, 1950, "SPEAKER_01")]
    fallback = refine_cues_speech_bounds([cues[0]], wav_path, turns=disjoint_turns)
    assert fallback[0].speech_start_ms == refined[0].speech_start_ms
    assert fallback[0].speech_end_ms == refined[0].speech_end_ms


def _speaker_embeddings(
    rng: np.random.Generator, n: int, speakers: int, noise: float
) -> np.ndarray:
    centers = rng.normal(size=(speakers, 24))
    emb = centers[rng.integers(0, speakers, size=n)] + noise * rng.normal(size=(n, 24))
    emb /= np.linalg.norm(emb, axis=1, keepdims=True)
    return emb.astype(np.float32)


def test_cluster_embeddings_matches_the_reference_implementation() -> None:
    # n stays small: float near-ties in large sets could legitimately break
    # either way, and the point is label parity with the O(n^3) original.
    rng = np.random.default_rng(20261029)
    for case in range(50):
        n = int(rng.integers(2, 61))
        emb = _speaker_embeddings(
            rng, n, int(rng.integers(1, 6)), float(rng.choice([0.05, 0.3, 0.8, 2.0]))
        )
        max_speakers = int(rng.choice([0, 0, 2, 3, 8]))
        assert _cluster_embeddings(emb, max_speakers) == reference_cluster_embeddings(
            emb, max_speakers
        ), (case, n, max_speakers)

    with pytest.raises(DiarizationCancelled):
        _cluster_embeddings(emb, cancel_check=lambda: True)


@pytest.mark.benchmark
def test_cluster_embeddings_handles_2000_cues_within_budget() -> None:
    emb = _speaker_embeddings(np.random.default_rng(7), 2000, 4, 0.5)
    start = time.perf_counter()
    labels = _cluster_embeddings(emb)
    elapsed = time.perf_counter() - start
    assert len(labels) == 2000
    assert elapsed < 2.0, f"{elapsed:.2f}s"


def _bursty_speech(rng: np.random.Generator, sr: int, seconds: float) -> np.ndarray:
    """Noise bursts of varying level separated by silences of varying length."""
    out = np.zeros(int(sr * seconds), dtype=np.float32)
    pos = 0
    while pos < out.size:
        gap = int(sr * rng.uniform(0.05, 0.6))
        burst = int(sr * rng.uniform(0.1, 1.5))
        level = float(rng.choice([0.002, 0.05, 0.3]))
        seg = out[pos + gap : pos + gap + burst]
        seg[:] = level * rng.standard_normal(seg.size).astype(np.float32)
        pos += gap + burst
    return out


def test_refine_cues_speech_bounds_opens_the_wav_once_and_matches_the_reference(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sr = 16_000
    rng = np.random.default_rng(5)
    wav = write_wav_file(_bursty_speech(rng, sr, 20.0), tmp_path / "source_16k.wav", sr)
    speakers = ["SPEAKER_01", "SPEAKER_02", "SPEAKER_03", ""]
    cues = []
    for i in range(40):  # unsorted, overlapping, some past the end of the WAV
        start = int(rng.integers(0, 21_000))
        cues.append(
            Cue(i + 1, start, start + int(rng.integers(0, 2500)), f"c{i}", speaker=speakers[i % 4])
        )
    cues.append(Cue(41, 3000, 4000, "giữ", speech_start_ms=3100, speech_end_ms=3900))
    turns = []
    for _ in range(25):
        start = int(rng.integers(0, 20_000))
        turns.append(RttmTurn(start, start + int(rng.integers(1, 3000)), speakers[_ % 3]))
    expected = {
        "vad": reference_refine_cues_speech_bounds(cues, wav),
        "vad+turns": reference_refine_cues_speech_bounds(cues, wav, turns=turns),
        "turns": reference_refine_cues_speech_bounds(cues, None, turns=turns),
    }

    opened: list[str] = []

    class CountingSoundFile(soundfile.SoundFile):
        def __init__(self, file, *args, **kwargs) -> None:
            opened.append(str(file))
            super().__init__(file, *args, **kwargs)

    monkeypatch.setattr(soundfile, "SoundFile", CountingSoundFile)
    assert refine_cues_speech_bounds(cues, wav) == expected["vad"]
    assert len(opened) == 1, opened
    assert refine_cues_speech_bounds(cues, wav, turns=turns) == expected["vad+turns"]
    assert refine_cues_speech_bounds(cues, None, turns=turns) == expected["turns"]
    assert len(opened) == 2, opened


@pytest.mark.benchmark
def test_refine_cues_speech_bounds_sweeps_an_hour_within_budget(tmp_path: Path) -> None:
    sr = 16_000
    rng = np.random.default_rng(11)
    block = _bursty_speech(rng, sr, 60.0)
    wav = tmp_path / "hour_16k.wav"
    with soundfile.SoundFile(wav, "w", samplerate=sr, channels=1, subtype="PCM_16") as fh:
        for _ in range(60):
            fh.write(block)
    cues = [Cue(i + 1, i * 2400, i * 2400 + 2000, f"c{i}") for i in range(1500)]
    turns = [RttmTurn(i * 1200, i * 1200 + 1100, f"SPEAKER_0{i % 3 + 1}") for i in range(3000)]
    start = time.perf_counter()
    refined = refine_cues_speech_bounds(cues, wav, turns=turns)
    elapsed = time.perf_counter() - start
    assert len(refined) == 1500
    assert elapsed < 3.0, f"{elapsed:.2f}s"


def _install_audio_model(data_dir: Path, model_id: str) -> None:
    spec = AUDIO_MODELS[model_id]
    path = data_dir / "models" / spec.subdir / spec.filename
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as fh:  # sparse, at the catalog size
        fh.truncate(spec.size_bytes)


class _LoudFrameSilero:
    """Fake Silero: a frame is speech when its RMS is above 0.1."""

    def __init__(self) -> None:
        self.frames = 0

    def run(self, _outputs: object, feeds: dict[str, np.ndarray]) -> list[np.ndarray]:
        self.frames += 1
        frame = feeds["input"][0, 64:]
        prob = 0.9 if float(np.sqrt(np.mean(frame * frame))) > 0.1 else 0.05
        return [np.array([[prob]], dtype=np.float32), feeds["state"]]


class _AlternatingEmbedding:
    """Fake speaker model: calls alternate between two orthogonal voices."""

    def __init__(self) -> None:
        self.inputs: list[np.ndarray] = []

    def get_inputs(self) -> list[object]:
        return [type("Input", (), {"name": "x"})()]

    def run(self, _outputs: object, feeds: dict[str, np.ndarray]) -> list[np.ndarray]:
        vec = np.zeros((1, 512), dtype=np.float32)
        vec[0, len(self.inputs) % 2] = 1.0
        self.inputs.append(feeds["x"])
        return [vec]


def test_installed_onnx_models_replace_the_energy_vad_and_24_dim_embeddings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sr = 16_000
    rng = np.random.default_rng(9)
    # Four alike cues: 0.5 s hum (speech to the energy VAD, not to Silero),
    # 1 s of loud speech, 0.5 s silence; 0.5 s gaps between cues.
    cue_audio = np.concatenate(
        [
            0.05 * rng.standard_normal(sr // 2),
            0.2 * rng.standard_normal(sr),
            np.zeros(sr // 2),
            np.zeros(sr // 2),
        ]
    ).astype(np.float32)
    wav = write_wav_file(np.tile(cue_audio, 4), tmp_path / "source_16k.wav", sr)
    cues = [Cue(i + 1, i * 2500, i * 2500 + 2000, f"Câu {i + 1}.") for i in range(4)]
    plain = tmp_path / "plain"
    models = tmp_path / "models_home"
    _install_audio_model(models, SILERO_VAD_MODEL_ID)
    _install_audio_model(models, SPEAKER_EMBEDDING_MODEL_ID)
    silero, speaker = _LoudFrameSilero(), _AlternatingEmbedding()
    silero_name = AUDIO_MODELS[SILERO_VAD_MODEL_ID].filename
    monkeypatch.setattr(
        onnx_audio,
        "default_session_factory",
        lambda path, _threads: silero if path.name == silero_name else speaker,
    )

    energy = refine_cues_speech_bounds(cues, wav, data_dir=plain)
    assert [c.speech_start_ms for c in energy] == [c.start_ms for c in cues]
    neural = refine_cues_speech_bounds(cues, wav, data_dir=models)
    for cue, refined in zip(cues, neural, strict=True):
        assert refined.speech_start_ms is not None and refined.speech_end_ms is not None
        assert 400 <= refined.speech_start_ms - cue.start_ms <= 500
        assert 1500 <= refined.speech_end_ms - cue.start_ms <= 1560
    assert silero.frames > 0

    # The acoustic fallback: 24-dim embeddings cannot tell these cues apart;
    # the installed model's embeddings (of Silero-trimmed speech) can.
    baseline = diarize_cues(wav, cues, DiarizationConfig(backend="acoustic"), data_dir=plain)
    assert {c.speaker for c in baseline} == {"SPEAKER_01"}
    assert speaker.inputs == []
    tagged = diarize_cues(wav, cues, DiarizationConfig(backend="acoustic"), data_dir=models)
    assert [c.speaker for c in tagged] == ["SPEAKER_01", "SPEAKER_02"] * 2
    assert len(speaker.inputs) == 4
    assert all(x.shape[1] < 150 for x in speaker.inputs)  # ~1 s of speech, not the 2 s cue

    # Reference-clip scoring reads its speech ratio from Silero too.
    before = silero.frames
    select_speaker_reference_clip(tagged, "SPEAKER_01", wav_path=wav, data_dir=models)
    assert silero.frames > before


def test_neural_labels_cluster_long_cues_and_attach_short_ones() -> None:
    rng = np.random.default_rng(4)

    def voice(*weights: float) -> np.ndarray:
        vec = np.zeros(8)
        vec[: len(weights)] = weights
        vec += 0.05 * rng.standard_normal(8)
        return (vec / np.linalg.norm(vec)).astype(np.float32)

    a, b = (1.0,), (0.0, 1.0)
    # A short "Vâng." embeds far from everyone (0.65 from A): clustered with
    # the long cues it became a third speaker. A cue with no audio (None)
    # sits next to a B cue in time.
    vectors = [voice(*a), voice(*b), voice(0.35, 0, 0, 0.94), voice(*a), None, voice(*b)]
    vectors.append(voice(*a))
    speech_ms = [3000, 3000, 600, 3000, 0, 3000, 3000]
    cues = [Cue(i + 1, i * 3000, i * 3000 + 2500, f"c{i}") for i in range(7)]
    cues[4] = Cue(5, 13900, 14400, "c4")

    for max_speakers in (0, 2):
        assert _neural_cue_labels(cues, vectors, speech_ms, max_speakers) == [0, 1, 0, 0, 1, 1, 0]
    # Too few long cues to anchor on: every cue with audio is clustered.
    assert _neural_cue_labels(cues[:3], vectors[:3], [600] * 3, 0) == [0, 1, 2]
