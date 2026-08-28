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
   voices grouped North/Central/South.
2. **File/paragraph import** — `.txt`, `.md`, `.docx`, `.pdf` with
   auto-chunking, live progress, and cancel.
3. **Instant voice cloning** — enroll a voice from a 3–8 s reference
   clip (with consent notice), reuse by name.
4. **Streaming playback + WAV export** — incremental playback starting
   in ~300 ms; export 48 kHz WAV.
5. **Auto engine detection** — CPU/ONNX vs NVIDIA/CUDA + workload
   heuristic, with manual override in Settings.
6. **Audiobook studio (EPUB)** — import DRM-free `.epub`, chapter-aware
   render with per-chapter WAV cache, continuous listening
   (pause/seek/auto-advance + pipelined pre-render of the next chapter),
   resume across sessions, ordered chapter export.

## Success Measures (v1)
- All Section 7.1–7.4 acceptance criteria pass (text, file, cloning,
  settings flows).
- Installable signed/notarized artifacts per OS (`.dmg`, `.msi`/`.exe`,
  `.deb`/AppImage) with green CI.
- Streaming < 300 ms first-audio latency on CPU; smooth progress and
  cancel for long jobs.

## Implementation Status (2026-08-27)

All five v1 core features are implemented through Phase 4 (478 tests
green at the time); the 2026-08-28 audiobook track (`audiobook_epub_20260828`)
added EPUB audiobook support (core features item 6) — 592+ tests green; real-model first-audio latency measured at 99–102 ms on CPU int8
(≈3× inside the ~300 ms target) and long-doc streaming RSS held at
1120 MB (< 2 GB budget). Remaining for v1: packaging & offline bundling
(Phase 5) and hardening/release (Phase 6) — the signed/notarized
artifacts success measure above is not yet met. See `PROJECT_PLAN.md` §0
and `conductor/tracks.md`.
