"""Regression tests for diarization hardening: fallback warnings, cancel, timeout, cache.

These cover the failure modes that previously produced silent wrong output:
``auto`` swallowing the model error, a missing turns file being treated as a
crash, a hung child process blocking forever, 48 kHz audio producing 3x-off
timestamps, and every re-run re-shelling out to the CLI.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from vienetts_app.core.ai_models import DIARIZATION_GGUF_MODELS
from vienetts_app.core.audio import read_wav, write_wav_file
from vienetts_app.core.diarization import (
    DiarizationCancelled,
    DiarizationConfig,
    DiarizationError,
    RttmTurn,
    _CliProgress,
    apply_speaker_ceiling,
    diarize_cues,
    extract_speaker_reference_wav,
    remap_speakers_to_previous,
    select_speaker_reference_clip,
)
from vienetts_app.core.subtitles import Cue

TURNS_TWO_SPEAKERS = json.dumps(
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


def _install_fake_cli(tmp_path: Path, script: str, *, variant: str = "") -> Path:
    bin_dir = tmp_path / "runtimes" / "audiocpp" / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    cli = bin_dir / ("audiocpp_cli.exe" if __import__("os").name == "nt" else "audiocpp_cli")
    cli.write_text(script, encoding="utf-8")
    cli.chmod(0o755)
    if variant:
        (bin_dir.parent / "runtime.json").write_text(
            json.dumps({"variant": variant}), encoding="utf-8"
        )
    model_dir = tmp_path / "models" / "diarization"
    model_dir.mkdir(parents=True, exist_ok=True)
    # Match the catalog size: a truncated managed GGUF now reads as "not
    # installed" (sparse file, so this costs no disk space).
    spec = DIARIZATION_GGUF_MODELS["nemotron-3-diarization-q8_0"]
    with (model_dir / spec.filename).open("wb") as fh:
        fh.truncate(int(spec.size_bytes))
    return cli


def _turns_writer(turns: str) -> str:
    return (
        "#!/usr/bin/env python3\n"
        "import sys\n"
        "from pathlib import Path\n"
        "args = sys.argv[1:]\n"
        "out = args[args.index('--turns-out') + 1]\n"
        f"Path(out).write_text({turns!r}, encoding='utf-8')\n"
    )


def _cues() -> list[Cue]:
    return [Cue(1, 100, 1900, "Một."), Cue(2, 2300, 4100, "Hai.")]


@pytest.fixture()
def mono16(tmp_path: Path) -> Path:
    return write_wav_file(
        np.zeros(16_000 * 5, dtype=np.float32), tmp_path / "in16.wav", sample_rate=16_000
    )


def test_auto_records_fallback_reason_instead_of_hiding_it(tmp_path: Path, mono16: Path) -> None:
    notes: list[str] = []
    # No CLI/model installed -> primary cannot run, acoustic must take over.
    tagged = diarize_cues(
        mono16,
        _cues(),
        DiarizationConfig(backend="auto"),
        data_dir=tmp_path,
        notes=notes,
    )
    assert len(tagged) == 2
    assert any("Nemotron-3" in note for note in notes), notes
    assert any("dự phòng" in note for note in notes), notes


def test_auto_never_swallows_cancellation(tmp_path: Path, mono16: Path) -> None:
    with pytest.raises(DiarizationCancelled):
        diarize_cues(
            mono16,
            _cues(),
            DiarizationConfig(backend="auto"),
            data_dir=tmp_path,
            cancel_check=lambda: True,
        )


def test_missing_turns_file_means_no_speech_not_failure(tmp_path: Path) -> None:
    _install_fake_cli(tmp_path, "#!/usr/bin/env python3\nimport sys\nsys.exit(0)\n")
    wav = write_wav_file(
        np.zeros(16_000 * 5, dtype=np.float32), tmp_path / "silent.wav", sample_rate=16_000
    )
    notes: list[str] = []
    tagged = diarize_cues(
        wav,
        _cues(),
        DiarizationConfig(backend="nemotron_gguf", device="cpu"),
        data_dir=tmp_path,
        notes=notes,
    )
    assert [c.speaker for c in tagged] == ["SPEAKER_01", "SPEAKER_01"]
    assert any("Không phát hiện giọng nói" in note for note in notes), notes


def test_nemotron_progress_is_relayed_in_monotonic_steps(tmp_path: Path) -> None:
    # Parsed: percent, "[i/n]" and "chunk i of n" lines on both pipes, with
    # noise lines (and a lower, out-of-order value) that must not move the bar.
    _install_fake_cli(
        tmp_path,
        "#!/usr/bin/env python3\n"
        "import sys\n"
        "from pathlib import Path\n"
        "args = sys.argv[1:]\n"
        "print('loading model with 16 threads', flush=True)\n"
        "for i in range(1, 9):\n"
        "    print(f'diarize: {i * 5}%', flush=True)\n"
        "    print('frame 2024/10/29 noise', flush=True)\n"
        "for i in range(9, 15):\n"
        "    print(f'[{i}/20] window', file=sys.stderr, flush=True)\n"
        "print('diarize: 3%', flush=True)\n"
        "for i in range(15, 21):\n"
        "    print(f'chunk {i} of 20 done', flush=True)\n"
        "out = args[args.index('--turns-out') + 1]\n"
        f"Path(out).write_text({TURNS_TWO_SPEAKERS!r}, encoding='utf-8')\n",
    )
    wav = write_wav_file(
        np.zeros(16_000 * 5, dtype=np.float32), tmp_path / "prog.wav", sample_rate=16_000
    )
    seen: list[float] = []
    diarize_cues(
        wav,
        _cues(),
        DiarizationConfig(backend="nemotron_gguf", device="cpu"),
        data_dir=tmp_path,
        progress_cb=seen.append,
    )
    assert len(seen) >= 10, seen
    assert seen == sorted(seen), seen
    assert seen[-1] == 1.0
    assert max(seen[:-1]) <= 0.95, seen

    # A model that fails after reporting progress: the acoustic fallback
    # resumes from there instead of moving the bar back.
    _install_fake_cli(
        tmp_path,
        "#!/usr/bin/env python3\nimport sys\nprint('60%', flush=True)\nsys.exit(3)\n",
    )
    fallback: list[float] = []
    diarize_cues(
        wav,
        _cues(),
        DiarizationConfig(backend="auto", device="cpu"),
        data_dir=tmp_path,
        progress_cb=fallback.append,
    )
    assert fallback == sorted(fallback), fallback
    assert fallback[-1] == 1.0

    # Unparseable output: the estimate follows elapsed time against the
    # expected run time, read from the injected clock.
    now = [100.0]
    estimated: list[float] = []
    meter = _CliProgress(
        estimated.append, start=0.1, end=0.95, expected_s=20.0, clock=lambda: now[0]
    )
    meter.on_line("audiocpp: session ready\n")
    meter.tick()
    assert estimated == []
    for _ in range(30):
        now[0] += 1.0
        meter.tick()
    assert len(estimated) >= 10, estimated
    assert estimated == sorted(estimated), estimated
    assert estimated[0] == pytest.approx(0.1 + 0.85 / 20)
    assert estimated[-1] == pytest.approx(0.95)


def test_hung_child_is_killed_at_timeout(tmp_path: Path) -> None:
    _install_fake_cli(tmp_path, "#!/usr/bin/env python3\nimport time\ntime.sleep(30)\n")
    wav = write_wav_file(
        np.zeros(16_000 * 2, dtype=np.float32), tmp_path / "hang.wav", sample_rate=16_000
    )
    with pytest.raises(DiarizationError, match="quá thời gian"):
        diarize_cues(
            wav,
            _cues(),
            DiarizationConfig(backend="nemotron_gguf", device="cpu", timeout_s=0.7),
            data_dir=tmp_path,
        )


def test_48k_wav_is_downmixed_to_16k_and_a_legacy_cache_is_adopted(tmp_path: Path) -> None:
    counter = tmp_path / "calls.txt"
    cli_py = (
        "#!/usr/bin/env python3\n"
        "import json, sys, wave\n"
        f"with open({str(counter.as_posix())!r}, 'a', encoding='utf-8') as _fh:\n"
        "    _fh.write('x\\n')\n"
        "args = sys.argv[1:]\n"
        "out = args[args.index('--turns-out') + 1]\n"
        "audio = args[args.index('--audio') + 1]\n"
        "with wave.open(audio) as w:\n"
        "    if w.getframerate() != 16000 or w.getnchannels() != 1:\n"
        "        sys.stderr.write(f'bad input {w.getframerate()}Hz/{w.getnchannels()}ch\\n')\n"
        "        sys.exit(3)\n"
        "    total = w.getnframes()\n"
        "json.dump([{'start_sample': 0, 'end_sample': min(32000, total),"
        " 'speaker_id': 'speaker_0'}], open(out, 'w'))\n"
    )
    _install_fake_cli(tmp_path, cli_py)
    sr = 48_000
    wav = write_wav_file(
        np.zeros(sr * 3, dtype=np.float32), tmp_path / "movie48.wav", sample_rate=sr
    )
    work_dir = tmp_path / "work"
    cfg = DiarizationConfig(backend="nemotron_gguf", device="cpu")
    tagged = diarize_cues(wav, _cues(), cfg, data_dir=tmp_path, work_dir=work_dir)
    assert [c.speaker for c in tagged] == ["SPEAKER_01", "SPEAKER_01"]
    shared = work_dir / "source_16k.wav"
    assert shared.is_file() and not (work_dir / "source_wav_16k.wav").exists()

    # A pre-upgrade cache dir (diarization's own source_wav_16k.wav, its unversioned
    # sidecar and turns keyed on that path) is adopted without reconversion and still
    # serves the cached turns.
    stamp = shared.stat().st_mtime_ns
    sidecar = work_dir / "source_16k.wav.json"
    legacy_sidecar = json.loads(sidecar.read_text(encoding="utf-8"))
    legacy_sidecar.pop("format")
    (work_dir / "source_wav_16k.wav.json").write_text(json.dumps(legacy_sidecar), encoding="utf-8")
    sidecar.unlink()
    shared.rename(work_dir / "source_wav_16k.wav")
    turns_cache = work_dir / "diar_turns_cache.json"
    turns_cache.write_text(
        turns_cache.read_text(encoding="utf-8").replace("source_16k.wav", "source_wav_16k.wav"),
        encoding="utf-8",
    )
    again = diarize_cues(wav, _cues(), cfg, data_dir=tmp_path, work_dir=work_dir)
    assert [c.speaker for c in again] == ["SPEAKER_01", "SPEAKER_01"]
    assert counter.read_text(encoding="utf-8").count("x") == 1
    assert shared.stat().st_mtime_ns == stamp


def test_turns_are_cached_across_runs(tmp_path: Path) -> None:
    counter = tmp_path / "calls.txt"
    script = _turns_writer(TURNS_TWO_SPEAKERS).replace(
        "#!/usr/bin/env python3\n",
        "#!/usr/bin/env python3\n"
        f"with open({str(counter.as_posix())!r}, 'a', encoding='utf-8') as _fh:\n"
        "    _fh.write('x\\n')\n",
    )
    _install_fake_cli(tmp_path, script)
    wav = write_wav_file(
        np.zeros(16_000 * 5, dtype=np.float32), tmp_path / "cached.wav", sample_rate=16_000
    )
    work_dir = tmp_path / "work"
    cfg = DiarizationConfig(backend="nemotron_gguf", device="cpu")
    first = diarize_cues(wav, _cues(), cfg, data_dir=tmp_path, work_dir=work_dir)
    second = diarize_cues(wav, _cues(), cfg, data_dir=tmp_path, work_dir=work_dir)
    assert [c.speaker for c in first] == ["SPEAKER_01", "SPEAKER_02"]
    assert [c.speaker for c in second] == ["SPEAKER_01", "SPEAKER_02"]
    assert counter.read_text().count("x") == 1


def test_reference_clip_scoring_prefers_a_clean_in_range_clip(tmp_path: Path) -> None:
    sr = 16_000
    rng = np.random.default_rng(3)

    def speech(seconds: float) -> np.ndarray:
        return (0.1 * rng.standard_normal(int(sr * seconds))).astype(np.float32)

    def clipped(seconds: float) -> np.ndarray:
        clip = speech(seconds)
        hits = rng.choice(clip.size, clip.size // 20, replace=False)  # 5 %
        clip[hits] = np.where(clip[hits] >= 0, 1.0, -1.0)
        return clip

    def mix(ten_s: np.ndarray, name: str) -> Path:
        # 1 s gaps keep each clip its own candidate (runs only bridge <= 400 ms).
        gap = np.zeros(sr, dtype=np.float32)
        parts = [gap, speech(2.0), gap, speech(8.0), gap, ten_s, gap, speech(9.0), gap]
        return write_wav_file(np.concatenate(parts), tmp_path / name, sample_rate=sr)

    cues = [
        Cue(1, 1000, 3000, "Hai giây.", speaker="SPEAKER_01"),
        Cue(2, 4000, 12000, "Tám giây sạch.", speaker="SPEAKER_01"),
        Cue(3, 13000, 23000, "Mười giây bị méo.", speaker="SPEAKER_01"),
        Cue(4, 24000, 33000, "Chín giây có người chen.", speaker="SPEAKER_01"),
        Cue(5, 27000, 30000, "Người khác chen vào.", speaker="SPEAKER_02"),
    ]
    wav = mix(clipped(10.0), "clipped.wav")
    eight_s = (4000, 12000, "Tám giây sạch.")
    assert select_speaker_reference_clip(cues, "SPEAKER_01", wav_path=wav) == eight_s
    # Explicit model turns stand in for the other speaker's cue.
    turns = [RttmTurn(27000, 30000, "SPEAKER_02"), RttmTurn(4000, 12000, "SPEAKER_01")]
    assert select_speaker_reference_clip(cues[:4], "SPEAKER_01", wav_path=wav, turns=turns) == (
        eight_s
    )
    out, text = extract_speaker_reference_wav(wav, cues, "SPEAKER_01", tmp_path / "ref.wav")
    assert text == eight_s[2]
    assert read_wav(out)[0].size == 8 * sr

    # Duration breaks ties: a clean 10 s clip scores like the 8 s one, and wins.
    clean = mix(speech(10.0), "clean.wav")
    chosen = select_speaker_reference_clip(cues, "SPEAKER_01", wav_path=clean)
    assert chosen is not None and chosen[:2] == (13000, 23000)
    # Without audio (the GUI thread's cache key) the cue-only choice is unchanged.
    assert select_speaker_reference_clip(cues, "SPEAKER_01") == eight_s


def test_speaker_ceiling_merges_surplus_speakers() -> None:
    cues = [
        Cue(1, 0, 1000, "a", speaker="SPEAKER_01"),
        Cue(2, 1000, 2000, "b", speaker="SPEAKER_02"),
        Cue(3, 2000, 3000, "c", speaker="SPEAKER_03"),
        Cue(4, 3000, 4000, "d", speaker="SPEAKER_04"),
    ]
    capped = apply_speaker_ceiling(cues, 2)
    assert {c.speaker for c in capped} <= {"SPEAKER_01", "SPEAKER_02"}
    assert apply_speaker_ceiling(cues, 0) == cues
    assert {c.speaker for c in apply_speaker_ceiling(cues, 1)} == {"SPEAKER_01"}


def test_labels_are_remapped_to_previous_run() -> None:
    previous = [
        Cue(1, 0, 2000, "a", speaker="SPEAKER_01"),
        Cue(2, 2000, 4000, "b", speaker="SPEAKER_02"),
    ]
    updated = [
        Cue(1, 0, 2000, "a", speaker="SPEAKER_02"),
        Cue(2, 2000, 4000, "b", speaker="SPEAKER_01"),
    ]
    remapped = remap_speakers_to_previous(previous, updated)
    assert [c.speaker for c in remapped] == ["SPEAKER_01", "SPEAKER_02"]

    fresh = remap_speakers_to_previous(
        previous, [Cue(1, 10_000, 12_000, "c", speaker="SPEAKER_01")]
    )
    assert fresh[0].speaker not in {"SPEAKER_01", "SPEAKER_02"}


def _arg_logger(path: Path, turns: str) -> str:
    log_p = str(path.as_posix())
    head = (
        "#!/usr/bin/env python3\n"
        "import sys\n"
        "from pathlib import Path\n"
        f"Path({log_p!r}).write_text('\\n'.join(sys.argv[1:]) + '\\n', encoding='utf-8')\n"
    )
    return head + _turns_writer(turns).split("#!/usr/bin/env python3\n", 1)[1]


def test_cpu_only_runtime_forces_f32_even_on_auto(tmp_path: Path) -> None:
    args_file = tmp_path / "args.txt"
    _install_fake_cli(tmp_path, _arg_logger(args_file, TURNS_TWO_SPEAKERS))
    wav = write_wav_file(
        np.zeros(16_000 * 5, dtype=np.float32), tmp_path / "a.wav", sample_rate=16_000
    )
    diarize_cues(
        wav,
        _cues(),
        DiarizationConfig(backend="nemotron_gguf", device="auto"),
        data_dir=tmp_path,
    )
    args = args_file.read_text(encoding="utf-8")
    assert "best" in args.splitlines()
    assert "nemotron_3_diar.weight_type=f32" in args


def test_gpu_runtime_keeps_native_weight_type(tmp_path: Path) -> None:
    args_file = tmp_path / "args_gpu.txt"
    _install_fake_cli(tmp_path, _arg_logger(args_file, TURNS_TWO_SPEAKERS), variant="vulkan")
    wav = write_wav_file(
        np.zeros(16_000 * 5, dtype=np.float32), tmp_path / "b.wav", sample_rate=16_000
    )
    diarize_cues(
        wav,
        _cues(),
        DiarizationConfig(backend="nemotron_gguf", device="vulkan"),
        data_dir=tmp_path,
    )
    args = args_file.read_text(encoding="utf-8")
    assert "vulkan" in args.splitlines()
    assert "weight_type" not in args
