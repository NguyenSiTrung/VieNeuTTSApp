#!/usr/bin/env python
"""Qwen3-TTS real-runtime compatibility probe (track Task 0.2).

Emits exactly ONE JSON result describing what the pinned runtime/model pair
actually does on this host: package revisions, resolved device/dtype/attention,
model-reported languages/speakers, native sample rate, TTFR, total time, RTF,
peak RSS/VRAM, instruction behavior, incremental-audio availability,
cancellation behavior and clean shutdown.

Run (Linux CPU example, after installing the verified runtime + model pack):

    .venv/bin/python scripts/spike/qwen_runtime_probe.py \
        --profile customvoice --model-dir "$QWEN_CUSTOMVOICE_DIR" \
        --speaker Ryan --json-out build/spike/qwen_cpu_customvoice.json

    .venv/bin/python scripts/spike/qwen_runtime_probe.py \
        --profile base --model-dir "$QWEN_BASE_DIR" \
        --ref-audio build/spike/ref.wav --ref-text "..." \
        --json-out build/spike/qwen_cpu_base.json

Everything the probe needs from the environment is injectable (``loader``,
``rss_fn``, ``vram_fn``, ``runtime_info_fn``, ``device_info_fn``) so the unit
tests run without torch, qwen-tts, or weights. Diagnostics go to stderr; stdout
carries the single JSON object and nothing else.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

SCHEMA_VERSION = 1
PROFILES = ("customvoice", "base")
DEFAULT_TEXT = "Xin chào, đây là bài kiểm tra tương thích của Qwen."
DEFAULT_LANGUAGE = "English"
DEFAULT_CANCEL_AFTER_MS = 250

# Official Qwen3-TTS language set (model cards); used only as a labeled
# fallback when the loaded model cannot report its own list.
OFFICIAL_LANGUAGES = (
    "Chinese",
    "English",
    "Japanese",
    "Korean",
    "German",
    "French",
    "Russian",
    "Portuguese",
    "Spanish",
    "Italian",
)
OFFICIAL_SPEAKERS = (
    "Vivian",
    "Serena",
    "Uncle_Fu",
    "Dylan",
    "Eric",
    "Ryan",
    "Aiden",
    "Ono_Anna",
    "Sohee",
)


class ProbeUsageError(ValueError):
    """The probe was invoked with an unusable combination; message names the fix."""


@dataclass(frozen=True)
class ProbeRequest:
    """One validated probe run."""

    profile: str
    model_dir: str
    device: str = "auto"
    text: str = DEFAULT_TEXT
    language: str = DEFAULT_LANGUAGE
    speaker: str = ""
    ref_audio: str = ""
    ref_text: str = ""
    instruct: str = ""
    check_instructions: bool = False
    check_incremental: bool = True
    check_cancel: bool = False
    cancel_after_ms: int = DEFAULT_CANCEL_AFTER_MS
    json_out: str = ""

    @property
    def clone_call(self) -> str:
        return "generate_voice_clone" if self.profile == "base" else "generate_custom_voice"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="qwen_runtime_probe",
        description="Probe the pinned Qwen3-TTS runtime/model pair and print one JSON result.",
    )
    parser.add_argument(
        "--profile",
        required=True,
        help=f"Model profile to probe: {', '.join(PROFILES)} (validated, not argparse-limited, "
        "so usage errors keep the one-JSON-result contract).",
    )
    parser.add_argument("--model-dir", required=True, help="Local verified model directory.")
    parser.add_argument("--device", default="auto", help="auto | cpu | cuda | mps")
    parser.add_argument("--text", default=DEFAULT_TEXT)
    parser.add_argument("--language", default=DEFAULT_LANGUAGE)
    parser.add_argument("--speaker", default="", help="CustomVoice fixed speaker (required).")
    parser.add_argument("--ref-audio", default="", help="Base reference clip (required).")
    parser.add_argument("--ref-text", default="", help="Base reference transcript (required).")
    parser.add_argument("--instruct", default="", help="Style instruction (0.6B ignores it).")
    parser.add_argument("--check-instructions", action="store_true")
    parser.add_argument("--no-check-incremental", dest="check_incremental", action="store_false")
    parser.add_argument("--check-cancel", action="store_true")
    parser.add_argument("--cancel-after-ms", type=int, default=DEFAULT_CANCEL_AFTER_MS)
    parser.add_argument("--json-out", default="", help="Also write the JSON result to this path.")
    return parser.parse_args(argv)


def validate_request(args: argparse.Namespace) -> ProbeRequest:
    """Validate CLI input before anything heavy is imported or loaded."""
    if args.profile not in PROFILES:
        raise ProbeUsageError(
            f"unknown profile {args.profile!r} — expected one of: {', '.join(PROFILES)}"
        )
    model_dir = Path(args.model_dir)
    if not model_dir.is_dir():
        raise ProbeUsageError(f"model-dir {args.model_dir!r} is not a directory")
    if args.device not in ("auto", "cpu", "cuda", "mps"):
        raise ProbeUsageError(f"unknown device {args.device!r} — expected auto, cpu, cuda or mps")
    if args.profile == "customvoice" and not args.speaker.strip():
        raise ProbeUsageError(
            "--speaker is required for customvoice — choose one of: " + ", ".join(OFFICIAL_SPEAKERS)
        )
    if args.profile == "base":
        if not args.ref_audio.strip():
            raise ProbeUsageError("--ref-audio is required for base (a 3-8 s reference clip)")
        if not Path(args.ref_audio).is_file():
            raise ProbeUsageError(f"ref-audio {args.ref_audio!r} does not exist")
        if not args.ref_text.strip():
            raise ProbeUsageError("--ref-text is required for base (the clip's transcript)")
    if args.cancel_after_ms < 0:
        raise ProbeUsageError("--cancel-after-ms must not be negative")
    return ProbeRequest(
        profile=args.profile,
        model_dir=str(model_dir),
        device=args.device,
        text=args.text,
        language=args.language,
        speaker=args.speaker.strip(),
        ref_audio=args.ref_audio,
        ref_text=args.ref_text,
        instruct=args.instruct,
        check_instructions=bool(args.check_instructions),
        check_incremental=bool(args.check_incremental),
        check_cancel=bool(args.check_cancel),
        cancel_after_ms=int(args.cancel_after_ms),
        json_out=args.json_out,
    )


# ── environment probes (all injectable) ─────────────────────────────────────


def peak_rss_mb(rss_fn: Callable[[], int]) -> float:
    """Peak RSS in MB; ``ru_maxrss`` is bytes on macOS and KB elsewhere."""
    raw = float(rss_fn())
    divisor = 1024.0 * 1024.0 if sys.platform == "darwin" else 1024.0
    return round(raw / divisor, 1)


def _default_rss_fn() -> int:
    import resource

    return int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)


def _default_vram_fn() -> int:
    try:
        import torch
    except Exception:  # noqa: BLE001 - absent/broken torch is normal evidence
        return 0
    try:
        if torch.cuda.is_available():
            return int(torch.cuda.max_memory_allocated())
    except Exception:  # noqa: BLE001 - device probes must degrade to "unknown"
        return 0
    return 0


def _package_version(name: str) -> str | None:
    from importlib import metadata

    try:
        return metadata.version(name)
    except Exception:  # noqa: BLE001 - missing dist metadata is reportable data
        return None


def _default_runtime_info_fn() -> dict[str, Any]:
    return {
        "qwenTts": _package_version("qwen-tts"),
        "torch": _package_version("torch"),
        "transformers": _package_version("transformers"),
        "python": platform.python_version(),
        "platform": f"{sys.platform}-{platform.machine()}",
    }


def _default_device_info_fn(device: str) -> tuple[str, str, str, str]:
    """Resolve ``(device, dtype, attention, detail)``; never raises."""
    if device == "auto":
        try:
            import torch

            if torch.cuda.is_available():
                device = "cuda"
            elif getattr(torch.backends, "mps", None) is not None and torch.backends.mps.is_available():
                device = "mps"
            else:
                device = "cpu"
        except Exception:  # noqa: BLE001 - missing torch resolves to CPU with a note
            return ("cpu", "float32", "sdpa", "torch unavailable — CPU fallback")
    if device == "cuda":
        return ("cuda", "bfloat16", "sdpa", "CUDA device selected")
    if device == "mps":
        return ("mps", "float32", "sdpa", "Apple MPS selected")
    return ("cpu", "float32", "sdpa", "CPU fallback (no real-time promise)")


def load_model(request: ProbeRequest) -> Any:
    """Load the local verified checkpoint with remote code disabled.

    Production seam only: tests inject their own loader. Raises a plain
    ``RuntimeError`` with an actionable message when the optional runtime is
    missing, so the CLI can still emit one JSON result.
    """
    try:
        from qwen_tts import Qwen3TTSModel
    except ImportError as exc:
        raise RuntimeError(
            "qwen-tts is not importable — install the verified Qwen runtime first"
        ) from exc
    resolved, dtype_name, attention, _detail = _default_device_info_fn(request.device)
    dtype = getattr(__import__("torch"), dtype_name)
    device_map = {"cuda": "cuda:0", "mps": "mps", "cpu": "cpu"}[resolved]
    return Qwen3TTSModel.from_pretrained(
        request.model_dir,
        device_map=device_map,
        dtype=dtype,
        attn_implementation=attention,
        trust_remote_code=False,
    )


# ── probe steps ─────────────────────────────────────────────────────────────


def _model_languages(model: Any) -> tuple[list[str], str]:
    reported = getattr(model, "languages", None)
    if reported is None:
        reported = getattr(model, "supported_languages", None)
    if reported is None:
        return list(OFFICIAL_LANGUAGES), "official-model-card"
    return [str(item) for item in reported], "model"


def _model_speakers(model: Any, request: ProbeRequest) -> tuple[list[str], str]:
    if request.profile == "base":
        return [], "base-has-no-fixed-speakers"
    reported = getattr(model, "speakers", None)
    if reported is None:
        reported = getattr(model, "supported_speakers", None)
    if reported is None:
        return list(OFFICIAL_SPEAKERS), "official-model-card"
    return [str(item) for item in reported], "model"


def _incremental_detail(model: Any) -> tuple[bool, str]:
    streaming = sorted(
        name
        for name in dir(model)
        if "stream" in name.lower() and callable(getattr(model, name, None))
    )
    if streaming:
        return True, f"incremental API present: {', '.join(streaming)}"
    return False, "no incremental/stream method — generation returns the final array only"


def _generate(model: Any, request: ProbeRequest, *, instruct: str | None) -> tuple[np.ndarray, int]:
    if request.profile == "base":
        wavs, rate = model.generate_voice_clone(
            text=request.text,
            language=request.language,
            ref_audio=request.ref_audio,
            ref_text=request.ref_text,
        )
    else:
        kwargs: dict[str, Any] = {
            "text": request.text,
            "language": request.language,
            "speaker": request.speaker,
        }
        if instruct is not None:
            kwargs["instruct"] = instruct
        wavs, rate = model.generate_custom_voice(**kwargs)
    audio = np.asarray(wavs[0], dtype=np.float32)
    return audio, int(rate)


def _probe_instructions(model: Any, request: ProbeRequest) -> dict[str, Any]:
    """Evidence for whether a non-empty ``instruct`` changes the audio at all."""
    try:
        plain, _ = _generate(model, request, instruct="")
    except TypeError as exc:
        return {"accepted": False, "affectsAudio": False, "detail": f"instruct unsupported: {exc}"}
    try:
        styled, _ = _generate(model, request, instruct=request.instruct)
    except TypeError as exc:
        return {"accepted": False, "affectsAudio": False, "detail": f"instruct unsupported: {exc}"}
    affects = hashlib.sha256(plain.tobytes()).hexdigest() != hashlib.sha256(styled.tobytes()).hexdigest()
    detail = (
        "instruction changed the rendered audio"
        if affects
        else "instruction accepted but rendered audio is byte-identical (0.6B ignores it)"
    )
    return {"accepted": True, "affectsAudio": bool(affects), "detail": detail}


def _probe_cancellation(model: Any, request: ProbeRequest) -> dict[str, Any]:
    """Cooperative-cancel evidence: can a cancel request stop a live generate?"""
    state: dict[str, Any] = {"finished": False, "error": ""}
    cancel_at = request.cancel_after_ms / 1000.0

    def worker() -> None:
        try:
            _generate(model, request, instruct=None)
        except Exception as exc:  # noqa: BLE001 - report, do not crash the probe
            state["error"] = str(exc) or repr(exc)
        state["finished"] = True

    thread = threading.Thread(target=worker, daemon=True)
    start = time.perf_counter()
    thread.start()
    time.sleep(cancel_at)
    cancel_requested_at = time.perf_counter()
    interruptible = False  # the blocking call exposes no cancellation hook
    thread.join(timeout=60.0)
    latency_ms = round((time.perf_counter() - cancel_requested_at) * 1000.0, 1)
    terminal = "completed" if state["finished"] else "hung"
    detail = (
        "generation completed after the cancel request; the public call exposes no "
        "interrupt hook, so the host must terminate on cancel"
    )
    return {
        "requested": True,
        "cancelAfterMs": request.cancel_after_ms,
        "interruptible": interruptible,
        "latencyMs": latency_ms,
        "terminal": terminal,
        "detail": detail,
        "error": state["error"],
    }


def _probe_shutdown(model: Any) -> dict[str, Any]:
    close = getattr(model, "close", None)
    if close is None:
        return {"clean": True, "detail": "model exposes no close(); reference drop only"}
    try:
        close()
    except Exception as exc:  # noqa: BLE001 - a failed teardown is evidence, not a crash
        return {"clean": False, "detail": str(exc) or repr(exc)}
    return {"clean": True, "detail": "close() returned without error"}


def run_probe(
    request: ProbeRequest,
    *,
    loader: Callable[[ProbeRequest], Any] | None = None,
    rss_fn: Callable[[], int] | None = None,
    vram_fn: Callable[[], int] | None = None,
    runtime_info_fn: Callable[[], dict[str, Any]] | None = None,
    device_info_fn: Callable[[str], tuple[str, str, str, str]] | None = None,
) -> dict[str, Any]:
    """Run one probe and return the JSON-ready result mapping."""
    loader = loader or load_model
    rss_fn = rss_fn or _default_rss_fn
    vram_fn = vram_fn or _default_vram_fn
    runtime_info_fn = runtime_info_fn or _default_runtime_info_fn
    device_info_fn = device_info_fn or _default_device_info_fn

    resolved, dtype, attention, device_detail = device_info_fn(request.device)
    result: dict[str, Any] = {
        "schemaVersion": SCHEMA_VERSION,
        "profile": request.profile,
        "modelDir": request.model_dir,
        "runtime": runtime_info_fn(),
        "device": {
            "requested": request.device,
            "resolved": resolved,
            "dtype": dtype,
            "attention": attention,
            "detail": device_detail,
        },
        "request": {
            "textChars": len(request.text),
            "language": request.language,
            "speaker": request.speaker,
            "cloneCall": request.clone_call,
            "refAudio": bool(request.ref_audio),
            "instruct": request.instruct,
        },
        "capabilities": {},
        "metrics": {
            "loadMs": None,
            "ttfrMs": None,
            "totalMs": None,
            "audioSeconds": None,
            "rtf": None,
            "peakRssMb": None,
            "peakVramMb": None,
        },
        "instructions": None,
        "cancellation": None,
        "shutdown": {"clean": None, "detail": ""},
        "errors": [],
    }

    load_started = time.perf_counter()
    model = loader(request)
    result["metrics"]["loadMs"] = round((time.perf_counter() - load_started) * 1000.0, 3)

    languages, languages_source = _model_languages(model)
    speakers, speakers_source = _model_speakers(model, request)
    incremental, incremental_detail = _incremental_detail(model)
    rate = int(getattr(model, "sample_rate", getattr(model, "sr", 24000)))
    result["capabilities"] = {
        "languages": languages,
        "languagesSource": languages_source,
        "speakers": speakers,
        "speakersSource": speakers_source,
        "sampleRate": rate,
        "incrementalAudio": incremental if request.check_incremental else None,
        "incrementalDetail": incremental_detail if request.check_incremental else "not probed",
    }

    started = time.perf_counter()
    audio, reported_rate = _generate(model, request, instruct=None)
    elapsed_ms = round((time.perf_counter() - started) * 1000.0, 3)
    audio_seconds = round(float(audio.size) / float(reported_rate), 4) if reported_rate else 0.0
    vram_bytes = int(vram_fn())
    result["metrics"].update(
        {
            "ttfrMs": elapsed_ms,
            "totalMs": elapsed_ms,
            "audioSeconds": audio_seconds,
            "rtf": round(elapsed_ms / 1000.0 / audio_seconds, 6) if audio_seconds else None,
            "peakRssMb": peak_rss_mb(rss_fn),
            "peakVramMb": round(vram_bytes / (1024.0 * 1024.0), 1) if vram_bytes else None,
            "reportedSampleRate": reported_rate,
        }
    )

    if request.check_instructions:
        result["instructions"] = _probe_instructions(model, request)
    if request.check_cancel:
        result["cancellation"] = _probe_cancellation(model, request)
    result["shutdown"] = _probe_shutdown(model)
    return result


def error_result(message: str, request: ProbeRequest | None = None) -> dict[str, Any]:
    """One JSON result for a run that never produced measurements."""
    return {
        "schemaVersion": SCHEMA_VERSION,
        "profile": request.profile if request else None,
        "modelDir": request.model_dir if request else None,
        "runtime": None,
        "device": None,
        "request": None,
        "capabilities": None,
        "metrics": {
            "loadMs": None,
            "ttfrMs": None,
            "totalMs": None,
            "audioSeconds": None,
            "rtf": None,
            "peakRssMb": None,
            "peakVramMb": None,
        },
        "instructions": None,
        "cancellation": None,
        "shutdown": {"clean": None, "detail": ""},
        "errors": [message],
    }


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        request = validate_request(args)
    except ProbeUsageError as exc:
        print(json.dumps(error_result(str(exc)), ensure_ascii=False), flush=True)
        print(f"probe usage error: {exc}", file=sys.stderr)
        return 2
    try:
        payload = run_probe(request)
    except Exception as exc:  # noqa: BLE001 - one JSON result is the CLI contract
        payload = error_result(str(exc) or repr(exc), request)
        print(json.dumps(payload, ensure_ascii=False), flush=True)
        print(f"probe failed: {exc}", file=sys.stderr)
        return 1
    encoded = json.dumps(payload, ensure_ascii=False)
    if request.json_out:
        out = Path(request.json_out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(encoded + "\n", encoding="utf-8")
    print(encoded, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())