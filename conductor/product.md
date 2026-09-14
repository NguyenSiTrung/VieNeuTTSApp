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
   text by default, with a checkbox to keep the original timecodes. Since
   v0.1.14 the Paragraph tab is a segmented "One document | Multiple files |
   Subtitles (SRT)" composition with a docked synthesis bar (voice, generate,
   play, export, run-all) that never scrolls off-screen; mode switches keep the
   page scrolled in place.
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
   Performance/Auto/Efficiency ONNX thread profiles. Settings presents these
   as separate Engine, Managed CUDA runtime, and Model source cards
   (v0.1.14), with verbose CUDA guidance behind an expand-on-demand
   diagnostics disclosure.
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
9. **Audio Studio (v0.1.13 → pro-audio deck v0.1.14)** — post-synthesis
   editing tab (`StudioTab.qml` + `core/studio.py`): non-destructive
   gain/fade/speed/gap/normalize/trim op stack, live waveform + playhead,
   clip manager with reorder, per-segment voice-switch re-synthesis (10 ms
   crossfade); feeder "Studio…" buttons on Text/Paragraph/Audiobook tabs.
   v0.1.14 adds a 72 px master waveform deck with peak/RMS/dynamic-range
   telemetry, a grouped FX rack (Dynamics / Tempo & Cadence / Transitions),
   an op-stack timeline with contextual undo/reset, interactive pause +
   click-drag seek on the shared player, per-clip audition and in-dialog
   transcript editing, off-GUI-thread render/export behind `studioBusy` with
   stale-render guards, and full vi/en localization.
10. **Platform hardening (v0.1.6–v0.1.14)** — in-app update checks
   (platform-aware, background recheck + Settings badge); crash reporter
   (`crash.py` → `logs/crash.log` + native Windows dialog); cross-platform
   path normalization + BOM/newline-safe import; Windows reliability
   (Qt audio plugins shipped, int16 fallback, WinError-32 retries, no
   console flash, long-path/AV handling); OS-aware CUDA driver guide;
   64-bit QML-facing byte counts (the multi-GB CUDA manifest no longer
   raises `OverflowError` on first Settings visit) and deterministic QML
   tear down at app exit (v0.1.14).
11. **Subtitle (SRT) dub/transcript studio** — third Paragraph-tab mode
    ("Phụ đề (SRT)", `SubtitleCard.qml` + `ui/subtitle_controller.py`,
    registered as the `subtitleController` context property) that turns an
    `.srt` file into a cue-aligned audio track and a matching subtitle file.
    Cues parse through a pure cue model (`core/subtitles.py`: styling and
    `{\an8}`-style override blocks dropped, tolerant `HH:MM:SS,mmm` stamps),
    then synthesize through the same single worker/model via the
    synthesis-listener seam — no second model load. Two fit policies
    (`core/align.py`): **dub** keeps the SRT clock as master (compress an
    overlong take up to a 1.0–2.0× rate cap, push later cues on overflow) and
    **transcript** keeps the voice as master (never compress; reproduce the
    subtitle's pauses capped at a max-gap setting). Optional sentence merging
    groups cues into sentence-sized synthesis units, an offset field shifts
    the whole track, and the render streams clip-by-clip straight to a
    `PCM_16` `track.wav` (a two-hour film never sits in RAM). Plays with
    active-cue highlight + tap-to-seek from the *measured* timeline, and exports
    both the aligned WAV and a retimed SRT to mux back onto the video. A render
    fingerprint (`core/subtitle_project.py`) caches the track per workspace, so
    unchanged settings reuse the rendered `track.wav` instead of re-synthesizing.

## Success Measures (v1)
- All Section 7.1–7.4 acceptance criteria pass (text, file, cloning,
  settings flows).
- Installable signed/notarized artifacts per OS (`.dmg`, `.msi`/`.exe`,
  `.deb`/AppImage) with green CI.
- Streaming < 300 ms first-audio latency on CPU (direct-engine TTFC
  evidence; live preview is now opt-in — default is silent
  generate-then-replay); smooth progress and
  cancel for long jobs.

## Implementation Status (2026-09-14)

All eleven v1 feature areas above are implemented: Phases 1–4, the 2026-08-28
audiobook track (`audiobook_epub_20260828`), and bead-driven batches with
no tracks. Current app version 0.1.14 (SRT subtitle studio is on `main`,
unreleased); curated notes in
`packaging/release-notes/v0.1.1.md`–`v0.1.14.md`. Test suite grew with the SRT
studio to 1036 items collected / 1024 selected (12 benchmarks deselected via
`-m 'not benchmark'`). Latest run (2026-09-14): `ruff check .` passes, but
`ruff format --check .` is **RED** — 7 files would be reformatted, all
SRT-studio-touched (`core/subtitles.py`, `ui/subtitle_controller.py`,
`tests/unit/test_{subtitles,subtitle_project,subtitle_controller,importers}.py`,
`tests/smoke/test_ui_tabs.py`); `pytest` gave 1022 passed + 1 skipped, plus one
intermittent under `-n auto`
(`TestStreamLifecycleSmoke::test_stream_bindings_e2e_cancel_cross_tab_and_error_recovery`
— the `stream_cancel` `no_audio_retained` assert can flake on a loaded 14-worker
run; passes in isolation). Prior verified gate: 859 passed + 1 skipped
(2026-09-10, commit `3eb6c90`); before that 893 passed + 1 skipped in ~23 s
(2026-09-04; 37 unit files + 5 smoke modules; one flaky ordering failure
`TestRenderTelemetry::test_eta_completes_to_zero_on_last_segment` passes
in isolation). Known host-specific failures on this Linux workstation —
`run_gui` CUDA-inspection deferral tests, tracked as beads `VieNeuTTSApp-b0t`
and `VieNeuTTSApp-o98` (fail on a clean tree too). Since the 08-28 tracks,
further work is bead-driven with no conductor track: SRT subtitle
dub/transcript studio (`core/subtitles.py` + `core/align.py` +
`core/subtitle_project.py` + `ui/subtitle_controller.py` + `SubtitleCard.qml`,
third Paragraph-tab mode), v0.1.14 pro-audio Studio
deck, Paragraph tab document/file-queue mode split, Settings
engine/CUDA/model-source card split, 64-bit QML byte counts + deterministic
QML teardown. Playback visualization shipped 2026-08-29 (bead-driven, no
track): replay/chapter envelope overview with click+drag-to-seek
(`PlaybackWaveform.qml`), animated live meter with peak-hold, and
per-chapter waveform sidecars (`ch_XXXX.waveform.json`). Release pipeline:
tag-triggered 3-OS builds (`.github/workflows/release.yml`), CI gates on
`main` pushes + PRs only (`.github/workflows/ci.yml`: ruff + full suite on
ubuntu-22.04 + windows), manual CUDA-runtime spike
(`.github/workflows/cuda-runtime-spike.yml`). Production-path evidence
(incl. artifact-first/transport bounds) is tracked in `docs/performance`.
Remaining for v1: release hardening — weights install on demand as a
SHA-256-verified baseline (~330 MB, offline-pack import supported) by
design rather than frozen into the build; macOS is ad-hoc codesigned only
(no Developer ID/notarization), so the signed/notarized success measure
above is not yet met. `PROJECT_PLAN.md` Phase 5 status is stale (bead:
`VieNeuTTSApp-cw7`). See `PROJECT_PLAN.md` §0 and `conductor/tracks.md`.

<!-- refreshed 2026-09-14: feature 2 three-mode Paragraph composition (document/files/SRT); feature 11 SRT dub/transcript studio added; status rolled to 1036 collected-1024 selected, gate 1022 passed + 1 skipped + 1 intermittent (stream_cancel no_audio_retained under -n auto, passes isolated); SRT studio is main-not-released; shipped 3ef41f9 README + 617cfdc SRT i18n + e255027 Paragraph mode-nav stability -->
