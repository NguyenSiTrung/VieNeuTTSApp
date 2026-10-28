# Track Learnings: dub_asr_diar_hardening_20261029

Patterns, gotchas, and context discovered during implementation.

## Codebase Patterns (Inherited)

📚 **227 patterns** are available in `conductor/patterns.md`. This track also draws on learnings from:
- `asr_hardening_20261021`
- `subtitles_ux_overhaul_20261023`
- `dub_pacing_alignment_20261026`
- `video_preview_panel_20261028`
- `llm_provider_hardening_20261028`

Most relevant here:

- **Quality gate per task**:
  - Run `ruff check .`, `ruff format --check .` and `pytest` (default `-m 'not benchmark'` selection) before each commit.
  - Also run `pytest -m benchmark` when a task adds or edits a benchmark.
  - Commit with a conventional prefix, then `git notes add`. Never push.
- **Native CLI stubs**:
  - Fake `whisper-cli`, `audiocpp_cli`, `ffmpeg` and `ffprobe` binaries are pure-Python `#!/usr/bin/env python3` scripts, invoked through `ai_models.cli_command()`.
  - Windows `CreateProcess` rejects non-PE `.exe` stubs (`WinError 216`), and CRLF shell stubs loop on `shift\r` (`8398dd8`, `0d0320a`).
- **Windows child processes**:
  - Spawn with `CREATE_NO_WINDOW`, using the `detector.py` / `qwen_runtime.py` / `qwen_engine.py` idiom.
  - A piped child's stdout is encoded with the ANSI code page. Decode with `encoding="utf-8", errors="replace"`, and export `PYTHONIOENCODING=utf-8` to Python children.
- **Grandchild pipe hang**:
  - After `proc.kill()`, use a bounded `proc.wait(timeout=0.05)` plus a reader `thread.join(timeout=0.5)`. Never use `communicate()`, which blocks forever when a grandchild inherits the pipes (asr_hardening Task 2.2).
  - Keep this inside `core/processes.py`.
- **Managed installs**:
  - Pin `size_bytes` + `sha256`.
  - Guard removal with `resolved.is_relative_to(models_dir)` plus an extension check.
  - Probe staged CLIs with `--help` and `cwd=cli.parent` before the atomic swap.
  - Relocatable packs need RUNPATH rewrites (`install_name_tool -add_rpath @loader_path` on macOS), proven from a foreign cwd.
- **Published vs probe-verified**:
  - A pack cell may claim only what was verified.
  - Unpublished cells stay `published: false`, and README/docs never name them as available (`ysl8.7`).
- **Frozen manifests**: list every new `core/*_manifests.json` as data in `packaging/vienetts-app.spec` (lines 99-102). Otherwise every frozen-build lookup reports the runtime as unshipped.
- **Workflow `if:`**:
  - A job-level `if:` may only read `github`, `inputs`, `needs` or `vars`. Naming `matrix` makes GitHub reject the whole file.
  - Shrink the matrix in a `plan` job instead, as `qwen-gguf-runtime-build.yml` does.
- **ffmpeg timeline gating**:
  - A disabled `volume` filter passes audio at 1.0×. Gate a sidechain trigger with `volume=volume=0:enable='not(between(t,...))'`.
  - `refine_cues_speech_bounds` and `trim_speech_edges` leave all-silent slices untouched.
- **Burn-in**:
  - Never hand libass the raw SRT; generate an ASS script with `PlayRes` pinned to the frame.
  - Keep the source bit depth (`_burn_pix_fmt`).
  - Relay ffmpeg `-progress` to the UI.
- **Renderer seams**:
  - `SubtitleTrackRenderer.add_unit_clip` delegates through `self.add_clip` with `_unit_pretrimmed`/`_unit_forced_rate` and an injectable `split_fn`.
  - Keep both seams when placing contiguous merged-unit pieces, so existing spies still see every cue.
- **Caption hold vs audio bounds**: `adjusted_cues` extends `end_ms` for a readable caption hold. Timeline and splice code build from `timeline.segments`, never from `adjusted`.
- **`bool` subclasses `int`**:
  - Settings clampers, `Cue` bound validators and Qt numeric setters reject `isinstance(v, bool)` before `isinstance(v, int)`.
  - Settings clamp per field and drop only the bad field. `asr_cloud_fallback` follows the same rule in the other direction: it rejects ints.
- **`_set_setting` signal table**: list every new AI setting key with `aiSettingsChanged`, or QML bindings never refresh after a write.
- **Staged render invalidation**:
  - Cue and voice changes on a rendered project go through `_PendingInvalidation`.
  - Settings that never change audio (glossary, mix knobs, embed mode) must not go through it.
- **Fingerprint evolution**: keep new project fields out of `render_fingerprint` unless they change audio, and pin the pre-change digest with a golden test.
- **GUI-thread offload**:
  - Controllers take an injectable `bg_runner` (`run_sync` or `DeferredRunner` in tests).
  - Use one generation counter PER operation; a shared counter lets one op cancel another.
  - Qt cross-thread signals are queued, so pump `processEvents()` while waiting.
- **Bench-gate every default**:
  - A default flips only after a scripted sweep on the executing host shows a win well outside run-to-run noise, re-confirmed in interleaved rounds.
  - Each flip is its own commit. This applies to parallel ASR + Nemotron and to the embedding-model choice.
- **Oracle for rewrites**:
  - Keep the old implementation as a test oracle (`tests/unit/wsola_reference.py` precedent).
  - Parity tests use short inputs, because float near-ties can flip picks on long ones.
- **QML fakes**:
  - Smoke fake controllers need real NOTIFY `@Property` members, and list props use `@Property("QVariantList")`.
  - Add scenarios inside the existing consolidated smoke groups; each QML instantiation costs ≥ 1 s.
  - `click()` bypasses `enabled`, so assert the `enabled` property itself.
- **i18n**:
  - `scripts/update_i18n.sh` is the only check, since `test_i18n.py` was removed.
  - Translate the new `unfinished` entries in `vienetts_en.ts` and re-run the script until 0 remain.
- **Testing diet**:
  - No default, constant or round-trip micro-tests.
  - Keep regression guards, error paths, cancel/ordering tests and cross-file invariants.
  - Merge same-function scenarios into existing tests.

## Audit evidence (2026-10-29, pre-implementation)

External facts relied on, checked with `gh` / PyPI metadata on 2026-10-29:

**whisper.cpp**
- The latest release with binaries is the `b5454` pre-release (2026-10-06); `v1.9.5` has no assets.
- `b5454` ships:
  - `whisper-b5454-xcframework.zip`, which has **no macOS CLI**;
  - Ubuntu x64/arm64 tarballs;
  - Windows x64/Win32/arm64/CUDA zips.
- So macOS needs a project-built static `whisper-cli`, or Homebrew.

**Silero VAD for whisper.cpp**
- Model URL: `https://huggingface.co/ggml-org/whisper-vad/resolve/main/ggml-silero-v6.2.0.bin` (`v5.1.2` also exists).
- It is enabled with `--vad -vm <model>`.

**audio.cpp `v0.9.2`**
- Ships Windows `cuda12.4` / `cuda13.3` zips.
- Ships **no** Linux arm64 build.

**Python dependencies**
- `vieneu==3.3.0` already requires:
  - `onnxruntime>=1.20.0` (1.29.0 installed);
  - `soxr` (1.1.0);
  - `kaldi-native-fbank>=1.20`;
  - `librosa>=0.11.0`.
- `scipy` 1.18.1 is also present.
- New direct imports only need declaring in `pyproject.toml`; `uv lock` must resolve no new packages.

**ffmpeg progress units**
- ffmpeg's `-progress` reports `out_time_us` **and** `out_time_ms` in microseconds; `out_time_ms` is a long-standing misnomer.
- `video_dub.py:491` divides it as milliseconds, and the stub at `tests/unit/test_video_dub.py:476` encodes the same mistake.

**Code evidence at `295e0d0`**
- `merge_duck_spans(max_spans=200)` collapses spans in O(n²), and the `enable` expression is inlined twice into `-filter_complex` (`video_dub.py:680-710`, `:852-881`).
- `playSpeakerSample` bumps the shared `_ai_generation` and plays without the `_play_track` guard (`subtitle_controller.py:2819-2850`).
- `removeProject` deletes before it cancels (`:1674-1686`), and `exportSrt` reads only `project.adjusted` (`:2311`).
- `_OneShotBridge` never calls `deleteLater()` (`ui/bg_ops.py:31-75`).
- `report_asr`/`report_diar` and the LLM `progress_cb` call `_set_ai_state` on the worker thread (`subtitle_controller.py:2609-2623`, `:2998`).
- Raw `subprocess` spawns without `CREATE_NO_WINDOW` or UTF-8 decoding:
  - `video_dub.py:114, 360, 444, 447, 589, 608, 668`;
  - `asr.py:655`;
  - `diarization.py:885`;
  - `ai_models.py:591`.

Reviewer repros for the alignment and merged-unit fixes are written as the Task 6.1 red tests in `plan.md`.

---

<!-- Learnings from implementation will be appended below -->
