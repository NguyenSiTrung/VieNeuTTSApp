#!/usr/bin/env python
"""Phase 0 spike: performance & memory vs PROJECT_PLAN.md §18 budgets (FR-0.4).

Run (each invocation is a fresh process → cold start is measurable):
    .venv/bin/python scripts/spike/phase0_perf.py
Findings recorded in docs/spike-report.md §5.
"""

from __future__ import annotations

import resource
import time

SHORT = "Xin chào thế giới."
LONG = (
    "Việt Nam là đất nước có bề dày lịch sử hàng nghìn năm với những trang sử hào hùng. "
    "Từ núi Chí Linh đến đồng bằng sông Cửu Long, mỗi vùng đất đều mang trong mình những "
    "câu chuyện riêng về con người, văn hóa và truyền thống. Ngôn ngữ tiếng Việt vì thế "
    "cũng phong phú với ba miền Bắc Trung Nam, mỗi miền một sắc thái giọng nói riêng. "
    "Công nghệ tổng hợp giọng nói tiếng Việt chất lượng cao giúp giữ gìn và lan tỏa tiếng "
    "nói ấy đến với mọi người, từ sách nói, podcast cho đến các ứng dụng hỗ trợ đọc. "
    "Chúng tôi xây dựng ứng dụng này để bất kỳ ai cũng có thể tạo giọng đọc tự nhiên trên "
    "máy tính của mình, không cần internet, không gửi dữ liệu lên đám mây. "
) * 3  # ~1900 chars -> ~8 chunks of max_chars=256


def rss_mb() -> float:
    # ru_maxrss is KiB on Linux, bytes on macOS.
    raw = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return raw / 1_048_576 if raw > 10_000_000 else raw / 1_024


def main() -> int:
    from vieneu import Vieneu

    t0 = time.perf_counter()
    tts = Vieneu(backend="onnx", precision="int8")
    cold_s = time.perf_counter() - t0
    print(f"cold start (fresh process, cached weights): {cold_s:.2f} s   budget < 15 s")

    # Warm short-text latency: best of 3.
    best = min(
        (measure_short(tts) for _ in range(3)), key=lambda m: m[0]
    )
    warm_s, dur = best
    print(f"warm short-text latency: {warm_s:.2f} s (audio {dur:.2f} s)   budget < 1 s")

    t = time.perf_counter()
    audio = tts.infer(LONG, voice="Adam", show_progress=False)
    long_s = time.perf_counter() - t
    long_dur = len(audio) / tts.sample_rate
    print(f"long text: {len(LONG)} chars -> {long_dur:.1f} s audio in {long_s:.2f} s | RTF {long_s / long_dur:.2f}   budget < 1")

    print(f"peak RSS: {rss_mb():.0f} MB   budget < 2048 MB")

    ok = cold_s < 15 and warm_s < 1 and long_s / long_dur < 1 and rss_mb() < 2048
    print("PERF BUDGETS:", "ALL PASS" if ok else "FAIL")
    return 0 if ok else 1


def measure_short(tts) -> tuple[float, float]:
    t = time.perf_counter()
    audio = tts.infer(SHORT, voice="Adam", show_progress=False)
    return time.perf_counter() - t, len(audio) / tts.sample_rate


if __name__ == "__main__":
    raise SystemExit(main())
