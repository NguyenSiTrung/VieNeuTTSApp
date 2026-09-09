# Mini Studio — Design (2026-09-09)

Scope: **B — offline polish + single-segment re-gen**. One Studio surface fed from all tabs.

## 1. Surface (§1, approved)

- New `StudioTab.qml`; per-tab `Studio` buttons on Text / Paragraph / Audiobook tabs.
- Button enabled iff a committed artifact exists (same guard as `exportButton`).
- `controller.openInStudio(artifactPath, owner, segmentHints)` loads artifact into Studio tab and switches to it. No per-tab duplicate editors.
- Studio reuses `Theme.qml` tokens, `PlaybackWaveform` (extended with selection range), existing `exportDialog` name filters (`*.wav *.mp3`).

## 2. Data model (§2, approved)

- New `core/studio.py` (NumPy + soundfile only; torch-free):
  - `StudioClip {id, label, source_path, start_frame, end_frame}` — one per paragraph (Text/Paragraph, split parity with importer chunking) or per chapter (Audiobook, existing `ch_XXXX.wav`).
  - `StudioProject {clips[], ops[]}` — non-destructive op stack, applied lazily.
  - Ops: `trim`, `silence_trim_db`, `fade_in/out_ms`, `gain_db` (−20..+12), `normalize_peak`, `speed` (reuse `core/audio.time_stretch_audio` WSOLA 0.5–2.0×), `gap_ms`, `reorder/concat`. Undo = pop op.
- Segment re-gen: one `SynthesisJob` via existing `workers/job_queue.py` for the edited segment text; result splices into that clip with 10 ms crossfade; old clip retained for undo.
- Render: preview → temp WAV + envelope via `compute_waveform_envelope`; final → `export_wav_file` / `export_audio_file` (WAV 16-bit PCM / MP3 via libsndfile), off GUI thread via `run_on_thread_pool`, completion on existing `exportFinished(path, ok)`.

Out of v1: EQ, noise reduction, multitrack mixing, MP3-import decode beyond reference clips.

## 3. UI flow (§3, approved)

- Entry: `Studio` button → `openInStudio(...)` → Studio tab shows waveform + clip list.
- Edit: waveform selection drives trim/fade scope; sliders for gain/fade/speed; silence-threshold control; clip rows with listen + re-generate-one actions.
- Preview: renders op stack to temp file, plays through existing player (same path as replay).
- Export: studio-rendered buffer becomes the export source through the existing `exportAudio` machinery (format follows suffix, `exportFormat` setting for bare names, off-thread + toast unchanged).
- Discard: reload artifact, clear ops.

## 4. Edges + verification (§4, approved)

- Invariants: 48 kHz mono float32 throughout; fades clamped to selection length; block-streamed render (same pattern as `export_wav_file`) so cap-length docs stay bounded; Windows `os.replace` retry honored.
- Degradation: re-gen failure keeps old clip + error toast; preview/export failure surfaces `errorText` without losing the op stack; empty selection disables trim/fade.
- Tests: unit on `core/studio.py` (each op round-trip, concat order, undo, splice crossfade length, paragraph-split parity); smoke QML `objectName` contract (studioTab, studioWaveform, studioOpStack, studioClipList, studioPreviewButton, studioExportButton); full suite + ruff gates unchanged. No new dependencies.
