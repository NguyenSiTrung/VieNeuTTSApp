# Implementation Plan: Natural Dubbing Pacing, Acoustic Speech-Window Alignment & Smart Timing (`dub_pacing_alignment_20261026`)

## Overview

4-phase TDD implementation across `core/subtitles.py`, `core/audio.py`, `core/asr.py`, `core/diarization.py`, `core/llm_refine.py`, `core/align.py`, `core/subtitle_project.py`, `core/studio.py`, `core/video_dub.py`, `ui/subtitle_controller.py`, and `ui/controller.py` — automated verification only (`ruff check .`, `ruff format --check .`, `pytest`), no manual verification gates.

---

## Phase 1: Acoustic Speech-Window Model & Pre-Fit Audio DSP Primitives
<!-- execution: parallel -->

- [x] **Task 1.1: Extend `Cue` with optional `speech_start_ms` / `speech_end_ms` and sparse project JSON serialization** (765c10a)
  <!-- files: src/vienetts_app/core/subtitles.py, src/vienetts_app/core/subtitle_project.py, tests/unit/test_subtitles.py, tests/unit/test_subtitle_project.py -->
  - Red tests:
    - `Cue` accepts optional `speech_start_ms` and `speech_end_ms`, validates `start_ms <= speech_start_ms <= speech_end_ms <= end_ms`, and exposes `effective_speech_start_ms`, `effective_speech_end_ms`, and `speech_duration_ms`.
    - `_cue_to_json` / `_cue_from_json` round-trip `speechStartMs` and `speechEndMs` when set, and omit both keys when `None` so existing `render_fingerprint` golden hashes stay byte-identical.
  - Green implementation:
    - Add `speech_start_ms: int | None = None` and `speech_end_ms: int | None = None` plus validation and helper properties to `Cue`.
    - Update `_cue_to_json` and `_cue_from_json` in `core/subtitle_project.py` with sparse key emission.

- [x] **Task 1.2: Add `trim_speech_edges` and `apply_micro_fade` DSP helpers in `core/audio.py`** (6a6335d)
  <!-- files: src/vienetts_app/core/audio.py, tests/unit/test_audio.py -->
  - Red tests:
    - `trim_speech_edges()` strips leading and trailing silence below `threshold_db=-45.0` while preserving `pad_ms=15` of natural attack/release guard on both ends.
    - `trim_speech_edges()` returns an all-silent or very short array untouched without raising.
    - `apply_micro_fade()` applies a smooth cosine taper (`fade_ms=5.0`) to clip edges without altering interior samples.
  - Green implementation:
    - Implement vectorized `trim_speech_edges` and `apply_micro_fade` in `core/audio.py`.

---

## Phase 2: Source-Audio Speech Detection & Smart LLM Timing Budget
<!-- execution: parallel -->
<!-- depends: phase1 -->

- [x] **Task 2.1: Acoustic RMS VAD + diarization turn speech-bound refinement and Whisper word-timestamp splitting** (85e31e9)
  <!-- files: src/vienetts_app/core/diarization.py, src/vienetts_app/core/asr.py, tests/unit/test_diarization.py, tests/unit/test_asr.py -->
  - Red tests:
    - `refine_cues_speech_bounds(cues, wav_path, turns=...)` detects active speech onset/offset inside a cue that has 250 ms leading silence and 500 ms trailing silence in `source_16k.wav`, intersects with `RttmTurn` bounds when present, and keeps `Cue.start_ms` / `Cue.end_ms` intact.
    - `_run_faster_whisper` passes `word_timestamps=True`; `_split_long_segment` uses word timestamps when available so split points land on true word boundaries rather than character-ratio approximations, and `normalize_asr_segments_to_cues` populates `speech_start_ms` / `speech_end_ms`.
  - Green implementation:
    - Implement `refine_cues_speech_bounds` in `core/diarization.py` and call it in `assign_speakers_from_turns` / `diarize_cues`.
    - Add optional `words` timing tuples to `RawSegment` in `core/asr.py`, enable `word_timestamps=True` in `_run_faster_whisper`, parse word timestamps in `_run_openai_whisper_api` when present, and refine cue speech bounds against the extracted 16 kHz WAV in `transcribe_media_to_cues`.

- [x] **Task 2.2: Language-aware speaking rates and speech-window + post-cue slack budgets in `core/llm_refine.py`** (54cd966)
  <!-- files: src/vienetts_app/core/llm_refine.py, tests/unit/test_llm_refine.py -->
  - Red tests:
    - `max_chars_for_window()` scales character budgets by language (`vi`/`en` ~14 cps vs `zh` ~4.8 cps, `ja` ~6.0 cps, `ko` ~5.5 cps) and adds bounded post-cue slack (`slack_ms`).
    - `refine_cues_with_llm()` computes `maxChars` from `cue.speech_duration_ms` (not inflated caption hold) plus borrowable gap before the next cue (`min(gap_after_ms - 120, 800)`), and includes `speechDurationMs` and `gapAfterMs` in the LLM JSON batch items.
  - Green implementation:
    - Add `LANGUAGE_CHARS_PER_SEC` lookup and `language` / `slack_ms` parameters to `max_chars_for_window`.
    - Update `refine_cues_with_llm` to compute per-cue `gap_after_ms` and speech-window-based `maxChars` for `fit_timing` and `translate_dub`.

---

## Phase 3: Elastic Alignment, Lookahead Slack Borrowing & Uniform Unit Rate
<!-- execution: sequential -->
<!-- depends: phase1, phase2 -->

- [x] **Task 3.1: Two-stage elastic `plan_cue` / `plan_alignment` and readable `adjusted_cues` caption hold** (3d30d0c)
  <!-- files: src/vienetts_app/core/align.py, tests/unit/test_align.py -->
  - Red tests:
    - `plan_cue` anchors speech start at `cue.effective_speech_start_ms + policy.offset_ms`.
    - A cue that exceeds its target window at `1.0×` first compresses gently up to `COMFORT_RATE = min(rate_cap, 1.15)`, then borrows post-cue silence up to `min(target_end + 1200, next_start_ms - 120)` before increasing `rate` above `1.15×`.
    - Same-speaker consecutive cues smooth rate jumps (`|rate - prev_rate| <= 0.20`) when post-cue slack permits.
    - `adjusted_cues()` preserves readable subtitle display hold (`max(fit.end_ms, min(srt_end_ms + max(0, fit.pushed_ms), next_fit.start_ms - 80))`) when `fit.end_ms < srt_end_ms`.
  - Green implementation:
    - Update `plan_cue`, `plan_alignment`, and `adjusted_cues` in `core/align.py` with `next_start_ms`, `prev_rate`, two-stage elastic fitting, and caption-hold preservation.

- [x] **Task 3.2: Uniform merged speech-unit rate, punctuation-weighted splitting & pre-fit silence trimming in `SubtitleTrackRenderer` and `Studio`** (9f16684)
  <!-- files: src/vienetts_app/core/align.py, src/vienetts_app/core/subtitle_project.py, src/vienetts_app/core/studio.py, tests/unit/test_align.py, tests/unit/test_subtitle_project.py, tests/unit/test_studio.py -->
  <!-- depends: task1 -->
  - Red tests:
    - `SubtitleTrackRenderer` and `render_alignment` trim edge silence via `trim_speech_edges` before planning, so a clip with 300 ms of vocoder silence is not falsely compressed.
    - When `merge_sentences=True`, a multi-cue unit is stretched at one uniform rate (`add_unit_clip` / `split_unit_audio` with punctuation pause weights) so every cue in the merged sentence shares the same `CueFit.rate` without mid-sentence tempo shifts.
    - `refit_subtitle_clips` in `core/studio.py` trims synthesized raw audio edges and passes `next_start_ms` lookahead when fitting.
  - Green implementation:
    - Enhance `split_unit_audio` with punctuation pause weighting, add uniform unit planning/stretching (`add_unit_clip` on `SubtitleTrackRenderer`), wire `trim_speech_edges` + `apply_micro_fade` + `next_start_ms` lookahead in `SubtitleTrackRenderer._place` and `core/studio.py:refit_subtitle_clips`.

---

## Phase 4: Full-Span Video Ducking & Controller End-to-End Integration
<!-- execution: sequential -->
<!-- depends: phase2, phase3 -->

- [x] **Task 4.1: Full speech- + dub-span background ducking in `mux_dubbed_video`** (c9ac3f2)
  <!-- files: src/vienetts_app/core/video_dub.py, tests/unit/test_video_dub.py -->
  - Red tests:
    - `mux_dubbed_video(..., mix_mode="duck", duck_spans=[...])` builds an ffmpeg filtergraph that keeps background video audio attenuated across the union of original speech spans `[speech_start_ms, speech_end_ms]` and dubbed spans `[fit.start_ms, fit.end_ms]` (with smooth attack/release transitions), preventing source speech bleed when a dub clip finishes early.
  - Green implementation:
    - Add `duck_spans: Sequence[tuple[int, int]] | None = None` (and automatic span derivation when `cues` / `srt_path` are provided) to `mux_dubbed_video` in `core/video_dub.py`.

- [x] **Task 4.2: Wire speech-bound refinement, LLM target language/slack, uniform unit placement, and duck spans in `SubtitleController` & `AppController`** (dfac5b8)
  <!-- files: src/vienetts_app/ui/subtitle_controller.py, src/vienetts_app/ui/controller.py, tests/unit/test_subtitle_controller.py, tests/unit/test_studio_controller.py -->
  <!-- depends: task1 -->
  - Red tests:
    - Attaching/importing a video or running `transcribeVideo` / `runDiarization` refines cue speech bounds from `source_16k.wav` when available.
    - `_place_unit` in `SubtitleController` routes merged units through `renderer.add_unit_clip` so merged sentences render at a uniform rate.
    - `SubtitleController.exportVideo` and `AppController.studioExportVideo` pass full speech+dub `duck_spans` into `mux_dubbed_video`.
  - Green implementation:
    - Wire `refine_cues_speech_bounds`, `add_unit_clip`, language-aware LLM refinement, and `duck_spans` in `ui/subtitle_controller.py` and `ui/controller.py`.
