# Track Learnings: dub_pacing_alignment_20261026

Patterns, gotchas, and context discovered during implementation.

## Codebase Patterns (Inherited)

📚 **211 patterns** available in `conductor/patterns.md`, plus learnings from `asr_hardening_20261021` and `subtitles_ux_overhaul_20261023`:

- **Sparse fingerprint-payload evolution (`core/subtitle_project.py`)**: Emit a JSON key ONLY when the data it carries is present (`speechStartMs` / `speechEndMs` only when `is not None`), so pre-existing renders keep byte-identical payloads and their caches stay valid. Pin the pre-change digest with a golden test.
- **Three pure subtitle core layers**: `core/subtitles.py` (cue model: no Qt, no disk policy), `core/align.py` (fit planning & WSOLA alignment: pure NumPy), `core/subtitle_project.py` (workspace store + streaming `SubtitleTrackRenderer`).
- **Streaming track render**: `SubtitleTrackRenderer` writes the mix clip-by-clip to `PCM_16` `track.wav` — never materialize a full two-hour float32 track in RAM.
- **Persistent per-video media cache (`SubtitleController._media_cache_dir`)**: `subtitles/_media_cache/<stem>_<hash8>/` stores `source_16k.wav` (with `.json` fingerprint sidecar), `asr_segments_cache.json`, and `diar_turns_cache.json`.
- **Staged render invalidation (`SubtitleController`)**: Any mutation to cues or policy knobs on a rendered project goes through `_PendingInvalidation` (`confirmRenderInvalidation()` / `rejectRenderInvalidation()`); existing or new tests mutating a rendered project must confirm invalidation.
- **Windowed SRT-in-Studio (`core/studio.py`)**: `load_project_from_subtitle` loads bounded 50-cue windows via `read_wav_range`, keeps 1.0× `raw_audio` on `StudioClip` so repeated speed refits never compound WSOLA artifacts, and splices edited windows back in 64k-frame chunks.
- **Quality gate per task**: `ruff check .` + `ruff format --check .` + `pytest` must all pass before committing; commit with conventional prefix + `git notes add` task summary.

---

<!-- Learnings from implementation will be appended below -->

## [2026-10-26] - Phase 1 Task 1.1: Extend Cue with optional speech_start_ms / speech_end_ms and sparse project JSON serialization
- **Implemented:** Added `speech_start_ms` and `speech_end_ms` to `Cue` with validation and `effective_speech_start_ms`/`effective_speech_end_ms`/`speech_duration_ms` properties; updated `_cue_to_json`/`_cue_from_json` for sparse serialization.
- **Files changed:** `src/vienetts_app/core/subtitles.py`, `src/vienetts_app/core/subtitle_project.py`, `tests/unit/test_subtitles.py`, `tests/unit/test_subtitle_project.py`
- **Commit:** 765c10a
- **Learnings:**
  - Patterns: Sparse JSON emission (`speechStartMs`/`speechEndMs` omitted when `None`) preserves legacy `render_fingerprint` golden hashes byte-for-byte.
  - Gotchas: Reject `bool` in integer timestamp validation since `isinstance(True, int)` is True in Python.
---

## [2026-10-26] - Phase 1 Task 1.2: Add trim_speech_edges and apply_micro_fade DSP helpers in core/audio.py
- **Implemented:** Added vectorized `trim_speech_edges` (threshold_db=-45.0, pad_ms=15) and `apply_micro_fade` (half-cosine 5 ms taper) in `core/audio.py`.
- **Files changed:** `src/vienetts_app/core/audio.py`, `tests/unit/test_audio.py`
- **Commit:** 6a6335d
- **Learnings:**
  - Patterns: Returning all-silent or tiny arrays untouched from `trim_speech_edges` and `apply_micro_fade` keeps synthetic zero-filled test clips safe without special-casing callers.
---

## [2026-10-26] - Phase 2 Task 2.1: Acoustic RMS VAD + diarization turn speech-bound refinement and Whisper word-timestamp splitting
- **Implemented:** Added `refine_cues_speech_bounds` in `core/diarization.py` using 10 ms RMS windows over `read_wav_range` intersected with `RttmTurn` bounds; added word-timestamp clause splitting and speech bound propagation in `core/asr.py`.
- **Files changed:** `src/vienetts_app/core/diarization.py`, `src/vienetts_app/core/asr.py`, `tests/unit/test_diarization.py`, `tests/unit/test_asr.py`
- **Commit:** 85e31e9
- **Learnings:**
  - Patterns: Leave `speech_start_ms` / `speech_end_ms` unset (`None`) when the source WAV slice is all-silent so synthetic zero-filled test fixtures retain their exact `[start_ms, end_ms]` windows.
---

## [2026-10-26] - Phase 2 Task 2.2: Language-aware speaking rates and speech-window + post-cue slack budgets in core/llm_refine.py
- **Implemented:** Added `LANGUAGE_CHARS_PER_SEC` and `language`/`slack_ms` scaling to `max_chars_for_window`, and updated `refine_cues_with_llm` to compute `maxChars` from `speech_duration_ms` plus bounded post-cue silence slack (`min(gap_after_ms - 120, 800)`).
- **Files changed:** `src/vienetts_app/core/llm_refine.py`, `tests/unit/test_llm_refine.py`
- **Commit:** 54cd966
- **Learnings:**
  - Patterns: CJK scripts (`zh`/`ja`/`ko`) have ~2.5x higher syllable density per character than Latin/Vietnamese scripts, so scaling `chars_per_sec` prevents over-long translated lines.
---

## [2026-10-26] - Phase 3 Task 3.1: Two-stage elastic plan_cue / plan_alignment and readable adjusted_cues caption hold
- **Implemented:** Added `COMFORT_RATE = 1.15`, `MIN_BREATH_GAP_MS = 120`, `MAX_SLACK_BORROW_MS = 1200`, `MAX_RATE_STEP = 0.20`, and `MIN_SUBTITLE_GAP_MS = 80` in `core/align.py`; updated `plan_cue` / `plan_alignment` for speech-onset anchoring, two-stage elastic fitting, lookahead gap borrowing, and same-speaker rate step smoothing; updated `adjusted_cues` to preserve readable display hold.
- **Files changed:** `src/vienetts_app/core/align.py`, `tests/unit/test_align.py`
- **Commit:** 3d30d0c
- **Learnings:**
  - Patterns: Only count `overflow_ms > 0` in `dub` mode when a cue overruns even its elastically borrowed `extended_end` window so clean gap borrowing never triggers false overflow warnings.
---

## [2026-10-26] - Phase 3 Task 3.2: Uniform merged speech-unit rate, punctuation-weighted splitting & pre-fit silence trimming
- **Implemented:** Added punctuation-weighted `split_unit_audio`, `SubtitleTrackRenderer.add_unit_clip` for uniform unit rate, and pre-fit `trim_speech_edges` + `apply_micro_fade` in `SubtitleTrackRenderer._place`, `render_alignment`, and `refit_subtitle_clips`; separated audio `Timeline` segments from caption display hold in `splice_studio_window_into_subtitle`.
- **Files changed:** `src/vienetts_app/core/align.py`, `src/vienetts_app/core/subtitle_project.py`, `src/vienetts_app/core/studio.py`, `tests/unit/test_align.py`, `tests/unit/test_subtitle_project.py`, `tests/unit/test_studio.py`
- **Commit:** 9f16684
- **Learnings:**
  - Gotchas: `splice_studio_window_into_subtitle` must build `Timeline.segments` from audio segment bounds (`timeline.segments`) rather than `adjusted` cues because `adjusted_cues` extends `end_ms` for readable caption hold.
---

## [2026-10-26] - Phase 4 Task 4.1: Full speech- + dub-span background ducking in mux_dubbed_video
- **Implemented:** Added `merge_duck_spans` and `build_duck_spans` in `core/video_dub.py`, and updated `mux_dubbed_video` to accept `duck_spans` and gate both the sidechain trigger and a span-based volume attenuation filter across the union of original speech and dubbed spans.
- **Files changed:** `src/vienetts_app/core/video_dub.py`, `tests/unit/test_video_dub.py`
- **Learnings:**
  - Gotchas: In ffmpeg timeline editing (`enable='...'`), disabling a `volume` filter bypasses it (passing audio at 1.0×), so gating a sidechain trigger signal requires `volume=volume=0:enable='not(between(t,...))'`.
---

## [2026-10-26] - Phase 4 Task 4.2: Wire speech-bound refinement, LLM target language/slack, uniform unit placement, and duck spans in SubtitleController & AppController
- **Implemented:** Wired `refine_cues_speech_bounds` on video import/attach when cached `source_16k.wav` is present, language/slack-aware `max_chars_for_window` in `_cues_model`, `renderer.add_unit_clip` in `SubtitleController._place_unit`, and `build_duck_spans` in `SubtitleController.exportVideo` and `AppController.studioExportVideo`.
- **Files changed:** `src/vienetts_app/core/subtitle_project.py`, `src/vienetts_app/ui/subtitle_controller.py`, `src/vienetts_app/ui/controller.py`, `tests/unit/test_subtitle_controller.py`, `tests/unit/test_studio_controller.py`
- **Commit:** dfac5b8
- **Learnings:**
  - Patterns: Delegate `add_unit_clip` per-cue placements through `self.add_clip` with temporary `_unit_pretrimmed` / `_unit_forced_rate` state and injectable `split_fn` so unit tests spying on `SubtitleTrackRenderer.add_clip` or `split_unit_audio` observe every cue.
---
