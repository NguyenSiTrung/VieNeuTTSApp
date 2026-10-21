# Track Learnings: asr_hardening_20261021

Patterns, gotchas, and context discovered during implementation.

## Codebase Patterns (Inherited)

- **Verified managed installs (`core/ai_models.py`)**: Every managed model download must verify `Content-Length`, catalog `size_bytes`, and `sha256` before promoting `.part` → final path, and `_managed_model_size_ok` must gate `is_*_installed()` so truncated files from interrupted downloads read as uninstalled.
- **Guarded model removal**: Model settings (`asr_model`, `diarization_model`) are user-writable strings; `remove_*_model` must `resolve()` both the target and `<data_dir>/models/<subdir>` and verify `resolved.is_relative_to(base)` plus suffix `.bin`/`.gguf` before unlinking.
- **Staged native runtime install with `--help` probe**: Extract runtime archives into a `.staging_<uuid>` directory, `chmod +x` the binary on POSIX, run `<cli> --help` with `cwd=cli.parent` (so `RUNPATH=$ORIGIN` and `ggml` backend discovery find sibling shared libraries), write `runtime.json`, and atomically swap into `<data_dir>/runtimes/<name>` with backup rollback on failure.
- **Dedicated cancellation exception in `auto` chains**: `auto` fallback loops catch backend `RuntimeError` subclasses (`AsrError`, `DiarizationError`); user cancellation must raise a distinct subclass (`AsrCancelled`, `DiarizationCancelled`) that `auto` re-raises immediately instead of falling through to the next backend.
- **Windows subprocess UTF-8 discipline**: Always pass `text=True, encoding="utf-8", errors="replace"` to `subprocess.Popen` / `subprocess.run` when reading stdout/stderr from native CLIs (`whisper-cli`, `audiocpp_cli`, `ffmpeg`) so non-ASCII Vietnamese output never crashes `cp1252` reader threads.
- **i18n catalog gate**: Any change to user-facing `qsTr` / `self.tr` strings requires running `scripts/update_i18n.sh`, translating new `<translation type="unfinished">` entries in `src/vienetts_app/ui/i18n/vienetts_en.ts`, and re-running `scripts/update_i18n.sh` to compile `vienetts_en.qm`.

---

<!-- Learnings from implementation will be appended below -->
