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

## 2026-10-29 - Phase 6 Task 6.1: Alignment fixes + contiguous merged-unit placement
- **Implemented:** `plan_cue` measures a pushed cue's windows from `anchor = max(start, srt_start)`, so drift is absorbed (up to `rate_cap`) instead of carried; new `_extended_end()` owns the borrow rule (caption window up to `srt_end`, borrow ≤ `MAX_SLACK_BORROW_MS`, never past `next_start − MIN_BREATH_GAP_MS`); `plan_cue(start_ms=, forced_rate=)` replaces the renderer's duplicated forced-rate fit (its stale hard-coded `next_start − 120` overflow limit is gone). `adjusted_cues` retimes silent cues from their fit too (no raw SRT times out of order). `split_unit_audio` snaps each weighted split to the quietest 10 ms frame within ±150 ms (`_snap_split`, ties → nearest the weighted split). `add_unit_clip`: only the first piece anchors, later pieces (and empty pieces) follow contiguously; split edges get 5 ms micro-fades.
- **Files changed:** `core/align.py`, `core/subtitle_project.py`, `tests/unit/test_align.py`, `tests/unit/test_subtitle_project.py`.
- **Learnings:**
  - Judgment call: a take that overruns its speech window at comfort pace but fits the cue's own (silent) caption hold up to the breath limit now plays at **1.0×** instead of `comfort_cap` (new `own_window` stage). Contract tests changed accordingly (`test_adjusted_cues_contract`: last cue end 4000 → 4609).
  - Gotchas: overflow is still measured per piece inside a merged unit — a piece can report overflow against its own cue window while the unit as a whole fits (false overflow in stats). Follow-up bead filed; per-unit counting is the fix.
  - Patterns: merged-unit per-piece state (`_unit_follow`, `_unit_split_edges`, `_unit_pretrimmed`, `_unit_forced_rate`) is now initialised in `__init__` and passed explicitly to `_place` — spies on `add_clip` still see every cue.
  - Context: `finish` commits promote → `save_timeline` → `store.save` — wrong order for crash consistency; Task 6.2 reorders it (timeline → project.json → promote, with `render_id`).

## 2026-10-29 - Phase 1 Task 1.3: Shared `source_16k.wav` with streaming anti-aliased resampling
- **Implemented:** new `core/audio_prep.py` — `ensure_source_16k(media, cache_dir, *, data_dir, cancel_check, progress_cb)`: a 16 kHz mono `.wav` is used in place; soundfile-readable lossless audio (WAV/FLAC/AIFF/CAF/W64/RF64) streams in 64 Ki-frame blocks through `soxr.ResampleStream` (VHQ) into `StreamingWavWriter` → `.part.wav` → `os.replace`; video/MP3/Ogg (and undecodable WAVs) go through `extract_video_audio`. One `source_16k.wav` + versioned JSON sidecar (`source`/`size`/`mtime_ns`/`format`) per media cache dir, per-target lock so ASR + diarization convert once. Legacy `source_wav_16k.wav` / `input_16k.wav` with a matching sidecar are renamed into place; `cache_key_paths()` + `key_path=`/`aliases=` keep old ASR-segment and diarization-turn caches hitting. `asr._ensure_16k_wav` / `diarization._ensure_16k_wav` are thin wrappers. `soxr>=1.0` declared in `pyproject.toml` (already bundled via vieneu).
- **Files changed:** `core/audio_prep.py` (new), `core/asr.py`, `core/diarization.py`, `tests/unit/test_audio_prep.py` (new), `tests/unit/test_asr.py`, `tests/unit/test_diarization_hardening.py`, `pyproject.toml`, `uv.lock`.
- **Learnings:**
  - Gotchas: `uv lock` (uv 0.11.11) rewrites markers across ~60 unrelated lines — hand-apply only the project's own hunks; `uv lock --check` still passes.
  - Patterns: soxr's stream output is time-aligned and exact-length after `resample_chunk(empty, last=True)`; 20 min converts in ~0.25 s at ~2 MB peak (the old full read was ~220 MB; the old `np.interp` path aliased a 10 kHz tone at ~0 dB).
  - Context: a sidecar-less `source_16k.wav` at least as new as its source is still trusted (pre-upgrade caches; `test_diarization.py` depends on it). WAV inputs no longer need ffmpeg, so the 48 kHz diarization test now runs everywhere.
  - Context: WAV media now also get a cached `source_16k.wav`, so `refine_cues_speech_bounds` in the controller (`importSrt`/`importVideo` paths) now runs for WAV media too — on the GUI thread. Adds to the "no WAV read on the GUI thread" NFR gap (routed to the controller offload owner, Task 7.2/7.3).
  - Context: Task 4.2 (concurrent ASR + Nemotron) should call `ensure_source_16k` once before forking; the lock covers the race anyway.

## 2026-10-29 - Phase 2 Task 2.1: One busy owner + render from a start snapshot
- **Implemented:** `_owner_busy(except_owner)` / `_owner_reason` / `_refuse_if_busy` / `_refuse_knob` replace `_compute_busy_owner`; AI, render and export refuse each other with a distinct reason on the hint channel, and the new `busyReason` property (same text) feeds the dock's `disabledReason`. Render works from `_render_project` + `_render_voice` (set in `render()`, cleared in `finished`/`_abort_renderer`) — `_submit_next_unit`/`_consume_unit`/`_finish_render` never read `self._project`. `_can_apply_now()` is False while rendering: a mid-render knob change is staged; a confirm waits in `_deferred_invalidation` and runs when the render ends (`finished` also drops play-after-render; `_fail_render` covers cancel; `_stop_render` drops it and emits the revert signals). `*_now` AI runners re-check after their confirm dialog. QML: Render/Play/Export (and WAV/SRT items) disabled while `aiBusy`; project delete buttons disabled while any owner is busy.
- **Files changed:** `ui/subtitle_controller.py`, `ui/qml/components/SubtitleActionDock.qml`, `ui/qml/SubtitlesTab.qml`, `tests/unit/test_subtitle_controller.py`, `tests/unit/test_subtitle_components.py`.
- **Learnings:**
  - Deviations: the property is `busyReason` (matches SubtitleCard's existing `busyReason()`); the snapshot is the whole project + voice (`_render_project`), not `_render_cues`, because units need speaker voices and context too. Play stays enabled during `aiBusy` while playback is under way (pause/resume). The `voice` setter is NOT refused while AI is busy (the VoicePicker writes one-way and never re-reads it) — it is still staged mid-render. `setLlmGlossary`, the video/mix setters and `reloadCurrentProject` are not owner-gated.
  - Gotchas: Repeater delegates inside a QML `Dialog` are not QObject children — `findChildren` misses them; walk `contentItem.childItems()` (needs `from PySide6.QtQuick import QQuickItem` or the converter fails). `click()`/`invokeMethod("click")` ignore `enabled` — gating tests must assert `enabled`.
  - Gotchas: any new `@Property` fails `test_every_property_is_consumed_or_api_only` until QML binds it or it joins `API_ONLY_PROPERTIES`.
  - Context: H1 root cause — `_rebuild_for_policy` returns early while `_rendering`, so a knob applied mid-render was silently never rebuilt.
  - Follow-ups (routed): `_update_project_cues` still saves project.json mid-render and `finished` overwrites `self._project` (Task 2.4 merge); `on_enrolled → setSpeakerVoice` can now be refused if an owner starts during enrollment (Task 2.3 project_id capture); optional SubtitleCard.qml knobs could grey out on `aiBusy` instead of snapping back (Task 7.3 / UI polish).

> Coordinator note (2026-10-29): Phase 5 Tasks 5.1–5.3 start before Phase 3 completes — they touch only `core/diarization.py` and its tests and need nothing from the catalog. Task 5.4 (ONNX models) still waits for Task 3.1.

## 2026-10-29 - Phase 5 Task 5.1: `split_segments_at_turns` with word timings
- **Implemented:** pure `diarization.split_segments_at_turns(segments, turns, *, min_part_ms=600, min_words=2)`: each word is labelled by max turn overlap (ties keep the previous label, unlabelled words inherit a neighbour), runs of same-speaker words become parts, the weakest too-short run (< `min_words` words or < `min_part_ms`) folds into its closer neighbour until all parts qualify; inner part edges are word bounds, outer parts keep the segment edges; parts are labelled by max overlap with speech bounds from their words. Input type is a structural `WordTimedSegment` Protocol (diarization cannot import asr); parts are built with `dataclasses.replace`, so `asr.RawSegment` works directly. Not wired yet (Task 7.1).
- **Files changed:** `core/diarization.py`, `tests/unit/test_diarization.py`.
- **Learnings:**
  - Context: the word shape is exactly `asr.RawSegment.words` — `tuple[tuple[int, int, str], ...]`, `(start_ms, end_ms, text)` in absolute media ms (`WordTiming`); `_extract_words` already fills it for faster-whisper/dict/tuple input. Task 4.1 only has to fill it from whisper.cpp JSON-full in the same form.
  - Deviation: unsplit segments come back unchanged (speaker untouched) — labelling them stays with `assign_speakers_from_turns`; only split parts get a speaker here. Gaps between words stay gaps between parts.
  - Patterns: per-word majority labelling gives the nearest-word-boundary cut for free and tolerates overlapping turns (Nemotron emits them); "fold the weakest short run into the nearer neighbour, repeat" handles A-B-A interjections and short edge runs with one rule.
  - Context: Task 7.1 should call it before `normalize_asr_segments_to_cues` (that only merges same-speaker micro-fragments, so differently labelled parts survive).

## 2026-10-29 - Phase 6 Task 6.3: Mux progress, ducking at scale, limiter, ISO 639-2/B tags, finalize stage
- **Implemented:** `_run_ffmpeg` reads `out_time_us`/`out_time_ms` as µs (both are µs in real ffmpeg), monotonic, capped at 0.95 (also on `progress=end`); `mux_dubbed_video` probes whenever `progress` is passed, uses `max(video, dub WAV)` as the length (WAV via `soundfile.info`; clamped exports use the video), and reports 1.0 only after `_promote_atomic`. Ducking: the `max_spans` collapse + inline `between()` sum are replaced by two `asendcmd` command files (`duck_gate.cmd`, `duck_bg.cmd`), each directly in front of its named `volume@duckgate`/`volume@duckbg`; files live in a `.{hex}.mux` scratch dir used as ffmpeg's cwd (burn-in ASS + fonts share it), named relatively so no path reaches the filtergraph. `alimiter=limit=0.891:level=0` ends the duck mix and the replace path. `_subtitle_language_tag` maps to ISO 639-2/B (`vi`→`vie`, `zh-CN`→`chi`, unknown→`und`).
- **Files changed:** `core/video_dub.py`, `tests/unit/test_video_dub.py`.
- **Learnings:**
  - Gotchas: `alimiter` defaults to `level=1` (auto-level back up to 0 dBFS) — a real ceiling needs `level=0`. Its `latency` option needs ffmpeg ≥ 5.1, so it is not used (5 ms lookahead delay remains). The graph floor is ffmpeg 4.4 (`amix normalize=`).
  - Patterns: `sendcmd`/`asendcmd` deliver to the first filter matching the target — name each target `volume@id` (the graph parser keeps the full `class@id` as the instance name) and put its `asendcmd` directly in front.
  - Gotchas: stdlib `wave` cannot read the renderer's float WAVs; use `soundfile.info`. Once ffmpeg runs in a scratch cwd, every `-i`/output path must be absolute (SRT path now is).
  - Patterns: stub-ffmpeg tests record argv + cwd and copy the cwd's `*.cmd` files to a capture dir, because the mux deletes its scratch dir in `finally`.
  - Deviations: limiter also in replace mode (the float dub can exceed full scale); language tag + `title=Phụ đề` are now always written; `max_spans` removed.
  - Context: **stub-only** — real-ffmpeg validation (asendcmd syntax/cwd, instance names, eval=frame, alimiter level, replace filter_complex, progress parsing) is bead VieNeuTTSApp-ap63 (needs a host with ffmpeg). Double padding of duck spans (`build_duck_spans` + mux both pad/merge) is bead VieNeuTTSApp-zefv.

## 2026-10-29 - Phase 3 Task 3.1: Catalog & installers (macOS whisper-cli cells, managed ffmpeg, audio.cpp variants, optional audio models)
- **Implemented:** new `core/media_runtime_manifests.json` (schema 1, `runtimes.{whisper_cpp,ffmpeg}.cells`, every cell `published:false` until Task 3.2/8.3) + strict parser (`load_media_runtime_manifest`/`parse_media_runtime_manifest`, `MediaRuntimeCell`; published cells need an allowlisted HTTPS URL on `MEDIA_PACK_DOWNLOAD_HOSTS`, size > 0, 64-hex sha). macOS whisper cells (`macos-arm64-metal` with `cpu`+`metal` keys, `macos-x64-cpu`); unpublished → `unavailable_reason` Homebrew hint, never downloads. Managed ffmpeg: `ffmpeg_runtime_status`/`install_ffmpeg_runtime(progress_cb, cancel_check)`/`remove_ffmpeg_runtime` — streamed size+SHA download, staged extract to `<data>/runtimes/ffmpeg/bin`, probes `-version`/`-encoders` (libx264, aac, libvpx-vp9)/`-filters` (subtitles) through `run_cli`, rejection keeps the previous pack, symlink + `is_relative_to` remove guard. audio.cpp: Windows `cpu`/`vulkan` + `cuda12.4`/`cuda13.3` by driver CUDA version (`detector.probe_cuda_driver()`), Linux non-x64 → `{}` + `audiocpp_unavailable_reason() == "unsupported"`, all assets pinned (v0.9.2 digests). Optional `AUDIO_MODELS` catalog + `audio_model_status/path`, `download_audio_model(progress_cb, cancel_check)`, `remove_audio_model`. Shared `_promote_runtime_dir` atomic swap with rollback. Frozen spec lists the manifest as data (cross-file test).
- **Files changed:** `core/ai_models.py`, `core/media_runtime_manifests.json` (new), `packaging/vienetts-app.spec`, `tests/unit/test_ai_models.py`.
- **Learnings:**
  - Context — model pins (downloaded + SHA-checked, never executed): `whisper-vad-silero-v6.2.0` (ggml-org/whisper-vad @9ffd54a1, 885,098 B, MIT, `models/whisper`); `silero-vad` (snakers4/silero-vad @be95df91 v6.2, 2,327,524 B, MIT, `models/vad`); `speaker-embedding` = 3D-Speaker CAM++ VoxCeleb (csukuangfj/speaker-embedding-models @0743f301, 29,596,978 B, Apache-2.0, `models/speaker`). WeSpeaker VoxCeleb models are CC-BY-4.0 — rejected as a shipped default (Task 5.4 DER comparison must keep that licence constraint in mind).
  - Context — ONNX IO for Task 5.4: speaker model input `x` [N,T,80] fbank (16 kHz, `normalize_samples=1`, `feature_normalize_type=global-mean`) → `embedding` [N,512]; Silero input [N,T] + `state` [2,N,128] + `sr` int64 → `output` [N,1] + `stateN`.
  - Deviations: CUDA variants also fetch the matching `cudart-*` companion zip (bin zips ship ggml-cuda.dll without cudart/cublas); darwin arm64 also exposes `cpu` (test_settings asserts it); model id is `whisper-vad-silero-v6.2.0` (spec said `silero-v6.2.0`); `url_override` still bypasses an unpublished cell (tests/self-hosted rely on it).
  - Gotchas: to test real verification offline, delegate the fake downloader to the module-level imported `_stream_download` over `file://` URLs — capturing `ai_models._stream_download` after monkeypatching recurses into the fake.
  - Routed: `models._DIARIZATION_DEVICES` + `"cuda"` and the controller's `downloadAudiocppRuntime` guard accepting `cuda12.4`/`cuda13.3` → Task 3.3; `diarization._resolve_cli_backend` mapping `cuda`/installed `cuda*` → `--backend cuda` → Task 5.4; QML diarization device option → Task 8.1. Task 3.2 packs need `bin/ffmpeg` + `bin/ffprobe` at the archive root, LICENSE/SOURCE files, URL ending `.<archive>` on `MEDIA_PACK_DOWNLOAD_HOSTS`; the macOS whisper-cli must be ad-hoc signed. Follow-up bead: pin whisper.cpp b5454 Windows/Linux assets by size+SHA.

## 2026-10-29 - Phase 2 Task 2.2: GUI-thread, generation-checked AI progress; per-op generations; guarded sample playback
- **Implemented:** every AI op starts through `_begin_ai_op(stage_text, progress) -> gen` (bump generation, reset cancel flag, clear error, set busy, seed the relay). `report_asr`/`report_diar` and the LLM `progress_cb` only call `self._ai_progress_relay.report(gen, value, text)`; the relay's generation getter is `self._ai_generation if self._ai_busy else -1`, its sink `_on_ai_progress`. New `_sample_generation` (bumped by `_play_track`, `stopPlay`, `notifyVideoPreviewPlaying` and each audition) — `playSpeakerSample` no longer touches `_ai_generation`. `ref_<spk>.key` sidecar (`"{start}-{end}|{resolved video}|{size}|{mtime_ns}"`, bounds from `select_speaker_reference_clip`) makes a moved clip or replaced video re-extract; `_extract_speaker_ref` deletes the key, extracts, writes the key (clone shares it). `_play_file(path) -> bool` is the guarded playback path for the track and auditions.
- **Files changed:** `ui/subtitle_controller.py`, `tests/unit/test_subtitle_controller.py`.
- **Learnings:**
  - Patterns: ProgressRelay controller tests run the deferred `work` on a real `threading.Thread`, join, `QCoreApplication.sendPostedEvents()`, then call `done` — with `run_sync`, `done` runs before queued reports, which are then (correctly) dropped as stale.
  - Patterns: reading `_ai_busy` in the relay's generation getter makes "finished" free — `_set_ai_state(False)` in `done` turns late reports stale without a generation bump. Seed the relay's high-water mark with the op's starting value, or ASR's own 0.05 report × 0.7 moves the bar backwards.
  - Deviations: `playSpeakerSample` returns False when the player refuses/raises on a cached clip (QML ignores it); the audition no longer calls `_stop_playback()` first (`PlaybackController.play` replaces the source).
  - Follow-ups: `playSpeakerSample` is not owner-gated — two concurrent extractions of the same speaker write the same `ref_<spk>.wav` (fix: unique temp + rename); `_speaker_ref_key` resolves/stats the video on the GUI thread (cheap, but sync I/O). If Task 5.3's scoring makes `select_speaker_reference_clip` read audio, the key must not call it on the GUI thread.

## 2026-10-29 - Phase 4 Task 4.1: whisper.cpp invocation — `-mc 0`, Silero `--vad`, JSON-full word timings, cache-dir outputs, `asr_version`
- **Implemented:** whisper-cli argv gains `-mc 0`, `-ojf` (+ `-osrt` fallback) and `--vad -vm <path>` exactly when `ai_models.audio_model_path(WHISPER_VAD_MODEL_ID, data_dir)` resolves (size-checked install). `_run_whisper_cpp(out_dir=)` writes `out_dir.absolute()/.whisper_out_<hex>.{json,srt}` into the media cache (or the run's temp dir) and removes both in `finally` — nothing lands beside a 16 kHz WAV used in place. `_parse_whisper_json` + `_merge_whisper_tokens`: leading-space tokens start words, other text tokens extend them, `[_…]` specials skipped, words via `_extract_words` (absolute ms, clamped), `avg_logprob` = mean ln p over text tokens (new `RawSegment.avg_logprob`). SRT only read when the JSON is missing/unparseable. Cache payload `{"asr_version": 2, "key", "segments"}`; other/missing version = miss (aliases still work); `avg_logprob` persisted sparsely.
- **Files changed:** `core/asr.py`, `tests/unit/test_asr.py`.
- **Learnings:**
  - Context (whisper.cpp @ b5454 source): `-ojf` enables token timestamps by itself; offsets are ms; all specials are `[_…]`; from b5454 split-UTF-8 tokens are merged and token times remapped through VAD — older builds (Homebrew) don't, so the parser decodes with `surrogateescape` and rejoins bytes per word.
  - Gotchas: JSON-full has **no `no_speech_prob`** (C API only) — Task 4.2's whisper.cpp path can use only `avg_logprob` + a text compression ratio. C++ `ostream << float` can print `nan` (invalid JSON) — hence the SRT fallback and `json.loads(strict=False)`.
  - Deviations: `asr_version` is a payload field checked on load, not part of the key string (spec: "caches carry an `asr_version` field"); `avg_logprob` lands now so 4.2 needs no second version bump.
  - Follow-up: installing/removing the VAD model does not invalidate an existing segments cache (no VAD flag in the key) — folded into Task 4.2.
