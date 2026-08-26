#!/usr/bin/env python
"""Phase 0 spike: validate the vieneu 3.3.0 CPU/ONNX synthesis contract (FR-0.2).

Run: .venv/bin/python scripts/spike/phase0_contract.py [--with-cloning]

First run downloads model weights (~240 MB) from Hugging Face. Findings are
recorded in docs/spike-report.md §3.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import numpy as np

OUT = Path("build/spike")
OUT.mkdir(parents=True, exist_ok=True)


def check(label: str, cond: bool, detail: str = "") -> None:
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {label}" + (f" — {detail}" if detail else ""))
    if not cond:
        globals()["failures"] = globals().get("failures", 0) + 1


def main() -> int:
    import soundfile as sf
    from vieneu import Vieneu

    globals()["failures"] = 0

    t0 = time.perf_counter()
    tts = Vieneu(backend="onnx", precision="int8")
    init_s = time.perf_counter() - t0
    print(f"init (incl. first-run weight download): {init_s:.1f}s; backend={tts.backend!r}")
    check("sample_rate == 48000", tts.sample_rate == 48_000, f"sample_rate={tts.sample_rate}")

    # ── Preset voices ──────────────────────────────────────────────────────
    voices = tts.list_preset_voices()
    check("list_preset_voices() >= 20", len(voices) >= 20, f"count={len(voices)}")
    print(f"  voices sample: {voices[:6]}")
    default_voice = tts.get_preset_voice(None)
    print(f"  default voice fields: {default_voice!r}"[:400])
    voice = voices[0][1]  # (label, voice_id): voice_id is what infer(voice=...) takes

    # ── infer() contract: vi + en ─────────────────────────────────────────
    for label, text in (("vi", "Xin chào, đây là giọng nói tiếng Việt."), ("en", "Hello world, this is an English test.")):
        t = time.perf_counter()
        audio = tts.infer(text, voice=voice, show_progress=False)
        dt = time.perf_counter() - t
        dur = len(audio) / tts.sample_rate
        check(f"infer({label}) dtype float32", audio.dtype == np.float32, f"dtype={audio.dtype}")
        check(f"infer({label}) mono (ndim==1)", np.ndim(audio) == 1, f"ndim={np.ndim(audio)}")
        check(f"infer({label}) duration > 0", len(audio) > 0, f"dur={dur:.2f}s")
        check(f"infer({label}) non-silent", float(np.abs(audio).max()) > 0.01, f"peak={float(np.abs(audio).max()):.3f}")
        print(f"  infer({label}): {dur:.2f}s audio in {dt:.2f}s (RTF={dt/dur:.2f})")

    # ── save() WAV export ─────────────────────────────────────────────────
    wav_path = OUT / "contract_vi.wav"
    tts.save(audio, str(wav_path))
    data, sr = sf.read(str(wav_path))
    check("save() WAV read-back 48 kHz", sr == 48_000, f"sr={sr}")
    check("save() WAV non-empty", len(data) > 0 and float(np.abs(data).max()) > 0.01)
    print(f"  saved {wav_path} ({wav_path.stat().st_size} bytes)")

    # ── Cloning: denoise + add_voice + infer + persistence ────────────────
    if "--with-cloning" in sys.argv:
        # Reference clip: synthesize 5 s of speech, then use it as the clone ref.
        ref_src = OUT / "clone_ref.wav"
        long_vi = "Đây là đoạn văn bản dài hơn dùng làm mẫu giọng tham chiếu cho việc sao chép giọng nói."
        ref_audio = tts.infer(long_vi, voice=voice, show_progress=False)
        tts.save(ref_audio, str(ref_src))
        ref_dur = len(ref_audio) / tts.sample_rate
        check("clone ref clip 3-8 s", 3 <= ref_dur <= 8, f"dur={ref_dur:.2f}s")

        wav44, sr44 = tts.denoise(str(ref_src), out_path=str(OUT / "denoised.wav"), max_seconds=8.0)
        check("denoise() returns (float32 mono, 44100)", wav44.dtype == np.float32 and np.ndim(wav44) == 1 and sr44 == 44_100,
              f"dtype={wav44.dtype}, ndim={np.ndim(wav44)}, sr={sr44}")

        name = "spike_clone"
        returned = tts.add_voice(name, str(ref_src), denoise=True, save=True, description="spike")
        check("add_voice() returns name", returned == name, f"returned={returned!r}")
        cloned = tts.infer("Thử giọng đã sao chép.", voice=name, show_progress=False)
        check("infer with cloned voice non-silent", float(np.abs(cloned).max()) > 0.01,
              f"peak={float(np.abs(cloned).max()):.3f}, dur={len(cloned)/48000:.2f}s")
        names_after = [v[1] for v in tts.list_preset_voices()]
        check("cloned voice in voice list", name in names_after)

    total = globals()["failures"]
    print(f"\n{'ALL CHECKS PASSED' if total == 0 else 'FAILURES: ' + str(total)}")
    return 0 if total == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
