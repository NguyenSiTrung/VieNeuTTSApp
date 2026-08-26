# Codebase Patterns

Reusable patterns discovered during development. Read this before starting new work.

## Code Conventions

- Dev loop: `uv venv --python 3.13 .venv` (SDK caps at 3.13; system python may be newer), install `-e ".[dev]"`, gates via `.venv/bin/{ruff,pytest}` (from: phase01_core_20260827, 2026-08-27).
- Quality gate per task: `ruff check .` + `ruff format --check .` + `pytest` must all pass before committing; commit with conventional prefix + `git notes add` task summary (from: phase01_core_20260827, 2026-08-27).
- Ruff excludes `.agents/`, `conductor/`, `*.md` (tooling and frozen planning docs are not app code; ruff 0.16 otherwise formats Python inside Markdown) (from: phase01_core_20260827, 2026-08-27).

## Architecture

- `Vieneu` is a factory FUNCTION (`Vieneu(mode="v3turbo", backend=..., precision=...)`), not a class; kwargs pass through to the engine. Full confirmed contract: `docs/spike-report.md` §0 (from: phase01_core_20260827, 2026-08-27).
- Engine ownership: exactly one `TTSEngine` (lazily initializes its Vieneu) owned by one `InferenceWorker` QThread; requests serialize through its queue; cancel is cooperative between stream chunks (from: phase01_core_20260827, 2026-08-27).
- Voice keys: `list_preset_voices()` → `[(label, voice_id)]`; **voice_id (element [1])** is the key for `infer(voice=...)`; labels encode gender·region·style for North/Central/South grouping (from: phase01_core_20260827, 2026-08-27).
- Offline bundling: portable `HF_HOME` + `HF_HUB_OFFLINE=1` with pre-seeded cache; the codec repo is NOT dir-loadable via the public API; minimal CPU bundle via `scripts/fetch_models.py` (327 MB, SHA256 manifest) (from: phase01_core_20260827, 2026-08-27).

## Gotchas

- SDK sample rates differ by function: `infer`/`infer_stream` → float32 mono @ 48 kHz; `denoise` → float32 mono @ **44.1 kHz** (from: phase01_core_20260827, 2026-08-27).
- `add_voice(save=True)` persists into **site-packages** by default — the app must redirect via `save_voices(path=<app data dir>)` (from: phase01_core_20260827, 2026-08-27).
- Qt cross-thread signals are queued to the receiver thread: tests/headless code must pump `QCoreApplication.processEvents()` while waiting (from: phase01_core_20260827, 2026-08-27).
- coverage.py cannot trace PySide6 QThread-run code (C++ threads) — keep logic in directly callable methods and test those synchronously (from: phase01_core_20260827, 2026-08-27).
- `Vieneu(backend="torch")` on a torch-free install raises `ModuleNotFoundError` — always surface an actionable "install [gpu] extra or switch to onnx" message (from: phase01_core_20260827, 2026-08-27).
- ONNX arena memory grows with the largest workload and is never returned to the OS: long-text synthesis plateaus ~2.5 GB RSS (budget breach → beads VieNeuTTSApp-u5c); interactive use stays ~766 MB (from: phase01_core_20260827, 2026-08-27).

## Testing

- Worker/engine tests use injectable fakes: `TTSEngine(factory=lambda **kw: FakeVieneu())`; `detect_hardware(probe=..., system=..., machine=..., nvidia_smi=...)` for the §6.1 matrix (from: phase01_core_20260827, 2026-08-27).
- CLI tests call `main([...], engine_factory=...)` for exit codes + WAV verification without loading the model (from: phase01_core_20260827, 2026-08-27).

---

Last refreshed: 2026-08-27 (from track phase01_core_20260827)
