#!/usr/bin/env python
"""Qwen **GGUF** (qwentts.cpp) real-model release validation (Task 6.2).

OPT-IN, never part of the ordinary suite: this runs on a machine (or a release
runner) that already holds the verified packs — the native runtime pack of one
platform cell and the GGUF model tree — and it downloads nothing. Ordinary CI
proves the same flows with the deterministic fake host (tests/smoke).

What it proves per cell and variant, through the app's OWN host subprocess:

* the packs install through the app's managed installers (offline import,
  checksum-verified against the shipped manifests, so a tampered pack fails
  before anything loads) and the promoted identities match the pins exactly;
* the requested backend is the one the host reports — a backend the pack does
  not ship is refused with a structured error, never a silent CPU fallback;
* one bounded segment synthesizes real audio at the app's 48 kHz rate, checked
  by ``scripts/check_smoke_wav.py`` AND for finite samples (a native buffer
  bug can mint a constant-NaN stream that the silence gate would pass);
* a segment over the protocol limit is refused before it reaches the host;
* a second job streams on the same resident host (repeated jobs);
* cancellation settles the job and the next job still works;
* a killed host is restarted lazily by the next job (crash recovery);
* shutdown leaves no child behind.

It records TTFR, total time, RTF, peak memory, cancellation latency, restart
recovery, the requested/reported backend and the three content identities as
ONE JSON object on stdout; diagnostics go to stderr.

Usage (repository root, inside the APP environment):

    python scripts/qwen_gguf_release_smoke.py \
      --profile customvoice --quantization Q8_0 --device cpu --speaker Ryan \
      --packs build/qwen-gguf-packs --out build/qwen-gguf-smoke \
      --json-out build/qwen-gguf-smoke/linux-x64-cpu-customvoice-Q8_0.json

    python scripts/qwen_gguf_release_smoke.py \
      --profile base --quantization Q4_K_M --device metal \
      --ref-audio build/spike/ref.wav --ref-text "Xin chào" ...

``--packs`` holds ``runtime/`` (the cell's pack directory or archive) and
``models/`` (the model root tree: ``<variant-key>/`` dirs plus ``shared/``).
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import struct
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

if __package__ in (None, ""):  # `python scripts/qwen_gguf_release_smoke.py`
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts import check_smoke_wav  # noqa: E402 - after the sys.path bootstrap
from scripts.qwen_release_smoke import (  # noqa: E402 - shared runner helpers
    ReleaseSmokeError,
    ReleaseSmokeUsageError,
    capabilities_report,
    check_cancellation,
    check_restart,
    host_pid,
    platform_report,
    process_alive,
    sample_rss_bytes,
    stream_segment,
)

SCHEMA_VERSION = 1
KIND = "qwen-gguf-release-smoke"
APP_SAMPLE_RATE = 48_000
PROFILES = ("customvoice", "base")
QUANTIZATIONS = ("Q8_0", "Q4_K_M")
#: The native vocabulary — `auto` is a UI selection and `mps` is the PyTorch
#: host's spelling; neither is evidence of a device that ran.
DEVICES = ("cpu", "cuda", "metal")
DEFAULT_TIMEOUT_SECONDS = 900.0
DEFAULT_CANCEL_AFTER_MS = 500
DEFAULT_TEXT = "Xin chào, đây là bài kiểm tra phát hành của Qwen."
SHORT_TEXT = "Xin chào."
DEFAULT_SPEAKER = "Ryan"

PROFILE_KEYS = {"customvoice": "qwen_custom_0_6b", "base": "qwen_base_0_6b"}

#: The sections a report must carry before it may exit 0 — an unrun or
#: truncated report must never read as success. The lifecycle probes are
#: always present (``run`` records a skip marker when a flag disables them),
#: so a hand-built report that omits them is still refused here.
REQUIRED_SECTIONS = (
    "install",
    "identities",
    "deviceEvidence",
    "synthesis",
    "wav",
    "segmentation",
    "backendRefusal",
    "repeatedJob",
    "cancellation",
    "restart",
    "shutdown",
)


@dataclass(frozen=True)
class GgufSmokeRequest:
    """One validated validation run — one cell, one profile, one quantization."""

    profile: str
    quantization: str
    device: str
    packs: Path
    data_dir: Path
    out_dir: Path
    runtime_pack: Path
    model_pack: Path
    text: str = DEFAULT_TEXT
    language: str = ""
    speaker: str = DEFAULT_SPEAKER
    ref_audio: str = ""
    ref_text: str = ""
    timeout: float = DEFAULT_TIMEOUT_SECONDS
    min_seconds: float = 0.5
    cancel_after_ms: int = DEFAULT_CANCEL_AFTER_MS
    check_cancel: bool = True
    check_restart: bool = True
    json_out: str = ""

    @property
    def engine_profile(self) -> str:
        return PROFILE_KEYS[self.profile]

    @property
    def wav_path(self) -> Path:
        return self.out_dir / f"qwen-gguf-{self.profile}-{self.quantization}-{self.device}.wav"


@dataclass(frozen=True)
class GgufPackInstall:
    """Where the verified packs landed, exactly as the controller would find them."""

    cell: str
    runtime_dir: Path
    runtime_identity: str
    backends: tuple[str, ...]
    talker_path: Path
    codec_path: Path
    model_identity: str
    tokenizer_identity: str
    runtime_bytes: int = 0
    model_bytes: int = 0


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="qwen_gguf_release_smoke",
        description="Validate a pinned qwentts.cpp runtime + GGUF model through the app's host.",
    )
    parser.add_argument(
        "--profile",
        required=True,
        help=f"Model profile to validate: {', '.join(PROFILES)} (validated, not argparse-limited, "
        "so usage errors keep the one-JSON-result contract).",
    )
    parser.add_argument(
        "--quantization",
        default="",
        help=f"GGUF variant to validate: {', '.join(QUANTIZATIONS)} — required in "
        "validate_request so an omitted one names the choices instead of exiting "
        "with argparse's bare usage line.",
    )
    parser.add_argument(
        "--packs",
        default="build/qwen-gguf-packs",
        help="Root holding runtime/ (the cell pack) and models/ (the model root tree).",
    )
    parser.add_argument("--runtime-pack", default="", help="Override the runtime pack path.")
    parser.add_argument("--model-pack", default="", help="Override the model root tree.")
    parser.add_argument(
        "--device",
        default="cpu",
        help=f"Native device to prove: {', '.join(DEVICES)} (no 'auto', no 'mps').",
    )
    parser.add_argument("--text", default=DEFAULT_TEXT)
    parser.add_argument(
        "--language", default="", help="App language code (zh, en, …); default: the profile's own."
    )
    parser.add_argument("--speaker", default=DEFAULT_SPEAKER, help="CustomVoice fixed speaker.")
    parser.add_argument("--ref-audio", default="", help="Base reference clip (required for base).")
    parser.add_argument("--ref-text", default="", help="Base reference transcript (required).")
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT_SECONDS,
        help="Per-frame/load budget in seconds; a CPU generation may exceed the app default.",
    )
    parser.add_argument("--min-seconds", type=float, default=0.5, help="Minimum audio duration.")
    parser.add_argument("--cancel-after-ms", type=int, default=DEFAULT_CANCEL_AFTER_MS)
    parser.add_argument(
        "--no-check-cancel", dest="check_cancel", action="store_false", help="Skip cancellation."
    )
    parser.add_argument(
        "--no-check-restart", dest="check_restart", action="store_false", help="Skip host restart."
    )
    parser.add_argument(
        "--data-dir",
        default="",
        help="App data dir the packs are installed into (default: <out>/data, a fresh one).",
    )
    parser.add_argument("--out", default="build/qwen-gguf-smoke", help="Output dir for the WAV.")
    parser.add_argument("--json-out", default="", help="Also write the JSON result to this path.")
    return parser.parse_args(argv)


def validate_request(args: argparse.Namespace) -> GgufSmokeRequest:
    """Validate CLI input before anything heavy is imported or loaded."""
    if args.profile not in PROFILES:
        raise ReleaseSmokeUsageError(
            f"unknown profile {args.profile!r} — expected one of: {', '.join(PROFILES)}"
        )
    if not args.quantization:
        raise ReleaseSmokeUsageError(
            f"--quantization is required — expected one of: {', '.join(QUANTIZATIONS)}"
        )
    if args.quantization not in QUANTIZATIONS:
        raise ReleaseSmokeUsageError(
            f"unknown quantization {args.quantization!r} — "
            f"expected one of: {', '.join(QUANTIZATIONS)}"
        )
    if args.device not in DEVICES:
        raise ReleaseSmokeUsageError(
            f"unknown device {args.device!r} — the native host speaks: {', '.join(DEVICES)} "
            "(the app's 'auto'/'mps' spellings are selection vocabulary, not evidence)"
        )
    packs = Path(args.packs)
    runtime_pack = Path(args.runtime_pack) if args.runtime_pack else packs / "runtime"
    model_pack = Path(args.model_pack) if args.model_pack else packs / "models"
    if not runtime_pack.exists():
        raise ReleaseSmokeUsageError(f"runtime pack {runtime_pack} does not exist")
    if not model_pack.is_dir():
        raise ReleaseSmokeUsageError(f"model pack {model_pack} is not a directory")
    if args.profile == "base":
        if not args.ref_audio.strip() or not Path(args.ref_audio).is_file():
            raise ReleaseSmokeUsageError(
                "--ref-audio is required for base (a 3-8 s reference clip)"
            )
        if not args.ref_text.strip():
            raise ReleaseSmokeUsageError("--ref-text is required for base (the clip's transcript)")
    elif not args.speaker.strip():
        raise ReleaseSmokeUsageError("--speaker is required for customvoice (a fixed speaker)")
    if args.language.strip():
        from vienetts_app.core import engine_profiles

        try:
            engine_profiles.language_model_name(
                engine_profiles.get_capabilities(PROFILE_KEYS[args.profile]), args.language.strip()
            )
        except engine_profiles.EngineProfileError as exc:
            raise ReleaseSmokeUsageError(str(exc)) from exc
    if args.timeout <= 0:
        raise ReleaseSmokeUsageError("--timeout must be positive")
    if args.cancel_after_ms < 0:
        raise ReleaseSmokeUsageError("--cancel-after-ms must not be negative")
    out_dir = Path(args.out)
    data_dir = Path(args.data_dir) if args.data_dir else out_dir / "data"
    return GgufSmokeRequest(
        profile=args.profile,
        quantization=args.quantization,
        device=args.device,
        packs=packs,
        data_dir=data_dir,
        out_dir=out_dir,
        runtime_pack=runtime_pack,
        model_pack=model_pack,
        text=args.text,
        language=args.language,
        speaker=args.speaker.strip(),
        ref_audio=args.ref_audio,
        ref_text=args.ref_text,
        timeout=float(args.timeout),
        min_seconds=float(args.min_seconds),
        cancel_after_ms=int(args.cancel_after_ms),
        check_cancel=bool(args.check_cancel),
        check_restart=bool(args.check_restart),
        json_out=args.json_out,
    )


# --------------------------------------------------------------------------- #
# packs -> managed installs (the app's own installers, checksum-verified)
# --------------------------------------------------------------------------- #


def _runtime_manager_for(root: Path, pack: Any) -> Any:
    from vienetts_app.core.qwen_gguf_runtime import QwenGgufRuntimeManager

    return QwenGgufRuntimeManager(root, pack)


def _model_manager_for(root: Path, variant: Any) -> Any:
    from vienetts_app.core.qwen_gguf_models import QwenGgufModelManager

    return QwenGgufModelManager(root, variant)


def _model_pack_view(request: GgufSmokeRequest, recipe: Any) -> Path:
    """A per-variant view of the model root for ``install_offline``.

    The provisioned models tree holds every variant (``<key>/`` + ``shared/``);
    the manager's offline import rightly rejects a source carrying another
    variant's files, so the runner stages a hardlinked (copied fallback) view
    holding exactly this variant's talker and the shared codec.
    """
    view = request.data_dir / ".pack-view" / recipe.key
    talker_src = request.model_pack / recipe.key / recipe.talker.path
    codec_src = request.model_pack / "shared" / recipe.tokenizer.path
    targets = {
        view / recipe.key / recipe.talker.path: talker_src,
        view / "shared" / recipe.tokenizer.path: codec_src,
    }
    for target, source in targets.items():
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            continue
        try:
            os.link(source, target)
        except OSError:
            shutil.copyfile(source, target)
    return view


def install_packs(request: GgufSmokeRequest) -> GgufPackInstall:
    """Install both packs offline into the app data dir; raise on any mismatch.

    Nothing here reaches the network: ``install_from_offline_pack`` /
    ``install_offline`` verify every byte against the pinned manifests before
    staging, so the release run proves the SAME artifacts a user would import.
    The promoted identities are then double-checked against the pins — a pack
    whose checksums were satisfied but whose identity drifted can never mint
    evidence for a cell or variant it is not.
    """
    from vienetts_app.core import qwen_gguf_model_manifest, qwen_gguf_runtime_manifest
    from vienetts_app.core.qwen_variants import variant_for

    cell = qwen_gguf_runtime_manifest.host_cell_key(request.device)
    if cell is None:
        raise ReleaseSmokeError(
            f"this host has no managed qwentts.cpp cell for device {request.device!r} "
            "— pick another --device or platform"
        )
    pack = qwen_gguf_runtime_manifest.manifest_for_cell(cell)
    if pack is None:
        raise ReleaseSmokeError(
            f"cell {cell} has no published pack in the shipped manifest — "
            "the cell is blocked, not passing: build and pin its pack first"
        )
    runtime_manager = _runtime_manager_for(request.data_dir / "qwen-gguf" / "runtime", pack)
    runtime_status = runtime_manager.install_from_offline_pack(request.runtime_pack)
    if runtime_status.state != "ready" or runtime_status.location is None:
        raise ReleaseSmokeError(
            f"the runtime pack did not install ({runtime_status.state}): "
            f"{runtime_status.error or 'no location'}"
        )
    location = runtime_status.location
    if location.cell != cell:
        raise ReleaseSmokeError(
            f"runtime identity mismatch: installed cell {location.cell!r} != requested {cell!r}"
        )
    if location.runtime_identity != pack.identity:
        raise ReleaseSmokeError(
            "runtime identity mismatch: the promoted pack reports "
            f"{location.runtime_identity!r}, the manifest pins {pack.identity!r}"
        )

    variant = variant_for(request.engine_profile, "gguf", request.quantization)
    recipe = qwen_gguf_model_manifest.recipe_for_variant(variant)
    if recipe is None:
        raise ReleaseSmokeError(
            f"no verified GGUF model recipe for {request.profile}/{request.quantization}"
        )
    model_manager = _model_manager_for(request.data_dir / "qwen-gguf" / "models", variant)
    model_status = model_manager.install_offline(_model_pack_view(request, recipe))
    if model_status.state != "ready" or model_status.location is None:
        raise ReleaseSmokeError(
            f"the model pack did not install ({model_status.state}): "
            f"{model_status.error or 'no location'}"
        )
    model_location = model_status.location
    if model_location.variant_key != recipe.key:
        raise ReleaseSmokeError(
            f"model identity mismatch: installed variant {model_location.variant_key!r} "
            f"!= requested {recipe.key!r}"
        )
    if model_location.model_identity != recipe.model_identity:
        raise ReleaseSmokeError(
            "model identity mismatch: the promoted talker reports "
            f"{model_location.model_identity!r}, the manifest pins {recipe.model_identity!r}"
        )
    return GgufPackInstall(
        cell=cell,
        runtime_dir=Path(location.root),
        runtime_identity=location.runtime_identity,
        backends=tuple(location.backends),
        talker_path=Path(model_location.talker_path),
        codec_path=Path(model_location.tokenizer_path),
        model_identity=model_location.model_identity,
        tokenizer_identity=recipe.tokenizer_identity,
        runtime_bytes=runtime_status.installed_bytes,
        model_bytes=model_status.installed_bytes,
    )


# --------------------------------------------------------------------------- #
# engine construction and the GGUF-specific probes
# --------------------------------------------------------------------------- #


def build_engine(
    request: GgufSmokeRequest,
    install: GgufPackInstall,
    *,
    device: str | None = None,
    environment: dict[str, str] | None = None,
) -> Any:
    """The app's own engine object, pointed at the verified installs."""
    from vienetts_app.core.qwen_gguf_engine import QwenGgufEngine

    return QwenGgufEngine(
        request.engine_profile,
        runtime_dir=install.runtime_dir,
        talker_path=install.talker_path,
        codec_path=install.codec_path,
        quantization=request.quantization,
        device=device or request.device,
        environment=environment,
        load_timeout=request.timeout,
        frame_timeout=request.timeout,
        shutdown_timeout=30.0,
    )


def audio_problems(audio: np.ndarray) -> list[str]:
    """Findings on the collected samples — the gates a WAV checker cannot see."""
    if not audio.size:
        return ["the host produced no audio"]
    if not np.isfinite(audio).all():
        return ["the host produced non-finite audio (NaN/Inf in the stream)"]
    return []


def device_evidence(engine: Any) -> dict[str, Any]:
    """The backend the host actually reported in its ``loaded`` log line."""
    for line in reversed(str(engine.stderr_tail()).splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if event.get("event") == "loaded":
            return {
                "device": str(event.get("device", "")),
                "backend": str(event.get("backend", "")),
                "modelType": str(event.get("modelType", "")),
                "libraryVersion": str(event.get("libraryVersion", "")),
            }
    return {}


def check_segment_limit(engine: Any, request: GgufSmokeRequest) -> dict[str, Any]:
    """A segment over the protocol limit must be refused BEFORE it reaches IPC —
    long input is segmented by the caller, never pushed whole into the host."""
    from vienetts_app.core.qwen_engine import MAX_TEXT_CHARS, QwenEngineError

    report: dict[str, Any] = {"limit": MAX_TEXT_CHARS, "refused": False}
    try:
        stream = engine.infer_stream(
            "x" * (MAX_TEXT_CHARS + 1),
            language=request.language or "zh",
            speaker=request.speaker,
            job_id="release-smoke-oversize",
        )
        for _chunk in stream:  # pragma: no cover - a non-refusal is the finding
            pass
    except QwenEngineError as exc:
        report["refused"] = True
        report["error"] = str(exc)
    except Exception as exc:  # noqa: BLE001 — a non-contract error is a finding too
        report["error"] = f"{type(exc).__name__}: {exc}"
    return report


def check_backend_refusal(
    request: GgufSmokeRequest,
    install: GgufPackInstall,
    *,
    engine_factory: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """Two refusal layers, both required:

    * the engine constructor refuses non-native spellings (``mps``) before any
      spawn — the native host speaks cpu/cuda/metal only;
    * a backend the pack does not ship must fail the load with a structured,
      non-fatal error — never a silent fallback to a different backend.
    """
    from vienetts_app.core.qwen_engine import QwenEngineError
    from vienetts_app.workers.qwen_gguf_host import GGUF_DEVICE_BACKENDS

    factory = engine_factory or build_engine
    spellings: list[dict[str, Any]] = []
    for spelling in ("mps", "tpu"):
        try:
            factory(request, install, device=spelling)
        except QwenEngineError as exc:
            spellings.append({"device": spelling, "refused": True, "error": str(exc)})
        except Exception as exc:  # noqa: BLE001 — constructor contract is QwenEngineError
            spellings.append(
                {"device": spelling, "refused": False, "error": f"{type(exc).__name__}: {exc}"}
            )
        else:
            spellings.append({"device": spelling, "refused": False, "error": "built anyway"})

    report: dict[str, Any] = {"spellings": spellings, "refused": False}
    wrong = next((d for d in DEVICES if GGUF_DEVICE_BACKENDS[d] not in install.backends), None)
    if wrong is None:  # pragma: no cover - packs never ship every backend
        report["skipped"] = "the pack ships every known backend"
        return report
    report["device"] = wrong
    engine = factory(request, install, device=wrong)
    try:
        engine.initialize()
    except QwenEngineError as exc:
        report["refused"] = True
        report["code"] = engine.last_error_code()
        report["error"] = str(exc)
    except Exception as exc:  # noqa: BLE001 — a crash IS the finding
        report["error"] = f"{type(exc).__name__}: {exc}"
    else:
        report["error"] = (
            f"the host accepted backend {GGUF_DEVICE_BACKENDS[wrong]!r} on a pack "
            f"that ships {install.backends}"
        )
    finally:
        engine.close()
    return report


def check_repeated_job(
    engine: Any,
    request: GgufSmokeRequest,
    *,
    pid: int = 0,
    rss_fn: Callable[[int], int | None] | None = None,
) -> dict[str, Any]:
    """A second job on the resident host: streaming reuse, not a fresh spawn."""
    audio, metrics = stream_segment(
        engine,
        replace(request, text=SHORT_TEXT),
        job_id="release-smoke-repeat",
        pid=pid,
        rss_fn=rss_fn or sample_rss_bytes,
    )
    return {
        "audioSeconds": metrics.audio_seconds,
        "ttfrSeconds": round(metrics.ttfr_seconds, 4),
        "peakRssBytes": metrics.peak_rss_bytes,
        "sameHostAlive": process_alive(pid) if pid else True,
    }


def validate_wav(request: GgufSmokeRequest) -> dict[str, Any]:
    """Validate the written WAV with the release checker and return its stats."""
    path = request.wav_path
    problems = check_smoke_wav.check(path, request.min_seconds, expect_rate=APP_SAMPLE_RATE)
    stats: dict[str, Any] = {"path": str(path), "problems": problems}
    try:
        channels, rate, frames, samples = check_smoke_wav.read_wav(path)
    except (ValueError, OSError, struct.error) as exc:
        stats["unreadable"] = str(exc)
        return stats
    rms = float(np.sqrt(np.mean(np.square(samples)))) if samples else 0.0
    peak = float(max((abs(value) for value in samples), default=0.0))
    stats.update(
        {
            "sampleRate": rate,
            "channels": channels,
            "frames": frames,
            "seconds": round(frames / rate, 4) if rate else 0.0,
            "rms": round(rms, 5),
            "peak": round(peak, 5),
        }
    )
    return stats


# --------------------------------------------------------------------------- #
# report
# --------------------------------------------------------------------------- #


def run(request: GgufSmokeRequest) -> dict[str, Any]:
    """Perform the whole validation and return the report (never raises on a finding)."""
    from vienetts_app.core import engine_profiles
    from vienetts_app.core.audio import write_wav_file

    started_at = datetime.now(UTC).isoformat(timespec="seconds")
    cell_guess = ""
    try:
        from vienetts_app.core import qwen_gguf_runtime_manifest

        cell_guess = qwen_gguf_runtime_manifest.host_cell_key(request.device) or ""
    except Exception:  # noqa: BLE001 - the install gate reports the real error
        pass
    report: dict[str, Any] = {
        "schemaVersion": SCHEMA_VERSION,
        "kind": KIND,
        "startedAt": started_at,
        "platform": platform_report(),
        "profile": request.profile,
        "engineProfile": request.engine_profile,
        "quantization": request.quantization,
        "device": request.device,
        "cell": cell_guess,
        "packs": {"runtime": str(request.runtime_pack), "model": str(request.model_pack)},
        "dataDir": str(request.data_dir),
        "problems": [],
    }
    problems: list[str] = report["problems"]

    install = install_packs(request)
    report["install"] = {
        "cell": install.cell,
        "runtimeDir": str(install.runtime_dir),
        "runtimeBytes": install.runtime_bytes,
        "modelBytes": install.model_bytes,
        "talker": str(install.talker_path),
        "codec": str(install.codec_path),
    }
    report["identities"] = {
        "runtime": install.runtime_identity,
        "model": install.model_identity,
        "tokenizer": install.tokenizer_identity,
    }
    if not all(report["identities"].values()):
        problems.append("a promoted install did not report its pinned identities")

    language = request.language or engine_profiles.default_language(request.engine_profile)
    request = replace(request, language=language)
    report["language"] = {
        "code": language,
        "modelName": engine_profiles.language_model_name(
            engine_profiles.get_capabilities(request.engine_profile), language
        ),
    }

    engine = build_engine(request, install)
    pid = 0
    try:
        load_started = time.monotonic()
        engine.initialize()
        report["loadSeconds"] = round(time.monotonic() - load_started, 4)
        report["capabilities"] = capabilities_report(engine)
        pid = host_pid(engine)
        report["hostPid"] = pid
        report["deviceEvidence"] = {
            "requested": request.device,
            **device_evidence(engine),
        }
        reported_device = report["deviceEvidence"].get("device")
        if not reported_device:
            problems.append("the host never reported the device it loaded on")
        elif reported_device != request.device:
            problems.append(
                f"the host reported device {reported_device!r}, not the requested "
                f"{request.device!r} — evidence would misname the backend"
            )

        audio, metrics = stream_segment(engine, request, job_id="release-smoke-main", pid=pid)
        report["synthesis"] = {"text": request.text, **metrics.as_json()}
        problems.extend(audio_problems(audio))
        if audio.size:
            write_wav_file(audio, request.wav_path, APP_SAMPLE_RATE)
            report["wav"] = validate_wav(request)
            problems.extend(report["wav"].get("problems", []))
        else:
            report["wav"] = {"path": str(request.wav_path)}

        # Refusals are cheap and must hold everywhere the real run does.
        report["segmentation"] = check_segment_limit(engine, request)
        if not report["segmentation"].get("refused"):
            problems.append("an overlong segment was not refused before IPC")
        report["backendRefusal"] = check_backend_refusal(request, install)
        if not report["backendRefusal"].get("refused"):
            problems.append("a backend the pack does not ship was not refused")
        if not all(entry.get("refused") for entry in report["backendRefusal"].get("spellings", [])):
            problems.append("a non-native device spelling reached the host")

        report["repeatedJob"] = check_repeated_job(engine, request, pid=pid)
        if not report["repeatedJob"].get("audioSeconds"):
            problems.append("the repeated job produced no audio on the resident host")

        if request.check_cancel:
            cancellation = check_cancellation(engine, request, pid=pid)
            report["cancellation"] = cancellation
            if cancellation.get("terminal") != "cancelled":
                problems.append(
                    f"cancellation did not stop the job (terminal: {cancellation.get('terminal')})"
                )
            if cancellation.get("hostAliveAfterCancel") is False:
                if not cancellation.get("recovered"):
                    problems.append(
                        "the engine was not usable again after the cancel terminated the host"
                    )
                if not cancellation.get("hostReaped"):
                    problems.append("the cancelled host was terminated but never reaped")
        else:
            report["cancellation"] = {"skipped": "--no-check-cancel"}
            problems.append("the cancellation check was skipped — evidence is incomplete")

        if request.check_restart:
            restart = check_restart(engine, request, lambda: host_pid(engine))
            report["restart"] = restart
            if not restart.get("recovered"):
                problems.append("the host did not restart after being killed")
            if not restart.get("killed"):
                problems.append(f"could not kill the host mid-job ({restart.get('error', '')})")
        else:
            report["restart"] = {"skipped": "--no-check-restart"}
            problems.append("the restart check was skipped — evidence is incomplete")
    finally:
        shutdown_started = time.monotonic()
        engine.close()
        shutdown_seconds = time.monotonic() - shutdown_started
        reaped = not process_alive(pid) if pid else False
        report["shutdown"] = {
            "seconds": round(shutdown_seconds, 4),
            "reaped": reaped,
            "clean": reaped,
        }
        if pid and not reaped:
            problems.append(f"shutdown left host pid {pid} alive")
    report["finishedAt"] = datetime.now(UTC).isoformat(timespec="seconds")
    return report


def emit(report: dict[str, Any], json_out: str) -> int:
    """Print the single JSON result (stdout), mirror it to ``json_out``, return the exit code."""
    problems = list(report.get("problems", ()))
    missing = [section for section in REQUIRED_SECTIONS if section not in report]
    for section in missing:
        problems.append(f"missing evidence section: {section}")
    encoded = json.dumps(
        {**report, "problems": problems}, ensure_ascii=False, indent=2, sort_keys=True
    )
    print(encoded)
    if json_out:
        path = Path(json_out)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(encoded + "\n", encoding="utf-8")
    for problem in problems:
        print(f"FAIL {problem}", file=sys.stderr)
    if not problems:
        synthesis = report.get("synthesis", {})
        print(
            "ok: "
            f"{report['profile']} {report.get('quantization', '')} on {report['device']} — "
            f"ttfr={synthesis.get('ttfrSeconds')}s total={synthesis.get('totalSeconds')}s "
            f"rtf={synthesis.get('rtf')}",
            file=sys.stderr,
        )
    return 1 if problems else 0


def main(argv: list[str] | None = None) -> int:
    try:
        request = validate_request(parse_args(argv))
    except ReleaseSmokeUsageError as exc:
        print(f"usage error: {exc}", file=sys.stderr)
        return 2
    try:
        report = run(request)
    except (ReleaseSmokeError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return emit(report, request.json_out)


if __name__ == "__main__":
    raise SystemExit(main())
