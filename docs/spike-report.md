# Spike Report — vieneu 3.3.0 (Phase 0)

Track: `phase01_core_20260827`. Validates the `vieneu==3.3.0` SDK against
PROJECT_PLAN.md §3 ground truth, §18 budgets, and §21 open questions.
This report is the confirmed API contract that Phase 1 codes against.

Status: **in progress** (macOS validated first; Windows/Ubuntu per Task 7).

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

## 3. Runtime synthesis contract (macOS)

<!-- Task 2: infer() dtype/rate/duration, voices ≥ 20 + grouping fields,
     add_voice round-trip + persistence across restart, save() WAV,
     denoise() round-trip -->

_Pending Task 2._

## 4. infer_stream chunk format & latency (FR-0.3)

<!-- Task 3: chunk dtype/shape/channels, first-chunk latency, RTF,
     torch-backend behavior -->

_Pending Task 3._

## 5. Performance & memory vs §18 budgets (FR-0.4)

<!-- Task 4: cold start, warm latency, long-text RTF, RSS -->

_Pending Task 4._

## 6. Offline bundling (FR-0.5)

<!-- Task 5: local model path, HF_HUB_OFFLINE=1, minimal CPU int8 bundle -->

_Pending Task 5._

## 7. Open questions resolved (FR-0.6, §21)

| # | Question | Status |
|---|---|---|
| 1 | PDF library: PyMuPDF (AGPL) vs pypdf (MIT) | pending Task 6 |
| 2 | mp3 reference-clip decode | **works on macOS** via libsndfile MP3 (soundfile 0.14); Win/Linux TBD |
| 3 | SDK temperature param | **yes** — `infer(temperature=0.4, top_k=50)` |
| 4 | mono vs stereo | pending Task 2 (expected mono) |
| 5 | cloned-voice persistence | **supported** — `save_voices()` JSON; verify Task 2 |
| 6 | thread-safety assumption | pending Task 2 (treat as not thread-safe) |

## 8. Cross-OS validation (FR-0.7 / AC-1)

_Pending Task 7._
