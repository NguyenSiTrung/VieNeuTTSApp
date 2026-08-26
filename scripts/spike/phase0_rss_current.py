"""Spike helper: current RSS (not peak) across long inferences, with gc between."""

import gc
import os
import subprocess


def cur_rss_mb() -> float:
    out = subprocess.run(
        ["ps", "-o", "rss=", "-p", str(os.getpid())], capture_output=True, text=True
    )
    return int(out.stdout.strip()) / 1024


def main() -> None:
    from vieneu import Vieneu

    tts = Vieneu(backend="onnx", precision="int8")
    print(f"current rss after init: {cur_rss_mb():.0f} MB")
    long_text = (
        "Việt Nam là đất nước có bề dày lịch sử với những trang sử hào hùng. "
        "Từ núi Chí Linh đến đồng bằng sông Cửu Long mỗi vùng đất đều mang câu chuyện riêng. "
    ) * 3
    for i in range(1, 4):
        audio = tts.infer(long_text, voice="Adam", show_progress=False)
        print(f"  (infer #{i}: {len(audio) / 48000:.0f}s audio)")
        del audio
        gc.collect()
        print(f"current rss after long infer #{i} + gc: {cur_rss_mb():.0f} MB")
    audio = tts.infer("Xin chào.", voice="Adam", show_progress=False)
    del audio
    gc.collect()
    print(f"current rss after short + gc: {cur_rss_mb():.0f} MB")


if __name__ == "__main__":
    main()
