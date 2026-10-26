# Specification: Natural Dubbing Pacing, Acoustic Speech-Window Alignment & Smart Timing (`dub_pacing_alignment_20261026`)

## Overview

In the current Dubbing & Subtitles pipeline (`core/subtitles.py`, `core/asr.py`, `core/diarization.py`, `core/llm_refine.py`, `core/align.py`, `core/subtitle_project.py`, `core/studio.py`, `core/video_dub.py`), each subtitle cue's `[start_ms, end_ms]` caption window is treated as the exact speech duration, and each synthesized clip is time-stretched in isolation. This causes five compounding quality issues:

1. **SRT caption windows $\neq$ actual speaking spans**: Human-authored `.srt` files and Whisper segments include lead-in reading pad (150–300 ms), trailing reading hold (300–800 ms), or internal pauses. Even though `source_16k.wav` and Nemotron-3 / acoustic diarization turns are available on disk, `Cue` only stores `[start_ms, end_ms]`, causing dubbed speech to start too early or misjudge the real speaking window.
2. **Untrimmed TTS head/tail silence inflates `natural_ms`**: Neural TTS engines emit 100–350 ms of leading/trailing silence or breath padding per clip. `SubtitleTrackRenderer._place()` measures `clip.size` before trimming edge silence, triggering false WSOLA speedups (`rate > 1.0×`) on takes that would fit at `1.0×` once trimmed.
3. **Accordion pacing from isolated per-cue compression**: `plan_cue()` compresses each cue strictly into its own `[start_ms, end_ms]` window up to `rate_cap` even when followed by 1–2 seconds of empty silence before the next cue. When `merge_sentences=True`, `split_unit_audio()` slices a single synthesized sentence proportionally by character count and stretches each fragment at a different WSOLA rate.
4. **LLM `maxChars` budget ignores speech bounds, post-cue slack, and language density**: `max_chars_for_window()` uses raw `cue.duration_ms` and a hardcoded `14.0 chars/s` for all languages, over-allocating text on cues with long reading holds or CJK target languages (`zh`/`ja`/`ko`) and under-allocating when borrowable post-cue silence exists.
5. **Shrunk subtitle hold & original voice bleed in `duck` mode**: When a dubbed take is shorter than the original speech/caption window, `adjusted_cues()` shrinks `end_ms` to `fit.end_ms` (causing captions to flash off screen early), and `mux_dubbed_video(..., mix_mode="duck")` releases `sidechaincompress` as soon as the short dub clip ends—allowing the source actor's trailing words to leak through.

---

## Functional Requirements

### FR-1: Acoustic Speech-Window Model & Source-Audio Detection (`core/subtitles.py`, `core/asr.py`, `core/diarization.py`, `ui/subtitle_controller.py`)
- **FR-1.1 (`Cue` speech bounds):** Extend `Cue` (`core/subtitles.py`) with optional `speech_start_ms: int | None = None` and `speech_end_ms: int | None = None` (validated when present: `start_ms <= speech_start_ms <= speech_end_ms <= end_ms`), plus properties `effective_speech_start_ms`, `effective_speech_end_ms`, and `speech_duration_ms`.
- **FR-1.2 (Sparse serialization & cache preservation):** `_cue_to_json` / `_cue_from_json` in `core/subtitle_project.py` must emit `speechStartMs` and `speechEndMs` **only when present** (`is not None`), so existing projects without speech bounds preserve byte-identical `render_fingerprint()` hashes.
- **FR-1.3 (Waveform VAD & Diarization turn refinement):** Add `refine_cues_speech_bounds(cues, wav_path, *, turns=None)` (`core/diarization.py` / `core/asr.py`) using a fast vectorized NumPy RMS energy VAD over the cached 16 kHz mono WAV (`source_16k.wav`), intersected with `RttmTurn` intervals when available, to populate `[speech_start_ms, speech_end_ms]` inside each cue's `[start_ms, end_ms]` window (with a 30 ms natural onset/offset guard and fallback to `None` when the slice is silent or unreadable).
- **FR-1.4 (Whisper word timestamps & clause splitting):** In `core/asr.py`, enable `word_timestamps=True` in `_run_faster_whisper` (and parse word timings from `verbose_json` in `_run_openai_whisper_api` when present) so `_split_long_segment` splits at exact word timestamps instead of character-length interpolation, and `normalize_asr_segments_to_cues` records accurate `speech_start_ms` / `speech_end_ms`. Automatically run `refine_cues_speech_bounds` when importing/attaching a video or running ASR/diarization.

### FR-2: Pre-Fit TTS Edge-Silence Trimming & Micro-Fade Boundary Polish (`core/audio.py`, `core/subtitle_project.py`, `core/studio.py`)
- **FR-2.1 (`trim_speech_edges`):** Add `trim_speech_edges(audio: np.ndarray, sample_rate: int = 48_000, *, threshold_db: float = -45.0, pad_ms: int = 15) -> np.ndarray` in `core/audio.py` that strips leading and trailing dead silence below `threshold_db` while retaining `pad_ms` of natural breath attack/release (and returning an all-silent clip untouched rather than raising).
- **FR-2.2 (Pre-fit trim in renderer & Studio):** Apply `trim_speech_edges` to synthesized unit/cue audio before measuring `natural_ms` and fitting in `SubtitleTrackRenderer._place()`, `render_alignment()`, and `refit_subtitle_clips()`, so TTS vocoder padding never triggers unnecessary WSOLA compression or late speech entry.
- **FR-2.3 (Boundary click prevention):** Apply a short 5 ms cosine edge taper (`apply_micro_fade`) when a clip is truncated or butt-joined at zero gap (`start_frame == self._cursor`) so overflowing/adjacent cues never click at sample boundaries.

### FR-3: Two-Stage Elastic Fit, Lookahead Slack Borrowing & Uniform Unit Rate (`core/align.py`, `core/subtitle_project.py`)
- **FR-3.1 (Speech-onset anchoring & two-stage elastic fit):** Update `plan_cue()` and `plan_alignment()` in `core/align.py`:
  - Anchor the target speech start at `cue.effective_speech_start_ms + policy.offset_ms` and target speech end at `cue.effective_speech_end_ms + policy.offset_ms`.
  - Accept optional `next_start_ms: int | None = None` lookahead in `plan_cue()`.
  - **Stage 1 (Comfort rate):** Compress up to `COMFORT_RATE = min(policy.rate_cap, 1.15)` to fit inside the cue's target window `[target_start, target_end]`.
  - **Stage 2 (Lookahead gap borrowing):** If `natural_ms / COMFORT_RATE > target_window`, borrow available silence after `target_end` up to `min(target_end + MAX_SLACK_BORROW_MS, next_start_ms - MIN_BREATH_GAP_MS)` (`MIN_BREATH_GAP_MS = 120`, `MAX_SLACK_BORROW_MS = 1200`; when `next_start_ms is None`, allow up to `max(cue.end_ms + policy.offset_ms, target_end + MAX_SLACK_BORROW_MS)`), keeping `rate = COMFORT_RATE` whenever the extended window accommodates the take.
  - **Stage 3 (Rate cap & neighbor rate clamping):** Only when the take still exceeds the extended window, increase `rate` up to `policy.rate_cap`. When `prev_rate` of the same speaker is provided and post-cue slack remains, clamp rate steps (`|rate - prev_rate| <= MAX_RATE_STEP = 0.20`) to prevent jarring tempo jumps between consecutive cues.
- **FR-3.2 (Uniform merged speech-unit stretching):** When `merge_sentences=True` in `dub` mode, compute and apply **one uniform WSOLA rate** across the entire merged speech unit (against the unit's spanned window from first cue start to last cue's borrowable end) and split the unit across cues using punctuation-weighted character spans (weighting `,;:` and `.!?` pauses above plain characters), so a single sentence never changes speed mid-utterance.

### FR-4: Language- & Slack-Aware LLM Timing Budget (`core/llm_refine.py`, `ui/subtitle_controller.py`)
- **FR-4.1 (Language-aware speaking rate):** Update `max_chars_for_window()` in `core/llm_refine.py` to accept `language: str = "vi"` (`chars_per_sec` table: `vi`/`en`/`fr`/`es`/`de`/`pt`/`it`/`ru` $\approx 14.0$, `zh` $\approx 4.8$, `ja` $\approx 6.0$, `ko` $\approx 5.5$) and optional `slack_ms: int = 0`.
- **FR-4.2 (Speech-window + slack prompt payload):** In `refine_cues_with_llm()`, compute each cue's effective speaking budget from `cue.speech_duration_ms` plus borrowable post-cue slack (up to `min(gap_after_ms - 120, 800)` at comfort rate `min(rate_cap, 1.20)`), and include `speechDurationMs` and `gapAfterMs` in the JSON prompt items so `fit_timing` and `translate_dub` produce lines that fit at `~1.0×–1.15×` without talking through silent scene pauses.

### FR-5: Readable Subtitle Hold & Full-Span Video Ducking (`core/align.py`, `core/video_dub.py`, `core/studio.py`, `ui/subtitle_controller.py`)
- **FR-5.1 (Readable caption hold in `adjusted_cues`):** Update `adjusted_cues()` in `core/align.py` so that when a placed cue ends earlier than its offset-adjusted SRT caption end (`fit.end_ms < srt_end_ms + max(0, fit.pushed_ms)`), the exported subtitle keeps its readable hold up to `min(srt_end_ms + max(0, fit.pushed_ms), next_placed_start_ms - 80)` instead of shrinking to a brief flash.
- **FR-5.2 (Full-span background ducking in `mux_dubbed_video`):** Update `mux_dubbed_video()` in `core/video_dub.py` (and callers in `SubtitleController.exportVideo` / `AppController.studioExportVideo`) to accept optional `duck_spans: Sequence[tuple[int, int]] | None = None` covering the union of each cue's `[min(speech_start_ms, fit.start_ms), max(speech_end_ms, fit.end_ms)]`. When `mix_mode == "duck"`, keep the background audio attenuated across both the original speech spans and the dubbed speech spans (with smooth attack/release transitions) so original actor speech never leaks through when a translated dub line finishes early.

---

## Non-Functional Requirements

- **Zero new PyPI dependencies**: All VAD, silence trimming, elastic alignment, and ffmpeg filtergraph generation use only `numpy`, `soundfile`, stdlib, and `ffmpeg`.
- **Backward compatibility & cache stability**: Existing `project.json` workspaces load without migration errors, and projects without `speech_start_ms` / `speech_end_ms` retain their exact pre-existing `render_fingerprint` where policy inputs match.
- **Streaming memory bound**: `SubtitleTrackRenderer` continues streaming clip-by-clip to `PCM_16` `track.wav` in $O(1)$ cue-window memory without loading full-movie audio into RAM.

---

## Acceptance Criteria

1. `trim_speech_edges()` strips leading/trailing silence from synthesized clips while keeping a 15 ms pad; a 1.0 s speech take with 300 ms of vocoder silence fits a 1.1 s cue window at `1.00×` instead of being compressed to `1.18×`.
2. In `dub` mode, a cue whose natural speech exceeds its `[start_ms, end_ms]` window by 400 ms but is followed by a 1000 ms silent gap borrows from the gap at `<= 1.15×` comfort rate without pushing the next cue or rushing at `1.40×`.
3. When `merge_sentences=True`, a multi-cue sentence unit is stretched at one uniform rate across its cues and split with punctuation-aware weights; adjacent cues in the unit have identical `CueFit.rate`.
4. `refine_cues_speech_bounds()` tightens `[speech_start_ms, speech_end_ms]` inside cues from a 16 kHz WAV (and `RttmTurn`s when present), leaving `[start_ms, end_ms]` unchanged and omitting `speechStartMs`/`speechEndMs` from JSON when `None`.
5. `max_chars_for_window()` scales character budgets by target language (`zh`/`ja`/`ko` vs `vi`/`en`) and incorporates `speech_duration_ms` + bounded post-cue slack.
6. `adjusted_cues()` preserves readable subtitle display hold when `fit.end_ms < srt_end_ms` without overlapping `next_fit.start_ms`.
7. `mux_dubbed_video(..., mix_mode="duck")` attenuates background video audio across the full union of original speech spans and dubbed audio spans so trailing source speech is suppressed even when the dub clip is shorter.
8. `ruff check .`, `ruff format --check .`, `scripts/update_i18n.sh`, and `pytest` all pass.

---

## Out of Scope

- Changing TTS model weights or adding external neural forced-aligner dependencies (e.g., MFA / wav2vec2).
- Slowing down speech below `1.0×` (`MIN_RATE` remains `1.0` so speech is never time-expanded into robotic drawl).
