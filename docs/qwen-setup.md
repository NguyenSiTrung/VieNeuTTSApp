# Qwen engine pack — offline installation and platform limits

Qwen3-TTS (CustomVoice + Base) is an **optional engine pack**. The default
install stays torch-free: VieNeu-TTS v3 Turbo works fully offline with the
CPU ONNX baseline, and nothing Qwen-related downloads, imports torch, or
runs unless you opt in.

## What the pack needs

1. **Runtime** — `qwen-tts` plus pinned PyTorch (`torch`/`torchaudio`/
   `transformers`, same pins as the `gpu` extra):
   ```bash
   pip install "vienetts-app[qwen]"
   ```
2. **Checkpoints** — both official 0.6B repos, fetched once into the
   Hugging Face hub cache:
   ```bash
   python scripts/fetch_qwen_models.py
   ```
   The settings tab ("Gói Qwen tùy chọn" card) reports runtime / torch /
   device / per-checkpoint status; a Qwen job whose pieces are missing
   fails with the exact install step instead of a stack trace.

## Fully-offline machines

Fetch on an online machine, then carry the cache over:

```bash
# online machine: fetch into a portable dir
HF_HOME=/mnt/usb/qwen-hf python scripts/fetch_qwen_models.py
# offline machine: point the app at it
HF_HOME=/mnt/usb/qwen-hf vienetts-app
```

`HF_HOME` (or `HF_HUB_CACHE`) is the only knob — the runtime resolves
`Qwen/Qwen3-TTS-12Hz-0.6B-{CustomVoice,Base}` snapshots from there, and
the settings card's model ticks read the same layout
(`models--<owner>--<repo>/snapshots/*`), so "✓ CustomVoice" in the UI
means the checkpoint really is on disk.

No offline-pack `.zip` import exists for Qwen (the VieNeu "Nhập gói
ngoại tuyến" flow is backbone/codec-layout-specific); copy the `hub/`
tree instead.

## Platform limits

- **VRAM**: the 0.6B checkpoints need ~4 GB of VRAM on CUDA for
  comfortable synthesis. Below that, prefer CPU (slow) or stay on VieNeu.
- **CPU fallback is slow**: there is no real-time promise on CPU — the
  app says so in the runtime diagnostics rather than implying otherwise.
  Vietnamese synthesis stays on VieNeu (recommended engine for `vi`).
- **CUDA**: Qwen uses the PyTorch CUDA path (`torch.cuda.is_available()`),
  independent of the managed CUDA runtime card (which serves the VieNeu
  torch path). No separate Qwen CUDA bundle ships; use a CUDA-enabled
  torch from the `qwen` extra.
- **macOS**: torch CPU wheels work; expect slower-than-CUDA synthesis.
  The app is ad-hoc codesigned (no Developer ID/notarization) — same
  caveat as the base app.
- **Packaged builds**: the frozen CPU build excludes torch/transformers
  (torch-free VieNeu path preserved). Qwen inside a frozen build needs
  the `qwen` extra installed into the same environment (unfrozen
  deployment) — frozen-in Qwen weights are out of scope, same as the
  frozen-in VieNeu baseline (on-demand verified downloads by design).

## Engine behavior notes

- **CustomVoice**: 9 fixed speakers (Vivian … Sohee), 10 languages
  (en/zh/ja/ko/de/fr/ru/es/it/pt — **no Vietnamese**), optional
  natural-language style `instruct`. No cloning on this engine.
- **Base**: reference-audio cloning from a 3–8 s clip **plus its
  transcript** (`ref_text` is an official API requirement), per-voice
  consent, engine-isolated storage (`voices/qwen_base/`).
- One backend per synthesis job — no per-paragraph mixed-engine routing
  in this track. Audiobook chapter caches are engine-aware: switching
  engines re-renders stale chapters instead of replaying them.
