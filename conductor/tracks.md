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

<!-- Archived: qwen_gguf_engine_20260923 (Qwen 0.6B GGUF engine support — official full weights or GGUF for Base and CustomVoice, PyTorch/qwentts.cpp engine selection, Q8_0/Q4_K_M quantization, managed CPU/CUDA/Metal native runtime packs, the ctypes child host, variant-aware controller/UI and release compatibility gates; all 15 tasks complete 2026-09-24; beads epic VieNeuTTSApp-ysl8 closed. Residual AC-12 is hardware-gated on open bead VieNeuTTSApp-ysl8.7: four packs publish (linux-x64-cpu, windows-x64-cpu, macos-arm64-cpu, macos-arm64-metal) but only linux-x64-cpu was ever probe-verified, on the pre-bump pin (stale since 801ffc7); CUDA cells stay unpublished — do not claim unverified cells), archived 2026-10-10 → ./archive/qwen_gguf_engine_20260923/ -->

<!-- Archived: perf_hardening_20261009 (Performance hardening — the 2026-10-09 audit's 20 findings + the Int16 live-sink bug: off-thread Qwen integrity/engine prep, background audiobook export/chapter model/subtitle dub DSP, debounced text metrics, live writer decoupled from the 2 s transport + Qwen progressive segments and prefetch, faster WSOLA, lazy async QML tabs, row-level list models, AOT QML packaging, and bench-gated engine tuning — the only default flipped is the numpy BLAS cap = 1 thread (RTF 1.43 → 0.80 on linux-arm64, docs/performance/tuning-vieneu.md); all 7 phases complete 2026-10-09; beads epic VieNeuTTSApp-w1in closed. Follow-ups: hay8 (unmeasured hardware cells), t5la, rg4a, cqqs, v09a), archived 2026-10-10 → ./archive/perf_hardening_20261009/ -->

---
