# Audiobook sync reader & render telemetry — design

Date: 2026-08-28
Status: approved for implementation (user: "deep analyze … make plan and implement")
Track: extends `conductor/tracks/audiobook_epub_20260828` (adds FR-A9, FR-A10)

## Problem

The EPUB audiobook studio renders per-chapter WAVs and plays them back, but:

1. **Render progress** shows only a per-chapter segment bar (0–100 %) plus a
   static "x/y đã xong" chapter count. There is no estimated time remaining
   and no overall progress across a "Tạo tất cả" (render-all) run.
2. **While listening** there is no way to see the chapter text at all — the
   listener cannot read along.
3. **Nothing maps audio time to text**: no sentence/paragraph/word
   highlighting, no tap-to-seek in the text.

## Goal

- FR-A9 **Sync reader**: a togglable reader panel showing the chapter text
  while audio plays, with karaoke-style highlighting (active paragraph +
  active word), automatic follow-scroll, and click-a-paragraph-to-seek.
- FR-A10 **Render telemetry**: ETA for the in-flight chapter render and an
  overall progress bar for a render-all run.

Non-goals: forced alignment (audio↔text via ASR/DTW), exporting highlights,
re-synthesis of already-cached chapters (NFR-A1 still wins), changes to the
streaming playback path used by the Text tab.

## Key insight: exact segment timing is already on the wire

Chapter renders go through the worker's *chunked stream* mode: the chapter
text is split by the pure, deterministic `split_text_for_streaming` into
sentence-packed segments (≤ 512 chars). The worker emits, per segment:
`chunk_ready(chunk)` for each audio chunk **in order**, then
`progress(done=k+1)`. The final `done` signal carries the concatenated audio
(48 kHz float32).

Therefore: **the samples that arrive between progress tick k−1 and tick k are
exactly segment k's audio.** Counting them gives *exact* per-segment
`start_ms`/`end_ms` in the final WAV — no alignment model needed. The
audiobook controller already receives progress/done through the
synthesis-listener seam; only `chunk_ready` is not routed to the listener
today (one small, additive change in `AppController._on_chunk_ready`).

Within a segment (one or a few sentences), word timing is interpolated
linearly by character offset — the standard karaoke approximation; segment
boundaries are exact, so drift never accumulates across the chapter.

### Char-offset mapping

`split_text_for_streaming` collapses whitespace (`"\n\n"` between paragraphs
becomes a space when two units pack into one segment), so segment text is NOT
a verbatim substring of the chapter text. Offsets are recovered by a
lock-step word-token scan: tokenize chapter text into whitespace-delimited
spans once; consume tokens in order against each segment's tokens (prefix
match tolerated for the >512-char hard-split edge case). O(n), monotonic.

### Legacy chapters (cached before this feature)

No captured timing exists. On first playback we build an **approximate**
timeline: same segmentation, total duration from the player's `durationMs`,
allocated proportionally to segment character length. In-memory only
(never persisted — only measured timelines are written to disk).

## Architecture

```
workers/inference_worker.py      (unchanged)
ui/controller.py                 _on_chunk_ready: also delegate to an attached
                                 listener's optional on_synthesis_chunk
core/timeline.py                 NEW — pure alignment math
  SegmentSpan(text?, charStart, charEnd, startMs, endMs)
  map_segment_offsets(text, segments)        word-lockstep char mapping
  build_timeline(text, segments, samples, sr)  exact measured timeline
  estimate_timeline(text, durationMs)        proportional fallback
  locate_segment(timeline, posMs) -> i       binary search
  word_spans(text) -> [(a, b)]               precomputed token spans
  active_word(spans, char) -> (a, b)         containing/nearest word
  paragraphs(text) -> [{index,text,charStart,charEnd}]
core/audiobook.py                timeline persistence next to the WAV:
                                 ch_0000.timeline.json {version,sampleRate,
                                 approximate:false,segments:[{charStart,
                                 charEnd,startMs,endMs}]}
ui/audiobook_controller.py       capture during render; reader/sync state
ui/qml/AudiobookTab.qml          reader card + progress telemetry UI
```

### Data flow

- **Render**: `_start_render` snapshots `split_text_for_streaming(text)`;
  `on_synthesis_chunk` accumulates sample counts; `on_synthesis_progress`
  closes segments; `on_synthesis_done` validates `sum(samples) == len(audio)`
  → `build_timeline` → saved atomically with the WAV.
  Mismatch (engine quirk/fake) → estimate fallback persisted with
  `approximate: true` (duration is real, only intra-chapter allocation is
  proportional); chapters cached BEFORE this feature keep building
  in-memory-only estimates on first playback (nothing written retroactively).
- **Playback**: `_play_file` loads the chapter timeline (file → else build
  estimate once `durationMs` is known). Position ticks drive
  `locate_segment` + `active_word` → exposed QML state.
- **Reader**: QML reads `paragraphs`, `activeParagraph`, `activeCharStart`,
  `activeCharEnd`, `syncAvailable`; click → `seekToParagraph(i)`.

### QML surface additions (`audiobook.` context property)

```
readerOpen bool (rw)        readerVisible implies chapter open
paragraphs QVariantList    [{index,text,charStart,charEnd}]
activeParagraph int (-1)    activeCharStart/End int (word span, chapter coords)
syncAvailable bool          timeline loaded (measured or estimated)
seekToParagraph(i) slot     seek(first segment whose charStart ≥ para.charStart)
renderEtaMs int (-1)        ETA for in-flight chapter render
renderAllTotal int (0)      renderAllDone int (0)   — render-all run state
```

Reader card design (Signal system): sits between the chapter card and the
player bar; a "Văn bản" toggle button (open-book icon) in the player bar.
Active paragraph gets `accentSubtle` background; active word is bold accent
(rich-text span rebuilt only for the active paragraph). Follow-scroll fires on
paragraph change only (word ticks never scroll). Clicking a paragraph seeks
and keeps playing.

## Error handling

- Timeline JSON unreadable/corrupt → treated as missing (estimate path).
- `sum(samples) != len(audio)` → estimate fallback (never crash, log once).
- Zero-sample segment (silent engine) → kept with `startMs == endMs`,
  `locate_segment` skips empty spans.
- No timeline and no duration (player error) → reader shows plain text,
  `syncAvailable == false`, no highlight (graceful degradation).
- seek into empty span → clamps to the next non-empty segment start.

## Testing

- `tests/unit/test_timeline.py` (new): offset mapping (paragraph splits,
  packed segments, hard-split prefix), measured build, estimate, locate
  (binary search, boundaries, empty spans), word spans (CJK-free Vietnamese
  diacritics), paragraphs splitting.
- `test_audiobook.py`: timeline save/load round-trip, corrupt → None,
  WAV-without-timeline stays playable.
- `test_audiobook_controller.py`: chunk capture → measured timeline saved;
  active span follows position ticks; seekToParagraph; estimate fallback on
  legacy cache; readerOpen toggle; render ETA; render-all totals.
- `test_controller.py`: chunk_ready delegated to attached listener only.
- `smoke/test_ui_tabs.py`: reader toggle + highlight objectNames contract,
  offscreen, fake controller (same driver pattern).

## i18n

All new QML/controller strings are `qsTr`/`QT_TRANSLATE_NOOP` Vietnamese
source; regenerate `vienetts_en.ts` via `scripts/update_i18n.sh`, translate,
recompile — the unit suite gates unfinished entries.
