"""Diarization error rate of the acoustic fallback: 24-dim baseline vs ONNX speaker models.

Builds multi-voice conversations from the app's own TTS preset voices with
known turns (FR-4.5), then labels the known cue spans the way
``diarize_cues(backend="acoustic")`` does and scores each system.

The cues are the reference segments, so missed and false-alarm speech are
zero by construction: the DER reported here is the speaker-confusion share of
speech time after the best one-to-one label mapping, i.e. exactly what the
cue-clustering step controls.

Fixtures are synthesised once and cached in ``--fixture-dir``. Example::

    python -m scripts.benchmarks.diarization_der \\
        --fixture-dir /tmp/der --models-dir ~/Library/Application\\ Support/VieNeuTTSApp \\
        --model wespeaker=/path/to/wespeaker_en_voxceleb_resnet34_LM.onnx
"""

from __future__ import annotations

import argparse
import itertools
import json
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from vienetts_app.core.ai_models import SPEAKER_EMBEDDING_MODEL_ID, audio_model_path
from vienetts_app.core.diarization import (
    NEURAL_STOP_DISTANCE,
    DiarizationConfig,
    _cluster_embeddings,
    _cue_acoustic_embedding,
    _neural_cue_labels,
    _neural_cue_vectors,
    _read_mono_ms,
    _run_acoustic_diarization,
)
from vienetts_app.core.onnx_audio import SpeakerEmbedder, installed_silero_vad
from vienetts_app.core.subtitles import Cue

SAMPLE_RATE = 16_000

SENTENCES = [
    "Vâng.",
    "Đúng rồi.",
    "Không, tôi không nghĩ vậy.",
    "Anh có chắc không?",
    "Hôm nay trời đẹp, chúng ta cùng nhau đi dạo quanh hồ nhé.",
    "Tôi vừa nhận được tin nhắn từ văn phòng, cuộc họp bị hoãn sang chiều mai.",
    "Chị ấy nói rằng mọi thứ đã sẵn sàng cho buổi biểu diễn tối nay.",
    "Thật ra thì tôi cũng chưa quyết định được nên chọn phương án nào.",
    "Nếu mình đi sớm thì sẽ tránh được kẹt xe ở cầu.",
    "Cảm ơn bạn rất nhiều vì đã giúp đỡ tôi trong thời gian qua.",
    "Món phở ở quán đầu ngõ ngon nhất khu này đấy.",
    "Để tôi kiểm tra lại lịch rồi báo cho anh sau nhé.",
    "Chúng ta cần thêm ít nhất hai người nữa mới hoàn thành kịp tiến độ.",
    "Ừ, được thôi.",
    "Sao lại thế được? Hôm qua mọi thứ vẫn bình thường mà.",
    "Bộ phim này kể về một gia đình nhỏ sống ở vùng biển miền Trung.",
    "Tôi nghĩ chúng ta nên bắt đầu lại từ đầu.",
    "Mai em có rảnh không, đi uống cà phê với anh nhé?",
    "Giá cả dạo này tăng nhanh quá, đi chợ mà chẳng mua được bao nhiêu.",
    "Thôi, muộn rồi, mọi người về nghỉ ngơi đi.",
]


@dataclass(frozen=True)
class Scenario:
    name: str
    voices: tuple[str, ...]  # "m0", "f1"...: the n-th male/female preset
    utterances: int


SCENARIOS = (
    Scenario("1spk", ("m0",), 16),
    Scenario("2spk-mf", ("m0", "f0"), 30),
    Scenario("2spk-mm", ("m0", "m1"), 30),
    Scenario("3spk", ("m0", "m1", "f0"), 40),
    Scenario("4spk", ("m0", "m1", "f0", "f1"), 48),
    Scenario("3spk-fff", ("f2", "f3", "f4"), 36),
    Scenario("5spk", ("m2", "m3", "m4", "f5", "f6"), 50),
)


def _pick_voices(spec: tuple[str, ...]) -> list[str]:
    from vienetts_app.core.engine import preset_voices

    by_gender: dict[str, list[str]] = {"m": [], "f": []}
    for voice in preset_voices():
        gender = voice["gender"].strip().lower()
        key = "m" if gender.startswith(("m", "nam")) else "f"
        by_gender[key].append(voice["name"])
    return [by_gender[item[0]][int(item[1:])] for item in spec]


def _synthesise(
    scenario: Scenario, out_dir: Path, engine, seed: int
) -> tuple[Path, list[Cue], list[str]]:
    """Write ``source_16k.wav`` and ``truth.json`` for ``scenario``."""
    import soundfile as sf
    import soxr

    voices = _pick_voices(scenario.voices)
    rng = np.random.default_rng(seed)
    pieces: list[np.ndarray] = []
    cues: list[Cue] = []
    pos = int(rng.integers(3_000, 8_000))
    pieces.append(np.zeros(pos, np.float32))
    speaker = int(rng.integers(len(voices)))
    for i in range(scenario.utterances):
        if i and len(voices) > 1 and rng.random() < 0.75:
            speaker = int(rng.choice([k for k in range(len(voices)) if k != speaker]))
        text = SENTENCES[int(rng.integers(len(SENTENCES)))]
        wav = np.asarray(engine.infer(text, voice=voices[speaker]), dtype=np.float32).reshape(-1)
        wav = soxr.resample(wav, int(engine.sample_rate), SAMPLE_RATE).astype(np.float32)
        # Random level per utterance, so loudness is never a speaker cue.
        rms = float(np.sqrt(np.mean(wav * wav))) or 1.0
        wav *= np.float32(10.0 ** (rng.uniform(-26.0, -18.0) / 20.0) / rms)
        start_ms = pos * 1000 // SAMPLE_RATE
        pieces.append(wav)
        pos += wav.size
        end_ms = pos * 1000 // SAMPLE_RATE
        cues.append(Cue(i + 1, start_ms, end_ms, text, speaker=f"S{speaker}"))
        gap = int(rng.integers(150, 900) * SAMPLE_RATE // 1000)
        pieces.append(np.zeros(gap, np.float32))
        pos += gap
    audio = np.concatenate(pieces)
    audio += np.float32(1e-3) * rng.standard_normal(audio.size).astype(np.float32)
    out_dir.mkdir(parents=True, exist_ok=True)
    wav_path = out_dir / "source_16k.wav"
    sf.write(str(wav_path), np.clip(audio, -1.0, 1.0), SAMPLE_RATE, subtype="PCM_16")
    truth = [
        {"start_ms": c.start_ms, "end_ms": c.end_ms, "text": c.text, "speaker": c.speaker}
        for c in cues
    ]
    (out_dir / "truth.json").write_text(
        json.dumps({"voices": voices, "cues": truth}, ensure_ascii=False, indent=1),
        encoding="utf-8",
    )
    return wav_path, cues, voices


def _load(out_dir: Path) -> tuple[Path, list[Cue], list[str]] | None:
    truth_path = out_dir / "truth.json"
    if not truth_path.is_file() or not (out_dir / "source_16k.wav").is_file():
        return None
    data = json.loads(truth_path.read_text(encoding="utf-8"))
    cues = [
        Cue(i + 1, c["start_ms"], c["end_ms"], c["text"], speaker=c["speaker"])
        for i, c in enumerate(data["cues"])
    ]
    return out_dir / "source_16k.wav", cues, list(data["voices"])


def confusion_der(cues: list[Cue], labels: list[int]) -> float:
    """Speaker-confusion share of speech time after the best one-to-one mapping."""
    refs = sorted({c.speaker for c in cues})
    hyps = sorted(set(labels))
    weight = np.zeros((len(refs), len(hyps)))
    for cue, lbl in zip(cues, labels, strict=True):
        weight[refs.index(cue.speaker), hyps.index(lbl)] += cue.end_ms - cue.start_ms
    total = float(weight.sum())
    best = 0.0
    if len(hyps) >= len(refs):
        for perm in itertools.permutations(range(len(hyps)), len(refs)):
            best = max(best, sum(weight[r, h] for r, h in enumerate(perm)))
    else:
        for perm in itertools.permutations(range(len(refs)), len(hyps)):
            best = max(best, sum(weight[r, h] for h, r in enumerate(perm)))
    return 1.0 - best / total if total else 0.0


def _baseline_labels(wav_path: Path, cues: list[Cue], cap: int) -> list[int]:
    import soundfile as sf

    with sf.SoundFile(str(wav_path)) as wav_file:
        sr = int(wav_file.samplerate)
        emb = [
            _cue_acoustic_embedding(_read_mono_ms(wav_file, c.start_ms, c.end_ms), sr) for c in cues
        ]
    return _cluster_embeddings(np.vstack(emb), max_speakers=cap)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--fixture-dir", type=Path, required=True)
    parser.add_argument(
        "--models-dir",
        type=Path,
        default=None,
        help="app data dir holding the catalog models (CAM++ and Silero VAD)",
    )
    parser.add_argument(
        "--model",
        action="append",
        default=[],
        metavar="NAME=PATH",
        help="extra speaker-embedding ONNX model to compare (repeatable)",
    )
    parser.add_argument(
        "--stop-distances",
        default="0.4,0.45,0.5,0.55,0.6,0.65,0.7",
        help="neural AHC thresholds to sweep",
    )
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--output", type=Path, default=None, help="JSON results file")
    return parser


def run(args: argparse.Namespace) -> int:
    models: dict[str, Path] = {}
    campplus = audio_model_path(SPEAKER_EMBEDDING_MODEL_ID, args.models_dir)
    if campplus is not None:
        models["campplus"] = campplus
    for item in args.model:
        name, _, path = item.partition("=")
        models[name] = Path(path)
    vad = installed_silero_vad(args.models_dir)
    stops = [float(x) for x in args.stop_distances.split(",") if x.strip()]

    engine = None
    fixtures = []
    for k, scenario in enumerate(SCENARIOS):
        out_dir = args.fixture_dir / scenario.name
        loaded = _load(out_dir)
        if loaded is None:
            if engine is None:
                from vienetts_app.core.engine import TTSEngine
                from vienetts_app.core.model_manager import ModelManager
                from vienetts_app.core.settings import default_data_dir

                location = ModelManager(default_data_dir() / "models").inspect().location
                if location is None:
                    print("The app's TTS model is not installed: no fixture.", file=sys.stderr)
                    return 2
                engine = TTSEngine(managed_model=location)
            started = time.perf_counter()
            loaded = _synthesise(scenario, out_dir, engine, args.seed + k)
            print(f"synthesised {scenario.name} in {time.perf_counter() - started:.1f}s")
        fixtures.append((scenario, *loaded))
    if engine is not None:
        engine.close()

    results: list[dict[str, object]] = []

    def record(system: str, scenario: Scenario, cues: list[Cue], cap: int, labels: list[int]):
        der = confusion_der(cues, labels)
        results.append(
            {
                "system": system,
                "scenario": scenario.name,
                "max_speakers": cap,
                "true_speakers": len(scenario.voices),
                "found_speakers": len(set(labels)),
                "der": round(der, 4),
            }
        )

    for scenario, wav_path, cues, voices in fixtures:
        print(f"{scenario.name}: {len(cues)} cues, voices {voices}")
        caps = (0, len(scenario.voices))
        for cap in caps:
            record("baseline-24d", scenario, cues, cap, _baseline_labels(wav_path, cues, cap))
        for name, path in models.items():
            embedder = SpeakerEmbedder(path)
            started = time.perf_counter()
            vectors, speech_ms = _neural_cue_vectors(wav_path, cues, embedder, vad)
            per_cue_ms = (time.perf_counter() - started) * 1000 / max(1, len(cues))
            print(f"  {name}: {per_cue_ms:.1f} ms/cue (Silero {'on' if vad else 'off'})")
            for cap in caps:
                labels = _neural_cue_labels(cues, vectors, speech_ms, cap, stop_distance=None)
                record(f"{name}-silhouette", scenario, cues, cap, labels)
                for stop in stops:
                    labels = _neural_cue_labels(cues, vectors, speech_ms, cap, stop_distance=stop)
                    record(f"{name}-stop{stop:g}", scenario, cues, cap, labels)
        if "campplus" in models:
            # The wired app path must agree with the swept one at the default threshold.
            for cap in caps:
                tagged = _run_acoustic_diarization(
                    wav_path, cues, DiarizationConfig(max_speakers=cap), data_dir=args.models_dir
                )
                order: dict[str, int] = {}
                labels = [order.setdefault(c.speaker, len(order)) for c in tagged]
                record(f"app-campplus-stop{NEURAL_STOP_DISTANCE:g}", scenario, cues, cap, labels)

    systems = sorted({str(r["system"]) for r in results})
    names = [s.name for s, *_ in fixtures]
    for cap_kind in ("auto", "true"):
        print(f"\nDER (max_speakers={'0' if cap_kind == 'auto' else 'true count'}):")
        print(f"{'system':28s} " + " ".join(f"{n:>9s}" for n in names) + "      mean")
        for system in systems:
            row = [
                r
                for r in results
                if r["system"] == system and (r["max_speakers"] == 0) == (cap_kind == "auto")
            ]
            if not row:
                continue
            by_name = {r["scenario"]: r for r in row}
            cells = [
                f"{by_name[n]['der']:6.3f}/{by_name[n]['found_speakers']}" if n in by_name else "-"
                for n in names
            ]
            mean = float(np.mean([float(r["der"]) for r in row]))
            print(f"{system:28s} " + " ".join(f"{c:>9s}" for c in cells) + f"  {mean:8.3f}")
    if args.output is not None:
        args.output.write_text(json.dumps(results, indent=1), encoding="utf-8")
    return 0


def main(argv: list[str] | None = None) -> int:
    return run(_parser().parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
