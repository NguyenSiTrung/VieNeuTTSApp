# VieNeuTTS Desktop App — Product Guide

## Initial Concept
Cross-platform on-device Vietnamese/English TTS desktop app powered by
VieNeu-TTS v3 Turbo, with optional multilingual Qwen 0.6B engine profiles.
Fully offline and on-device: no cloud, no network after install.
Single-process PySide6 + QML app with a worker thread owning the `vieneu`
SDK instance, and a separate isolated model-host subprocess for Qwen.

## Product Vision
Make high-quality Vietnamese/English text-to-speech available as a
fast, private, native desktop tool on macOS, Windows, and Ubuntu, and let
users who need other languages opt into the pinned Qwen 0.6B CustomVoice /
Base profiles without giving up the Vietnamese-first default. The app
auto-detects the best engine (CPU/ONNX vs NVIDIA/CUDA), surfaces it in the
UI, and keeps 48 kHz synthesis off the UI thread — so the user
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
9. **Audio Studio (v0.1.13 → pro-audio deck v0.1.14 → v0.1.15–v0.1.16)** —
   post-synthesis editing tab (`StudioTab.qml` + `core/studio.py`):
   non-destructive gain/fade/speed/gap/normalize/trim op stack, live waveform
   + playhead, clip manager with reorder, per-segment voice-switch
   re-synthesis (10 ms crossfade); feeder "Studio…" buttons on
   Text/Paragraph/Audiobook tabs.
   v0.1.14 adds a 72 px master waveform deck with peak/RMS/dynamic-range
   telemetry, a grouped FX rack (Dynamics / Tempo & Cadence / Transitions),
   an op-stack timeline with contextual undo/reset, interactive pause +
   click-drag seek on the shared player, per-clip audition and in-dialog
   transcript editing, off-GUI-thread render/export behind `studioBusy` with
   stale-render guards, and full vi/en localization.
   v0.1.15 redesigns the tab around a **pinned transport dock** that never
   scrolls away (play/pause/seek, timecodes, previews), adds
   **master-waveform region selection** with one-click trim-to/delete-selection,
   makes the op stack **truthful** (rack readouts fold gain sums and speed
   multipliers to match the rendered mix and apply in place instead of stacking
   duplicates), makes clip auditioning publish the actually-playing clip so the
   dock and highlighted row agree, turns op-history into **clickable rollback
   breadcrumbs**, adds keyboard transport (space/Esc/arrows), and guarantees
   UI integrity down to 640×420.
   v0.1.16 makes that transport **discoverable** (visible seek ±5 s and stop
   buttons with key hints in tooltips), adds precise numeric `AppNumberField`
   entry beside every FX slider, fade dirty-state parity + reset confirmation,
   a single primary dock CTA with quick export demoted to an icon action,
   clickable equal-height guide cards, danger styling for destructive actions,
   square 40 px icon hit targets, and clips the last of the Studio chrome into
   reusable `StudioRackModule` / `StudioParamRow` / `StudioClipRow` components
   (`StudioTab.qml` 1757 → 1398 lines).
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
12. **Optional multilingual Qwen engines (track `qwen_multiengine_20260920`,
    unreleased on `main`)** — two opt-in engine profiles beside VieNeu, which
    stays the fresh-install and migrated-settings default:
    **Qwen CustomVoice 0.6B** (10 languages, 9 fixed speakers) and **Qwen Base
    0.6B** (same languages, user-enrolled voice clones from a clip + transcript).
    The active profile is explicit and global; the app never guesses an engine
    from text, and exactly one model owner is resident at a time. Qwen runs in a
    dedicated, app-managed **model-host subprocess** backed by its own verified
    runtime install (~1.5–3 GB per platform, PyTorch CPU/CUDA/MPS) and its own
    checksum-pinned model install (~2.5 GB per profile, ~683 MB shared), both
    installable from Settings with offline-pack import — nothing enters the
    frozen bundle or the app's own dependency lock. Every existing surface
    (Text, Paragraph/Batch, Audiobook, Subtitle, Audio Studio, caches, exports)
    serves both profiles: voices, languages, readiness and cloning capability
    come from one capability table, submissions carry an immutable engine
    context, caches never reuse another engine's artifact, Studio keeps
    truthful provenance and offers a one-click switch to the profile a clip was
    rendered with, and cancelling a job leaves the next one usable even after
    the host had to restart. Settings' own engine-scoped controls follow the
    same rule: the default voice and the temperature field belong to VieNeu and
    read as unavailable with the reason under a Qwen profile (a Qwen voice is
    chosen on each synthesis surface, and the 0.6B host samples with its own
    fixed settings).
13. **GGUF format variant for the Qwen profiles (track
    `qwen_gguf_engine_20260923`, unreleased on `main`)** — each Qwen profile
    offers *Official full weights* (PyTorch) **or** GGUF `Q8_0` / `Q4_K_M` on
    the pinned `qwentts.cpp` native engine (`cpu`/`cuda`/`metal`; the app's
    `auto`/`mps` spellings resolve to the native vocabulary). The GGUF side
    keeps the same contracts: a checksum-locked native runtime pack (~18 MB on
    `linux-x64-cpu`) plus per-variant talker + shared-codec model installs with
    offline import, an isolated `qwen_gguf_host` subprocess speaking the same
    framed protocol, variant-stamped provenance through every artifact/cache/
    Studio clip (a `Q4_K_M` render is never replayed as `Q8_0`), and an opt-in
    real-model release gate (`scripts/qwen_gguf_release_smoke.py` +
    `.github/workflows/qwen-gguf-runtime-smoke.yml`) covering 6 cells × 4
    variants = 24 combinations with identity, device, resource, cancellation,
    restart, and shutdown evidence. Only the `linux-x64-cpu` pack is published
    and probe-verified today; the remaining cells stay explicitly blocked until
    their packs build and pass — the app never advertises an unverified cell.

## Success Measures (v1)
- All Section 7.1–7.4 acceptance criteria pass (text, file, cloning,
  settings flows).
- Installable signed/notarized artifacts per OS (`.dmg`, `.msi`/`.exe`,
  `.deb`/AppImage) with green CI.
- Streaming < 300 ms first-audio latency on CPU (direct-engine TTFC
  evidence; live preview is now opt-in — default is silent
  generate-then-replay); smooth progress and
  cancel for long jobs.
- The optional Qwen profiles are validated against the real model on the
  locked matrix (Windows/Linux CPU+CUDA, Apple Silicon CPU/MPS) through the
  opt-in release smoke, while ordinary CI stays on the deterministic fake
  host and downloads nothing.

## Implementation Status (2026-09-21)

All thirteen v1 feature areas above are implemented: Phases 1–4, the 2026-08-28
audiobook track (`audiobook_epub_20260828`), the 2026-09-20/21 multi-engine
track (`qwen_multiengine_20260920`, feature 12), the 2026-09-23/24 GGUF
engine track (`qwen_gguf_engine_20260923`, feature 13), and bead-driven
batches with no tracks. Current app version 0.1.16; curated notes in
`packaging/release-notes/v0.1.1.md`–`v0.1.16.md`. Test suite grew with the SRT
studio to 1055 items collected / 1054 selected (12 benchmarks deselected via
`-m 'not benchmark'`). Latest gate (2026-09-16): `ruff check .` and
`ruff format --check .` pass (128 files); `pytest` 1054 passed + 1 failed in
29.8 s on this host. The single failure is
`tests/unit/test_stream_playback.py::TestRealQtSmoke::test_real_qaudiosink_offscreen_smoke`
— a device-dependent real-`QAudioSink` smoke that passes in isolation but fails
whenever it shares a run with other unit files (serially too, `-n 0`). It is
`skipif`-guarded on `CI=true`, so CI is unaffected; the local guard only skips
when sink construction raises or sets `errorText`, and on this device-less host
the ffmpeg sink constructs "successfully" without draining. Filed at this
refresh as bead `VieNeuTTSApp-3iy` (extends the b0t/o98 host-flake family).
One pre-existing intermittent also remains under `-n auto`
(`TestStreamLifecycleSmoke::test_stream_bindings_e2e_cancel_cross_tab_and_error_recovery`
— the `stream_cancel` `no_audio_retained` assert can flake on a loaded 14-worker
run; passes in isolation). Prior verified gate: 1023 passed + 1 skipped
(2026-09-14, after fixing the SRT-commit format debt in `658c564`, bead `c90`);
before that 859 passed + 1 skipped (2026-09-10, commit `3eb6c90`), and 893
passed + 1 skipped in ~23 s (2026-09-04; 37 unit files + 5 smoke modules; one
flaky ordering failure
`TestRenderTelemetry::test_eta_completes_to_zero_on_last_segment` passes
in isolation). Known host-specific failures on this Linux workstation —
`run_gui` CUDA-inspection deferral tests, tracked as beads `VieNeuTTSApp-b0t`
and `VieNeuTTSApp-o98` (fail on a clean tree too). Since the 08-28 tracks,
further work is bead-driven with no conductor track: the v0.1.15/v0.1.16 Audio
Studio redesigns (pinned transport dock + waveform region selection + truthful
op stack + op-history breadcrumbs + keyboard transport; then visible seek/stop,
precise numeric FX entry, danger-styled destructive actions, and the
`StudioRackModule`/`StudioParamRow`/`StudioClipRow` extraction — bead epic
`VieNeuTTSApp-3cl` + `vdo`), SRT subtitle dub/transcript studio
(`core/subtitles.py` + `core/align.py` + `core/subtitle_project.py` +
`ui/subtitle_controller.py` + `SubtitleCard.qml`, third Paragraph-tab mode),
v0.1.14 pro-audio Studio deck, Paragraph tab document/file-queue mode split,
Settings engine/CUDA/model-source card split, 64-bit QML byte counts +
deterministic QML teardown. Playback visualization shipped 2026-08-29
(bead-driven, no track): replay/chapter envelope overview with
click+drag-to-seek (`PlaybackWaveform.qml`), animated live meter with peak-hold,
and per-chapter waveform sidecars (`ch_XXXX.waveform.json`). Release pipeline:
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

Track `qwen_multiengine_20260920` (2026-09-20/21) added the optional Qwen
engines described in feature 12: the capability/profile contracts, the
verified runtime + model installers, the isolated model host and its parent
adapter, profile-scoped clone persistence, the submission-context/cache/Studio
provenance rules, the capability-aware QML surfaces (Settings engine cards,
`EngineState` singleton, per-surface gating) and the English catalog. It is
implemented on `main` and **unreleased** (still app v0.1.16). Coverage is
deterministic and torch-free: fake-host unit suites plus consolidated
subprocess scenarios in `tests/smoke/test_e2e_flows.py`; the real model is
validated only through the opt-in
`scripts/qwen_release_smoke.py` + `.github/workflows/qwen-runtime-smoke.yml`
matrix, whose six cells remain `pending` until a machine with the provisioned
packs runs them. Gate at this refresh: `ruff check .` + `ruff format --check .`
green, `pytest` 1719 passed + 1 device-dependent real-`QAudioSink` host failure
(bead `VieNeuTTSApp-3iy`), 1732 collected / 1720 selected (12 benchmark
deselected). `PROJECT_PLAN.md` Phase 5 status remains stale (bead
`VieNeuTTSApp-cw7`).

<!-- refreshed 2026-09-14: feature 2 three-mode Paragraph composition (document/files/SRT); feature 11 SRT dub/transcript studio added; status rolled to 1036 collected-1024 selected, gate green (ruff check + format --check; pytest 1023 passed + 1 skipped) after fixing the SRT-commit format debt (`style:` 658c564, bead c90); one pre-existing stream_cancel intermittent under -n auto; SRT studio is main-not-released; shipped 3ef41f9 README + 617cfdc SRT i18n + e255027 Paragraph mode-nav stability -->
<!-- refreshed 2026-09-21: track `qwen_multiengine_20260920` implemented on `main` (unreleased, app v0.1.16): feature 12 added (optional Qwen CustomVoice/Base profiles — isolated managed model-host subprocess, verified runtime + model installs with offline-pack import, capability-aware UI, engine-stamped provenance/caches, opt-in real-model release smoke while ordinary CI stays on the deterministic fake host); no pyproject/uv.lock dep drift (the Qwen stack lives only in the managed runtime); test items 1732 collected / 1720 selected (12 benchmark deselected), gate 1719 passed + 1 device-dependent real-QAudioSink host failure (bead VieNeuTTSApp-3iy); the six matrix cells remain `pending` real-device evidence (Task 0.3 + the opt-in release workflow) -->
<!-- refreshed 2026-09-24: track `qwen_gguf_engine_20260923` implemented on `main` (unreleased): feature 13 added — GGUF Q8_0/Q4_K_M variants on the pinned qwentts.cpp native engine for both Qwen profiles (locked runtime/model manifests + offline installers, isolated qwen_gguf_host subprocess, variant-aware UI + provenance/caches/Studio, frozen --qwen-gguf-host packaging, opt-in 24-cell release gate); linux-x64-cpu is the only published + probe-verified cell, the rest stay explicitly blocked; gate: pytest 2240 passed, 2 documented device-dependent baselines deselected; ruff check + format green -->

<!-- refreshed 2026-09-16: v0.1.15 + v0.1.16 released (tagged d5b2529); feature 9 rolled forward with the v0.1.15 pinned-transport/region-selection/truthful-op-stack/breadcrumb/keyboard work and the v0.1.16 discoverable-transport/numeric-entry/danger-styling/component-extraction pass; test items 1055 collected / 1054 selected; gate 1054 passed + 1 device-dependent real-QAudioSink host failure (byte-guard gap in test_stream_playback.py — CI-skipped, bead filed); deps unchanged vieneu 3.3.0/PySide6 6.11.2 -->
