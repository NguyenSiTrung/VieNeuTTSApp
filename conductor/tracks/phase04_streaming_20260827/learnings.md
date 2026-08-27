# Track Learnings: phase04_streaming_20260827

Patterns, gotchas, and context discovered during implementation.

## Codebase Patterns (Inherited)

📚 Full set: `conductor/patterns.md` (refreshed from phase01_core +
phase02_uishell + phase03_corefeat). Most load-bearing for this track:

- **Dev loop / gates**: `uv venv --python 3.13 .venv`, install
  `-e ".[dev]"`, gates via `.venv/bin/{ruff,pytest}`;
  `QT_QPA_PLATFORM=offscreen` for pytest; ruff + format + pytest all
  green before every commit (conventional prefix + `git notes add`
  summary).
- **Streaming contract** (spike §4): `infer_stream` yields float32
  1-D mono chunks, variable size ~15 360–96 000 samples @ 48 kHz;
  concatenate for full audio. It carries its own sampling defaults
  (temperature=0.8, top_k=25, top_p=0.95) and accepts `max_chars=256`
  — relevant to the arena-mitigation chunked dispatch.
- **Worker plumbing already exists**: `InferenceWorker._process_stream`
  emits `chunk_ready`/`progress`/`done`; cancel is cooperative between
  chunks (SDK cannot cancel mid-chunk). Streaming requests serialize
  through the single engine-owning worker thread.
- **QtMultimedia offscreen**: `QAudioOutput` construction deadlocks
  under pytest fd capture (pipewire devicemonitor probe) — set
  `QT_AUDIO_BACKEND=ffmpeg` in tests needing the real player;
  `QMediaPlayer` has no `resume()`; enum str() is
  "PlaybackState.PlayingState" — map via `.name`.
- **Memory**: ONNX arena grows with the largest workload and never
  returns to the OS — long-text synthesis plateaus ~2.5 GB RSS (budget
  breach, bead `VieNeuTTSApp-u5c`); interactive use ~766 MB.
  Measurement needs BOTH `ru_maxrss` and current RSS via
  `ps -o rss= -p PID`.
- **Qt threading**: cross-thread signals are queued to the receiver
  thread — tests pump `QCoreApplication.processEvents()` while
  waiting; coverage.py cannot trace QThread-run code, keep logic in
  directly callable methods.
- **QML integration**: anchor context-property objects on the engine;
  QML files use same-directory `import "."` for the Theme singleton
  (registered in qmldir); read QML-declared properties via
  `.property("name")`; Repeater delegates have no QObject parent —
  walk `contentItem().childItems()`.
- **GUI tests in pytest**: ONE QGuiApplication per process;
  GUI-object-tree assertions run in a subprocess (`sys.executable -c`
  + `RESULT:` json stdout line).
- **Testing fakes**: place the fake at the SDK layer (FakeVieneu
  implementing the spike §0 surface incl. generator `infer_stream`)
  and run the REAL controller/worker/engine/QML/playback code.
- **Consent gate already ships** (phase03): persisted acknowledgment
  in `<app data dir>/cloning_consent.json`; this track only polishes
  the copy.

---

<!-- Learnings from implementation will be appended below -->
