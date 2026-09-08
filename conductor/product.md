# VieNeuTTS Desktop App — Product Guide

## Initial Concept
Cross-platform on-device Vietnamese/English TTS desktop app powered by
VieNeu-TTS v3 Turbo. Fully offline and on-device: no cloud, no network
after install. Single-process PySide6 + QML app with a worker thread
owning the `vieneu` SDK instance.

## Product Vision
Make high-quality Vietnamese/English text-to-speech available as a
fast, private, native desktop tool on macOS, Windows, and Ubuntu.
The app auto-detects the best engine (CPU/ONNX vs NVIDIA/CUDA), surfaces
it in the UI, and keeps 48 kHz synthesis off the UI thread — so the user
gets streaming playback in ~300 ms and can synthesize anything from a
short snippet to a full document, fully offline.

## Target Users
- **Vietnamese content creators** — YouTubers, podcasters, voiceover
  producers who need natural vi/en narration without a studio or cloud
  API.
- **Accessibility readers** — users who need reliable on-device
  text-to-speech for reading documents aloud.

## Core Features (v1)
1. **Free text TTS** — multiline vi/en code-switching input, 20 preset
   voices grouped North/Central/South, with per-preset audition
   (v0.1.5: `auditionVoice` slot + redesigned VoicePicker with regional
   filter, gender/style badges, search).
2. **File/paragraph import** — `.txt`, `.md`, `.docx`, `.pdf`, `.srt` with
   auto-chunking, live progress, and cancel. Subtitles import as clean spoken
   text by default, with a checkbox to keep the original timecodes.
3. **Instant voice cloning** — enroll a voice from a 3–8 s reference
   clip (with consent notice), reuse by name.
4. **Silent generate-then-replay + optional live preview + WAV export** —
   default path synthesizes incrementally to an on-disk artifact
   (`<job>.part.wav` → atomic promote, artifact-first since v0.1.4) then
   auto-replays; a Settings toggle enables live preview through a bounded
   PCM transport (2 s / 384 KB cap, 150 ms prebuffer). Export 48 kHz WAV.
   Reading speed 0.5–2.0× (NumPy WSOLA) + inter-paragraph pause 0–2.0 s
   (v0.1.5).
5. **Auto engine detection** — CPU/ONNX vs NVIDIA/CUDA + workload
   heuristic, with manual override in Settings. Startup/resource profiles:
   post-first-paint init, Performance/Auto/Efficiency ONNX thread profiles.
6. **Audiobook studio (EPUB)** — import DRM-free `.epub`, chapter-aware
   render with per-chapter WAV cache, continuous listening
   (pause/seek/auto-advance + pipelined pre-render of the next chapter),
   resume across sessions, ordered chapter export, selectable transcript
   with tap-to-seek + one-tap chapter copy (v0.1.1).
7. **Guided model setup (v0.1.3)** — first-run setup card
   (Download/Cancel/Retry + stored/needed readout), SHA-256-pinned official
   CPU baseline (~330 MB backbone + codec, atomic promote, resume,
   free-space preflight), offline-pack import + model-folder copy/open in
   Settings. Windowed `.exe` stdio→devnull so packaged builds can
   download + synthesize.
8. **Batch synthesis queue (v0.1.7)** — multi-file `.txt`/`.md`/`.docx`/
   `.pdf`/`.srt` queue on the Paragraph tab with off-thread import,
   sequential auto-run, per-file auto-export, and failure-continue
   resilience.
9. **In-app updates + crash diagnostics (v0.1.6/v0.1.8)** — background
   GitHub-Releases update check with platform-aware assets (stdlib only,
   zero telemetry), frozen version stamping + `--version`; structured
   `crash.log` diagnostics with Windows native-dialog fallback.
10. **GPU delivery + MP3 export (v0.1.9–v0.1.12)** — CPU-only downloads
    with opt-in app-managed CUDA runtime (verified per-user PyTorch
    install, v0.1.11; supersedes the short-lived `-cuda` bundles of
    v0.1.9); user-choosable MP3 export alongside WAV (libsndfile, no
    ffmpeg) with remembered Settings choice.
11. **Multilingual multi-engine (2026-09-08 track, archived)** —
    selectable VieNeu / Qwen3-TTS CustomVoice / Qwen3-TTS Base backends
    behind one `TtsBackend` contract with 48 kHz normalization,
    engine-aware audiobook cache, CJK-aware segmentation, and an optional
    `qwen` install extra. VieNeu stays the Vietnamese default.

## Success Measures (v1)
- All Section 7.1–7.4 acceptance criteria pass (text, file, cloning,
  settings flows).
- Installable signed/notarized artifacts per OS (`.dmg`, `.msi`/`.exe`,
  `.deb`/AppImage) with green CI.
- Streaming < 300 ms first-audio latency on CPU (direct-engine TTFC
  evidence; live preview is now opt-in — default is silent
  generate-then-replay); smooth progress and
  cancel for long jobs.

## Implementation Status (2026-09-08)

All seven v1 core features are implemented, plus six bead-driven releases
(v0.1.6–v0.1.12, notes in `packaging/release-notes/`) and the
2026-09-08 Qwen multi-engine track (archived →
`conductor/archive/qwen-multiengine_20260908/`):

- **v0.1.6** — in-app update checks via GitHub Releases API (stdlib
  `urllib`, zero telemetry, platform-aware assets, Settings card + badge),
  frozen version stamping + CLI `--version`.
- **v0.1.7** — multi-file batch synthesis queue (BatchFileController +
  BatchQueueCard: off-thread import, sequential auto-run, per-file
  auto-export, failure-continue) + instant voice switching and
  cancellation hardening.
- **v0.1.8** — crash diagnostics (`crash.py`: structured `crash.log` +
  Windows native dialog), cross-platform path normalization (`paths.py`),
  Windows file-lock resilience (retry-bounded atomic replace + sibling
  fallback).
- **v0.1.9** — NVIDIA CUDA `-cuda` bundles (Windows/Linux) + truthful
  backend selection (`torchAvailable` probe, frozen-aware errors).
- **v0.1.10** — Windows export-cleanup repair that unblocked the v0.1.9
  artifacts.
- **v0.1.11** — managed CUDA runtime (opt-in verified per-user PyTorch
  install from Settings; downloads CPU-only again by design).
- **v0.1.12** — user-choosable MP3 export alongside WAV (libsndfile, choice
  remembered) + Windows crash-hardening batch + repaired CUDA activation +
  OS-aware driver upgrade guide.
- **Qwen multi-engine** — selectable VieNeu / Qwen3-TTS CustomVoice /
  Qwen3-TTS Base behind one `TtsBackend` contract (48 kHz normalization,
  engine-aware audiobook cache, CJK-aware segmentation, `qwen` install
  extra, `docs/qwen-setup.md` + `scripts/fetch_qwen_models.py`).

Test suite: 1106 items collected 2026-09-08 (was 893 passed + 1 skipped on
2026-09-04; micro-test consolidation 1029 → 861 on 2026-09-07, then
batch/CUDA/Qwen suites regrew it; full-suite gates stayed green through
the Qwen phases at 937 → 982 → 1022 passed + 15 skipped). The historical
real-model CPU-int8 result was a 99–102 ms preloaded direct-engine
first-chunk observation, not audible or end-to-end first audio;
production-path evidence (incl. artifact-first/transport bounds) is
tracked in `docs/performance`. Remaining for v1: release hardening —
weights install on demand as a SHA-256-verified baseline (~330 MB,
offline-pack import supported) by design rather than frozen into the
build; macOS is ad-hoc codesigned only (no Developer ID/notarization),
so the signed/notarized success measure above is not yet met. See
`PROJECT_PLAN.md` §0 and `conductor/tracks.md`.
