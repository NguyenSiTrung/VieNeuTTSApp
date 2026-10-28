# VieNeuTTS Desktop App — Development Workflow

<!-- refreshed 2026-10-28 (3): track `llm_provider_hardening_20261028` (`VieNeuTTSApp-f783`, `3dfb2b5..4374d0f`, archived `4374d0f`) — no gate or policy change. Gates re-verified live: suite 1630 collected / 1612 selected (18 benchmark deselected; `-m "not slow"` → 1590, `-m "not slow and not benchmark"` → 1572), +15 pytest items across new `tests/unit/test_llm_client.py` (6 items) and expanded `test_llm_refine.py` (+6), `test_asr.py` (+1), `test_settings.py` (+1), and `test_subtitle_controller.py` (+1), plus consolidated QML/smoke assertions in `test_subtitle_components.py`, `test_subtitle_project.py`, `test_controller.py`, and `test_ui_tabs.py`; `ruff check .` + `ruff format --check .` (216 files) green; `scripts/update_i18n.sh` 100% clean (1216 messages, 0 unfinished, +26 strings). -->
<!-- refreshed 2026-10-28 (2): track `video_preview_panel_20261028` (`VieNeuTTSApp-t80a`, `4ddf6f8..2e96c3a`, archived `2faa30a`) — no gate or policy change. Gates re-verified live: suite 1615 collected / 1597 selected (18 benchmark deselected; `-m "not slow"` → 1575, `-m "not slow and not benchmark"` → 1557), +1 pytest item (`test_subtitle_controller.py` 66 → 67 test functions) while the new QML coverage extends the existing consolidated `test_subtitle_components.py` (`COMPONENTS` gains `SubtitleVideoPreview`) and `test_ui_tabs.py` scenarios; `ruff check .` + `ruff format --check .` (214 files) green; `scripts/update_i18n.sh` 100% clean (1190 messages, 0 unfinished, +15 strings). -->
<!-- refreshed 2026-10-28: no drift — 1 commit since the 2026-10-26 refresh (`c0c482d`: beads export + `vienetts_en.ts` `<location>` offsets only). Gates re-verified live: suite 1614 collected / 1596 selected (18 benchmark deselected; `-m "not slow"` -> 1574, `-m "not slow and not benchmark"` -> 1556), `ruff check .` + `ruff format --check .` (214 files) green, `scripts/update_i18n.sh` 100% clean (1175/1175 finished, 0 unfinished) -->
<!-- refreshed 2026-10-26 (track `dub_pacing_alignment_20261026`, epic `VieNeuTTSApp-o8af`, archived in `3d6418b`): suite now 1614 collected / 1596 selected (18 benchmark deselected; `-m "not slow"` -> 1574, `-m "not slow and not benchmark"` -> 1556; verified live via pytest --collect-only -q). +17 test items across `dub_pacing_alignment_20261026` (`765c10a..dfac5b8`: `test_subtitles.py`, `test_subtitle_project.py`, `test_audio.py`, `test_diarization.py`, `test_asr.py`, `test_llm_refine.py`, `test_align.py`, `test_studio.py`, `test_video_dub.py`, `test_subtitle_controller.py`, `test_studio_controller.py`). `scripts/update_i18n.sh` is 100% clean (1175/1175 finished, 0 unfinished). `ruff check .` and `ruff format --check .` (214 files) are green -->
<!-- refreshed 2026-10-23 (full refresh after `2ifr`, `4mqj`, `asr_hardening_20261021`, and `subtitles_ux_overhaul_20261023` + `42ab565`): suite now 1597 collected / 1579 selected (18 benchmark deselected; `-m "not slow"` -> 1557, `-m "not slow and not benchmark"` -> 1539; verified live via pytest --collect-only -q). +56 net items since 2026-10-21 refresh (+13 net across diarization hardening `2ifr` `b41caf7..cb0c1e9` and test-runtime diet `4mqj` `1542053`+`5eb6f85` -> 1554/1536; +13 across `asr_hardening_20261021` `b09dd45..d50f43b` -> 1567/1549; +30 across `subtitles_ux_overhaul_20261023` `da92163..42ab565` -> 1597/1579, incl. new `tests/unit/test_diarization_hardening.py` and `tests/unit/test_subtitle_components.py`, plus `docs/screenshots/redesign/phase-5`). `5eb6f85` also fixed the `TestTorchPosture` `sys.modules["torch"]` deletion bug and split the 12 giant smoke items into smaller fixtures (~32-43 s full-suite wall). `scripts/update_i18n.sh` is 100% clean (1175/1175 finished, 0 unfinished). `ruff check .` and `ruff format --check .` (214 files) are green -->
<!-- refreshed 2026-10-20: suite now 1532 collected / 1514 selected (18 benchmark deselected; verified live via pytest --collect-only -q). +2 test items across dedicated SubtitlesTab promotion (aef1d87: test_bridge.py, test_i18n.py, test_ui_shell.py, test_ui_studio.py, test_ui_tabs.py) and Video Dubbing Studio enhancements (12734a0, bead eet9: test_studio.py, test_studio_controller.py, test_ui_studio.py). scripts/update_i18n.sh is 100% clean (991/991 finished, 0 unfinished — resolving the unextracted string note from 72db0c1..ab1aad1). ruff check . and ruff format --check . (212 files) are green -->
<!-- refreshed 2026-10-21: suite now 1541 collected / 1523 selected (18 benchmark deselected; verified live via pytest --collect-only -q). +9 items across the subtitle burn-in / embed-mode batch (d17a261: test_diarization.py, test_llm_refine.py, test_studio.py, test_studio_controller.py, test_subtitle_controller.py, test_video_dub.py, test_ui_tabs.py; 6e7c747: test_video_dub.py, test_subtitle_project.py; d7b8721: test_video_dub.py). scripts/update_i18n.sh is 100% clean (1012/1012 finished, 0 unfinished, 0 vanished). ruff check . passes but ruff format --check . arrived RED with 5 files of debt (core/asr.py, core/diarization.py, core/llm_refine.py, core/subtitle_project.py, tests/unit/test_diarization.py) — reformatted in this refresh, now 212 files green. Full runs on a loaded host flake 2–4 varying tests, all reproducing at e08a58e (see the loaded-host flake bullet in Testing) -->
<!-- refreshed 2026-10-19 (2): suite now 1530 collected / 1512 selected (18 benchmark deselected; verified live via pytest --collect-only -q). +26 test items across windowed SRT Studio integration (e94d045..02b9181, bead n5ts: test_studio.py, test_subtitle_project.py, test_studio_controller.py, test_ui_studio.py), SubtitleCard exportFormat label i18n (cf39f07: test_i18n.py, test_ui_tabs.py), and Video Dubbing / Whisper ASR / Speaker Diarization / LLM Refinement / AI Model Downloads (ba69a5a..ab1aad1, epic b3nq: new suites test_video_dub.py, test_asr.py, test_diarization.py, test_llm_refine.py, test_ai_models.py plus test_align.py, test_settings.py, test_subtitles.py, test_subtitle_controller.py, test_ui_tabs.py). ruff check . and ruff format --check . (212 files) are green (the controller.py formatting debt from 2f44ee1 was fixed in cee60ed); note that new user-facing qsTr/self.tr strings added in 72db0c1..ab1aad1 have not yet been extracted with scripts/update_i18n.sh -->
<!-- refreshed 2026-10-19 (test diet, bead 7coj + 2f44ee1): suite now 1504 collected / 1486 selected (18 benchmark deselected; verified live via pytest --collect-only -q). Bead 7coj (d6f70bb..3a5ed39, closed 10b8042) folded 381 overlapping unit tests into multi-scenario cases across 25 test files (-20.4%, wall ~91s -> ~72.5s, all assertions retained); 2f44ee1 added 1 test in test_updates.py. On linux-aarch64 the x64-cell baseline is now 6 items (was 7 before 4 TestInstallPacks items merged into 3; beads 4jwz/x898). Release v0.3.0 fixes: 0d190da moved test_ui_shell.py's ~70 KB driver to a temp file (Windows 32 KB argv cap) with per-scenario stderr markers + faulthandler timeout dump; 8db2677 skips modal crash dialogs under QT_QPA_PLATFORM=offscreen/minimal -->
<!-- refreshed 2026-10-12 (test diet): suite now 1884 collected / 1866 selected (18 benchmark deselected; verified live via pytest --collect-only -q over tests/unit+tests/smoke); -37 low-value items in the inference-worker/voice-profiles/protocol/text-metrics/gguf-build/macos/pcm-transport batch — parametrize collapses (identical code paths), same-function merges with all assertions retained, and constructor-arg-echo / already-covered duplicate drops; a planned wider fan-out was stopped early, so this is a partial pass -->
<!-- refreshed 2026-10-11: suite now 1920 collected / 1902 selected (18 benchmark deselected; verified live via pytest --collect-only -q over tests/unit+tests/smoke) after the macOS platform audit fixes (+5 items: TLS CA-bundle tests in test_startup_hardening, osascript alert tests in test_crash); gates unchanged: ruff check + format --check + pytest -->
<!-- refreshed 2026-10-11: suite now 1915 collected / 1897 selected (18 benchmark deselected; verified live via pytest --collect-only -q over tests/unit+tests/smoke) after the Windows-hardening pass (dcaeb04: native-path bytes for qwen.dll, SetStdHandle isolation, WaitForSingleObject liveness, MAX_PATH extraction, unprivileged pack links — +7 items: qwen_gguf_abi contract tests, no-symlink install fallback, to_short_path); gates unchanged: ruff check + format --check + pytest -->
<!-- refreshed 2026-10-10 (test diet, bead zc8p): suite now 1879 collected / 1861 selected (18 benchmark deselected; verified live via pytest --collect-only -q); full run ~175s -> ~55s wall on 4 cores: pytest addopts gained `--dist worksteal` (xdist default `load` left the long smoke tests as a tail), smoke scenarios that start from the same fake-controller state now share one QML window (62 -> ~25 instantiations; each costs >=1s), and fixed real-time waits in cancel/restart probes became event-driven; ~76 tautological / duplicate / static-pin tests removed. Rule: do not shorten a timing margin to save <0.1s (a 20ms-cancel vs 60ms-generate probe flaked at load avg ~11 and was reverted). If `PYTHONDONTWRITEBYTECODE` is set in the shell, pytest re-rewrites every test module on every worker (+~7s per run) — unset it for test runs -->
<!-- refreshed 2026-10-10: suite now 1955 collected / 1937 selected (18 benchmark deselected; verified live via pytest --collect-only -q) after track perf_hardening_20261009; gates unchanged (ruff check + format --check + pytest); new rules from that track: run `pytest -m benchmark` whenever scripts/benchmarks/ changes (the default gate skips the perf-harness smoke tests), and run scripts/update_i18n.sh whenever user-facing strings change; release.yml gained a precompiled-QML bundle assertion; bead 4jwz: 7 Qwen/GGUF tests assume an x64 host cell and fail on linux-aarch64 -->
<!-- refreshed 2026-10-07: no gate/CI drift since the 2026-10-04 marker; suite counts re-verified live (pytest --collect-only -q, 2026-10-07): 1754 collected / 1742 selected (12 benchmark deselected); the pytest-cov removal and 80%-coverage-rule drop from 20d1270 are already reflected here; only code landing since that marker is GGUF pack re-publication content (no workflow impact) -->
<!-- refreshed 2026-10-04: test items now 1754 collected / 1742 selected (12 benchmark deselected) after the 2026-10-04 reduction batch (~20% of items: merged same-function micro-tests across engine/host/protocol/library suites, all assertions retained; coverage tooling dropped per request — no threshold). Gates unchanged: ruff check + format --check + pytest. The real-QAudioSink smoke in tests/unit/test_stream_playback.py is still CI-skipped and still fails on device-less hosts when it shares a run with other unit files (bead VieNeuTTSApp-3iy). GGUF track qwen_gguf_engine_20260923 complete with AC-12 hardware residual on ysl8.7 -->
<!-- refreshed 2026-09-28: no drift — only housekeeping commits since 2026-09-27 (ruff-format fix 5097342, .bak cleanup e30b349, beads export f95617a); gates and suite counts carried forward unchanged -->
<!-- refreshed 2026-09-27: test items now 2190 collected / 2178 selected (12 benchmark deselected) after the 2026-09-27 test-reduction batch (ae93c6a…f5b40e8: drop inherited QwenEngine lifecycle twins, fold release-smoke YAML pins, merge audiobook/subtitle/studio clones, collapse mapping-table parametrize explosions, drop e2e-covered happy paths and cosmetic smoke cases); CI is lint + parallel unit/smoke matrix legs on linux-x64/windows-x64 (8710f7f, 2026-09-22) — wall time is the slower suite, not the sum. Gates unchanged: ruff check + format --check + pytest. The real-QAudioSink smoke in tests/unit/test_stream_playback.py is still CI-skipped and still fails on device-less hosts when it shares a run with other unit files (bead VieNeuTTSApp-3iy). GGUF track qwen_gguf_engine_20260923 complete with AC-12 hardware residual on ysl8.7 -->
<!-- refreshed 2026-09-21: test items now 1732 collected / 1720 selected (12 benchmark deselected) after the Qwen multi-engine track (`qwen_multiengine_20260920`: capability/profile contracts, managed runtime + model installers, isolated model host, profile-scoped clones, submission-context provenance, capability-aware UI, fake-host e2e + opt-in real-model release smoke); gates unchanged, and the real-QAudioSink smoke in tests/unit/test_stream_playback.py is still CI-skipped and still fails on device-less hosts when it shares a run with other unit files (bead VieNeuTTSApp-3iy) -->
<!-- refreshed 2026-09-16: test items now 1055 collected / 1054 selected (12 benchmark deselected; v0.1.15 test-suite consolidation folded duplicate micro-tests and smoke scenarios while 100% of assertions were retained); gates otherwise unchanged, but note the real-QAudioSink smoke in tests/unit/test_stream_playback.py is CI-skipped and fails on device-less hosts when it shares a run with other unit files (bead VieNeuTTSApp-3iy) -->
<!-- refreshed 2026-09-14: test items 1036 collected / 1024 selected (12 benchmark deselected; SRT subtitle studio added ~164 tests); gates otherwise unchanged -->

## Testing
 - `pytest` is the gate; run it before any commit.
 - Core logic must be well tested (see `code_styleguides/testing.md`);
   QML glue is smoke-tested outside CI.
- Benchmarks are excluded by default (`-m 'not benchmark'` in pyproject
  addopts) — run explicitly when changing perf-sensitive paths.
- Any change under `scripts/benchmarks/` also runs `pytest -m benchmark`:
  the perf-harness smoke tests are `benchmark`-marked, so the default gate
  never exercises `run_engine` end to end (perf track 7.4).
- Engine/runtime defaults change only on recorded evidence: a scripted
  `run_matrix` sweep under `docs/performance/` showing a win outside the
  run-to-run range, re-confirmed in interleaved rounds, one flip per commit.
- User-facing string changes run `scripts/update_i18n.sh` before commit
  (`test_i18n` does not catch unfinished catalog entries).
- On linux-aarch64 hosts, 6 Qwen/GGUF tests fail on a clean tree (no arm64
  host cell in the manifests; was 7 before `7coj` merged 4 `TestInstallPacks` items into 3; beads `VieNeuTTSApp-4jwz` / `VieNeuTTSApp-x898`) — a known baseline,
  like the real-`QAudioSink` smoke below.
- A local `pytest` run on a device-less host is expected to show one
  documented red test: `test_stream_playback.py::TestRealQtSmoke::test_real_qaudiosink_offscreen_smoke`
  (real `QAudioSink` never drains; CI-skipped, bead `VieNeuTTSApp-3iy`). Judge
  the gate on the rest of the run — do not "fix" it by editing app code, and
  do not treat it as a regression from the current change.
- **Loaded-host flake family (2026-10-21 refresh).** A full `-n auto` run on a
  busy machine (this 18-core host at load avg ~9–11 with a desktop browser open)
  typically also fails 2–4 tests that VARY between runs and all pass in
  isolation. Confirmed at both `e08a58e` (the previous refresh commit) and
  `HEAD`, so they are a pre-existing baseline, not regressions. Recurring
  members: `test_qwen_host.py::TestFrameLoop` (`test_session_lifecycle`,
  `test_cancel_frames`, `test_transition_and_pipes`; note that the self-inflicted
  in-process `TestTorchPosture` `monkeypatch.delitem(sys.modules, "torch")`
  trigger for `RuntimeError: Only a single TORCH_LIBRARY can be used to register
  the namespace triton` was fixed in `5eb6f85` by using
  `monkeypatch.setitem(sys.modules, "torch", None)`), plus
  `test_qwen_engine.py::TestInitialize::test_spawn_failures`,
  `test_controller.py::TestBgOpsAsync::test_file_io_runs_on_pool_and_lands_via_queued_signal`,
  `test_replay_transport.py::test_pause_keeps_position_and_resume_continues`, and
  the two long-standing smoke flakes
  (`TestAudiobookTabSmoke::test_waveform_render_progress_interactions_and_render_all`,
  `TestStreamLifecycleSmoke::test_stream_cancel_cross_tab_and_error_recovery`).
  One of them is NOT load-dependent and flakes in ISOLATION too:
  `test_ui_shell.py::TestShellSmoke::test_shell_navigation_theme_badge_and_edge_surfaces`
  (~1-in-5 at `-n 0`; 4/5 passed at HEAD), where the driver's QML instantiates
  before `setContextProperty` and every context property reads null (bead
  `VieNeuTTSApp-qjkk`). Procedure: re-run any suspect test in isolation
  (`pytest <nodeid> -q`); if it STILL fails, re-run it again and reproduce it at
  the previous commit before calling it a regression. Prefer `-n 4` for a clean
  read on a loaded box.
- **The format gate is `ruff format --check`, not `ruff check`.** This refresh
  again found the gate RED (`d17a261`/`6e7c747` left 5 files unformatted:
  `core/asr.py`, `core/diarization.py`, `core/llm_refine.py`,
  `core/subtitle_project.py`, `tests/unit/test_diarization.py`) while
  `ruff check .` passed. Run both before every commit.
- Merge same-function micro-tests instead of piling parameter rows
  (2026-09-06…10 consolidation: 1029 → 872 items collected / 860 selected,
  12 benchmark cases deselected; 2026-09-14: the SRT subtitle studio took the
  suite to 1036 collected / 1024 selected at that time — merge within the new
  subtitle/align/project suites too; 2026-09-15: the Studio redesign
  consolidation folded duplicate micro-tests and repetitive smoke scenarios
  (100% of assertions retained, full-suite runtime ~30% faster) taking the
  suite to 1055 collected / 1054 selected; 2026-09-27: the post-v0.2.0
  reduction batch dropped inherited QwenEngine lifecycle twins, folded
  release-smoke YAML contract pins, merged audiobook/subtitle/studio clones,
  collapsed mapping-table parametrize explosions, and dropped e2e-covered
  happy paths / cosmetic smoke cases → **2190 collected / 2178 selected**
  — keep smoke scenarios consolidated per subprocess driver; 2026-10-04:
  ~20% item reduction via same-function merges across the engine/host/
  protocol/library suites, all assertions retained, → **1754 collected /
  1742 selected**; 2026-10-09: the perf hardening track added coverage →
  **1955 collected / 1937 selected**, 18 benchmark deselected; 2026-10-10 diet → **1879 collected / 1861 selected**; 2026-10-12 diet (partial, `57634dc`) → **1884 collected / 1866 selected**; 2026-10-12 full semantic-merge pass (bead `7coj`, `d6f70bb..3a5ed39`, -381 items across 25 unit files) + `2f44ee1` → **1504 collected / 1486 selected**; 2026-10-19 SRT Studio + Video Dubbing/ASR/Diarization/LLM (`n5ts` + `cf39f07` + `b3nq` + `ab1aad1`, +26 items) → **1530 collected / 1512 selected**; 2026-10-20 `SubtitlesTab` promotion + Video Dubbing Studio (`aef1d87` + `12734a0`, bead `eet9`, +2 items) → **1532 collected / 1514 selected**; 2026-10-21 subtitle burn-in / embed-mode batch (`d17a261` + `6e7c747` + `d7b8721`, beads `4kht`/`poph`/`ni2i`/`vxil`, +9 items) → **1541 collected / 1523 selected**; 2026-10-21 diarization hardening (`2ifr`, `b41caf7..cb0c1e9`) + test-runtime reduction (`4mqj`, `1542053` + `5eb6f85`, +13 net items) → **1554 collected / 1536 selected**; 2026-10-21 `asr_hardening_20261021` (`7u58`, `b09dd45..d50f43b`, +13 items) → **1567 collected / 1549 selected**; 2026-10-23 `subtitles_ux_overhaul_20261023` + post-review fix (`weit`, `da92163..42ab565`, +30 net items) → **1597 collected / 1579 selected**; 2026-10-26 `dub_pacing_alignment_20261026` (`o8af`, `765c10a..dfac5b8`, +17 items) → **1614 collected / 1596 selected**; 2026-10-28 `video_preview_panel_20261028` (`t80a`, `e2d9492..52e4f25`, +1 item — the new `SubtitleVideoPreview` coverage rides inside the existing consolidated `test_subtitle_components.py` / `test_ui_tabs.py` scenario items) → **1615 collected / 1597 selected**; 2026-10-28 `llm_provider_hardening_20261028` (`f783`, `d6a5ab0..16f8e28`, +15 items, incl. new `tests/unit/test_llm_client.py`) → **1630 collected / 1612 selected**).

## Commits
- Commit **after each task** completes and its tests pass.
- Conventional commit prefix: `feat:`, `fix:`, `chore:`, `refactor:`,
  `test:`, `docs:`.
- Task summary is stored in **git notes** (`git notes add -m "..."`),
  keyed to the task from the plan; the commit message stays concise.

## Remote Sync
- Per-task commits stay **local**. Never `git push`, `git pull`, `git fetch`,
  or `bd dolt push` as part of a task.
- `bd close` / `bd update --notes` for the finished task run normally — task
  tracking is local and ungated. Run `bd dolt push` **once at session end**,
  when the user asks, to persist that state to the remote.
- Push and pull happen only on the user's explicit request.

## Workflow Order (per task)
1. Read `conductor/patterns.md` (project patterns) and the track's
   `learnings.md`.
2. Write/fail tests first (TDD) for the changed contract.
3. Implement minimal code to pass; refactor.
4. Run `ruff check`, `ruff format --check`, `pytest`.
5. Commit; attach a git note with the task summary.
6. Append any new gotcha to `tracks/<id>/learnings.md`.

## Validation Gate
- `ruff check .` and `ruff format --check .` and `pytest` all green.
- Never merge with failing tests, ignored type errors, or `# noqa`
  without a comment.
