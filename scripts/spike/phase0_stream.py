#!/usr/bin/env python
"""Phase 0 spike: infer_stream chunk format + first-chunk latency (FR-0.3).

Run: .venv/bin/python scripts/spike/phase0_stream.py
Findings recorded in docs/spike-report.md §4.
"""

from __future__ import annotations

import time

import numpy as np

TEXT = (
    "Trí tuệ nhân tạo đang thay đổi cách con người làm việc và giao tiếp mỗi ngày. "
    "Các mô hình tổng hợp giọng nói ngày càng tự nhiên hơn, gần với giọng người thật. "
    "Ứng dụng này giúp bạn tạo giọng đọc tiếng Việt và tiếng Anh ngay trên máy tính, "
    "hoàn toàn ngoại tuyến, không cần kết nối internet hay dịch vụ đám mây nào cả."
)


def main() -> int:
    from vieneu import Vieneu

    tts = Vieneu(backend="onnx", precision="int8")
    sr = tts.sample_rate

    chunks: list[np.ndarray] = []
    t_first: float | None = None
    t0 = time.perf_counter()
    for i, chunk in enumerate(tts.infer_stream(TEXT, voice="Adam")):
        if i == 0:
            t_first = time.perf_counter() - t0
        chunks.append(np.asarray(chunk))
    t_total = time.perf_counter() - t0

    audio = np.concatenate(chunks) if chunks else np.array([])
    dur = len(audio) / sr

    print(f"chunks: {len(chunks)}")
    print(f"chunk dtypes: {sorted({str(c.dtype) for c in chunks})}")
    print(f"chunk ndims: {sorted({c.ndim for c in chunks})}")
    shapes = [c.shape for c in chunks]
    print(f"first 5 chunk shapes: {shapes[:5]}")
    print(f"last chunk shape: {shapes[-1]}")
    print(f"chunk length range: {min(s[0] for s in shapes)}..{max(s[0] for s in shapes)} samples")
    print(f"first-chunk latency: {t_first:.3f} s (budget ~0.300 s)")
    print(f"audio duration: {dur:.2f} s | stream wall time: {t_total:.2f} s | RTF: {t_total / dur:.2f} (budget < 1)")
    print(f"audio dtype {audio.dtype}, peak {float(np.abs(audio).max()):.3f}")

    ok = (
        chunks
        and all(c.dtype == np.float32 for c in chunks)
        and all(c.ndim == 1 for c in chunks)
        and dur > 1
        and float(np.abs(audio).max()) > 0.01
    )
    print("STREAM CONTRACT:", "PASS" if ok else "FAIL")

    # backend="torch" without torch installed: capture the failure mode for engine.py.
    try:
        Vieneu(backend="torch")
        print("backend='torch' without torch: NO ERROR (unexpected)")
    except Exception as e:  # noqa: BLE001 - spike: record any failure type
        print(f"backend='torch' without torch raises {type(e).__name__}: {str(e)[:160]}")

    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
