# Mini Studio Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the mini studio: a single Studio tab with offline polish ops plus single-segment re-generation, fed from Text/Paragraph/Audiobook tabs, exporting through the existing WAV/MP3 machinery.

**Architecture:** New pure-NumPy `core/studio.py` (op stack + render + splice) under the existing artifact-first flow; thin controller slots reuse `generateStream`, `_on_terminal`, and `_start_export`; one new `StudioTab.qml` registered beside the existing tabs. No new dependencies, torch-free.

**Tech Stack:** Python + NumPy + soundfile (via `core/audio.py` helpers), PySide6 + QML, pytest with offscreen Qt.

**Spec:** `docs/superpowers/specs/2026-09-09-mini-studio-design.md`

## Global Constraints

- 48 kHz mono float32 throughout (`DEFAULT_SAMPLE_RATE` in `src/vienetts_app/core/audio.py`); never resample in studio code.
- No new third-party dependencies: only `numpy` and the existing `core/audio.py` helpers (`read_wav`, `write_wav_file`, `time_stretch_audio`, `compute_waveform_envelope`, `export_wav_file`, `export_audio_file`).
- Gain clamp −20..+12 dB; speed factor 0.5–2.0 (matches Settings reading-speed range); fades clamp to selection length.
- Final export reuses the existing off-thread machinery (`controller._start_export`-equivalent path, `exportFinished(path, ok)` signal); preview plays through the existing replay player.
- QML follows `Theme.qml` tokens and the tested `objectName` contract; ruff + full suite green per task.

---

### Task 1: Studio model + basic ops

**Files:**
- Create: `src/vienetts_app/core/studio.py`
- Test: `tests/unit/test_studio.py`

**Interfaces:**
- Consumes: `numpy`.
- Produces: `StudioClip` (frozen: `id: str`, `label: str`, `text: str`, `audio: np.ndarray`), op dataclasses `TrimOp(start_frame, end_frame)`, `FadeOp(edge, ms)`, `GainOp(db)`, `NormalizeOp(peak=1.0)`, `StudioProject` (frozen: `clips: tuple`, `ops: tuple`), `push_op(project, op) -> StudioProject`, `render_project(project) -> np.ndarray`. Later tasks use these exact names.

- [ ] **Step 1: Write the failing test**

```python
"""Studio model + basic ops (Task 1)."""
import numpy as np
from vienetts_app.core.studio import (
    GainOp, NormalizeOp, StudioClip, StudioProject, push_op, render_project,
)

def _tone(n=4800, freq=440.0):
    t = np.arange(n, dtype=np.float32) / 48_000
    return (0.5 * np.sin(2 * np.pi * freq * t)).astype(np.float32)

def _project(audio):
    return StudioProject(clips=(StudioClip(id="c0", label="P1", text="hello", audio=audio),), ops=())

def test_concat_single_clip_round_trips():
    audio = _tone()
    out = render_project(_project(audio))
    assert np.allclose(out, audio, atol=1e-6)

def test_gain_6db_doubles_amplitude():
    out = render_project(push_op(_project(_tone()), GainOp(db=6.0)))
    assert np.allclose(out, _tone() * (10.0 ** (6.0 / 20.0)), atol=1e-5)

def test_gain_clamps_at_minus_20_plus_12():
    from vienetts_app.core.studio import GainOp as G
    import pytest
    with pytest.raises(ValueError):
        G(db=13.0)
    with pytest.raises(ValueError):
        G(db=-21.0)

def test_normalize_sets_peak():
    audio = _tone() * 0.25
    out = render_project(push_op(_project(audio), NormalizeOp(peak=0.9)))
    assert abs(float(np.max(np.abs(out))) - 0.9) < 1e-3
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_studio.py -v`
Expected: FAIL with "No module named 'vienetts_app.core.studio'" (or ImportError).

- [ ] **Step 3: Write minimal implementation**

```python
"""Non-destructive mini-studio model: clips + op stack + render (48 kHz float32)."""
from __future__ import annotations
from dataclasses import dataclass, field
import numpy as np

SAMPLE_RATE = 48_000
MIN_GAIN_DB = -20.0
MAX_GAIN_DB = 12.0

@dataclass(frozen=True)
class StudioClip:
    id: str
    label: str
    text: str
    audio: np.ndarray  # mono float32 @48k

@dataclass(frozen=True)
class TrimOp:
    start_frame: int
    end_frame: int  # exclusive; -1 = end of mix
    def __post_init__(self):
        if self.start_frame < 0 or (self.end_frame != -1 and self.end_frame <= self.start_frame):
            raise ValueError(f"bad trim range {self.start_frame}:{self.end_frame}")

@dataclass(frozen=True)
class FadeOp:
    edge: str  # "in" | "out"
    ms: int
    def __post_init__(self):
        if self.edge not in ("in", "out"):
            raise ValueError(f"edge must be 'in'/'out', got {self.edge!r}")
        if self.ms < 0:
            raise ValueError("fade ms must be >= 0")

@dataclass(frozen=True)
class GainOp:
    db: float
    def __post_init__(self):
        if not (MIN_GAIN_DB <= self.db <= MAX_GAIN_DB):
            raise ValueError(f"gain {self.db} dB outside [{MIN_GAIN_DB}, {MAX_GAIN_DB}]")

@dataclass(frozen=True)
class NormalizeOp:
    peak: float = 1.0
    def __post_init__(self):
        if not 0.0 < self.peak <= 1.0:
            raise ValueError(f"peak must be in (0, 1], got {self.peak}")

StudioOp = TrimOp | FadeOp | GainOp | NormalizeOp

@dataclass(frozen=True)
class StudioProject:
    clips: tuple = ()
    ops: tuple = ()

def push_op(project: StudioProject, op: StudioOp) -> StudioProject:
    return StudioProject(clips=project.clips, ops=project.ops + (op,))

def _concat(project: StudioProject) -> np.ndarray:
    if not project.clips:
        raise ValueError("studio project has no clips")
    parts = [np.ascontiguousarray(c.audio, dtype=np.float32) for c in project.clips]
    return np.concatenate(parts).astype(np.float32)

def render_project(project: StudioProject) -> np.ndarray:
    mix = _concat(project)
    for op in project.ops:
        if isinstance(op, TrimOp):
            end = len(mix) if op.end_frame == -1 else min(op.end_frame, len(mix))
            mix = mix[op.start_frame:end]
        elif isinstance(op, GainOp):
            mix = (mix * (10.0 ** (op.db / 20.0))).astype(np.float32)
        elif isinstance(op, NormalizeOp):
            peak = float(np.max(np.abs(mix))) if mix.size else 0.0
            mix = mix if peak <= 1e-9 else (mix * (op.peak / peak)).astype(np.float32)
        elif isinstance(op, FadeOp):
            n = min(int(SAMPLE_RATE * op.ms / 1000), len(mix))
            if n > 0:
                ramp = np.linspace(0.0, 1.0, n, dtype=np.float32)
                mix = mix.copy()
                if op.edge == "in":
                    mix[:n] *= ramp
                else:
                    mix[-n:] *= ramp[::-1]
    return np.ascontiguousarray(mix, dtype=np.float32)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/unit/test_studio.py -v`
Expected: PASS (4 passed).

- [ ] **Step 5: Commit**

```bash
git add src/vienetts_app/core/studio.py tests/unit/test_studio.py
git commit -m "feat(studio): model, op stack, trim/gain/normalize/fade render"
```

### Task 2: Timeline ops + splice + undo

**Files:**
- Modify: `src/vienetts_app/core/studio.py`
- Test: `tests/unit/test_studio.py` (append new classes)

**Interfaces:**
- Consumes: Task 1 names + `time_stretch_audio(audio, rate)` from `vienetts_app.core.audio`.
- Produces: `SpeedOp(factor)`, `SilenceTrimOp(threshold_db=-50.0)`, `GapOp(ms)`, `pop_op(project)`, `move_clip(project, clip_id, new_index)`, `splice_clip_audio(project, clip_id, new_audio)`, `project_envelope(project) -> list[float]`. Task 4 uses all of these.

- [ ] **Step 1: Write the failing tests (append to `tests/unit/test_studio.py`)**

```python
class TestTimelineOps:
    def test_speed_passthrough_near_one_is_bit_identical(self):
        from vienetts_app.core.studio import SpeedOp, push_op, render_project
        audio = _tone(4800)
        out = render_project(push_op(_project(audio), SpeedOp(factor=1.0)))
        assert np.array_equal(out, audio)

    def test_speed_out_of_range_rejected(self):
        import pytest
        from vienetts_app.core.studio import SpeedOp
        with pytest.raises(ValueError):
            SpeedOp(factor=2.5)
        with pytest.raises(ValueError):
            SpeedOp(factor=0.0)

    def test_gap_inserts_silence_between_clips(self):
        from vienetts_app.core.studio import GapOp, StudioClip, StudioProject, push_op, render_project
        a = np.ones(100, dtype=np.float32)
        p = StudioProject(
            clips=(StudioClip(id="a", label="A", text="a", audio=a),
                   StudioClip(id="b", label="B", text="b", audio=a),),
            ops=(),
        )
        out = render_project(push_op(p, GapOp(ms=1000)))
        assert len(out) == 200 + 48_000
        assert np.allclose(out[100:100 + 48_000], 0.0)

    def test_move_clip_reorders_concat(self):
        from vienetts_app.core.studio import move_clip
        a = np.zeros(10, dtype=np.float32)
        b = np.ones(10, dtype=np.float32)
        from vienetts_app.core.studio import StudioClip, StudioProject, render_project
        p = StudioProject(clips=(StudioClip(id="a", label="A", text="a", audio=a),
                                 StudioClip(id="b", label="B", text="b", audio=b)), ops=())
        out = render_project(move_clip(p, "b", 0))
        assert np.allclose(out[:10], 1.0) and np.allclose(out[10:], 0.0)

    def test_undo_pops_last_op(self):
        from vienetts_app.core.studio import GainOp, pop_op, push_op
        p = push_op(push_op(_project(_tone(100)), GainOp(db=6.0)), GainOp(db=-6.0))
        assert len(pop_op(p).ops) == 1

    def test_splice_replaces_clip_with_crossfade(self):
        from vienetts_app.core.studio import splice_clip_audio, render_project
        a = np.zeros(1000, dtype=np.float32)
        new = np.ones(1000, dtype=np.float32)
        from vienetts_app.core.studio import StudioClip, StudioProject
        p = StudioProject(clips=(StudioClip(id="a", label="A", text="a", audio=a),), ops=())
        out = render_project(splice_clip_audio(p, "a", new))
        assert np.allclose(out[-100:], 1.0)  # tail fully replaced past the 10 ms blend

    def test_envelope_has_160_buckets(self):
        from vienetts_app.core.studio import project_envelope
        env = project_envelope(_project(_tone(48_000)))
        assert len(env) == 160 and all(0.0 <= v <= 1.0 for v in env)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/test_studio.py -v`
Expected: FAIL with ImportError (SpeedOp etc. not defined).

- [ ] **Step 3: Extend `studio.py` (append; extend the `StudioOp` union and `render_project`)**

```python
MIN_SPEED = 0.5
MAX_SPEED = 2.0
REGEN_CROSSFADE_MS = 10.0
ENVELOPE_BUCKETS = 160

@dataclass(frozen=True)
class SpeedOp:
    factor: float
    def __post_init__(self):
        if not (MIN_SPEED <= self.factor <= MAX_SPEED):
            raise ValueError(f"speed {self.factor} outside [{MIN_SPEED}, {MAX_SPEED}]")

@dataclass(frozen=True)
class SilenceTrimOp:
    threshold_db: float = -50.0

@dataclass(frozen=True)
class GapOp:
    ms: int
    def __post_init__(self):
        if self.ms < 0:
            raise ValueError("gap ms must be >= 0")
```

Render changes in `render_project` (gap is a join property, everything else applies in op order after concat):
1. Before concat, scan ops for the LAST `GapOp`; its `ms` becomes the inter-clip join silence (`gap_frames = ms * SAMPLE_RATE // 1000`, 0 when no GapOp). Change `_concat(project)` to `_concat(project, gap_frames)` which joins clip parts with `np.zeros(gap_frames)` between them.
2. `SpeedOp`: `from vienetts_app.core.audio import time_stretch_audio` (lazy import inside the branch so `studio.py` stays import-light); `mix = time_stretch_audio(mix, op.factor)`.
3. `SilenceTrimOp`: threshold `amp = 10 ** (op.threshold_db / 20)`; find first/last index with `|x| > amp`, slice to that span; empty mix or all-silent raises `ValueError("silence trim left nothing")`.
4. `GapOp` itself applies nothing in the op loop (it was consumed at concat) — `pass`.

New helpers:

```python
def pop_op(project: StudioProject) -> StudioProject:
    if not project.ops:
        raise ValueError("nothing to undo")
    return StudioProject(clips=project.clips, ops=project.ops[:-1])

def move_clip(project: StudioProject, clip_id: str, new_index: int) -> StudioProject:
    clips = list(project.clips)
    ids = [c.id for c in clips]
    if clip_id not in ids:
        raise ValueError(f"unknown clip {clip_id!r}")
    clip = clips.pop(ids.index(clip_id))
    clips.insert(max(0, min(new_index, len(clips))), clip)
    return StudioProject(clips=tuple(clips), ops=project.ops)

def splice_clip_audio(project: StudioProject, clip_id: str, new_audio: np.ndarray) -> StudioProject:
    """Replace one clip's audio with a 10 ms crossfade at its head (old→new)."""
    n_fade = int(SAMPLE_RATE * REGEN_CROSSFADE_MS / 1000)
    out = []
    for c in project.clips:
        if c.id != clip_id:
            out.append(c)
            continue
        new = np.ascontiguousarray(new_audio, dtype=np.float32)
        old = np.ascontiguousarray(c.audio, dtype=np.float32)
        if len(old) >= n_fade and len(new) >= n_fade and n_fade > 0:
            blend = np.linspace(0.0, 1.0, n_fade, dtype=np.float32)
            head = old[:n_fade] * (1.0 - blend) + new[:n_fade] * blend
            new = np.concatenate([head, new[n_fade:]])
        out.append(StudioClip(id=c.id, label=c.label, text=c.text, audio=new))
    if len(out) == len(project.clips) and all(c.id != clip_id for c in project.clips):
        raise ValueError(f"unknown clip {clip_id!r}")
    return StudioProject(clips=tuple(out), ops=project.ops)

def project_envelope(project: StudioProject) -> list[float]:
    mix = render_project(project)
    if mix.size == 0:
        return [0.0] * ENVELOPE_BUCKETS
    peaks = []
    for i in range(ENVELOPE_BUCKETS):
        seg = mix[i * len(mix) // ENVELOPE_BUCKETS:(i + 1) * len(mix) // ENVELOPE_BUCKETS]
        peaks.append(float(np.max(np.abs(seg))) if seg.size else 0.0)
    loudest = max(peaks) or 1.0
    return [min(p / loudest, 1.0) for p in peaks]
```

Also extend `StudioOp = TrimOp | FadeOp | GainOp | NormalizeOp | SpeedOp | SilenceTrimOp | GapOp`. Gap semantics: document in the module docstring that the LAST GapOp in the stack sets the inter-clip join silence (no GapOp = butt-joined clips).

- [ ] **Step 4: Run tests**

Run: `pytest tests/unit/test_studio.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/vienetts_app/core/studio.py tests/unit/test_studio.py
git commit -m "feat(studio): timeline ops, splice with crossfade, undo, envelope"
```

### Task 3: Project loaders (artifact + chapters)

**Files:**
- Modify: `src/vienetts_app/core/studio.py`
- Test: `tests/unit/test_studio.py` (append)

**Interfaces:**
- Consumes: `read_wav` (`core/audio.py`), `split_paragraphs` (`core/timeline.py`), `AudiobookStore.chapter_wav_path` (`core/audiobook.py:305`).
- Produces: `load_project_from_artifact(path, text) -> StudioProject`, `load_project_from_chapters(store, book_id, indices, texts) -> StudioProject`. Task 4 calls these from controller slots.

- [ ] **Step 1: Write the failing tests**

```python
class TestLoaders:
    def test_artifact_loader_splits_one_clip_per_paragraph(self, tmp_path):
        from vienetts_app.core.audio import write_wav_file
        from vienetts_app.core.studio import load_project_from_artifact, render_project
        audio = _tone(48_000)
        path = write_wav_file(audio, tmp_path / "art.wav")
        project = load_project_from_artifact(str(path), "first para\n\nsecond para")
        assert [c.label for c in project.clips] == ["1", "2"]
        assert [c.text for c in project.clips] == ["first para", "second para"]
        assert len(render_project(project)) == 48_000

    def test_artifact_loader_rejects_rate_mismatch(self, tmp_path):
        import soundfile as sf
        from vienetts_app.core.studio import load_project_from_artifact
        import pytest
        p = tmp_path / "odd.wav"
        sf.write(str(p), _tone(1000), 24_000)
        with pytest.raises(ValueError):
            load_project_from_artifact(str(p), "hi")

    def test_chapter_loader_skips_missing_chapters(self, tmp_path):
        from vienetts_app.core.audio import write_wav_file
        from vienetts_app.core.studio import load_project_from_chapters
        (tmp_path / "ch_0000.wav").parent.mkdir(parents=True, exist_ok=True)
        write_wav_file(_tone(1000), tmp_path / "ch_0000.wav")
        class FakeStore:
            def chapter_wav_path(self, book_id, index):
                from pathlib import Path
                return Path(tmp_path) / f"ch_{index:04d}.wav"
        project = load_project_from_chapters(FakeStore(), "b1", [0, 1], ["c0", "c1"])
        assert [c.id for c in project.clips] == ["ch0"]
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/unit/test_studio.py::TestLoaders -v`
Expected: FAIL with ImportError.

- [ ] **Step 3: Implement loaders (append to `studio.py`)**

```python
def load_project_from_artifact(path: str, text: str) -> StudioProject:
    """One clip per paragraph; audio sliced proportionally to char length.

    v1 approximation (no per-chunk timing exists upstream): the artifact is
    split into contiguous spans proportional to each paragraph's char count.
    Re-generating a clip replaces its audio wholesale, so drift self-heals.
    """
    from vienetts_app.core.audio import read_wav
    from vienetts_app.core.timeline import split_paragraphs
    audio, sr = read_wav(path)
    if sr != SAMPLE_RATE:
        raise ValueError(f"studio needs 48 kHz, got {sr}")
    paras = split_paragraphs(text)
    if not paras:
        return StudioProject(clips=(StudioClip(id="c0", label="1", text=text, audio=audio),), ops=())
    total_chars = sum(len(p["text"]) for p in paras) or 1
    clips, cursor = [], 0
    for i, p in enumerate(paras):
        n = len(audio) - cursor if i == len(paras) - 1 else int(round(len(audio) * len(p["text"]) / total_chars))
        clips.append(StudioClip(id=f"c{i}", label=str(i + 1), text=p["text"],
                                audio=np.ascontiguousarray(audio[cursor:cursor + n], dtype=np.float32)))
        cursor += n
    return StudioProject(clips=tuple(clips), ops=())

def load_project_from_chapters(store: object, book_id: str, indices: list[int], texts: list[str]) -> StudioProject:
    from vienetts_app.core.audio import read_wav
    clips = []
    for i, text in zip(indices, texts):
        wav = store.chapter_wav_path(book_id, i)  # AudiobookStore.chapter_wav_path
        if not wav.is_file():
            continue
        audio, sr = read_wav(wav)
        if sr != SAMPLE_RATE:
            raise ValueError(f"studio needs 48 kHz, got {sr}")
        clips.append(StudioClip(id=f"ch{i}", label=f"Ch {i + 1}", text=text,
                                audio=np.ascontiguousarray(audio, dtype=np.float32)))
    if not clips:
        raise ValueError("no rendered chapter audio to open in studio")
    return StudioProject(clips=tuple(clips), ops=())
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/unit/test_studio.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/vienetts_app/core/studio.py tests/unit/test_studio.py
git commit -m "feat(studio): artifact and chapter project loaders"
```

### Task 4: Controller + shell wiring

**Files:**
- Modify: `src/vienetts_app/ui/controller.py` (new signals/props/slots; hook `_on_terminal` at line ~2629; reuse `_start_export` at ~1838 and `_submit_text_job` used by `generateStream` at ~1590)
- Modify: `src/vienetts_app/ui/bridge.py` (TABS tuple ~line 75: add `("studio", QT_TRANSLATE_NOOP("ShellBridge", "Studio"))`; TAB_IDS derives automatically)
- Modify: `src/vienetts_app/ui/qml/Main.qml` (ids list ~line 364: append `"studio"`; StackLayout: add `StudioTab {}` after `ParagraphTab {}` — direct, like TextTab, since the studio is light)
- Test: `tests/unit/test_studio_controller.py` (new; construct AppController exactly like `tests/unit/test_controller.py` does — read that file's fixture/setup first, same constructor args)

**Interfaces:**
- Consumes: Task 2–3 names.
- Produces (exact slot/property names Task 5 QML binds): signals `studioProjectChanged`, `studioEnvelopeChanged`; properties `hasStudioProject: bool`, `studioClips: QVariantList` (list of `{"id": str, "label": str}` — ids feed `studioRegenClip`/`studioMoveClip`), `studioEnvelope: QVariantList`; slots `openInStudio(owner: str, text: str) -> bool`, `openChapterInStudio(book_id: str, index: int) -> bool`, `studioPushGain(db: float)`, `studioPushFade(edge: str, ms: int)`, `studioPushNormalize()`, `studioPushSpeed(factor: float)`, `studioPushSilenceTrim()`, `studioPushGap(ms: int)`, `studioPushTrim(start: int, end: int)`, `studioMoveClip(clip_id: str, index: int)`, `studioUndo() -> bool`, `studioPreview() -> bool`, `studioExport(path: str) -> bool`, `studioRegenClip(clip_id: str, voice: str)`.

- [ ] **Step 1: Write the failing test**

```python
"""Controller studio wiring (Task 4)."""
import numpy as np
import pytest
from pathlib import Path
from vienetts_app.core.artifacts import SynthesisArtifact
from vienetts_app.core.audio import write_wav_file


def _tone(n=4800, freq=440.0):
    t = np.arange(n, dtype=np.float32) / 48_000
    return (0.5 * np.sin(2 * np.pi * freq * t)).astype(np.float32)


# Fixture construction: open tests/unit/test_controller.py lines 1-80 and copy
# the AppController(...) call verbatim (same tmp data_dir + fake engine/worker
# seams — do not invent constructor args). Then wrap it:
@pytest.fixture()
def controller_with_artifact(tmp_path, qcoreapp):
    c = None  # replace with the copied construction, passing tmp_path as data_dir
    wav = write_wav_file(np.concatenate([_tone(), _tone()]), tmp_path / "art.wav")
    c._current_artifact = SynthesisArtifact(job_id="j" * 32, path=wav,
                                            sample_rate=48_000, frames=9600)
    return c


@pytest.fixture()
def controller_without_artifact(tmp_path, qcoreapp):
    return None  # same construction as above, but leave _current_artifact None


@pytest.fixture()
def controller_with_studio(controller_with_artifact):
    assert controller_with_artifact.openInStudio("text", "first\n\nsecond") is True
    return controller_with_artifact


def test_open_in_studio_needs_artifact(controller_without_artifact):
    assert controller_without_artifact.openInStudio("text", "hello") is False


def test_open_in_studio_loads_two_clips(controller_with_artifact):
    c = controller_with_artifact  # artifact wav exists; text has two paragraphs
    assert c.openInStudio("text", "first\n\nsecond") is True
    assert c.hasStudioProject is True
    assert list(c.studioClips) == [{"id": "c0", "label": "1"}, {"id": "c1", "label": "2"}]


def test_studio_undo_empty_is_false(controller_with_studio):
    assert controller_with_studio.studioUndo() is False
```

(Verify `SynthesisArtifact` field names against `src/vienetts_app/core/artifacts.py:34-44` before running — adjust the constructor call to match, keeping `path` pointed at the real wav.)

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/unit/test_studio_controller.py -v`
Expected: FAIL (slots don't exist / fixture to fill).

- [ ] **Step 3: Implement controller additions**

State: `self._studio_project: StudioProject | None = None`, `self._studio_regen_clip_id: str | None = None`, `self._studio_preview_path = ""`. Place beside `_current_artifact` init (~line 427).

`openInStudio(owner, text)`: guard `hasArtifact` (same failure string as export: "Chưa có gì để xuất — hãy tổng hợp âm thanh trước." — reuse `self.tr(...)`); `load_project_from_artifact(str(artifact.path), text)`; set + emit both signals; return True.

`openChapterInStudio(book_id, index)`: needs the audiobook store + chapter texts — call into the existing audiobook controller/store the same way `AudiobookTab` loads chapters (grep `chapter_wav_path\|load_book` in `audiobook_controller.py` for the exact accessor); on missing audio return False with errorText.

Op slots: each loads `self._studio_project`, applies the `studio.py` helper, reassigns, emits both signals. No-artifact/no-project → `set_error(tr("Chưa có dự án studio — hãy mở âm thanh trong Studio trước."))`, return False/None.

`studioPreview()`: `render_project` → `write_wav_file` to temp preview path → play through the existing replay path (`self.replay()`-equivalent file playback used for artifacts — grep `def replay` ~line 1961 and reuse its file-play helper, not a new player).

`studioExport(path)`: render → temp wav → same `work()`/`done()` off-thread shape as `_start_export` (~line 1838) calling `export_wav_file`/`export_audio_file` by suffix; emit `exportFinished`.

`studioRegenClip(clip_id, voice)`: find clip text in project; set `self._studio_regen_clip_id = clip_id`; call `self.generateStream(clip.text, voice)` (same as tab submit). In `_on_terminal` (~line 2629), after the existing `_set_current_artifact`-equivalent assignment succeeds: if `self._studio_regen_clip_id` is set and the new artifact file exists, `read_wav` it and `splice_clip_audio` into the project, clear the flag, emit signals. On regen-job failure/cancel: clear the flag, keep the old clip, surface existing errorText (no new error paths).

`bridge.py`: add the TABS entry. `Main.qml`: ids list + `StudioTab {}` child.

- [ ] **Step 4: Run tests**

Run: `pytest tests/unit/test_studio_controller.py tests/unit/test_studio.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/vienetts_app/ui/controller.py src/vienetts_app/ui/bridge.py src/vienetts_app/ui/qml/Main.qml tests/unit/test_studio_controller.py
git commit -m "feat(studio): controller slots, regen hook, shell tab registration"
```

### Task 5: StudioTab.qml + per-tab entry buttons

**Files:**
- Create: `src/vienetts_app/ui/qml/StudioTab.qml`
- Modify: `src/vienetts_app/ui/qml/qmldir` (add `StudioTab 1.0 StudioTab.qml` — match existing entries' format)
- Modify: `src/vienetts_app/ui/qml/TextTab.qml`, `ParagraphTab.qml`, `AudiobookTab.qml` (one `Studio` button each)
- Modify: `src/vienetts_app/ui/qml/components/qmldir` if new shared components are added (avoid new components — reuse AppButton/AppCombo/PageShell/PageHeader/PlaybackWaveform)

**Interfaces:**
- Consumes: Task 4 slot/property names (bind exactly — QML is case-sensitive).
- Produces: objectNames `studioTab`, `studioWaveform`, `studioOpStack`, `studioClipList`, `studioPreviewButton`, `studioExportButton`, plus `studioButton` on each of the three feeder tabs. Task 6 asserts these.

- [ ] **Step 1: Write the (failing) contract check**

Run: `grep -c "studioTab" src/vienetts_app/ui/qml/StudioTab.qml`
Expected: FAIL (file missing).

- [ ] **Step 2: Author `StudioTab.qml`** — PageShell/PageHeader scaffold (copy the header pattern from `TextTab.qml:18-26`), sections:
  - `PlaybackWaveform` instance (`objectName: "studioWaveform"`, envelope bound to `controller.studioEnvelope`, visible iff `controller.hasStudioProject`).
  - Op stack panel (`objectName: "studioOpStack"`): gain slider −20..+12 + Apply, fade in/out ms fields, speed slider 0.5..2.0, buttons for Normalize / Silence-trim / Undo — each calling the matching `controller.studioPush*` slot; all `enabled: controller.hasStudioProject && !controller.busy`.
  - Clip list (`objectName: "studioClipList"`): Repeater over `controller.studioClips` with per-row `Text { text: modelData.label }` + `↻` button calling `controller.studioRegenClip(modelData.id, voicePicker.selectedVoice)` (ids come from the `studioClips` dicts — never invent clip ids in QML).
  - Row with `studioPreviewButton` ("Nghe thử" → `controller.studioPreview()`) and `studioExportButton` ("Xuất" → exportDialog.open()) + a Save-File `FileDialog` mirroring `TextTab.qml:101-109` filters, `onAccepted: controller.studioExport(selectedFile path via the same toLocalPath helper)`.
  - Empty state label when `!hasStudioProject`: "Mở âm thanh trong Studio để chỉnh sửa." Vietnamese copy to match app language.
- [ ] **Step 3: Add feeder buttons** — in each of TextTab/ParagraphTab/AudiobookTab, beside the existing exportButton row: `AppButton { objectName: "studioButton"; text: qsTr("Studio…"); enabled: controller.hasArtifact && !controller.busy; onClicked: { controller.openInStudio(...) / openChapterInStudio(...); bridge.setCurrentTab("studio") } }`. Text/Paragraph pass their editor text (`textEditor.text` / `paragraphEditor.text`) and voice; Audiobook passes current book id + chapter index (mirror the subsection's existing `audiobook.*` property reads).
- [ ] **Step 4: Verify QML parses** — run the app's QML smoke import: `pytest tests/smoke/test_ui_shell.py -v` (shell must still load with the new tab registered).
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/vienetts_app/ui/qml/StudioTab.qml src/vienetts_app/ui/qml/qmldir src/vienetts_app/ui/qml/TextTab.qml src/vienetts_app/ui/qml/ParagraphTab.qml src/vienetts_app/ui/qml/AudiobookTab.qml
git commit -m "feat(studio): StudioTab surface and per-tab entry buttons"
```

### Task 6: Smoke coverage + full gates

**Files:**
- Modify: `tests/smoke/test_ui_tabs.py` (extend the FakeController surface + add a `studio_*` scenario following the file's existing `stream_bindings` pattern: fake gains `openInStudio` slot + `hasStudioProject`/`studioClips` (list of `{"id","label"}` dicts)/`studioEnvelope` properties; scenario activates the studio tab via `bridge.setCurrentTab("studio")` and asserts the six Task 5 objectNames exist; find the driver + scenario table via `grep -n "stream_bindings\|RESULT" tests/smoke/test_ui_tabs.py`)
- Test: full suite + ruff

**Interfaces:**
- Consumes: Task 5 objectNames.

- [ ] **Step 1: Extend FakeController + scenario** (see grep anchors above; keep the "no native dialogs headless" policy from the file header — drive export through the quick-export-equivalent seam, never `FileDialog`).
- [ ] **Step 2: Run the new scenario**

Run: `pytest tests/smoke/test_ui_tabs.py -k studio -v`
Expected: PASS.

- [ ] **Step 3: Run full gates**

Run: `ruff check src tests && ruff format --check src tests && pytest tests/ -x -q`
Expected: PASS (note: `TestRenderTelemetry::test_eta_completes_to_zero_on_last_segment` is a known flake — if it fails in the full run but passes in isolation, re-run it alone and record the outcome; see `conductor/product.md` Implementation Status).

- [ ] **Step 4: Commit**

```bash
git add tests/smoke/test_ui_tabs.py
git commit -m "test(studio): smoke coverage for Studio tab entry and surface"
```
