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
2. **File/paragraph import** — `.txt`, `.md`, `.docx`, `.pdf`, `.srt` with
   auto-chunking, live progress, and cancel. Subtitles import as clean spoken
   text by default, with a checkbox to keep the original timecodes.
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

## Implementation Status (2026-09-02)

All six v1 core features are implemented through Phase 4 and the 2026-08-28
audiobook track (`audiobook_epub_20260828`), with 740 tests green (708 unit
+ 32 smoke incl. the e2e flows; verified 2026-09-02, 740 passed in ~20 s —
count rose from 684 with the settings-repo and perf/UX batch coverage,
after the earlier drops from 719 via shared-subprocess smoke drivers and
from 705 via the 2026-08-31 dedup/merge pass).
Playback visualization shipped 2026-08-29 (bead-driven, no track):
replay/chapter envelope overview with click+drag-to-seek
(`PlaybackWaveform.qml`), animated live meter with peak-hold, and
per-chapter waveform sidecars (`ch_XXXX.waveform.json`) beside the cached
WAVs. Also bead-driven: the 2026-08-29 tag-triggered 3-OS release
pipeline (`.github/workflows/release.yml` — per OS: quality gates → full
pytest offscreen → PyInstaller frozen build → `--smoke` binary verified by
`scripts/check_smoke_wav.py` → zip/dmg artifacts; since 2026-09-02 fast
per-push gates also run in `.github/workflows/ci.yml` — ruff + full suite
on ubuntu-22.04 + windows, the two non-dev platforms — while the heavy
PyInstaller build and real-synthesis smoke stay tag-triggered), the
2026-08-31 hardening pass (non-stream infer RSS bounded via segment
dispatch, live-meter drain-window fix, wav+mp3 reference-clip decode pinned
in the Release pytest on all three OSes), and a README rewrite with
verified screenshots. 2026-09-02 (bead-driven, no track): a configurable
backbone model repo (Settings field → `TTSEngine` backbone override, empty
= official repo; `scripts/fetch_models.py --backbone owner/repo` fetches
and manifests a custom repo for offline use) and a 15-bead cross-platform
perf/speed/UX batch — the per-push CI above; Linux desktop integration
shipped inside the release zip (`share/linux/`: `.desktop` entry, hicolor
icons, `install.sh`) with the GStreamer runtime requirement documented;
file import/export/EPUB-open and chapter persistence moved off the GUI
thread (`ui/bg_ops.py`, `ui/chapter_persist.py`); Windows integration
(`ui/windows.py` taskbar AUMID + dark titlebar; `os.replace` retry for
locked WAVs); and startup/streaming perf work (deferred imports, lazy QML
tabs, post-first-paint engine prewarm, zero-copy chunk views, offset-based
stream IO device). The historical real-model CPU-int8
result was a 99–102 ms preloaded direct-engine first-chunk observation,
not audible or end-to-end first audio; production-path evidence is
tracked in `docs/performance`. Remaining for v1: offline model bundling
into the frozen build and release hardening — installable artifacts now
exist, but macOS is ad-hoc codesigned only (no Developer ID/notarization),
so the signed/notarized success measure above is not yet met. See
`PROJECT_PLAN.md` §0 and `conductor/tracks.md`.
