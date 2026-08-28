# Audiobook Sync Reader & Render Telemetry — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Karaoke-style chapter reader synced to audiobook playback (word/paragraph highlight, follow-scroll, click-to-seek) plus render ETA and overall render-all progress.

**Architecture:** Capture exact per-segment audio timing during chapter renders by counting `chunk_ready` samples between `progress` ticks (the deterministic `split_text_for_streaming` segmentation defines the segments); persist a `ch_XXXX.timeline.json` next to each WAV; at playback, map `positionMs → segment → interpolated char → word/paragraph` in `AudiobookController` and render a paragraph ListView in QML with rich-text word highlighting. Legacy cached chapters get an in-memory char-proportional estimate.

**Tech Stack:** Python 3.13 / PySide6 QObject controllers + QML (QtQuick Controls), pytest, stdlib-only core (no new deps — NFR-A2).

**Spec:** `docs/superpowers/specs/2026-08-28-audiobook-sync-reader-design.md`

## Global Constraints

- No new dependencies (NFR-A2). No git commits/pushes (AGENTS.md conservative profile — report at handoff instead).
- Vietnamese is the `qsTr` source language; every new user-facing string must land in `vienetts_en.ts` + recompiled `.qm` (`scripts/update_i18n.sh`), or the unit suite fails.
- Quality gates: `.venv/bin/ruff check .`, `.venv/bin/ruff format --check .`, `.venv/bin/pytest` all green.
- QML objectNames are the tested contract — new interactive elements need objectNames and smoke coverage.
- Synthesis rate is 48 000 Hz everywhere (`SAMPLE_RATE` in `audiobook_controller.py`).
- Never re-synthesize cached chapters (NFR-A1); timeline capture must not alter render semantics.

---

### Task 1: `core/timeline.py` — pure alignment math

**Files:**
- Create: `src/vienetts_app/core/timeline.py`
- Test: `tests/unit/test_timeline.py`

**Interfaces (produced, used by Tasks 2/4/5):**
```python
TIMELINE_VERSION = 1
@dataclass(frozen=True) class SegmentSpan: char_start: int; char_end: int; start_ms: int; end_ms: int
@dataclass(frozen=True) class Timeline: segments: tuple[SegmentSpan, ...]; approximate: bool = False
def split_paragraphs(text: str) -> list[dict[str, Any]]        # [{index,text,charStart,charEnd}]
def word_spans(text: str) -> list[tuple[int, int]]             # non-whitespace token spans
def active_word(spans: list[tuple[int,int]], char_index: int) -> tuple[int, int]
def map_segment_offsets(text: str, segments: list[str]) -> list[tuple[int, int]]
def build_timeline(text: str, segments: list[str], segment_samples: list[int], sample_rate: int) -> Timeline
def estimate_timeline(text: str, duration_ms: int, segments: list[str] | None = None) -> Timeline
def locate_segment(timeline: Timeline, position_ms: int) -> int   # -1 when nothing matches
def paragraph_start_ms(timeline: Timeline, paragraph_char_start: int) -> int  # -1 when no segment matches
def timeline_to_json(timeline: Timeline) -> dict[str, Any]
def timeline_from_json(data: Any) -> Timeline | None
```

- [ ] **Step 1: Write failing tests** covering: token/word spans (Vietnamese diacritics, punctuation attached to words); `split_paragraphs` offsets with leading/trailing whitespace and blank chunks; `map_segment_offsets` mapping packed segments across `"\n\n"` paragraph joins (segment text has spaces where the chapter has `\n\n`) and a >cap hard-split prefix token; `build_timeline` cumulative ms math (48000 Hz), zero-sample segment, length mismatch raises `ValueError`; `estimate_timeline` proportional allocation, total ≈ duration, `approximate=True`; `locate_segment` boundaries (before first → 0? no: -1 only when empty timeline; pos < first start → 0; inside; at exact boundary; beyond end → last; zero-duration spans skipped); `paragraph_start_ms` finds first segment ending after the paragraph start; json round-trip incl. `approximate` flag, `timeline_from_json(None/garbage) → None`.
- [ ] **Step 2: Run** `uv run --python 3.13 pytest tests/unit/test_timeline.py -q 2>/dev/null || .venv/bin/pytest tests/unit/test_timeline.py -q` → module-not-found failure.
- [ ] **Step 3: Implement** `src/vienetts_app/core/timeline.py`:

```python
"""Chapter audio↔text alignment (FR-A9): pure functions, no Qt, no deps.

A Timeline maps each render segment (see core.engine.split_text_for_streaming)
to an exact [start_ms, end_ms] window of the chapter WAV plus the segment's
[char_start, char_end) offsets in the chapter text. Measured timelines come
from the render pipeline (samples per segment); estimates allocate the WAV
duration proportionally to segment length (legacy caches, approximate=True).

Segment text is NOT a verbatim substring of the chapter text (the splitter
collapses whitespace, so "\\n\\n" between paragraphs becomes " " when two
units pack into one segment) — offsets are recovered by a lock-step word
scan (map_segment_offsets). Word-level karaoke then interpolates a char
position inside the active segment and expands it to a word span.
"""
from __future__ import annotations
import bisect
from dataclasses import dataclass
from typing import Any
from vienetts_app.core.engine import split_text_for_streaming

TIMELINE_VERSION = 1

@dataclass(frozen=True)
class SegmentSpan:
    char_start: int; char_end: int; start_ms: int; end_ms: int

@dataclass(frozen=True)
class Timeline:
    segments: tuple[SegmentSpan, ...]
    approximate: bool = False

def word_spans(text): ...      # scan non-space runs -> [(a,b)]
def active_word(spans, char_index): ...  # bisect on starts; containing else next; (-1,-1) if none
def split_paragraphs(text): ... # split("\n\n"), keep char offsets, strip chunks
def map_segment_offsets(text, segments): ...  # lock-step: tokens of text vs segment.split(); monotonic pointer; prefix-tolerant
def build_timeline(text, segments, segment_samples, sample_rate): ...  # raises ValueError on len mismatch / sample_rate<=0
def estimate_timeline(text, duration_ms, segments=None): ...  # weights len(seg); round; approximate=True
def locate_segment(timeline, position_ms): ...  # last non-empty span with start<=pos; beyond end -> last non-empty; else -1
def paragraph_start_ms(timeline, paragraph_char_start): ...  # first non-empty span with char_end > paragraph_char_start -> start_ms
def timeline_to_json(timeline): ...  # {"version":1,"approximate":..,"segments":[{charStart,charEnd,startMs,endMs}]}
def timeline_from_json(data): ...   # validates shapes; None on anything wrong
```

- [ ] **Step 4:** pytest green; `.venv/bin/ruff check src/vienetts_app/core/timeline.py`.
- [ ] **Step 5:** Checkpoint (no commit per AGENTS.md).

### Task 2: Timeline persistence in `AudiobookLibrary`

**Files:**
- Modify: `src/vienetts_app/core/audiobook.py`
- Test: `tests/unit/test_audiobook.py`

**Interfaces (produced, used by Tasks 4/5):**
```python
TIMELINE_SUFFIX = ".timeline.json"   # ch_0000.timeline.json next to ch_0000.wav
def timeline_path(self, book_id: str, index: int) -> Path
def save_chapter_timeline(self, book_id: str, index: int, timeline: Timeline) -> Path   # AudiobookError like siblings
def load_chapter_timeline(self, book_id: str, index: int) -> Timeline | None            # None when WAV missing/corrupt JSON
```

- [ ] **Step 1: Failing tests:** round-trip save→load preserves spans + approximate flag; `load` returns None when no file, None on corrupt JSON (write garbage), None when the chapter WAV is missing (timeline without audio is useless); `save` raises `AudiobookError` for unknown book/index; file is named `ch_0000.timeline.json`.
- [ ] **Step 2:** Run → fails (AttributeError).
- [ ] **Step 3: Implement** — `timeline_path` mirrors `chapter_wav_path` (pattern `CHAPTER_WAV_PATTERN.format(...) + TIMELINE_SUFFIX`); `save_chapter_timeline` validates via `self._chapter(self.load_book(...))` then `_write_json_atomic(timeline_to_json(timeline))` (import `timeline_to_json, timeline_from_json, Timeline` from `core.timeline`); `load_chapter_timeline` returns None unless `has_chapter_audio`, delegates to `timeline_from_json(_read_json(...))`.
- [ ] **Step 4:** pytest `tests/unit/test_audiobook.py -q` green.
- [ ] **Step 5:** Checkpoint.

### Task 3: Route `chunk_ready` to the synthesis listener

**Files:**
- Modify: `src/vienetts_app/ui/controller.py` (`_on_chunk_ready` + seam docstring)
- Test: `tests/unit/test_controller.py`

**Interfaces (produced, used by Task 4):** attached listeners with an `on_synthesis_chunk(chunk)` method now receive every stream chunk (duck-typed, optional — listeners without it are unaffected); chunks of listener-owned jobs never reach the app stream sink.

- [ ] **Step 1: Failing test:** attach a recording fake listener, emit `worker.chunk_ready` while attached → fake got the chunk and `_stream_active` stayed False; detach → chunks ignored. (Follow the existing FakeWorker seam in `test_controller.py`.)
- [ ] **Step 2:** Run → fails.
- [ ] **Step 3: Implement** in `_on_chunk_ready`:
```python
def _on_chunk_ready(self, chunk: Any) -> None:
    """Stream session live? Then this chunk becomes audio (FR-4.1).

    Listener-owned jobs (audiobook renders) route chunks to the listener's
    optional ``on_synthesis_chunk`` instead — it counts samples per segment
    to build the chapter timeline (FR-A9) — and never feed the app sink.
    """
    if self._synthesis_listener is not None:
        handler = getattr(self._synthesis_listener, "on_synthesis_chunk", None)
        if handler is not None:
            handler(chunk)
        return
    if not self._stream_active or self._stream_playback is None:
        return
    ...
```
plus one line in the class-docstring seam contract (`on_synthesis_chunk(chunk)` optional).
- [ ] **Step 4:** pytest `tests/unit/test_controller.py -q` green.
- [ ] **Step 5:** Checkpoint.

### Task 4: Render capture + telemetry in `AudiobookController`

**Files:**
- Modify: `src/vienetts_app/ui/audiobook_controller.py`
- Test: `tests/unit/test_audiobook_controller.py`

**Interfaces (produced, used by Tasks 5/6):** existing render flow unchanged externally; new QML properties `renderEtaMs: int` (-1 unknown, NOTIFY `renderEtaMsChanged`), `renderAllTotal: int`, `renderAllDone: int` (NOTIFY each); internal: `self._render_segments/_segment_samples/_pending_samples/_segments_closed` bookkeeping and `on_synthesis_chunk` entry point; timeline persisted on every successful render.

- [ ] **Step 1: Failing tests** (drive FakeWorker signals like existing render tests): after `_start_render` and emitting `chunk_ready` arrays + per-segment `progress` ticks + `done` with concatenated audio of matching length → `library.load_chapter_timeline(book, idx)` returns a measured timeline whose segment ms match the sample counts and whose char offsets map into the chapter text; mismatched totals (audio longer than counted samples) → timeline still saved but `approximate is True`; `renderEtaMs` is -1 before the first segment tick and >= 0 after ≥1 tick with `total > done`; `renderAllPending` sets `renderAllTotal` to the count of pending+uncached chapters and each successful landing increments `renderAllDone`; cancel resets ETA to -1. Update the FakeEngine/FakeWorker in that suite only if needed (tests emit signals manually).
- [ ] **Step 2:** Run → fails.
- [ ] **Step 3: Implement:**
  - `_start_render`: snapshot `self._render_segments = split_text_for_streaming(text)`; reset `_segment_samples=[]`, `_pending_samples=0`, `_segments_closed=0`, `_render_started_at=time.monotonic()`, `_render_eta_ms=-1` (+ emits).
  - `on_synthesis_chunk(self, chunk)`: `self._pending_samples += int(np.asarray(chunk).size)`.
  - `on_synthesis_progress`: after the fraction update, close segments: `while self._segments_closed < done and self._segments_closed < len(self._render_segments): self._segment_samples.append(self._pending_samples); self._pending_samples = 0; self._segments_closed += 1`; then if `done >= 1`: `elapsed = time.monotonic() - self._render_started_at; self._set_render_eta(int(elapsed / done * (total - done) * 1000)) if total > done else self._set_render_eta(0)`.
  - `on_synthesis_done` (after `save_chapter_audio` success, before `_kick`): build + persist:
```python
audio_samples = int(np.asarray(audio).size)
if self._render_segments and sum(self._segment_samples) == audio_samples and audio_samples > 0:
    timeline = build_timeline(text, self._render_segments, self._segment_samples, SAMPLE_RATE)
elif audio_samples > 0:
    timeline = estimate_timeline(text, round(audio_samples * 1000 / SAMPLE_RATE), self._render_segments or None)
else:
    timeline = None
if timeline is not None:
    try:
        self._library.save_chapter_timeline(self._state.record.id, index, timeline)
    except AudiobookError:
        logger.exception("saving chapter timeline failed")  # sync degrades to estimate later
self._reset_render_capture()  # clears segments/samples/eta(-1)
```
  - Telemetry helpers `_set_render_eta`, properties + NOTIFY signals; `renderAllPending` counts `pending`-status chapters without cached audio → `_render_all_total`, `_render_all_done = 0`; `on_synthesis_done` success increments when a render-all run is active; `cancelRender`/`on_synthesis_error(cancelled)` reset ETA and render-all totals.
- [ ] **Step 4:** pytest `tests/unit/test_audiobook_controller.py -q` green.
- [ ] **Step 5:** Checkpoint.

### Task 5: Reader state in `AudiobookController`

**Files:**
- Modify: `src/vienetts_app/ui/audiobook_controller.py`
- Test: `tests/unit/test_audiobook_controller.py`

**Interfaces (produced, used by Task 6):**
```
readerOpen bool (rw, NOTIFY readerOpenChanged)
paragraphs QVariantList [{index,text,charStart,charEnd}] (NOTIFY paragraphsChanged)
activeParagraph int -1 (NOTIFY activeParagraphChanged)
activeCharStart int -1 / activeCharEnd int -1 (NOTIFY activeSpanChanged)
syncAvailable bool (NOTIFY syncAvailableChanged)
seekToParagraph(int) @Slot
```

- [ ] **Step 1: Failing tests:** after `_play_file` of a chapter WITH a saved timeline → `paragraphs` non-empty, `syncAvailable is True`; FakePlayer `positionChanged` tick inside segment 1 → `activeParagraph`/`activeCharStart/End` point at a word inside that segment's char range; tick beyond the end → last segment's word; `seekToParagraph(i)` calls `player.positions` with the first segment ms overlapping that paragraph; legacy chapter (WAV cached, no timeline file): position tick does nothing until `durationChanged` arrives → estimate built → `syncAvailable is True` and highlighting works (approximate); `readerOpen` toggles + persists across `openBook`; `stopPlay`/`selectBook("")` resets actives to -1.
- [ ] **Step 2:** Run → fails.
- [ ] **Step 3: Implement:**
  - `_ensure_reader_loaded(index)`: if `self._reader_chapter != index`: load text → `split_paragraphs` + `word_spans`; `self._timeline = library.load_chapter_timeline(...)`; if None and WAV exists → `self._timeline_estimated = False` (build once duration arrives); emit `paragraphsChanged`, `syncAvailableChanged`; reset actives.
  - `_play_file` calls `_ensure_reader_loaded(index)`; `openBook` calls it for the resume chapter; `readerOpen` setter calls it when opening with a chapter selected.
  - `_on_player_position`: existing body + `self._update_active_span()`; `_on_player_duration`: if a chapter is loaded, no timeline, `not self._timeline_estimated`, `ms > 0` → `self._timeline = estimate_timeline(text, ms)`; `self._timeline_estimated = True`; emit `syncAvailableChanged`; then `_update_active_span()`.
  - `_update_active_span()`: `locate_segment` → interpolate `char = cs + (pos-start)/(end-start) * (ce-cs)` (guard empty/`cs<0`) → `active_word(self._word_spans, char)` → paragraph via bisect over `_paragraphs`; set+emit on change (`activeParagraphChanged` when paragraph changed, `activeSpanChanged` when word span changed). No timeline → reset to -1 and emit.
  - `seekToParagraph(i)`: guards (state, `0 <= i < len(self._paragraphs)`, timeline) → `ms = paragraph_start_ms(self._timeline, self._paragraphs[i]["charStart"])`; if `ms >= 0`: `self.seek(ms)`.
  - `selectBook("")`/`removeBook`/`shutdown`: clear reader state (`_paragraphs=[]`, `_timeline=None`, actives -1, `_reader_chapter=-1`) + emits.
- [ ] **Step 4:** pytest green (whole unit dir).
- [ ] **Step 5:** Checkpoint.

### Task 6: QML reader card + progress telemetry + i18n

**Files:**
- Modify: `src/vienetts_app/ui/qml/AudiobookTab.qml`, `src/vienetts_app/ui/i18n/vienetts_en.ts` (+ `.qm`)
- Test: `tests/smoke/test_ui_tabs.py` (Task 7)

**Interfaces (produced):** objectNames `readerToggleButton`, `readerCard`, `readerView`, `readerParagraph`, `readerText`, `renderEtaLabel`, `renderAllProgressBar`, `renderAllProgressLabel`; fake-controller surface grows the Task-5 properties.

- [ ] **Step 1: QML** — player-bar toggle (next to transport): `AppButton { objectName:"readerToggleButton"; variant: audiobook.readerOpen ? "primary" : "secondary"; iconKind:"paragraph"; size:"sm"; text: qsTr("Văn bản"); onClicked: audiobook.readerOpen = !audiobook.readerOpen; ToolTip... }`. New AppCard between book card and player bar:
```qml
AppCard {
    id: readerCard
    objectName: "readerCard"
    Layout.fillWidth: true
    visible: audiobook.readerOpen && audiobook.currentBookId !== ""
        && audiobook.currentChapterIndex >= 0
    title: audiobook.currentChapterTitle
    ListView {
        id: readerView
        objectName: "readerView"
        Layout.fillWidth: true
        Layout.preferredHeight: Math.min(320, contentHeight)
        clip: true; spacing: Theme.spacingXs
        model: audiobook.paragraphs
        ScrollBar.vertical: ScrollBar { ... }
        delegate: Rectangle {
            id: readerParagraph
            objectName: "readerParagraph"
            required property var modelData
            readonly property bool isActive:
                audiobook.activeParagraph === readerParagraph.modelData.index
            width: readerView.width
            height: readerText.implicitHeight + Theme.spacingSm * 2
            radius: Theme.radiusMd
            color: readerParagraph.isActive ? Theme.accentSubtle : "transparent"
            MouseArea { anchors.fill: parent; cursorShape: Qt.PointingHandCursor
                onClicked: audiobook.seekToParagraph(readerParagraph.modelData.index) }
            Text {
                id: readerText
                objectName: "readerText"
                anchors { fill: parent; margins: Theme.spacingSm }
                textFormat: Text.RichText
                wrapMode: Text.Wrap
                text: root.paragraphHtml(readerParagraph.modelData)
                color: Theme.text
                font.family: Theme.fontFamily
                font.pixelSize: Theme.fontSizeBase
            }
        }
        Connections {
            target: audiobook
            function onActiveParagraphChanged() {
                if (audiobook.playerState === "playing" && audiobook.activeParagraph >= 0)
                    Qt.callLater(readerView.positionViewAtIndex,
                                 audiobook.activeParagraph, ListView.Contain);
            }
        }
    }
}
```
plus tab-level helpers:
```qml
function escapeHtml(s) {
    return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}
function paragraphHtml(p) {
    controller.language;  // retranslate hook (pattern: statusText)
    const a = audiobook.activeCharStart, b = audiobook.activeCharEnd;
    if (!audiobook.syncAvailable || a < 0 || b <= a || b <= p.charStart || a >= p.charEnd)
        return escapeHtml(p.text);
    const la = Math.max(a, p.charStart) - p.charStart;
    const lb = Math.min(b, p.charEnd) - p.charStart;
    return escapeHtml(p.text.slice(0, la))
        + "<b><font color=\"" + Theme.accent.toString() + "\">"
        + escapeHtml(p.text.slice(la, lb)) + "</font></b>"
        + escapeHtml(p.text.slice(lb));
}
```
Telemetry in the render-progress row: `Label { objectName:"renderEtaLabel"; visible: audiobook.renderEtaMs >= 0; text: qsTr("còn ~%1").arg(root.fmtTime(audiobook.renderEtaMs)); ... }`; new RowLayout `objectName:"renderAllRow"` (visible: `audiobook.renderAllTotal > 0 && audiobook.renderingIndex >= 0`): `Label { text: qsTr("Tổng: %1/%2 chương").arg(audiobook.renderAllDone).arg(audiobook.renderAllTotal) }` + `ProgressBar { objectName:"renderAllProgressBar"; from:0; to:1; value: audiobook.renderAllTotal > 0 ? audiobook.renderAllDone / audiobook.renderAllTotal : 0; ...same styling as renderProgressBar... }` + `Label { objectName:"renderAllProgressLabel"; text: Math.round((audiobook.renderAllTotal > 0 ? audiobook.renderAllDone / audiobook.renderAllTotal : 0) * 100) + "%" }`. Update the objectName contract comment at the top of the file.
- [ ] **Step 2: i18n** — run `scripts/update_i18n.sh`; translate the new unfinished entries in `vienetts_en.ts` ("Văn bản" → "Transcript", "còn ~%1" → "~%1 left", "Tổng: %1/%2 chương" → "Overall: %1/%2 chapters", tooltip strings); re-run the script to compile the `.qm`; `pytest tests/unit/test_i18n.py -q` green.
- [ ] **Step 3: Checkpoint** — `.venv/bin/ruff check . && .venv/bin/ruff format --check .`.

### Task 7: Smoke coverage + full gates

**Files:**
- Modify: `tests/smoke/test_ui_tabs.py` (AUDIOBOOK_DRIVER: extend `FakeAudiobook` with the new NOTIFY properties + `seekToParagraph` slot; new scenarios `ab_reader`, `ab_render_all`; extend `ab_load` expected objectNames)
- Run: full quality gates.

- [ ] **Step 1:** Extend FakeAudiobook (mirror real NOTIFY surface: `readerOpen/paragraphs/activeParagraph/activeCharStart/activeCharEnd/syncAvailable/renderEtaMs/renderAllTotal/renderAllDone` + `readerOpenChanged/paragraphsChanged/activeParagraphChanged/activeSpanChanged/syncAvailableChanged/renderEtaMsChanged/renderAllTotalChanged/renderAllDoneChanged` signals + `seekToParagraph` slot appending to `hits`).
- [ ] **Step 2: `ab_reader` scenario:** book+current chapter set; assert `readerCard` hidden; flip `readerOpen=True` + emit → `readerCard` visible; set `_paragraphs=[{index:0,text:"Câu một.",charStart:0,charEnd:9},{index:1,text:"Câu hai.",charStart:11,charEnd:19}]`, `activeParagraph=1`, `activeCharStart=11`, `activeCharEnd=18`, emit → exactly one `readerParagraph` has the active background color and its `readerText` text contains `"<b>"`; click the active paragraph (MouseArea click via `.property`/`QMetaObject` pattern used elsewhere in the file) → `hits` contains `["seekToParagraph", 1]`.
- [ ] **Step 3: `ab_render_all` scenario:** `renderAllTotal=5`, `renderAllDone=2`, `renderingIndex=1`, `renderProgress=0.4`, `renderEtaMs=80000` → `renderAllRow` visible, `renderAllProgressBar.value == 0.4`, eta label text non-empty and contains "1:20"; with `renderingIndex=-1` → row hidden.
- [ ] **Step 4: Full gates:** `.venv/bin/ruff check .` && `.venv/bin/ruff format --check .` && `.venv/bin/pytest -q` → all green.
- [ [ ] **Step 5:** Update the spec's persistence note (measured normally; estimate persisted only as flagged fallback from a finished render), mark bead for closure, report.

## Self-Review

- Spec coverage: render telemetry (Task 4/6), reader+sync (Task 5/6), legacy estimate (Task 5), click-to-seek (Task 5/6), i18n (Task 6), robustness cases (Tasks 1/2/5 tests). Covered.
- Types consistent: `Timeline`/`SegmentSpan` names used identically in Tasks 1/2/4/5; property names match between Tasks 5 and 6/7.
- No placeholders beyond `...` in already-implemented bodies kept for brevity in Tasks 1 (full behavior specified in prose + tests) — the executor writes them from the test contract.
