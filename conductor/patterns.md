# Codebase Patterns

Reusable patterns discovered during development. Read this before starting new work.

## Code Conventions

- Dev loop: `uv venv --python 3.13 .venv` (SDK caps at 3.13; system python may be newer), install `-e ".[dev]"`, gates via `.venv/bin/{ruff,pytest}`; set `QT_QPA_PLATFORM=offscreen` for pytest (from: phase01_core_20260827, 2026-08-27; reconfirmed in phase02_uishell_20260827 with PySide6 6.11.2).
- Qt allows ONE QGuiApplication per process and QML ABORTS the interpreter if only a headless QCoreApplication exists — GUI-object-tree assertions in pytest must run in a subprocess (`sys.executable -c` + RESULT: json stdout line) (from: phase02_uishell_20260827, 2026-08-27).
- `setContextProperty` does NOT take ownership of the Python object: anchor it (e.g. `engine._bridge = bridge`) or GC collects it and QML bindings see `null` (from: phase02_uishell_20260827, 2026-08-27).
- QML files should live in one dir and use same-directory `import "."` for the Theme singleton (registered in qmldir); the engine needs `addImportPath(qml_dir)` — bare absolute-path imports are rejected (from: phase02_uishell_20260827, 2026-08-27).
- `findChildren(QObject, name)` returns generic QObject wrappers — read QML-declared properties via `.property("name")`, not attribute access; StackLayout instantiates ALL children, so use currentIndex for visibility, not existence (from: phase02_uishell_20260827, 2026-08-27).
- Wayland blocks QScreen.grabWindow (0×0) without an XDG portal — for "window shows" evidence assert `isExposed()` + live event loop; post-quit "Cannot read property of null" stderr noise is harmless teardown (from: phase02_uishell_20260827, 2026-08-27).
- `QGuiApplication.styleHints().colorScheme()` reports Unknown under offscreen/no-platform → treat as dark (the app's safe default); real light/dark only appear on a live desktop session (from: phase02_uishell_20260827, archived 2026-08-27).
- Quality gate per task: `ruff check .` + `ruff format --check .` + `pytest` must all pass before committing; commit with conventional prefix + `git notes add` task summary (from: phase01_core_20260827, 2026-08-27).
- Ruff excludes `.agents/`, `conductor/`, `*.md` (tooling and frozen planning docs are not app code; ruff 0.16 otherwise formats Python inside Markdown) (from: phase01_core_20260827, 2026-08-27).

## Architecture

- `Vieneu` is a factory FUNCTION (`Vieneu(mode="v3turbo", backend=..., precision=...)`), not a class; kwargs pass through to the engine. Full confirmed contract: `docs/spike-report.md` §0 (from: phase01_core_20260827, 2026-08-27).
- Engine ownership: exactly one `TTSEngine` (lazily initializes its Vieneu) owned by one `InferenceWorker` QThread; requests serialize through its queue; cancel is cooperative between stream chunks (from: phase01_core_20260827, 2026-08-27).
- Voice keys: `list_preset_voices()` → `[(label, voice_id)]`; **voice_id (element [1])** is the key for `infer(voice=...)`; labels encode gender·region·style for North/Central/South grouping (from: phase01_core_20260827, 2026-08-27).
- Offline bundling: portable `HF_HOME` + `HF_HUB_OFFLINE=1` with pre-seeded cache; the codec repo is NOT dir-loadable via the public API; minimal CPU bundle via `scripts/fetch_models.py` (327 MB, SHA256 manifest) (from: phase01_core_20260827, 2026-08-27).
- ONNX precision→subfolder map: int8 → `onnx_int8`, fp32 → `onnx_update` (NOT `onnx/` as the plan sketch assumed) (from: phase01_core_20260827, archived 2026-08-27).
- `snapshot_download(..., local_dir=..., allow_patterns=[...])` writes a `.cache/huggingface` metadata dir INSIDE the bundle — always exclude it from manifests/hashes (from: phase01_core_20260827, archived 2026-08-27).
- Streaming contract: `infer_stream` yields float32 1-D mono chunks of variable size (~15360–96000 samples); concatenate for full audio. It carries its own sampling defaults (temperature=0.8, top_k=25, top_p=0.95), different from `infer` (temperature=0.4, top_k=50) (from: phase01_core_20260827, archived 2026-08-27).

## Gotchas

- SDK sample rates differ by function: `infer`/`infer_stream` → float32 mono @ 48 kHz; `denoise` → float32 mono @ **44.1 kHz** (from: phase01_core_20260827, 2026-08-27).
- `add_voice(save=True)` persists into **site-packages** by default — the app must redirect via `save_voices(path=<app data dir>)` (from: phase01_core_20260827, 2026-08-27).
- Qt cross-thread signals are queued to the receiver thread: tests/headless code must pump `QCoreApplication.processEvents()` while waiting (from: phase01_core_20260827, 2026-08-27).
- coverage.py cannot trace PySide6 QThread-run code (C++ threads) — keep logic in directly callable methods and test those synchronously (from: phase01_core_20260827, 2026-08-27).
- `Vieneu(backend="torch")` on a torch-free install raises `ModuleNotFoundError` — always surface an actionable "install [gpu] extra or switch to onnx" message (from: phase01_core_20260827, 2026-08-27).
- ONNX arena memory grows with the largest workload and is never returned to the OS: long-text synthesis plateaus ~2.5 GB RSS (budget breach → beads VieNeuTTSApp-u5c); interactive use stays ~766 MB (from: phase01_core_20260827, 2026-08-27).
- `sf.write` to an in-memory `BytesIO` needs explicit `format="WAV"`; and `TTSEngine.sample_rate`/`.backend` must raise before init — reading properties must never trigger the lazy model load (from: phase01_core_20260827, archived 2026-08-27).
- Docker Desktop image pulls can hang silently with no error output — compare `docker images` vs `docker ps` to distinguish pull-hang from container-run; kill early rather than waiting (from: phase01_core_20260827, archived 2026-08-27).

## Testing

- Worker/engine tests use injectable fakes: `TTSEngine(factory=lambda **kw: FakeVieneu())`; `detect_hardware(probe=..., system=..., machine=..., nvidia_smi=...)` for the §6.1 matrix (from: phase01_core_20260827, 2026-08-27).
- CLI tests call `main([...], engine_factory=...)` for exit codes + WAV verification without loading the model (from: phase01_core_20260827, 2026-08-27).
- Memory checks need BOTH `ru_maxrss` (monotonic peak) and current RSS via `ps -o rss= -p PID` — peak alone cannot distinguish a leak from arena growth (from: phase01_core_20260827, archived 2026-08-27).
- Phase 1+ CI smoke entry: `python -m vienetts_app --smoke "Xin chào" --voice Adam -o out.wav` (AC-3); engine readout comes from the detector's capability view (from: phase01_core_20260827, archived 2026-08-27).

---

Last refreshed: 2026-08-27 (archive of track phase01_core_20260827)
