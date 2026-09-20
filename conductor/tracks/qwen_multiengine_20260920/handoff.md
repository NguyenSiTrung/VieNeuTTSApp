# Handoff: qwen_multiengine_20260920

Status when this note was written: Phase 0 (partial), Phase 1 and Phase 2 complete;
Phase 3 Task 3.1 complete. All commits are **local on `main`** — nothing has been
pushed (AGENTS.md Git Policy).

## Commits

| Commit | Task |
| --- | --- |
| `a13cee1` | 0.1 port audit (`docs/performance/qwen-port-audit.md`) |
| `01de651` | 0.2 runtime probe CLI (`scripts/spike/qwen_runtime_probe.py`) |
| `5cc57a5` | 0.3 dependency/device matrix (`packaging/qwen-runtime-requirements.json`) — probes still pending hardware |
| `88feee2` | 1.1 + 1.2 engine profiles, generation context |
| `6b8845a` | 2.1 shared managed-install primitives |
| `dc12914` | 2.2 Qwen runtime manifests + installer |
| `a03f365` | 2.3 Qwen model manifests + shared-profile installer |
| `cec1b59` | 3.1 framed IPC protocol |

## Gate (always run before committing)

```bash
.venv/bin/ruff check . && .venv/bin/ruff format --check .
QT_QPA_PLATFORM=offscreen .venv/bin/pytest -q
```

Baseline: everything passes except
`tests/unit/test_stream_playback.py::TestRealQtSmoke::test_real_qaudiosink_offscreen_smoke`
(device-less host; documented, not a regression). Latest full run: 1253 passed, 1 failed.

## Next: Phase 3 Task 3.2 — model-host executable

Files: `src/vienetts_app/workers/qwen_host.py`, `tests/unit/test_qwen_host.py`.
Depends on `core/qwen_protocol.py` (3.1) and the managers from Phase 2.

Contract it must satisfy (plan.md):

- Load **only local verified paths** (the active `site-packages`/model dirs from
  `QwenRuntimeManager`/`QwenModelManager`) with remote code disabled
  (`trust_remote_code=False`, `local_files_only=True`).
- Implement CustomVoice speaker presets and Base clone prompts, report capabilities,
  select device/dtype/attention from the `load` frame.
- One stateful 24→48 kHz resampler for the whole job; emit bounded 48 kHz float32 `pcm`
  frames (`seq`, `final`) — never accumulate the whole utterance.
- Structured logging to **stderr only** (stdout carries frames), and a clean `shutdown`.

Test through fake Qwen models (no torch import in tests): segmentation bounds,
speaker/language validation, clone prompts, resampler continuity across frames,
OOM/device errors surfacing as `error`/`terminal` frames, and shutdown while idle.

## Design constraints already settled (do not re-litigate)

- Frames: `header_len|header|payload_len|payload`; only `pcm` carries bytes; job ids are
  immutable and tagged on every job frame. Use `SessionState("host")` +
  `accept()`/`record_sent()` in the host loop so transition violations are caught.
- `StaleFrameError` means "drop silently"; `EndOfStream` means a clean peer exit;
  `ProtocolError` mid-frame means a broken pipe.
- The Qwen runtime is a wheel-only closure; `sox` and (on macOS) `brotli` are recorded in
  `sdistOnly` because they publish no wheels — Task 3.2 must confirm the host never
  imports them.
- Managed installs are staging-only and checksum-pinned; the host must never re-download
  or repair anything itself.

## Still blocked (needs the user's machines)

- Task 0.3 real-device probes — the six commands live in
  `packaging/qwen-runtime-requirements.json` (`platforms[].evidence.probeCommand`).
- The Conductor "User Manual Verification" checkpoints for Phases 0–2 and Phase 3.

## Housekeeping

- `bd` epic `VieNeuTTSApp-nqx`; Phase 3 tasks are `.5.x` (`.5.1` closed).
- Do **not** push, pull, or run `bd dolt push` without an explicit request.