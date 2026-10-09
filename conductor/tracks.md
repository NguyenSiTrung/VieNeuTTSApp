# Tracks

Master list of work tracks. Status markers: `[ ]` pending, `[~]` in progress,
`[x]` complete, `[!]` blocked, `[-]` skipped. Completed tracks are archived
under `conductor/archive/`.

---

<!-- Archived: phase01_core_20260827 (Phase 0+1 — spike & environment validation + core headless engine), archived 2026-08-27 → ./archive/phase01_core_20260827/ -->

<!-- Archived: phase02_uishell_20260827 (Phase 2 — QML UI shell: bootstrap, navigation, theme, empty tabs), archived 2026-08-27 → ./archive/phase02_uishell_20260827/ -->

<!-- Archived: phase03_corefeat_20260827 (Phase 3 — Core features: tabs wired to engine; playback, export, cloning), archived 2026-08-27 → ./archive/phase03_corefeat_20260827/ -->

<!-- Archived: phase04_streaming_20260827 (Phase 4 — Streaming & polish: infer_stream → QAudioSink playback, waveform indicator, §11 edge cases; AC-1 ttfc ~100 ms vs ~300 ms target, AC-5 stream RSS 1120 MB vs <2 GB, u5c closed → residual VieNeuTTSApp-8jm), archived 2026-08-27 → ./archive/phase04_streaming_20260827/ -->

<!-- Archived: ui_redesign_20260827 (Comprehensive UI/UX redesign and modern desktop refactor — dynamic Theme tokens, AppCard/AppButton/EmotionChip/StatusBadge component library, desktop workstation shell and all four studios restyled with 100% smoke-contract compatibility; 481 tests green), archived 2026-09-21 → ./archive/ui_redesign_20260827/ -->

<!-- Archived: ui_refine_20260828 (UI refinement pass 2 — "Signal" design system: AppButton/AppCombo/VoicePicker single-skin consolidation, AppIcon Canvas icon set, bundled Be Vietnam Pro fonts, keyboard focus rings, MultiEffect elevation; 481 tests green, visual audit 8.5/10), archived 2026-09-21 → ./archive/ui_refine_20260828/ -->

<!-- Archived: audiobook_epub_20260828 (Audiobook support — EPUB first: EPUB spine normalization + chapter model, chapter cache, the single-worker synthesis-listener seam reused by Batch/Subtitle, WAV artifact per chapter; 481+ tests green), archived 2026-09-21 → ./archive/audiobook_epub_20260828/ -->

<!-- Archived: qwen_multiengine_20260920 (Qwen multilingual multi-engine TTS support — optional isolated Qwen 0.6B CustomVoice/Base engine profiles behind one capability table, managed runtime/model installs with verified offline packs, the framed-IPC model host with lazy restart, engine-stamped provenance across artifacts/caches/Studio, profile-scoped clones, capability-aware UI + localization, and two-tier validation (fake host in CI, opt-in real-model release smoke). Phases 1–7 implemented; Phases 3–7 manual verification approved 2026-09-21; beads epic VieNeuTTSApp-nqx closed. Phase 0's six real-device probes still need release hardware → beads nqx.2.4/nqx.3.3/nqx.4.4 stay open, as do the six `pending` cells in docs/performance/qwen-runtime-compatibility.md §5), archived 2026-09-21 → ./archive/qwen_multiengine_20260920/ -->

---

## [x] Track: Qwen 0.6B GGUF engine support

Official full weights or GGUF for Base and CustomVoice, with compatible
PyTorch/qwentts.cpp engine selection, Q8_0/Q4_K_M quantization, and managed
CPU/CUDA/Metal runtimes. Medium priority; sequential execution.
No per-phase manual verification gates.

All 15 plan tasks complete (`implement_state.json` status `complete`,
2026-09-24). Residual AC-12 is hardware-gated and lives on bead
`VieNeuTTSApp-ysl8.7`: four runtime packs publish recipes
(`linux-x64-cpu`, `windows-x64-cpu`, `macos-arm64-cpu`, `macos-arm64-metal`)
but only `linux-x64-cpu` is probe-verified; `windows-x64-cuda` and
`linux-x64-cuda` stay unpublished. Do not claim unverified cells.

*Link: [./conductor/tracks/qwen_gguf_engine_20260923/](./conductor/tracks/qwen_gguf_engine_20260923/)*

---

## [~] Track: Performance hardening — GUI-thread freezes, live pipeline, throughput

Implements the 2026-10-09 performance audit (20 findings + the Int16
live-sink bug): stat-stamp Qwen integrity with off-thread engine prep,
background audiobook export/chapter model/subtitle dub DSP, debounced text
metrics, live writer decoupled from the 2 s transport plus Qwen progressive
segments and prefetch, faster WSOLA, lazy async QML tabs and repaint
discipline, row-level list models, AOT QML, and bench-gated engine tuning
(defaults unchanged without evidence). High priority; sequential execution;
no manual verification of any kind. Beads epic `VieNeuTTSApp-w1in`.

*Link: [./conductor/tracks/perf_hardening_20261009/](./conductor/tracks/perf_hardening_20261009/)*
