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
4. **Silent generate-then-replay + optional live preview + WAV/MP3 export**
   — default path synthesizes incrementally to an on-disk artifact
   (`<job>.part.wav` → atomic promote, artifact-first since v0.1.4) then
   auto-replays; a Settings toggle enables live preview through a bounded
   PCM transport (2 s / 384 KB cap, 150 ms prebuffer). Export 48 kHz WAV,
   or MP3 alongside WAV since v0.1.12 (`exportFormat` setting, remembered;
   Save dialogs honor it since v0.1.13). Reading speed 0.5–2.0× (NumPy
   WSOLA) + inter-paragraph pause 0–2.0 s (v0.1.5).
5. **Auto engine detection** — CPU/ONNX vs NVIDIA/CUDA + workload
   heuristic, with manual override in Settings. Truthful backend selection
   (v0.1.9: frozen builds explain CUDA-unavailable instead of suggesting
   pip). App-managed CUDA runtime (v0.1.11, opt-in Settings download,
   verified wheels, offline after install; briefly v0.1.9 shipped `-cuda`
   bundles, withdrawn). Startup/resource profiles: post-first-paint init,
   Performance/Auto/Efficiency ONNX thread profiles.
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
8. **Batch synthesis queue (v0.1.7)** — multi-file drop onto Paragraph tab,
   off-thread import, sequential auto-run with per-file auto-export and
   failure-continue; instant voice-switch audition cut + race-free
   cancellation with live queued/cancelling UI states.
9. **Mini Audio Studio (v0.1.13)** — post-synthesis editing tab
   (`StudioTab.qml` + `core/studio.py`): non-destructive gain/fade/speed/
   gap/normalize/trim op stack, live waveform + playhead, clip manager with
   reorder, per-segment voice-switch re-synthesis (10 ms crossfade);
   feeder "Studio…" buttons on Text/Paragraph/Audiobook tabs.
10. **Platform hardening (v0.1.6–v0.1.12)** — in-app update checks
   (platform-aware, background recheck + Settings badge); crash reporter
   (`crash.py` → `logs/crash.log` + native Windows dialog); cross-platform
   path normalization + BOM/newline-safe import; Windows reliability
   (Qt audio plugins shipped, int16 fallback, WinError-32 retries, no
   console flash, long-path/AV handling); OS-aware CUDA driver guide.

## Success Measures (v1)
- All Section 7.1–7.4 acceptance criteria pass (text, file, cloning,
  settings flows).
- Installable signed/notarized artifacts per OS (`.dmg`, `.msi`/`.exe`,
  `.deb`/AppImage) with green CI.
- Streaming < 300 ms first-audio latency on CPU (direct-engine TTFC
  evidence; live preview is now opt-in — default is silent
  generate-then-replay); smooth progress and
  cancel for long jobs.

## Implementation Status (2026-09-10)

All ten v1 feature areas above are implemented: Phases 1–4, the 2026-08-28
audiobook track (`audiobook_epub_20260828`), and bead-driven batches with
no tracks. Current app version 0.1.14; curated notes in
`packaging/release-notes/v0.1.1.md`–`v0.1.14.md`. Test suite consolidated
2026-09-06…10 (same-function micro-tests merged, 1029 → 861 items;
benchmarks excluded by default via `-m 'not benchmark'`).
Prior verified counts: 893 passed + 1 skipped in ~23 s (2026-09-04; 37
unit files + 5 smoke modules; one flaky ordering failure
`TestRenderTelemetry::test_eta_completes_to_zero_on_last_segment` passes
in isolation). Playback visualization shipped 2026-08-29 (bead-driven, no
track): replay/chapter envelope overview with click+drag-to-seek
(`PlaybackWaveform.qml`), animated live meter with peak-hold, and
per-chapter waveform sidecars (`ch_XXXX.waveform.json`). Release pipeline:
tag-triggered 3-OS builds (`.github/workflows/release.yml`), per-push
gates (`.github/workflows/ci.yml`: ruff + full suite on ubuntu-22.04 +
windows), manual CUDA-runtime spike
(`.github/workflows/cuda-runtime-spike.yml`). Production-path evidence
(incl. artifact-first/transport bounds) is tracked in `docs/performance`.
Remaining for v1: release hardening — weights install on demand as a
SHA-256-verified baseline (~330 MB, offline-pack import supported) by
design rather than frozen into the build; macOS is ad-hoc codesigned only
(no Developer ID/notarization), so the signed/notarized success measure
above is not yet met. See `PROJECT_PLAN.md` §0 and `conductor/tracks.md`.

<!-- refreshed 2026-09-10: features 7 → 10 (batch queue, studio, hardening); status rolled to v0.1.14 -->
