"""Unit tests for core/diarization.py: speaker clustering, RTTM alignment, and reference clips."""

from __future__ import annotations

from pathlib import Path

import numpy as np

from vienetts_app.core.audio import read_wav, write_wav_file
from vienetts_app.core.diarization import (
    DiarizationConfig,
    assign_speakers_from_turns,
    diarize_cues,
    extract_speaker_reference_wav,
    parse_audiocpp_turns,
    parse_rttm_turns,
    select_speaker_reference_clip,
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
        "#!/bin/sh\n"
        "while [ $# -gt 0 ]; do\n"
        '  if [ "$1" = "--turns-out" ]; then\n'
        "    shift\n"
        '    printf \'[{"start_sample":0,"end_sample":32000,'
        '"speaker_id":"speaker_0","confidence":1.0},'
        '{"start_sample":35200,"end_sample":67200,'
        '"speaker_id":"speaker_1","confidence":0.95}]\' > "$1"\n'
        "    exit 0\n"
        "  fi\n"
        "  shift\n"
        "done\n"
        "exit 1\n",
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
