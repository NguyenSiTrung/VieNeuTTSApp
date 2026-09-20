# Handoff: qwen_multiengine_20260920

Status when this note was written: Phase 0 (partial), Phase 1, Phase 2 and Phase 3
Tasks 3.1–3.2 complete. All commits are **local on `main`** — nothing has been
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
| `7392e2f` | 3.2 isolated model-host executable |

## Gate (always run before committing)

```bash
.venv/bin/ruff check . && .venv/bin/ruff format --check .
QT_QPA_PLATFORM=offscreen .venv/bin/pytest -q
```

Baseline: everything passes except
`tests/unit/test_stream_playback.py::TestRealQtSmoke::test_real_qaudiosink_offscreen_smoke`
(device-less host; documented, not a regression). Latest full run: 1318 passed, 1 failed.

## Next: Phase 3 Task 3.3 — the parent Qwen adapter and lifecycle

Files: `src/vienetts_app/core/qwen_engine.py`, `tests/unit/test_qwen_engine.py`.
Depends on `workers/qwen_host.py` (3.2) and `core/qwen_protocol.py` (3.1).

Contract it must satisfy (plan.md): expose `initialize()`, `capabilities()`,
`infer_stream()`, `cancel()` and `close()` over a **shell-free sanitized subprocess**;
enforce handshake/version/timeouts, drain stderr, drop stale frames, reap every child,
and escalate cancellation from request → terminate → kill. Tests must cover hangs,
crashes, malformed output, OOM/device errors, stale delivery, partial PCM, and clean
lazy restart.

What the host (3.2) already guarantees — build on it, do not re-litigate:

- Spawn the host with the managed runtime on `sys.path` and the app package importable
  (`python -m vienetts_app.workers.qwen_host` is the intended entry; `main()` takes no
  arguments). Send the `load` frame first; the host answers `hello` immediately on start
  and `capabilities` after each successful load.
- `load` fields: `profile` (`customvoice`|`base`), `modelDir` = the profile tree
  (`<root>/<profile>`), `sharedDir` = `<root>/shared`, `device`, `dtype`
  (`float32|float16|bfloat16`), `attention` (`eager|sdpa|flash_attention_2`).
- The host merges both trees into `<model root>/.load/<profile>` itself and removes it on
  close, so the parent passes the managed paths unchanged.
- Failure codes on `error` frames: `load_failed` (job-less, host stays alive),
  `unsupported_selection`, `generation_failed`, `sample_rate_drift`,
  `clone_prompt_unsupported`, `not_loaded`. A `fatal: true` error settles the job
  `failed` **and** exits the host (exit 1) — that is the restart signal for the adapter;
  protocol violations exit 2, peer close/`shutdown` exit 0.
- `pcm` frames carry 48 kHz float32 with `seq`/`final`; `progress` frames carry
  `fraction`/`stage`; one `terminal` per job (`ok`/`cancelled`/`failed`).
- Cancellation: send `cancel` for the active job; the host records it in its reader
  thread and stops between resample chunks, settling `terminal(status="cancelled")`.
  Escalation to terminate/kill is the adapter's job.

## Still blocked (needs the user's machines)

- Task 0.3 real-device probes — the six commands live in
  `packaging/qwen-runtime-requirements.json` (`platforms[].evidence.probeCommand`).
- The Conductor "User Manual Verification" checkpoints for Phases 0–2 and Phase 3.

## Housekeeping

- `bd` epic `VieNeuTTSApp-nqx`; Phase 3 tasks are `.5.x` (`.5.1`, `.5.2` closed;
  next is `.5.3`). `conductor/tracks/qwen_multiengine_20260920/metadata.json` now
  carries the corrected phase→beads mapping.
- Do **not** push, pull, or run `bd dolt push` without an explicit request.