#!/usr/bin/env bash
# Phase 0 spike: Ubuntu 22.04 validation in Docker (FR-0.1 / Task 7).
# Validates: vieneu==3.3.0 CPU install on linux (python3.10) + torch-free
# synthesis with the offline model cache mounted from the host.
#
# Run: ./scripts/spike/phase0_ubuntu_docker.sh
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
HF_CACHE="${HF_CACHE:-$HOME/.cache/huggingface}"

docker run --rm \
  -v "$REPO_ROOT:/app" -w /app \
  -v "$HF_CACHE:/root/.cache/huggingface:ro" \
  -e HF_HUB_OFFLINE=1 \
  ubuntu:22.04 bash -euxo pipefail -c '
    apt-get update -qq && apt-get install -y -qq python3.10 python3.10-venv libsndfile1 >/dev/null
    python3.10 -m venv /venv
    /venv/bin/pip install -q vieneu==3.3.0
    /venv/bin/python - <<'"'"'PY'"'"'
import importlib.util, platform
import numpy as np
from vieneu import Vieneu
print("ubuntu:", platform.platform(), "| python:", platform.python_version())
print("torch-free:", importlib.util.find_spec("torch") is None)
tts = Vieneu(backend="onnx", precision="int8")
a = tts.infer("Xin chào từ Ubuntu.", voice="Adam", show_progress=False)
assert a.dtype == np.float32 and a.ndim == 1 and len(a) > 48000 and float(np.abs(a).max()) > 0.01
voices = tts.list_preset_voices()
assert len(voices) >= 20
import soundfile as sf
sf.write("/tmp/out.wav", a, tts.sample_rate)
data, sr = sf.read("/tmp/out.wav")
assert sr == 48000
print(f"UBUNTU CONTRACT PASS: {len(a)/48000:.2f}s audio, {len(voices)} voices, wav sr={sr}")
PY
  '
echo "UBUNTU VALIDATION OK"
