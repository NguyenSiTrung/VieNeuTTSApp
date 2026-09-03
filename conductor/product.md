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

## Success Measures (v1)
- All Section 7.1–7.4 acceptance criteria pass (text, file, cloning,
  settings flows).
- Installable signed/notarized artifacts per OS (`.dmg`, `.msi`/`.exe`,
  `.deb`/AppImage) with green CI.
- Streaming < 300 ms first-audio latency on CPU (direct-engine TTFC
  evidence; live preview is now opt-in — default is silent
  generate-then-replay); smooth progress and
  cancel for long jobs.

## Implementation Status (2026-09-04)

All seven v1 core features are implemented through Phase 4, the 2026-08-28
audiobook track (`audiobook_epub_20260828`), and the 2026-09-02/03
bead-driven batches (no tracks): 893 passed + 1 skipped in ~23 s (full
suite re-verified 2026-09-04; count rose from 740 on 2026-09-02 with the
job/artifacts/transport/model-manager/perf batch — 37 unit files + 5 smoke
modules. NOTE: one flaky ordering failure
(`TestRenderTelemetry::test_eta_completes_to_zero_on_last_segment`)
appeared in the full run but passes in isolation; treat as flaky until
re-characterized). Playback visualization shipped 2026-08-29
(bead-driven, no track): replay/chapter envelope overview with
click+drag-to-seek (`PlaybackWaveform.qml`), animated live meter with
peak-hold, and per-chapter waveform sidecars (`ch_XXXX.waveform.json`)
beside the cached WAVs. Also bead-driven: the 2026-08-29 tag-triggered
3-OS release pipeline (`.github/workflows/release.yml` — per OS: quality
gates → full pytest offscreen → PyInstaller frozen build → `--smoke`
binary verified by `scripts/check_smoke_wav.py` → zip/dmg artifacts; since
2026-09-02 fast per-push gates also run in `.github/workflows/ci.yml` —
ruff + full suite on ubuntu-22.04 + windows, the two non-dev platforms —
while the heavy PyInstaller build and real-synthesis smoke stay
tag-triggered), the 2026-08-31 hardening pass (non-stream infer RSS bounded
via segment dispatch, live-meter drain-window fix, wav+mp3 reference-clip
decode pinned in the Release pytest on all three OSes), and a README
rewrite with verified screenshots. 2026-09-02 (bead-driven, no track): a
configurable backbone model repo (Settings field → `TTSEngine` backbone
override, empty = official repo; `scripts/fetch_models.py --backbone
owner/repo` fetches and manifests a custom repo for offline use) and a
15-bead cross-platform perf/speed/UX batch — the per-push CI above; Linux
desktop integration shipped inside the release zip (`share/linux/`:
`.desktop` entry, hicolor icons, `install.sh`) with the GStreamer runtime
requirement documented; file import/export/EPUB-open and chapter
persistence moved off the GUI thread (`ui/bg_ops.py`,
`ui/chapter_persist.py`); Windows integration (`ui/windows.py` taskbar
AUMID + dark titlebar; `os.replace` retry for locked WAVs); and
startup/streaming perf work (deferred imports, lazy QML tabs,
post-first-paint engine prewarm, zero-copy chunk views, offset-based
stream IO device). 2026-09-03 releases v0.1.1–v0.1.5 (bead-driven, no
track; notes in `packaging/release-notes/`): selectable audiobook
transcript + one-tap chapter copy (9c7eeee); emotion-chip fix to the 3 tags
v3 Turbo supports (8adc267); guided model setup + job-queue routing +
`.srt` import + windowed-exe stdio fix (v0.1.3: d0d6298/6a17ad6/5e8af97/
184b600); artifact-first synthesis + bounded PCM transport + Windows
path/lock + `codec_dir` offline injection (v0.1.4); WSOLA reading speed +
voice audition + WASAPI restart-storm guard + silent default (v0.1.5:
e131631/dcbb58d/882fa17). `scripts/fetch_models.py` is now driven by
`core/official_model_manifest.py` constants. The historical real-model
CPU-int8 result was a 99–102 ms preloaded direct-engine first-chunk
observation, not audible or end-to-end first audio; production-path
evidence (incl. artifact-first/transport bounds) is tracked in
`docs/performance`. Remaining for v1: release hardening — weights install
on demand as a SHA-256-verified baseline (~330 MB, offline-pack import
supported) by design rather than frozen into the build; macOS is ad-hoc
codesigned only (no Developer ID/notarization), so the signed/notarized
success measure above is not yet met. See `PROJECT_PLAN.md` §0 and
`conductor/tracks.md`.
