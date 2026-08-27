# Plan: phase01_core_20260827

**Execution: sequential.** Phase 0 gates Phase 1 — the engine wrapper codes
against the API contract confirmed by the spike. Within each phase, tasks run
in order. Every task follows the workflow.md order: read patterns/learnings →
write failing tests first (where a code contract is being changed) → implement
minimal → run `ruff check`, `ruff format --check`, `pytest` → commit with a
conventional prefix → attach a git note with the task summary → append
learnings.

## Phase 0: Spike & Environment Validation
<!-- execution: sequential -->

- [x] Task 1: Scaffold repo toolchain and install vieneu (CPU stack)
  - `pyproject.toml` with pinned deps (`vieneu==3.3.0`; dev: pytest,
    pytest-qt, ruff), ruff config, `.gitignore`
  - `src/vienetts_app/` and `tests/{unit,integration,smoke}/` skeletons
  - Create venv, install CPU deps, verify `from vieneu import Vieneu` works
    and torch is absent
  - Record Python/wheel versions for the environment matrix

- [x] Task 2: Validate CPU/ONNX synthesis contract (macOS)
  - Spike script (tests/integration or scripts/): `infer()` on short vi + en
    text → assert `np.float32 @ 48 kHz`, duration > 0, non-silent
  - `list_preset_voices()` ≥ 20 entries; record fields for
    North/Central/South grouping
  - `add_voice()` from a 3–8 s clip → infer with cloned voice; check
    persistence across process restart
  - `tts.save()` WAV export; `denoise()` round-trip
  - Start `docs/spike-report.md` with findings

- [x] Task 3: Confirm infer_stream chunk format + latency
  - Iterate `infer_stream()`: record chunk dtype, shape, channel count
  - Measure first-chunk latency and streaming RTF vs §18 budgets
  - Confirm streaming is ONNX-only; note behavior when `backend="torch"`

- [x] Task 4: Measure performance & memory
  - Cold start (model load), warm short-text latency, long-text RTF, RSS
    during synthesis
  - Record numbers in the spike report against §18 budgets

- [x] Task 5: Validate offline bundling approach
  - Download weights once; test local model path loading + `HF_HUB_OFFLINE=1`
  - Identify minimal CPU int8 bundle: `onnx_int8/`, `denoiser.onnx`,
    `speaker_encoder.onnx`, tokenizer/config/preset-voice JSON
  - Draft `scripts/fetch_models.py` skeleton (download + verify hashes)

- [x] Task 6: Resolve open questions relevant to Phases 0–1 (§21)
  - PDF import library: PyMuPDF (AGPL) vs `pypdf` (MIT) — decide and record
  - mp3 reference-clip decode via soundfile on macOS; note cross-OS risk
  - Does the SDK expose temperature? mono vs stereo output channels?
  - Record decisions in the spike report; file beads for follow-ups outside
    track scope

- [x] Task 7: Cross-OS validation (Windows + Ubuntu)
  - Re-run the Task 2–4 spike scripts on Windows 10/11 and Ubuntu 22.04+
  - Record env matrix + measurements; note OS-specific install issues
  - If an environment is unavailable, record the gap in the spike report
    (do not block the track; CI covers it in Phase 6)

- [x] Task 8: Finalize spike report + confirmed API contract
  - `docs/spike-report.md` complete: environment matrix, measurements,
    decisions, and the API contract (signatures, dtypes/shapes, chunk
    format, voice-list fields) that Phase 1 codes against

## Phase 1: Core Engine (Headless)
<!-- execution: sequential -->
<!-- depends: phase0 -->

- [ ] Task 1: models.py — data model + validation
  - TDD: construct `EngineInfo`/`Settings`/`TTSRequest`/`TTSProgress`;
    defaults per §9; invalid values raise validation errors

- [ ] Task 2: settings.py — persistence
  - TDD: JSON round-trip via platformdirs dir (injectable for tests),
    defaults on first run, corrupt JSON → defaults + warning, no crash

- [ ] Task 3: detector.py — engine detection + heuristic
  - TDD with mocked torch/CUDA: full §6.1 matrix (NVIDIA ≥/\< 12.8, Apple
    Silicon, AMD/Arc, no GPU); §6.2 workload heuristic (streaming → onnx,
    short → onnx, long/bulk → torch if available); user override
    (`auto|onnx|torch`, `int8|fp32`) resolution

- [ ] Task 4: audio.py — WAV helpers
  - TDD: float32 @ 48 kHz → WAV encode in-memory (`QBuffer`-consumable) and
    to file; WAV header correctness; duration/format read-back via soundfile

- [ ] Task 5: engine.py — TTSEngine
  - TDD with a fake `Vieneu`: lazy init on first request, wrappers for
    `infer`/`infer_stream`/`infer_batch`/`add_voice`/`denoise`/`save` per the
    Phase 0 contract, error propagation with actionable messages

- [ ] Task 6: inference_worker.py — threaded worker
  - TDD: request queue serializes calls onto one worker thread; cooperative
    cancel between chunks (cancel flag checked per chunk); signals
    `progress`/`chunkReady`/`done`/`error` carry the §9 payloads; single
    `Vieneu` ownership enforced

- [ ] Task 7: __main__.py — smoke CLI
  - `--smoke "text" --voice Adam -o out.wav` runs end-to-end through the
    worker (not the UI-less main thread), correct exit codes, prints engine
    info + output path

- [x] Task 8: Phase validation gate
  - `ruff check .`, `ruff format --check .`, `pytest` all green; coverage
    ≥ 80% (line) on `src/vienetts_app`
  - Smoke run produces a valid 48 kHz WAV verified by soundfile read-back
    (samplerate == 48000, duration > 0, non-silent)
