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
   with tap-to-seek + one-tap chapter copy (v0.1.1). Oversize chapters (over
   `CHAPTER_CHAR_LIMIT`, 60 000 chars) auto-split into deterministic
   sub-audios (`ch_XXXX_sYY.wav`, `core/chapter_split.py`) rendered
   sequentially under the same logical chapter with chained playback and
   ordered part-file export (2026-09-29) — mega-chapters stay listenable
   instead of refusing with zero audio.
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
   (`StudioTab.qml` 1757 → 1398 lines). Post-v0.3.0 on `main` (2026-10-19,
   bead `VieNeuTTSApp-n5ts`): **windowed SRT Studio integration** —
   `SubtitleCard` (`subtitleStudioButton`) and `StudioTab`'s fourth guide card
   (`studioGuideSubtitleButton`) open rendered SRT projects in bounded 50-cue
   windows (`MAX_STUDIO_SRT_CUES = 50`, ranged WAV reads via `read_wav_range`)
   with `studioSubtitleBar` pagination, SRT-accurate inter-cue gaps, per-cue
   auto-fit vs custom speed (`0.50×–2.00×`, `studioSetClipSpeed` /
   `refit_subtitle_clips` over uncompressed 1.0× `raw_audio`), and chunked
   64k-frame save-back (`studioSaveToSubtitle` →
   `splice_studio_window_into_subtitle`) that updates `track.wav`,
   `track.timeline.json`, and downstream cue timestamps in place. Further
   extended on 2026-10-20 (bead `VieNeuTTSApp-eet9`, `12734a0`) with
   **first-class Video Dubbing Studio support**: `SubtitleStudioMeta` carries
   video path, mix mode, background gain, soft-subtitle embedding, and speaker
   voices; `load_project_from_subtitle()` resolves per-speaker voice context
   onto each `StudioClip` (displayed as `[SPEAKER_XX] → <Voice>` in a `Flow`
   row and pre-selected in `studioRegenDialog`); `studioExportVideo` splices
   active Studio edits and muxes the dubbed video in one click (`studioExportVideoButton`);
   and `studioPlayOriginalCue` (`studioClipOriginalAudioButton`) extracts and
   auditions the source video's original audio slice for any cue.
10. **Platform hardening (v0.1.6–v0.1.14)** — in-app update checks
   (platform-aware, background recheck + Settings badge); crash reporter
   (`crash.py` → `logs/crash.log` + native Windows dialog); cross-platform
   path normalization + BOM/newline-safe import; Windows reliability
   (Qt audio plugins shipped, int16 fallback, WinError-32 retries, no
   console flash, long-path/AV handling); OS-aware CUDA driver guide;
   64-bit QML-facing byte counts (the multi-GB CUDA manifest no longer
   raises `OverflowError` on first Settings visit) and deterministic QML
   tear down at app exit (v0.1.14). Shipped in v0.3.0 (2026-10-11):
   Windows native-GGUF hardening (ASCII-safe 8.3 paths for `qwen.dll`,
   `WaitForSingleObject` liveness probes, hardlink/copy fallback when
   symlinks are denied, MAX_PATH-safe installs) and macOS audit fixes
   (frozen floor macOS 14.0, certifi CA bundle for frozen TLS, native
   `osascript` error alert skipped under headless `offscreen`/`minimal` Qt so CI never blocks on a modal dialog, percent-encoded file URLs, drag-to-install dmg).
11. **Subtitle (SRT) & Video dub/transcript studio** — promoted on 2026-10-19
    (`aef1d87`) from the third Paragraph-tab mode to a dedicated top-level
    sidebar destination ("Lồng tiếng & Phụ đề" / "Dubbing & Subtitles",
    `SubtitlesTab.qml` + `SubtitleCard.qml` + `ui/subtitle_controller.py`,
    registered as the `subtitleController` context property) with a responsive
    two-column workstation layout that turns an `.srt` file or video into a
    cue-aligned audio track, retimed `.srt`, and dubbed video.
    Cues parse through a pure cue model (`core/subtitles.py`: styling and
    `{\an8}`-style override blocks dropped, tolerant `HH:MM:SS,mmm` stamps,
    optional `[SPEAKER_01]` speaker prefix round-trip),
    then synthesize through the same single worker/model via the
    synthesis-listener seam — no second model load. Two fit policies
    (`core/align.py`): **dub** keeps the SRT clock as master (compress an
    overlong take up to a 1.0–2.0× rate cap, push later cues on overflow) and
    **transcript** keeps the voice as master (never compress; reproduce the
    subtitle's pauses capped at a max-gap setting). Optional sentence merging
    groups cues into sentence-sized synthesis units (breaking at speaker
    boundaries), an offset field shifts
    the whole track, and the render streams clip-by-clip straight to a
    `PCM_16` `track.wav` (a two-hour film never sits in RAM). Plays with
    active-cue highlight + tap-to-seek from the *measured* timeline, exports
    both the aligned WAV/MP3 (`cf39f07` binds `subtitleExportWavButton` to
    `exportFormat`) and a retimed SRT, and opens 50-cue windows in Audio Studio
    with chunked save-back (`VieNeuTTSApp-n5ts`, 2026-10-19). A render
    fingerprint (`core/subtitle_project.py`) caches the track per workspace
    (including per-cue speakers and `speaker_voices`), so unchanged settings
    reuse the rendered `track.wav` instead of re-synthesizing.
    Post-v0.3.0 on `main` (2026-10-19, epic `VieNeuTTSApp-b3nq` + `ab1aad1`):
    - **Video dubbing & stream-copy muxing (`core/video_dub.py`)**: import or
      attach `.mp4`/`.mkv`/`.mov`/`.webm`/`.avi`/`.m4v` with companion `.srt`
      auto-discovery, `ffmpeg` audio extraction (full or `[start_ms, end_ms]`
      slices), and zero-reencode video muxing (`-c:v copy`, `replace` or `duck`
      background audio mix with `video_bg_gain_db`, and container-aware soft
      `.srt` embedding).
    - **Soft-vs-burned subtitle embed mode (2026-10-20/21, `6e7c747` + `d7b8721`,
      beads `poph` / `ni2i` / `vxil`)**: a per-project "Kiểu phụ đề" choice
      persisted as `subtitleEmbedMode` (`soft` default, or `burn` into the
      picture), honoured by both the Subtitles-tab and Studio exports and
      carried on `SubtitleStudioMeta` so the snapshot cannot drift. Burn-in
      renders a generated **ASS** script rather than handing libass the raw
      SRT — `PlayRes` pinned to the real frame, font/margins/outline as
      fractions of the picture, a line cap with adaptive font shrinking, and
      `fontsdir` pointing at the bundled Be Vietnam Pro — so a 1080×1920
      portrait export lands at ~9.6 % height / 3 lines instead of libass's
      21.6 % / 16 lines. `.webm` now carries a WebVTT soft track, `.avi`
      explains that it cannot and points at burn-in, source bit depth is
      preserved for HDR, cue text is escaped for ASS (`{…}`, backslashes, and
      `-->` — which used to make ffmpeg re-parse the file into a silently
      subtitle-free video), soft tracks are tagged with the ASR language
      instead of `und`, and ffmpeg progress streams to the UI with a
      dub-overrun warning (speech is never truncated; `clamp_to_video` is
      opt-in).
    - **On-device & OpenAI-compatible Whisper ASR (`core/asr.py`, track
      `asr_hardening_20261021`, epic `VieNeuTTSApp-7u58`)**: generate
      timestamped `Cue`s directly from video/audio via `faster-whisper` (with
      VAD and 1-entry model cache), `whisper.cpp` CLI (`whisper-cli` + local
      GGUF/bin weights, preferred by `auto` when installed), `openai-whisper`,
      or an OpenAI-compatible `/audio/transcriptions` endpoint. Hardened on
      2026-10-21 with in-process `soundfile`+`numpy` 16 kHz mono WAV
      normalization (`_ensure_16k_wav`), shared per-video media caching
      (`subtitles/_media_cache/<stem>_<hash8>/` storing `source_16k.wav`,
      `asr_segments_cache.json`, and `diar_turns_cache.json`), collision-safe
      `<stem>_<hash8>.srt` managed filenames with clean project titles, shared
      `NON_SPEECH_LABELS` filtering (`[BLANK_AUDIO]`, `[MUSIC]`, etc.),
      duration-aware segment splitting + bidirectional micro-fragment merging,
      `AsrCancelled` semantics that `auto` re-raises immediately, and
      `asr_device` / `asr_threads` settings.
    - **Speaker diarization & multi-speaker cast (`core/diarization.py`, epic
      `VieNeuTTSApp-2ifr`)**: label cues by speaker (`SPEAKER_01`, `SPEAKER_02`,
      …) via `audio-cpp/Nemotron-3-Diarization-GGUF` (`Q8_0` / `BF16` through
      `audiocpp_cli --mode diarize` on CPU/GPU with `--no-gpu` fallback and
      `weight_type=f32` on CPU-only runtimes, `ab1aad1` + `5794aca`) or the
      built-in zero-dependency NumPy acoustic speaker-embedding clustering
      (the unimplemented NeMo UI option was removed and migrated to `auto` in
      `cb0c1e9`); `max_speakers` ceiling (clamped 1–8) merges surplus clusters,
      re-runs remap labels to existing `speaker_voices`, `DiarizationCancelled`
      prevents silent fallback on cancel, and `subtitle_export_speaker_labels`
      controls `[SPEAKER_XX]` prefixes on `.srt` export. Map each speaker to a
      TTS voice via `VoicePicker` (`SubtitleCastPanel`) or extract the best
      3.5–8.0 s reference clip (`cloneSpeakerFromVideo`) for 1-click voice
      cloning and off-thread sample audition.
    - **Configurable LLM subtitle & timing refinement (`core/llm_refine.py`)**:
      stdlib-urllib OpenAI-compatible `/chat/completions` client (local Ollama /
      LM Studio / vLLM / llama.cpp or cloud APIs) for batched `refine_asr`,
      duration-budgeted `fit_timing` condensation, and `translate_dub` (with
      explicit target-language selection).
    - **Managed AI model & runtime downloads (`core/ai_models.py`,
      `SettingsModels.qml`)**: in-app `settingsAiDubbingCard` download/remove
      controls with size+SHA-256 verification, guarded path-traversal-safe
      removal, and throttled progress (`aiDownloadProgressChanged`) for all 5
      Whisper GGUF models (`tiny`..`large-v3-turbo`), Nemotron-3 Diarization
      GGUF models (`Q8_0` 102 MB / `BF16` 190 MB), and staged native runtime
      installers (`--help` probe + atomic swap) for both `whisper.cpp` (`b5454`,
      `whisper-cli`) and `audiocpp_cli` (`v0.9.2`).
    - **Subtitles workstation UX overhaul (track `subtitles_ux_overhaul_20261023`,
      epic `VieNeuTTSApp-weit`, archived 2026-10-23)**: remediated the 33-finding
      Subtitles UI/UX audit (`docs/ui-audit/SUBTITLES_TAB_UX_AUDIT.md`): a
      staged render-invalidation confirmation gate (`renderInvalidationRequested`
      + confirm/reject) protects live renders from policy-knob/cue/AI mutations;
      `renderStale` preserves `lastStats` and surfaces `exportBlockedReason`;
      project lifecycle switcher/create/delete (`projects`, `openProject`,
      `removeProject`) manages workspaces; `cancelExport()` terminates `ffmpeg`
      mid-mux; a single derived `state` + `busyOwner` (`""|ai|render|export`)
      gates the UI; a pinned `SubtitleActionDock` outside `PageShell` keeps
      render, transport, export menu, progress, and cancel always on screen; a
      three-stage segmented flow (`source` / `prepare` / `produce`) with a
      two-card drop launcher and ordered AI pipeline (`clean → translate → fit`)
      replaces the 731 px single-column stack; `SubtitleCard.qml` (1,473 → 646
      lines) composes `SubtitleSourcePanel`, `SubtitlePreparePanel`,
      `SubtitleCueEditor`, and `SubtitleCastPanel` with `AppProgressBar`,
      `AppTextField`, `Theme.warningText` AA contrast, `disabledReason`
      tooltips/accessibility, cue-list keyboard navigation, and
      `PageShell.ensureFocusVisible` focus-follow (`42ab565`).
12. **Optional multilingual Qwen engines (track `qwen_multiengine_20260920`,
    released in v0.2.0)** — two opt-in engine profiles beside VieNeu, which
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
    `qwen_gguf_engine_20260923`, released in v0.2.0)** — each Qwen profile
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
    restart, and shutdown evidence. Four packs publish download recipes
    (`linux-x64-cpu`, `windows-x64-cpu`, `macos-arm64-cpu`,
    `macos-arm64-metal`); they were re-locked and re-published 2026-09-28 at
    the bumped upstream pin (`6fae9291`, ggml 0.25.3), which per bead
    `ysl8.7` makes the pre-bump probe evidence stale for all four cells —
    including `linux-x64-cpu` — until the release smoke re-runs per cell
    (follow-ups `9wun` gh-pages pruning, `1sf0` stale pin comments); the two
    CUDA cells remain unpublished (no self-hosted runner). The app never
    advertises an unverified cell.
14. **Responsiveness & throughput hardening (track
    `perf_hardening_20261009`, released in v0.3.0)** — no
    multi-hundred-millisecond freezes: books load/export, Studio and audition
    file I/O, subtitle dub rendering and Qwen engine preparation run in the
    background with progress states; Settings gains a **Verify files** action
    (full re-hash on request; routine engine builds trust size/mtime/inode
    stamps). Live preview no longer throttles synthesis to playback speed
    (the transport refills from the on-disk part file), Qwen live jobs start
    with smaller first segments and prefetch the next one, and Int16-only
    audio devices play live PCM correctly (bug fix). Faster WSOLA speed
    changes, cached Studio stretch, debounced text metrics, lazy async tab
    loading, row-level list models, precompiled QML in packaged builds, and a
    default BLAS thread cap of 1 (VieNeu RTF 1.43 → 0.80 on a 4-core host;
    other engine knobs stay at SDK defaults until benchmarks justify a flip).

15. **UI/UX shell redesign (track `ui_shell_redesign_20261010`, released in
    v0.3.0; archived 2026-10-12)** — five destinations replaced the six-tab
    sidebar (a sixth, **Lồng tiếng & Phụ đề**, was promoted on 2026-10-19 in
    `aef1d87`; see feature 11):
    **Tạo giọng đọc** (one Create screen whose Soạn thảo / Tài liệu / Nhiều
    tệp modes share a single TransportDock, with a right-hand
    inspector: current voice + audition, the three most recent voices
    persisted per engine profile, per-run speed/pause and the one live-preview
    toggle), **Sách nói** (library ⇄ book master–detail, full-height chapter
    list, reader docked at ≥1200 px), **Giọng đọc** (20-preset library grouped
    Bắc/Trung/Nam with filters, search, audition and "Đặt làm mặc định",
    cloned voices, and the cloning flow as its clone view; "Đổi giọng…" from
    Create opens it in pick mode), **Studio** (timeline with proportional clip
    blocks, history chips, a Hiệu ứng panel whose edits stay pending with a
    Gốc / Đã chỉnh A/B until one "Áp dụng N thay đổi" step) and **Cài đặt**
    (six sections, label filter, engine summary card, "Nâng cao"
    disclosures). One 44 px status bar replaces the scattered status
    surfaces; the design system enforces a 12 px type floor, 44 px targets,
    AA-contrast text and at most one primary action per screen, and every
    destination is usable at the 640×420 minimum window. Legacy navigation
    ids (text / paragraph / cloning) stay as aliases. Post-v0.3.0 on `main`: optional Ko-fi support links in the sidebar (`navSupportButton`) and Cài đặt → Cập nhật (`supportCard` / `settingsSupportButton`) (`2f44ee1`, 2026-10-19); and `subtitles` (`Lồng tiếng & Phụ đề`, `SubtitlesTab.qml`) promoted out of `CreateTab` into a sixth top-level sidebar destination (`aef1d87`, 2026-10-19) so `CreateTab` serves `compose` / `document` / `files` with `CreateInspector` and `TransportDock` always active.

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

## Implementation Status (2026-10-23)

All fifteen v1 feature areas above are implemented: Phases 1–4, the 2026-08-28
audiobook track (`audiobook_epub_20260828`), the 2026-09-20/21 multi-engine
track (`qwen_multiengine_20260920`, feature 12), the 2026-09-23/24 GGUF
engine track (`qwen_gguf_engine_20260923`, feature 13), the 2026-10-09
perf track (`perf_hardening_20261009`, feature 14), the 2026-10-10 UI shell
redesign track (`ui_shell_redesign_20261010`, feature 15), the 2026-10-21
on-device ASR & dubbing hardening track (`asr_hardening_20261021`, feature 11),
the 2026-10-23 Subtitles UX overhaul track (`subtitles_ux_overhaul_20261023`,
feature 11), and bead-driven batches (incl. `VieNeuTTSApp-2ifr` diarization
hardening and `VieNeuTTSApp-4mqj` test-runtime reduction). All 13 conductor
tracks are complete and archived under `conductor/archive/`. Current app
version 0.3.0; curated notes in `packaging/release-notes/v0.1.1.md`–`v0.3.0.md`.
Test suite now stands at 1597 items collected / 1579 selected (18 benchmarks
deselected via `-m 'not benchmark'`; `-m "not slow"` → 1557,
`-m "not slow and not benchmark"` → 1539; verified live 2026-10-23 with
`pytest --collect-only -q`). Latest gate (2026-10-23 refresh): `ruff check .`
passes, `ruff format --check .` passes (214 files), and
`scripts/update_i18n.sh` is 100% clean (1175/1175 finished, 0 unfinished).
Full `pytest` on this 18-core host runs in ~32–43 s wall under
`-n auto --dist worksteal` after `VieNeuTTSApp-4mqj` (`1542053` + `5eb6f85`)
split the 12 giant smoke items (`test_ui_tabs.py`, `test_ui_shell.py`,
`test_ui_studio.py`) into smaller scenario-group fixtures and fixed the
in-process `test_qwen_host.py::TestTorchPosture` `sys.modules["torch"]`
deletion bug (`monkeypatch.setitem(sys.modules, "torch", None)` instead of
`delitem`, eliminating the self-inflicted `RuntimeError: Only a single
TORCH_LIBRARY can be used to register the namespace triton` when
`TestTorchPosture` and `TestFrameLoop` share an xdist worker). A full loaded-host
run can still flake 1–2 order-/load-sensitive tests (`test_controller.py::TestBgOpsAsync`,
`test_worker_thread_safety_smoke`, `test_qwen_host::TestFrameLoop::test_session_lifecycle`,
or `test_ui_shell.py` null-context-property race `VieNeuTTSApp-qjkk`); always
re-run any suspect test in isolation and reproduce at the previous commit
before calling it a regression. The documented device-less-host failure remains
`tests/unit/test_stream_playback.py::TestRealQtSmoke::test_real_qaudiosink_offscreen_smoke`
— a device-dependent real-`QAudioSink` smoke that passes in isolation but fails
whenever it shares a run with other unit files (serially too, `-n 0`). It is
`skipif`-guarded on `CI=true`; the local guard only skips
when sink construction raises or sets `errorText`, and on this device-less host
the ffmpeg sink constructs "successfully" without draining. Filed at the
2026-09-16 refresh as bead `VieNeuTTSApp-3iy` (extends the b0t/o98 host-flake family).
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
tag-triggered 3-OS builds (`.github/workflows/release.yml`) are the only
automatic gate — ruff + the full suite on all three platforms before anything
is built or published (the per-push/PR `ci.yml` was removed 2026-10-10 by
request: no CI per commit, only at release), manual CUDA-runtime spike
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
`EngineState` singleton, per-surface gating) and the English catalog. It
shipped in **v0.2.0** (2026-09-24). Coverage is
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

Track `perf_hardening_20261009` (2026-10-09, archived 2026-10-10) added
feature 14: 32/32 tasks across 7 phases, epic `VieNeuTTSApp-w1in` closed
(Task 6.6 lazy-import refactor measured and not kept — its ≤150 ms bound
missed the 200 ms gate). Follow-up beads: `hay8` (unmeasured tuning cells,
incl. CUDA batched export), `t5la` (GGUF thread ABI), `rg4a` (GGUF prefix-KV
reuse), `cqqs` (resampler anti-imaging), `v09a` (stateful streaming WSOLA);
`4jwz` / `x898` record 6 Qwen/GGUF tests (was 7 before `7coj` merged 4 `TestInstallPacks` items into 3) that assume an x64 host cell and fail on
linux-aarch64. Suite at this refresh: 1597 collected / 1579 selected (18
benchmark deselected; reduced from 1955 / 1937 by the 2026-10-10 `zc8p` diet,
the 2026-10-12 `57634dc` -37 pass and `VieNeuTTSApp-7coj` `d6f70bb..3a5ed39` -381 semantic-merge pass across 25 unit files, plus +1 in `2f44ee1`, +26 in the 2026-10-19 `VieNeuTTSApp-n5ts` SRT Studio, `cf39f07` export label i18n, and `VieNeuTTSApp-b3nq` + `ab1aad1` Video Dubbing / ASR / Diarization / LLM / AI model download suites, +2 in the 2026-10-20 `aef1d87` dedicated `SubtitlesTab` + `VieNeuTTSApp-eet9` `12734a0` Video Dubbing Studio suites, +9 in the 2026-10-20/21 subtitle burn-in / embed-mode batch `d17a261` + `6e7c747` + `d7b8721` (beads `4kht`, `poph`, `ni2i`, `vxil`), +13 net in the 2026-10-21 diarization hardening `VieNeuTTSApp-2ifr` (`b41caf7..cb0c1e9`) and test-runtime diet `VieNeuTTSApp-4mqj` (`1542053` + `5eb6f85`, → 1554/1536), +13 in track `asr_hardening_20261021` (`VieNeuTTSApp-7u58`, `b09dd45..d50f43b`, → 1567/1549), and +30 in track `subtitles_ux_overhaul_20261023` (`VieNeuTTSApp-weit`, `da92163..42ab565`, → 1597/1579)).

<!-- refreshed 2026-10-23: 37 commits since `e64ec18` (diarization hardening epic `2ifr` `b41caf7..cb0c1e9`, test-runtime reduction `4mqj` `1542053`+`5eb6f85`, track `asr_hardening_20261021` epic `7u58` `b09dd45..d50f43b`, skill pruning `9e216f4`, and track `subtitles_ux_overhaul_20261023` epic `weit` `2f6fe8a..42ab565`, both tracks archived in `ed66f6a`). Feature 11 expanded with managed `whisper.cpp` (`b5454`) + `audiocpp_cli` staged runtime installers, pinned Whisper/Nemotron GGUF digests, shared per-video `_media_cache`, `AsrCancelled`/`DiarizationCancelled`, ASR/diarization device+threads controls, and the 5-phase Subtitles UX overhaul (staged render-invalidation gate, `renderStale`, project lifecycle, `cancelExport`, derived `state`/`busyOwner`, pinned `SubtitleActionDock`, Source/Prepare/Produce stages, `SubtitleSourcePanel`/`SubtitlePreparePanel`/`SubtitleCueEditor`/`SubtitleCastPanel` extraction, `AppProgressBar`/`AppTextField`, `Theme.warningText`, focus-follow `42ab565`). Suite 1597 collected / 1579 selected (18 benchmark deselected, verified live); ruff check + format --check green (214 files); i18n 1175/1175 finished; no `pyproject.toml`/`uv.lock` dep drift; beads 519 total / 1 open (`t80a`) / 1 blocked (`nqx.2.4`) / 40 deferred / 477 closed. -->
<!-- refreshed 2026-10-21: 7 commits since `e08a58e` (`d17a261` video dubbing/diarization/muxing/subtitle-studio hardening, `1661604` VoicePicker popup clipping + SubtitleCard layout, `6e7c747` burn-in embed mode + webm/avi fixes, `d7b8721` styled ASS burn-in/HDR/progress, plus 3 beads-sync chores). Feature 11 gains the soft-vs-burn subtitle embed mode; feature 15 corrected to "five destinations replaced the six-tab sidebar, sixth (`subtitles`) promoted 2026-10-19 in `aef1d87`". Suite 1541 collected / 1523 selected (18 benchmark deselected, verified live); ruff format gate restored (5 files of debt from `d17a261`/`6e7c747` reformatted, 212 files green); no `pyproject.toml`/`uv.lock` dep drift; i18n 1012/1012 finished (0 unfinished, 0 vanished); beads 463 total / 46 open / 5 blocked / 416 closed (was 456 / 409 closed — `4kht` epic + children, `vxil`, `ni2i`, `poph` closed). Flake baseline rewritten: full runs flake 2–4 varying tests under load, all reproducing at `e08a58e` (triton `TORCH_LIBRARY` import race in `test_qwen_host`). -->

<!-- refreshed 2026-09-14: feature 2 three-mode Paragraph composition (document/files/SRT); feature 11 SRT dub/transcript studio added; status rolled to 1036 collected-1024 selected, gate green (ruff check + format --check; pytest 1023 passed + 1 skipped) after fixing the SRT-commit format debt (`style:` 658c564, bead c90); one pre-existing stream_cancel intermittent under -n auto; SRT studio is main-not-released; shipped 3ef41f9 README + 617cfdc SRT i18n + e255027 Paragraph mode-nav stability -->
<!-- refreshed 2026-09-21: track `qwen_multiengine_20260920` implemented on `main` (unreleased, app v0.1.16): feature 12 added (optional Qwen CustomVoice/Base profiles — isolated managed model-host subprocess, verified runtime + model installs with offline-pack import, capability-aware UI, engine-stamped provenance/caches, opt-in real-model release smoke while ordinary CI stays on the deterministic fake host); no pyproject/uv.lock dep drift (the Qwen stack lives only in the managed runtime); test items 1732 collected / 1720 selected (12 benchmark deselected), gate 1719 passed + 1 device-dependent real-QAudioSink host failure (bead VieNeuTTSApp-3iy); the six matrix cells remain `pending` real-device evidence (Task 0.3 + the opt-in release workflow) -->
<!-- refreshed 2026-09-24: track `qwen_gguf_engine_20260923` implemented on `main` (unreleased): feature 13 added — GGUF Q8_0/Q4_K_M variants on the pinned qwentts.cpp native engine for both Qwen profiles (locked runtime/model manifests + offline installers, isolated qwen_gguf_host subprocess, variant-aware UI + provenance/caches/Studio, frozen --qwen-gguf-host packaging, opt-in 24-cell release gate); linux-x64-cpu is the only published + probe-verified cell, the rest stay explicitly blocked; gate: pytest 2240 passed, 2 documented device-dependent baselines deselected; ruff check + format green -->
<!-- refreshed 2026-09-27: post-v0.2.0 on main — test-reduction batch (ae93c6a…f5b40e8) took the suite to 2190 collected / 2178 selected (12 benchmark deselected); UI fixes (VoicePicker trigger elide, Settings engine-state coherence across model switching, guide setup for unready Qwen profiles, reuse installed Qwen quantization, hide VieNeu emotion chips on Qwen); GGUF packs published for linux-x64-cpu / windows-x64-cpu / macos-arm64-cpu / macos-arm64-metal (only linux-x64-cpu probe-verified; CUDA cells unpublished, bead ysl8.7); CI is lint + parallel unit/smoke matrix legs (8710f7f). Track qwen_gguf_engine_20260923 closed with AC-12 residual on ysl8.7. Deps unchanged. PROJECT_PLAN.md Phase 5 still stale (bead cw7). -->
<!-- refreshed 2026-09-28: no drift — nothing landed since the 2026-09-27 refresh except a ruff-format style fix (5097342) and housekeeping (e30b349, f95617a); deps/CI/features unchanged; PROJECT_PLAN.md Phase 5 still stale (bead cw7) -->
<!-- refreshed 2026-10-07: feature 6 gains audiobook auto-split of oversize chapters into sub-audios (1244363, core/chapter_split.py — sequential render, chained playback, ordered part export); feature 13 GGUF packs re-locked/re-published 2026-09-28 at pin 6fae9291 (ggml 0.25.3, 801ffc7 + 31842ba) making pre-bump probe evidence stale for all four published cells until the release gate re-runs (bead ysl8.7 note; follow-ups 9wun, 1sf0); dev tooling: pytest-cov dropped + suite reduced ~20% via same-function merges → 1754 collected / 1742 selected (verified live 2026-10-07); deps unchanged (app v0.2.0); PROJECT_PLAN.md Phase 5 still stale (bead cw7) -->
<!-- refreshed 2026-10-10: feature 14 added for track perf_hardening_20261009 (2026-10-09, archived 2026-10-10, epic w1in closed): GUI-thread offload with progress states, Settings Verify files + stat-stamp integrity, live writer/transport decoupling with part-file refill, Qwen progressive segments + prefetch, Int16 live-sink fix, FFT WSOLA, Studio stretch cache, lazy tabs/row models/AOT QML, BLAS cap default 1; PySide6 floor >=6.9; suite 1955 collected / 1937 selected (18 benchmark deselected, verified live); PROJECT_PLAN.md Phase 5 still stale (bead cw7) -->

<!-- refreshed 2026-09-16: v0.1.15 + v0.1.16 released (tagged d5b2529); feature 9 rolled forward with the v0.1.15 pinned-transport/region-selection/truthful-op-stack/breadcrumb/keyboard work and the v0.1.16 discoverable-transport/numeric-entry/danger-styling/component-extraction pass; test items 1055 collected / 1054 selected; gate 1054 passed + 1 device-dependent real-QAudioSink host failure (byte-guard gap in test_stream_playback.py — CI-skipped, bead filed); deps unchanged vieneu 3.3.0/PySide6 6.11.2 -->

<!-- refreshed 2026-10-10 (2): test-suite diet (bead zc8p) — suite 1879 collected / 1861 selected; no feature or scope change; src-level test speedups parked in bead 37ma -->
<!-- refreshed 2026-10-10: feature 15 added for track ui_shell_redesign_20261010 (five-destination IA, CreateTab + inspector, Giọng đọc library, Sách nói master–detail, Studio pending-edit panel, Cài đặt sections, 640×420 pass) -->
<!-- refreshed 2026-10-12: track ui_shell_redesign_20261010 archived (feature 15 marked archived; Implementation Status now fifteen areas, dated 2026-10-12); feature 10 gains the 2026-10-11 Windows native-GGUF hardening (dcaeb04) and macOS audit fixes (a55b80f: LSMinimumSystemVersion 14.0, certifi frozen TLS, osascript alert, file-URL encoding, drag-install dmg); suite 1920 collected / 1902 selected -->
<!-- refreshed 2026-10-19: v0.3.0 tagged at 8db2677 (723948c release + 0d190da file-based shell smoke driver + 8db2677 headless crash-dialog guard); post-v0.3.0 on main: Ko-fi support links in sidebar, Settings > Updates, README, and .github/FUNDING.yml (2f44ee1); test-suite -20% semantic-merge pass (57634dc + bead 7coj d6f70bb..3a5ed39, closed) + 2f44ee1 -> 1504 collected / 1486 selected (18 benchmark deselected); no external dep drift -->
<!-- refreshed 2026-10-19 (2): features 9 & 11 extended on main with windowed 50-cue SRT Studio integration + per-cue speed refit + chunked 64k-frame save-back (e94d045..02b9181, bead n5ts closed), SubtitleCard exportFormat button label binding (cf39f07), and end-to-end Video Dubbing, Whisper ASR, Speaker Diarization (Nemotron-3-Diarization-GGUF via audio.cpp on CPU/GPU + NeMo + offline acoustic clustering + 1-click reference clip cloning), OpenAI-compatible LLM subtitle/timing refinement, and in-app AI model/runtime downloads (ba69a5a..ab1aad1, epic b3nq closed); suite 1530 collected / 1512 selected (18 benchmark deselected, +26 items); ruff check + format --check green (212 files); no pyproject/uv.lock dep drift -->
<!-- refreshed 2026-10-20: features 9, 11 & 15 extended on main with dedicated top-level SubtitlesTab destination + responsive two-column SubtitleCard workstation + full English i18n catalog completion (aef1d87, 991/991 finished), and Video Dubbing Studio enhancements (12734a0, bead eet9 closed: SubtitleStudioMeta video/speaker_voices fields, per-speaker StudioClip voice context & regen pre-selection, 1-click studioExportVideo splice+mux, and studioPlayOriginalCue source video audio audition); suite 1532 collected / 1514 selected (18 benchmark deselected, +2 items); ruff check + format --check green (212 files); no pyproject/uv.lock dep drift -->
