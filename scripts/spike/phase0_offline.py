#!/usr/bin/env python
"""Phase 0 spike: offline bundling approach (FR-0.5).

Proves three offline strategies for the CPU/ONNX int8 engine:
  A. HF_HUB_OFFLINE=1 against the default user HF cache (pre-seeded).
  B. HF_HOME=<portable copied cache> + HF_HUB_OFFLINE=1 (bundled-app model).
  C. Local model dir: backbone_repo=<dir> + onnx_dir=<dir>/onnx_int8
     (graphs/config/denoiser/speaker_encoder local; codec still via HF cache).

Run: .venv/bin/python scripts/spike/phase0_offline.py
Findings recorded in docs/spike-report.md §6.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

OUT = Path("build/spike")
BACKBONE_SNAP = Path(
    "~/.cache/huggingface/hub/models--pnnbao-ump--VieNeu-TTS-v3-Turbo/snapshots"
).expanduser()
CODEC_SNAP = Path(
    "~/.cache/huggingface/hub/models--OpenMOSS-Team--MOSS-Audio-Tokenizer-Nano-ONNX/snapshots"
).expanduser()

PROBE = (
    "import numpy as np, sys; from vieneu import Vieneu; "
    "tts = Vieneu(backend='onnx', precision='int8'{extra}); "
    "a = tts.infer('Kiểm tra ngoại tuyến.', voice='Adam', show_progress=False); "
    "sys.exit(0 if a.dtype == np.float32 and len(a) > 48000 else 1)"
)


def run_probe(label: str, env_extra: dict[str, str], extra: str = "") -> bool:
    env = {**os.environ, **env_extra}
    r = subprocess.run(
        [sys.executable, "-c", PROBE.format(extra=extra)], env=env, capture_output=True, text=True
    )
    ok = r.returncode == 0
    err = "" if ok else r.stderr.strip().splitlines()[-1][:200] if r.stderr.strip() else ""
    print(f"[{'PASS' if ok else 'FAIL'}] {label}" + (f" — {err}" if err else ""))
    return ok


def main() -> int:
    # B: portable cache copy (made before A invalidates anything; also proves copy portability)
    portable = OUT / "hf_home"
    if portable.exists():
        shutil.rmtree(portable)
    portable.mkdir(parents=True)
    shutil.copytree(BACKBONE_SNAP.parent.parent, portable / "hub", dirs_exist_ok=True)

    # C: local model dir (backbone files only — codec not exposed as local dir by SDK)
    local_model = OUT / "local_model"
    if local_model.exists():
        shutil.rmtree(local_model)
    snap = next(iter(BACKBONE_SNAP.iterdir()))
    shutil.copytree(snap, local_model)
    # keep only CPU int8 artifacts
    for p in list(local_model.rglob("*")):
        if p.is_file() and p.name not in {
            "config.json", "denoiser.onnx", "speaker_encoder.onnx", "tokenizer.json",
            "vieneu_acoustic_cached.onnx", "vieneu_backbone_shared.data",
            "vieneu_decode_step.onnx", "vieneu_prefill.onnx", "vieneu_v3_heads.npz",
        }:
            p.unlink()

    ok_a = run_probe("A: HF_HUB_OFFLINE=1 with user cache", {"HF_HUB_OFFLINE": "1"})
    ok_b = run_probe(
        "B: HF_HOME=portable copy + HF_HUB_OFFLINE=1",
        {"HF_HOME": str(portable.resolve()), "HF_HUB_OFFLINE": "1"},
    )
    ok_c = run_probe(
        "C: backbone local dir + onnx_dir (codec via cache)",
        {"HF_HUB_OFFLINE": "1"},
        extra=f", backbone_repo=r'{local_model.resolve()}', onnx_dir=r'{(local_model / 'onnx_int8').resolve()}'",
    )

    print(f"\nlocal model dir size: {sum(p.stat().st_size for p in local_model.rglob('*') if p.is_file()) / 1e6:.0f} MB")
    print(f"portable cache size:  {sum(p.stat().st_size for p in portable.rglob('*') if p.is_file()) / 1e6:.0f} MB")
    return 0 if (ok_a and ok_b and ok_c) else 1


if __name__ == "__main__":
    raise SystemExit(main())
