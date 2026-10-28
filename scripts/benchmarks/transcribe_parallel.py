"""Wall time of ASR then Nemotron diarization, sequential vs concurrent (Task 7.1 gate).

``transcribe_pipeline.run_transcription(parallel=True)`` runs whisper.cpp and the
audio.cpp Nemotron CLI side by side, each on half the thread budget. It ships
off by default and is enabled only if this benchmark shows a wall-time win of at
least 15 % with the real runtimes and models.

Only public entry points are used. The concurrent arm runs Nemotron through
``diarize_cues`` on one cue spanning the whole file while ASR runs, then labels the
real cues from the turn cache it left — the same work as a turn-only runner.

Every run gets a fresh work dir, so neither the ASR segment cache nor the turn
cache can help. The media is converted to 16 kHz once, outside the timing.

Nothing is downloaded: with a runtime or model missing, the script prints
``unmeasured: <reason>`` and exits with status 2. Example::

    python -m scripts.benchmarks.transcribe_parallel talk.mp4 --reps 3 \\
        --data-dir ~/Library/Application\\ Support/VieNeuTTSApp
"""

from __future__ import annotations

import argparse
import statistics
import sys
import tempfile
import threading
import time
import wave
from collections.abc import Callable
from pathlib import Path

from vienetts_app.core.ai_models import (
    resolve_audiocpp_cli,
    resolve_diarization_model_path,
    resolve_whisper_cpp_cli,
)
from vienetts_app.core.asr import (
    AsrBackendMissing,
    AsrConfig,
    default_asr_threads,
    resolve_whisper_model,
    transcribe_media_to_cues,
)
from vienetts_app.core.audio_prep import AudioPrepError, ensure_source_16k
from vienetts_app.core.diarization import DiarizationConfig, diarize_cues
from vienetts_app.core.subtitles import Cue

#: Minimum wall-time win for ``parallel=True`` to become the default.
WIN_THRESHOLD = 0.15
UNMEASURED_EXIT = 2


def missing_runtime(data_dir: Path, asr_model: str, diar_model: str) -> str | None:
    """Why the benchmark cannot run on this host, or ``None`` when it can."""
    if resolve_whisper_cpp_cli(data_dir) is None:
        return "whisper.cpp (whisper-cli) is not installed"
    if resolve_audiocpp_cli(data_dir) is None:
        return "the audio.cpp runtime is not installed"
    try:
        resolve_whisper_model(asr_model, data_dir)
    except AsrBackendMissing as exc:
        return f"no whisper.cpp model is installed ({exc})"
    if resolve_diarization_model_path(diar_model, data_dir) is None:
        return f"the diarization model {diar_model!r} is not installed"
    return None


def _duration_ms(wav: Path) -> int:
    with wave.open(str(wav), "rb") as handle:
        return int(handle.getnframes() * 1000 / max(1, handle.getframerate()))


def _run_sequential(
    wav: Path, asr_cfg: AsrConfig, diar_cfg: DiarizationConfig, work: Path, data_dir: Path
) -> list[Cue]:
    cues = transcribe_media_to_cues(wav, asr_cfg, work_dir=work, data_dir=data_dir)
    return diarize_cues(wav, cues, diar_cfg, work_dir=work, data_dir=data_dir)


def _run_concurrent(
    wav: Path, asr_cfg: AsrConfig, diar_cfg: DiarizationConfig, work: Path, data_dir: Path
) -> list[Cue]:
    whole = [Cue(1, 0, _duration_ms(wav), "")]
    failure: list[BaseException] = []

    def nemotron() -> None:
        try:
            diarize_cues(wav, whole, diar_cfg, work_dir=work, data_dir=data_dir)
        except BaseException as exc:  # noqa: BLE001 - re-raised on the main thread
            failure.append(exc)

    worker = threading.Thread(target=nemotron, name="bench-nemotron")
    worker.start()
    try:
        cues = transcribe_media_to_cues(wav, asr_cfg, work_dir=work, data_dir=data_dir)
    finally:
        worker.join()
    if failure:
        raise failure[0]
    # The turns are cached in ``work`` now, so this only labels the real cues.
    return diarize_cues(wav, cues, diar_cfg, work_dir=work, data_dir=data_dir)


def _time(
    run: Callable[[Path, AsrConfig, DiarizationConfig, Path, Path], list[Cue]],
    wav: Path,
    asr_cfg: AsrConfig,
    diar_cfg: DiarizationConfig,
    data_dir: Path,
) -> tuple[float, int]:
    with tempfile.TemporaryDirectory(prefix="bench_transcribe_") as tmp:
        start = time.perf_counter()
        cues = run(wav, asr_cfg, diar_cfg, Path(tmp), data_dir)
        return time.perf_counter() - start, len(cues)


def run(args: argparse.Namespace) -> int:
    if args.data_dir is not None:
        data_dir = Path(args.data_dir).expanduser()
    else:
        from vienetts_app.core.settings import default_data_dir

        data_dir = default_data_dir()
    diar_base = DiarizationConfig(backend="nemotron_gguf")
    reason = missing_runtime(data_dir, args.model, diar_base.model)
    if reason is None and args.media is None:
        reason = "no media file was given"
    if reason is not None:
        print(f"unmeasured: {reason}")
        return UNMEASURED_EXIT

    budget = args.threads if args.threads > 0 else default_asr_threads()
    half = max(1, budget // 2)
    with tempfile.TemporaryDirectory(prefix="bench_transcribe_prep_") as prep:
        try:
            wav = ensure_source_16k(Path(args.media), Path(prep), data_dir=data_dir)
        except AudioPrepError as exc:
            print(f"unmeasured: the media could not be converted to 16 kHz ({exc})")
            return UNMEASURED_EXIT
        # Sequential is today's flow: each runner on its own default threads.
        sequential_cfgs = (
            AsrConfig(backend="whisper_cpp", model=args.model, threads=budget),
            diar_base,
        )
        concurrent_cfgs = (
            AsrConfig(backend="whisper_cpp", model=args.model, threads=half),
            DiarizationConfig(backend="nemotron_gguf", threads=half),
        )
        print(f"media {args.media} ({_duration_ms(wav) / 1000:.1f} s), threads {budget}/{half}")
        seq_times: list[float] = []
        par_times: list[float] = []
        for rep in range(args.reps):
            seq_s, seq_n = _time(_run_sequential, wav, *sequential_cfgs, data_dir)
            par_s, par_n = _time(_run_concurrent, wav, *concurrent_cfgs, data_dir)
            seq_times.append(seq_s)
            par_times.append(par_s)
            print(
                f"rep {rep + 1}: sequential {seq_s:.2f} s ({seq_n} cues), "
                f"concurrent {par_s:.2f} s ({par_n} cues)"
            )

    seq = statistics.median(seq_times)
    par = statistics.median(par_times)
    win = 1.0 - par / seq if seq > 0 else 0.0
    print(f"median sequential {seq:.2f} s, concurrent {par:.2f} s, wall-time win {win:.1%}")
    if win >= WIN_THRESHOLD:
        print(f"verdict: enable parallel by default (win >= {WIN_THRESHOLD:.0%})")
    else:
        print(f"verdict: keep parallel=False (win < {WIN_THRESHOLD:.0%})")
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("media", nargs="?", help="video or audio file to transcribe")
    parser.add_argument("--data-dir", help="app data dir (default: the app's own)")
    parser.add_argument("--model", default="large-v3-turbo", help="whisper.cpp model id")
    parser.add_argument("--threads", type=int, default=0, help="thread budget (0 = automatic)")
    parser.add_argument("--reps", type=int, default=3, help="runs per arm (median reported)")
    return parser


def main(argv: list[str] | None = None) -> int:
    return run(_parser().parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
