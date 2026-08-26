# Spike Report — vieneu 3.3.0 (Phase 0)

Track: `phase01_core_20260827`. Validates the `vieneu==3.3.0` SDK against
PROJECT_PLAN.md §3 ground truth, §18 budgets, and §21 open questions.
This report is the confirmed API contract that Phase 1 codes against.

Status: **macOS validated end-to-end** (Windows/Ubuntu gaps recorded in §8).

## 0. Confirmed API contract for Phase 1

The exact surface `core/engine.py` wraps (all runtime-confirmed on
CPU/ONNX int8, macOS):

```python
from vieneu import Vieneu

tts = Vieneu(mode="v3turbo", backend="auto"|"onnx"|"torch",
             precision="int8"|"fp32",  # onnx only; int8→"onnx_int8", fp32→"onnx_update"
             backbone_repo=..., onnx_repo=..., onnx_dir=..., threads=0)
tts.sample_rate == 48_000;  tts.backend in {"onnx", "pytorch"}

voices = tts.list_preset_voices()      # [(label, voice_id)] × 20; voice_id is the infer key
info   = tts.get_preset_voice(name)    # {description, gender, style, speaker_emb, codes}

audio: np.ndarray = tts.infer(text, voice=voice_id|None, ref_audio=path|None,
                              temperature=0.4, top_k=50, show_progress=False)  # float32 mono 48 kHz
for chunk in tts.infer_stream(text, voice=voice_id, ...):  # float32 mono, 15 360..96 000 samples
    ...
wavs = tts.infer_batch(texts, voice=..., max_batch_size=4)  # List[np.ndarray]

wav44, sr44 = tts.denoise(clip_path, out_path=None, max_seconds=None)  # (float32 mono, 44_100)
name = tts.add_voice(name, ref_clip, *, denoise=True, save=False)      # save=True persists to JSON
tts.save_voices(path) / tts.remove_voice(name, save=...)               # voice-store management
tts.save(audio, "out.wav")                                             # 48 kHz WAV
tts.close()                                                            # also context manager
# torch-free install + backend="torch" → ModuleNotFoundError("No module named 'torch'")
```

## 1. Environment matrix (FR-0.1)

| | macOS (Apple Silicon) | Windows 10/11 | Ubuntu 22.04+ |
|---|---|---|---|
| CPU | Apple M4 | — | — |
| Python | 3.13.13 (uv-managed) | — | — |
| vieneu | 3.3.0 (py3-none-any wheel) | — | — |
| onnxruntime | 1.29.0 | — | — |
| numpy | 2.5.2 | — | — |
| soundfile | 0.14.0 | — | — |
| PySide6 | 6.11.2 | — | — |
| torch | **absent** (torch-free confirmed) | — | — |
| Install issues | none (uv, all wheels available) | — | — |

Notes:

- `requires-python >=3.10`; SDK classifiers 3.10–3.13 → the app pins
  `requires-python = ">=3.10,<3.14"`. System Homebrew Python 3.14 is **out of
  range**; use `uv venv --python 3.13`.
- Full dependency closure: 92 packages. Notable transitives: `librosa==1.0.0`,
  `sea-g2p==0.9.1`, `kaldi-native-fbank==1.22.3`, `tokenizers==0.23.1`,
  `huggingface-hub`, `perth==1.0.0` (watermark), `gradio==6.26.0` (pulled by
  vieneu for its `vieneu-web` entry point).
- `soundfile.available_formats()` includes **MP3 on macOS** → reference-clip
  mp3 decode works via libsndfile ≥1.1 without ffmpeg (cross-OS risk remains
  for Windows/Linux libsndfile builds; revisit at packaging).

## 2. API contract (FR-0.2, FR-0.3) — source-reading findings

Confirmed by reading the installed SDK source (runtime confirmation in §3):

- `from vieneu import Vieneu` — `Vieneu` is a **factory function**, not a
  class: `Vieneu(mode="v3turbo", **kwargs) -> BaseVieneuTTS` (default
  `mode="v3turbo"`). **Plan §3's `Vieneu(backend=..., precision=...)` works**
  because kwargs pass through to `V3TurboVieNeuTTS.__init__`.
- `V3TurboVieNeuTTS.__init__` accepts `backend` ("auto"|"onnx"|"torch"),
  `precision` ("int8"|"fp32" — ONNX-only; maps to `onnx_int8` / `onnx_update`
  subfolders), `device`, `onnx_subfolder`, `max_batch_size`, and model repo
  overrides. With `backend="auto"`: CUDA if `torch.cuda.is_available()` else
  CPU/ONNX. `sample_rate = 48_000`.
- `infer(text, voice=None, ref_audio=..., ref_codes=None, temperature=0.4,
  top_k=50, max_chars=256, skip_normalize=False, skip_phonemize=False,
  show_progress=True, apply_watermark=True, **kwargs) -> np.ndarray`.
  **Temperature IS exposed** (default 0.4) — resolves §21 settings question.
- `infer_batch(texts, voice=..., temperature=0.4, top_k=50, max_batch_size=4,
  ...) -> List[np.ndarray]` (GPU: static batching up to 32; CPU: sequential).
- `infer_stream(text, voice=..., temperature=..., top_k=..., max_chars=256)
  -> Generator[np.ndarray]` — chunk format measured in §4.
- `list_preset_voices() -> List[tuple[str, str]]`; voice registry also exposes
  `get_preset_voice(name) -> dict` with `description`/`gender`/`style`
  fields (useful for North/Central/South grouping).
- `add_voice(name, clip_path?, ..., save=True/False)` + `save_voices(path)`
  + `remove_voice(name)` — cloned voices **persist to a JSON file**
  (`assets/voices_v3_turbo.json` with speaker embedding + reference codes)
  → cloning persistence across restarts is supported (verify at runtime, §3).
- `save(audio, output_path)`, `denoise(...)` (inherited from BaseVieneuTTS /
  turbo class), `encode_reference(ref_audio_path)`, context-manager
  (`__enter__`/`__exit__`/`close()`).
- Output: `np.float32` array, `sample_rate == 48000`, mono (verify §3).

## 3. Runtime synthesis contract (macOS, Apple M4, CPU/ONNX int8) — CONFIRMED

All 16 checks pass (`scripts/spike/phase0_contract.py --with-cloning`):

- **Init:** warm (weights cached) `Vieneu(backend="onnx", precision="int8")` =
  **5.2 s**; first-ever init incl. HF download (240 MB) = 35.6 s. `backend`
  resolves to `"onnx"`; `sample_rate == 48000`.
- **Voices (FR-0.2):** `list_preset_voices()` returns **20** `(label, voice_id)`
  tuples — `voice_id` (2nd element) is what `infer(voice=...)` accepts. Labels
  encode grouping: `"<name> — <gender> · <region> · <style>"` where region ∈
  {Bắc, Trung, Nam} → North/Central/South grouping parses from the label or
  `get_preset_voice(name)["description"]`. `Adam` is voice #20. IDs: Minh Đức,
  Phạm Tuyên, Thái Sơn, Xuân Vĩnh, Thanh Bình, Trúc Ly, Ngọc Linh, Đoan Trang,
  Mai Anh, Thục Đoan, Minh Triết, Thùy Dung, Quang Sơn, Ngọc Trân, Mỹ Duyên,
  Quỳnh Anh, Đức Trí, Kim Thanh, Ngọc Huyền, Adam.
- **infer() (FR-0.2):** returns `np.float32`, **1-D mono**, 48 kHz, duration
  > 0, non-silent for both vi and en. M4 warm timings: vi 2.40 s audio in
  1.41 s (RTF 0.59); en 2.96 s audio in 0.34 s (RTF 0.12).
- **save() (FR-0.2):** writes WAV readable by soundfile at exactly 48 000 Hz.
- **denoise() (FR-0.2):** `denoise(ref, out_path=..., max_seconds=...)` →
  `(np.float32 mono, 44100)`. **The denoiser works at 44.1 kHz, not 48 kHz.**
- **Cloning (FR-0.2):** `add_voice(name, ref_clip, denoise=True, save=True)`
  enrolls from a synthesized 5.4 s ref; `infer(voice=name)` produces non-silent
  speech; `save=True` **persists across process restart** (voices JSON inside
  `vieneu/assets/voices_v3_turbo.json` in site-packages).
  `remove_voice(name, save=True)` removes it again.
  ⚠️ Persistence writes into **site-packages by default** — the packaged app
  must pass an explicit user-writable path (`save_voices(path=...)`) or own
  the voices file in the app data dir (Phase 3 concern).
- **Channels: mono** (ndim == 1) — resolves §21 mono/stereo question.

## 4. infer_stream chunk format & latency (FR-0.3) — CONFIRMED

`scripts/spike/phase0_stream.py` (~330-char vi paragraph, voice "Adam", M4):

- **Chunk format:** each yielded chunk is `np.float32`, **1-D mono**, variable
  length — observed 15 360..96 000 samples (0.32 s..2.0 s @ 48 kHz; multiples
  of 15 360). 12 chunks → 14.08 s audio.
- **First-chunk latency: 0.153 s** (§18 budget ~300 ms — **pass**).
- **Streaming RTF: 0.13** (budget < 1 — **pass**); total 14.08 s audio in
  1.86 s wall.
- Streaming is **not** ONNX-only in SDK 3.3.0: both the PyTorch and ONNX
  engines expose `infer_stream` (per SDK source; plan §3 said ONNX-only —
  corrected). CPU/ONNX path is what this app uses regardless.
- `infer_stream` accepts `temperature/top_k/top_p/max_new_frames/
  repetition_penalty/repetition_window/max_chars/apply_watermark` and yields
  per-frame sub-chunks (native engine streaming preferred over chunk-level
  fallback).
- `Vieneu(backend="torch")` on a torch-free install raises
  `ModuleNotFoundError: No module named 'torch'` — `engine.py` must catch this
  and surface an actionable "CUDA stack not installed" message (plan §11).

## 5. Performance & memory vs §18 budgets (FR-0.4) — measured, Apple M4

`scripts/spike/phase0_perf.py` + `scripts/spike/phase0_rss_current.py`:

| Metric | Measured | Budget | Verdict |
|---|---|---|---|
| Cold start (fresh process, cached weights) | 5.17 s | < 15 s | ✅ |
| Warm short-text latency (best of 3) | 0.13 s (1.2 s audio) | < 1 s | ✅ |
| Long text (1 953 chars → 94.3 s audio) | 12.85 s, RTF 0.14 | RTF < 1 | ✅ |
| Peak RSS after init | 710 MB | < 2 048 MB | ✅ |
| RSS after short synthesis | ~766 MB | < 2 048 MB | ✅ |
| RSS during/after long synthesis | **~2.5 GB, plateaus** | < 2 048 MB | ❌ long workloads only |

Memory attribution (current RSS, not just peak): interpreter 29 MB → init
710 MB → short infer 766 MB → first long infer (~600 chars) **2 468 MB** →
repeated long inferences plateau at ~2 558 MB; short infer afterwards adds
nothing. Conclusion: **ONNX Runtime arena growth, not a leak** — the arena
expands with the largest workload and is never returned to the OS. The §18
"< 2 GB" budget holds for the interactive v1 use case but **is exceeded by
long-document synthesis on CPU/ONNX int8** (follow-up filed: investigate
per-sentence app-level chunking, ORT arena options via SDK, or revise the
budget for long workloads).

## 6. Offline bundling (FR-0.5) — CONFIRMED

`scripts/spike/phase0_offline.py` — all three strategies pass (real synthesis
with `HF_HUB_OFFLINE=1`, no network):

- **A. Pre-seeded user HF cache + `HF_HUB_OFFLINE=1`:** works.
- **B. Portable `HF_HOME=<bundled cache copy>` + `HF_HUB_OFFLINE=1`:** works —
  the recommended packaging model (self-contained, no site-packages writes).
- **C. Local model dir:** `Vieneu(backbone_repo=<dir>, onnx_dir=<dir>/onnx_int8)`
  loads backbone/config/denoiser/speaker_encoder from the dir; the **codec repo
  is not exposable as a local dir** through the public API (always resolved via
  the HF cache), so a fully dir-based bundle is not possible without the SDK.

**Minimal CPU int8 bundle (16 files, ~327 MB)** — `scripts/fetch_models.py`
downloads exactly this set and writes/verifies a SHA256 `manifest.json`:

- Backbone `pnnbao-ump/VieNeu-TTS-v3-Turbo` (~236 MB): `config.json`,
  `denoiser.onnx`, `speaker_encoder.onnx`, `onnx_int8/{config.json,
  tokenizer.json, vieneu_acoustic_cached.onnx, vieneu_backbone_shared.data,
  vieneu_prefill.onnx, vieneu_decode_step.onnx, vieneu_v3_heads.npz}`
- Codec `OpenMOSS-Team/MOSS-Audio-Tokenizer-Nano-ONNX` (~86 MB, 6 files).
  ⚠️ Plan §3's "~240 MB CPU int8" estimate **excluded the codec repo**;
  true minimal bundle ≈ 327 MB (still within the §18 installer budget once
  compressed).
- fp32 graphs live in `onnx_update/` (not `onnx/` as plan §3 stated) — fetch
  via `--precision fp32` if the Settings precision switch ships.

## 7. Open questions resolved (FR-0.6, §21) — FINAL

| # | Question | Resolution |
|---|---|---|
| §21-5 | PDF import library: PyMuPDF (AGPL) vs pypdf (MIT) | **`pypdf>=6` (MIT)** — we distribute the app; AGPL PyMuPDF would force source-disclosure obligations. pypdf is pure-Python, fine for text-layer PDFs (scanned PDFs need OCR — out of scope either way). Dependency swapped in `pyproject.toml`; venv verified AGPL-free. Revisit PyMuPDF only if extraction quality demands it. |
| §21-6 | mp3 reference-clip decode | **Works on macOS** via libsndfile MP3 (soundfile 0.14, no ffmpeg). Cross-OS libsndfile-mp3 support (Windows/Linux, PyInstaller bundling) remains a packaging risk — follow-up bead filed; validate in Phase 5. |
| §9 | SDK temperature param | **Yes** — `infer(temperature=0.4, top_k=50)`, `infer_stream(temperature=0.8, top_k=25, top_p=0.95, ...)`. Settings gets a temperature field. |
| §10 | mono vs stereo output | **Mono** — `infer` returns 1-D `np.float32` @ 48 kHz. |
| §7.3 | cloned-voice persistence across restarts | **Supported** — `add_voice(..., save=True)` → JSON, survives process restart. Default write path is inside site-packages; the app must own the path (`save_voices(path=<app data dir>)`) — Phase 3. |
| §4 | `Vieneu` thread-safety | Engine has an internal `RLock` (ONNX), but the plan's conservative assumption stands: **single worker owns the instance; requests serialized** (NFR-2). Not relaxed. |
| §21-1 | QML vs Widgets | Not blocking Phases 0–1; decide at Phase 2 (team QML comfort). |
| §21-2/3/4 | Commercial vs OSS, branding, installer size | Not blocking Phases 0–1; decide before Phase 5 packaging. Installer budget: CPU-only default (~327 MB weights, §6). |

## 8. Cross-OS validation (FR-0.7 / AC-1) — partial, gaps recorded

- **macOS (Apple M4, arm64): fully confirmed** (§3–§6). All contract checks
  and budget measurements above were run on this machine.
- **Ubuntu 22.04 (linux/arm64 via Docker): NOT run.** The validation script
  `scripts/spike/phase0_ubuntu_docker.sh` is committed and ready (python3.10
  + vieneu + offline synthesis + WAV read-back), but Docker image pulls hang
  on this machine (daemon networking broken). **Recorded gap — does not block
  the track** per plan Task 7; re-run the script on a machine with working
  Docker or cover in Phase 6 CI.
- **Windows 10/11 (x64): NOT run.** No Windows environment available.
  Recorded gap; Phase 6 CI (`windows-latest`) covers install + headless smoke.
- Environment matrix (§1) to be extended when those runs land.
