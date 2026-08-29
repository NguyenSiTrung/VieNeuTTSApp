#!/usr/bin/env python
"""Validate a smoke-run WAV: real, non-silent synthesized audio.

Used by the release CI against the PACKAGED binary's ``--smoke`` output
(``VieNeuTTS --smoke "Xin chào" -o smoke.wav``): a zero exit code only
proves the pipeline ran, this proves the file is audible speech — valid
RIFF structure, sane sample rate, minimum duration, and RMS/peak floors
well below real TTS output but far above silence.

The app writes 48 kHz float32 WAVs via libsndfile (``subtype="FLOAT"``),
which the stdlib ``wave`` module refuses — hence the small direct RIFF
reader here. PCM16 is accepted too so hand-made fixtures also parse.
Stdlib only, so it runs on a bare interpreter.

Usage:
    python scripts/check_smoke_wav.py out.wav [--min-seconds 0.5]
"""

from __future__ import annotations

import argparse
import math
import struct
import sys
from pathlib import Path

MIN_SAMPLE_RATE = 16000
# Normalized (float-domain) floors: real TTS peaks near ±0.9 and RMS in the
# 0.05–0.3 range; silence or a dither-only buffer sits far below these.
MIN_RMS = 0.0015
MIN_PEAK = 0.03


def read_wav(path: Path) -> tuple[int, int, int, list[float]]:
    """Parse a RIFF/WAVE file → (channels, sample_rate, nframes, samples).

    Supports the formats this app can produce or import: PCM 16-bit, PCM
    32-bit int, and IEEE-float 32-bit. ``samples`` are normalized floats.
    Raises ValueError with a human-readable reason on anything else.
    """
    raw = path.read_bytes()
    if len(raw) < 12 or raw[:4] != b"RIFF" or raw[8:12] != b"WAVE":
        raise ValueError("not a RIFF/WAVE file")

    audio_format = channels = sample_rate = bits = 0
    data = b""
    pos = 12
    while pos + 8 <= len(raw):
        chunk_id, size = struct.unpack_from("<4sI", raw, pos)
        body = raw[pos + 8 : pos + 8 + size]
        if chunk_id == b"fmt ":
            audio_format, channels, sample_rate = struct.unpack_from("<HHI", body)
            bits = struct.unpack_from("<H", body, 14)[0]
        elif chunk_id == b"data":
            data = body
        pos += 8 + size + (size & 1)  # chunks are word-aligned

    if audio_format == 0 or not data:
        raise ValueError("missing fmt or data chunk")
    if audio_format == 1 and bits == 16:
        samples = [s / 32768.0 for s in struct.unpack(f"<{len(data) // 2}h", data)]
    elif audio_format == 1 and bits == 32:
        samples = [s / 2147483648.0 for s in struct.unpack(f"<{len(data) // 4}i", data)]
    elif audio_format == 3 and bits == 32:
        samples = list(struct.unpack(f"<{len(data) // 4}f", data))
    else:
        raise ValueError(f"unsupported WAV format: tag={audio_format} bits={bits}")

    nframes = len(samples) // channels if channels else 0
    return channels, sample_rate, nframes, samples


def check(path: Path, min_seconds: float) -> list[str]:
    """Return a list of problems ([] = the WAV is real, non-silent audio)."""
    problems: list[str] = []
    if not path.is_file() or path.stat().st_size == 0:
        return [f"{path} is missing or empty"]

    try:
        channels, sample_rate, nframes, samples = read_wav(path)
    except (ValueError, struct.error) as exc:
        return [f"unparseable WAV: {exc}"]

    if channels < 1:
        problems.append(f"bad channel count {channels}")
    if sample_rate < MIN_SAMPLE_RATE:
        problems.append(f"sample rate {sample_rate} < {MIN_SAMPLE_RATE}")
    duration = nframes / sample_rate if sample_rate and channels else 0.0
    if duration < min_seconds:
        problems.append(f"duration {duration:.2f}s < {min_seconds:.2f}s")

    if not samples:
        problems.append("no PCM frames decoded")
        return problems

    rms = math.sqrt(sum(s * s for s in samples) / len(samples))
    peak = max(abs(s) for s in samples)
    if rms < MIN_RMS:
        problems.append(f"RMS {rms:.5f} < {MIN_RMS} (silent or near-silent audio)")
    if peak < MIN_PEAK:
        problems.append(f"peak {peak:.5f} < {MIN_PEAK} (no audible amplitude)")

    if not problems:
        print(
            f"ok: {path.name} — {channels}ch {sample_rate}Hz {duration:.2f}s "
            f"rms={rms:.3f} peak={peak:.3f}"
        )
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("wav", type=Path, help="WAV file written by a --smoke run")
    parser.add_argument(
        "--min-seconds", type=float, default=0.5, help="minimum duration (default: 0.5)"
    )
    args = parser.parse_args(argv)

    problems = check(args.wav, args.min_seconds)
    for problem in problems:
        print(f"FAIL {problem}", file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
