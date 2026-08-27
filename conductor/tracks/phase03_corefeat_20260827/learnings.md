# Track Learnings: phase03_corefeat_20260827

Patterns, gotchas, and context discovered during implementation.

## Codebase Patterns (Inherited)

📚 Full set: `conductor/patterns.md` (28 patterns from phase01_core +
phase02_uishell). Most load-bearing for this track:

- **Dev loop / gates**: `uv venv --python 3.13 .venv`, install
  `-e ".[dev]"`, gates via `.venv/bin/{ruff,pytest}`;
  `QT_QPA_PLATFORM=offscreen` for pytest; ruff + format + pytest all green
  before every commit (conventional prefix + `git notes add` summary).
- **Engine seam**: `Vieneu(...)` is a factory function; exactly one
  `TTSEngine` per `InferenceWorker`; requests serialize through the
  worker queue; cancel is cooperative between stream chunks. Worker
  signals: `progress`, `chunk_ready`, `done`, `error`.
- **Voice keys**: `list_voices()` → `[(label, voice_id)]`; the
  **voice_id** (element [1]) is the key for `infer(voice=...)`; labels
  encode gender·region·style for N/C/S grouping.
- **`add_voice` persists into site-packages by default** — this track
  must redirect via `save_voices(path=<app data dir>)` (FR-3.4, §21).
- **Sample rates differ**: `infer`/`infer_stream` 48 kHz; `denoise`
  **44.1 kHz** — denoise preview playback must not assume 48 kHz.
- **Qt threading**: cross-thread signals are queued to the receiver
  thread — tests pump `QCoreApplication.processEvents()` while waiting;
  coverage.py cannot trace QThread-run code, keep logic in directly
  callable methods.
- **QML integration**: anchor context-property objects on the engine
  (`engine._controller = controller`) or GC collects them; QML files use
  same-directory `import "."` for the Theme singleton; read QML-declared
  properties via `.property("name")` from `findChildren` wrappers.
- **GUI tests in pytest**: ONE QGuiApplication per process; GUI-object-
  tree assertions run in a subprocess (`sys.executable -c` + `RESULT:`
  json stdout line); `colorScheme()` Unknown under offscreen → treat as
  dark.
- **Testing fakes**: `TTSEngine(factory=lambda **kw: FakeVieneu())`,
  injectable detector/player/settings — no model loads in unit tests.

---

<!-- Learnings from implementation will be appended below -->
