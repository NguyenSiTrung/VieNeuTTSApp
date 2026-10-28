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

## 2026-10-29 - Phase 1 Task 1.2: `_OneShotBridge` hygiene + `ProgressRelay`
- **Implemented:** `_OneShotBridge` drops `work`/`on_done`/`on_error` in `finally` and `deleteLater()`s itself (1,000 ops → ≤ 1 live bridge, captured objects freed by refcount alone); module-level `_BridgeJob` replaces the per-call `QRunnable` closure class; new `ProgressRelay(current_generation, on_progress)` — `report()` from any thread, generation checked **at delivery**, values clamped monotonic per generation (text still shown).
- **Files changed:** `ui/bg_ops.py`, `tests/unit/test_bg_ops.py`, `tests/smoke/test_ui_studio.py` (driver fix).
- **Learnings:**
  - Patterns: `ProgressRelay` uses AutoConnection — worker reports queue to the GUI thread, GUI-thread reports deliver inline, so `run_sync`-driven controller tests stay ordered. Callers make finished/cancelled ops stale by bumping the generation (or returning `-1` while idle); the relay does not track "finished".
  - Gotchas: `processEvents()` outside `exec()` never runs `DeferredDelete` — tests call `QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)`. A per-call `class _Job(QRunnable)` closure is a reference cycle that pins captures until a GC pass. A stored exception's traceback pins `work`'s frames — leak tests must not keep it.
  - Gotchas: the studio smoke driver's `items()` sorts by scene position; on offscreen, polish runs off a timer-driven `UpdateRequest`, so fresh Repeater delegates could still sit at (0, 0) after 4 `processEvents()` rounds (`chip_checked` flaked 1/3 at HEAD and failed 3/3 once bridges posted `DeferredDelete`s). Call `item.ensurePolished()` on the walked tree before sorting by position.
  - Context: repo `addopts` has `-n auto`, so `-p no:xdist` errors — use `-n 0` for a serial run.

## 2026-10-29 - Phase 1 Task 1.1: `core/processes.py` spawn helpers + native spawn migration
- **Implemented:** `find_executable` (managed → `PATH` → `WELL_KNOWN_DIRS`, POSIX-only, executable bit required; Windows tries `.exe` then the bare stub name), `spawn_cli`/`run_cli` (`CREATE_NO_WINDOW` on nt, UTF-8 `errors="replace"`, `stdin=DEVNULL`, `PYTHONIOENCODING=utf-8` for children, line callbacks on reader threads, `CliTimeout`/`CliCancelled`, terminate → 0.3 s grace → kill, bounded reader joins), a child registry with `terminate_children(timeout)`, timeout constants + `extraction_timeout_s()`. Every native spawn in `video_dub.py`, `asr.py`, `diarization.py`, `ai_models._probe_runtime` migrated; resolvers use `find_executable`. New `VideoDubCancelled(VideoDubError)`; `extract_video_audio(cancel_check=)` maps to `AsrCancelled`/`DiarizationCancelled`; a whisper-cli/audiocpp spawn `OSError` becomes `AsrError`/`DiarizationError` so the auto chain falls back.
- **Files changed:** `core/processes.py`, `core/video_dub.py`, `core/asr.py`, `core/diarization.py`, `core/ai_models.py`, `tests/unit/test_processes.py`, `tests/unit/test_video_dub.py`.
- **Learnings:**
  - Patterns: `video_dub` now spawns ffmpeg/ffprobe through `ai_models.cli_command()` too (Windows shebang-stub rule), so stub-ffmpeg tests run on every OS. Patch `video_dub.run_cli` (not `subprocess.run`) in tests that fake ffmpeg results.
  - Patterns: `test_video_dub.py`'s module-wide ffmpeg skip became a per-test `@requires_ffmpeg` marker — stub-driven and pure tests now run on hosts without ffmpeg (like this one).
  - Gotchas: to assert Windows spawn kwargs on macOS, make the `Popen` spy raise `OSError` after recording kwargs — no process is spawned with foreign flags.
  - Gotchas: `run_cli` line callbacks run on the reader thread; ffmpeg progress callbacks therefore fire off the GUI thread (the subtitle controller's `report` already marshals via queued `invokeMethod`). A raising callback is swallowed so the pipe keeps draining.
  - Context: nothing calls `processes.terminate_children()` yet — Task 2.3 wires it into `SubtitleController.shutdown()`. Muxing deliberately has no timeout (burn-in runs for minutes) but is cancellable. Without ffprobe a long video only gets the 120 s extraction floor.

## 2026-10-29 - Phase 6 Task 6.4: Subtitle parsing edge cases + decode fallback
- **Implemented:** `_split_cue_body` replaces the speaker/standalone-filler regexes. A leading `[label]` is a speaker only when it is a diarization id (`SPEAKER_01`, `Speaker 2`, `speaker_a`) or name-shaped (ASCII, ≤ 32 chars, every word capitalised, not a ≥ 2-word ALL-CAPS phrase, no `_SOUND_WORDS`); any other `[…]` and every `(…)` holding a letter is a sound cue, peeled off before speech and kept (non-speakable) when alone; bracketed `NON_SPEECH_LABELS`-only cues are still dropped. Tags strip only when `<` is followed by a tag name, `/`, or a VTT stamp (`x < y and y > z` survives). `is_speakable(text)` + computed `Cue.speakable` property (not a field → project.json, equality and `render_fingerprint` unchanged). Hour-less VTT `MM:SS.mmm`. `decode_subtitle_bytes` (BOM authoritative → BOM-less UTF-16 by NUL parity → UTF-8 → cp1258 (NFC) → cp1252, Vietnamese note per guess; stray NULs dropped) behind `read_cues(path, notes=)`. Coordinator also moved `core/importers._read_srt` (Audiobook SRT import) onto `decode_subtitle_bytes` and skips non-speakable cues.
- **Files changed:** `core/subtitles.py`, `core/importers.py`, `tests/unit/test_subtitles.py`, `tests/unit/test_importers.py`.
- **Learnings:**
  - Gotchas: re-imported lowercase labels (`[lan] Hi`), multi-word ALL-CAPS labels (`[MR SMITH]`) and diacritic labels (`[Anh Tuấn]`) are now sound cues — the label leaves the text and no speaker is set. App-written `SPEAKER_NN` and capitalised names still round-trip.
  - Gotchas: only LEADING sound cues are peeled; `Get out! [door slams]` keeps its trailing group in the text.
  - Gotchas: UTF-16 detection must run before UTF-8 (ASCII-range UTF-16 is valid UTF-8). ♪ is not representable in cp1258.
  - Routed consumers: synthesis skipping of non-speakable units → Task 7.2 (render path) + `align.speech_units(merge=True)` must isolate non-speakable cues; `importSrt`/`importVideo` must pass `notes=` to `read_cues` and surface them → Task 2.3.
  - Gotchas: the Edit tool can write an escaped combining character as a literal invisible char — check with `od -c`; ruff `SIM905` rejects `"""…""".split()` word lists.
