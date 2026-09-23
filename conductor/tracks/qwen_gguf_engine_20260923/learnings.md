# Track Learnings: qwen_gguf_engine_20260923

## Codebase patterns inherited

Sources: `conductor/patterns.md` and
`conductor/archive/qwen_multiengine_20260920/learnings.md`.

- Preserve one capability table and one submission gate. Model format and
  runtime are not new semantic voice profiles.
- One worker owns one resident engine. Attach listener controllers for
  Batch/Audiobook/Subtitle instead of creating another worker.
- Verified installs stage, verify every file, and atomically promote.
  Model/runtime installs remain separate from the base dependency lock.
- Immutable context travels with jobs, artifacts, caches and Studio clips.
  Missing identity is not permission to reuse an arbitrary engine's audio.
- Base clones persist source audio, transcript and consent, not tensors.
- Artifact-first audio and bounded preview avoid duration-sized buffers.
- Existing `StreamingResampler` is NumPy-only and stateful; share it rather
  than creating another subtly different 24→48 kHz conversion.
- GUI callbacks must not wait for host cancellation/reaping. Preserve
  cancel-before-pickup guards and drop stale frames after terminal delivery.
- Parent writes must remain SIGPIPE-safe. On Windows, pipe readers own
  closure and are joined before streams close.
- Windows process liveness uses `core/processes.py`, never `os.kill(pid, 0)`.
- Keep stdout exclusively for protocol frames, including native-library
  logging. Diagnostics must not contain user text or reference content.
- Use queued background results with selection-generation guards.
- QML byte counts use `qlonglong`; delegates declare required properties
  on their root; shared controls preserve tested objectNames.
- Consolidate fake-host e2e scenarios per subprocess. Ordinary CI never
  downloads weights; real release evidence is an independent obligation.
- Full gates precede local task commits; attach git notes. No automatic
  fetch/pull/push or Dolt remote sync.

## Planning research (2026-09-23)

- The requested model repository contains paired talker/tokenizer GGUFs
  with Q8_0 and Q4_K_M for both 0.6B profiles. Tokenizer sharing must be by
  immutable artifact identity and quantization, not filename alone.
- The model card swaps Base/CustomVoice use-case descriptions in one
  table. The runtime README explicitly uses Base for reference cloning
  and CustomVoice for named speakers, matching the app. Verify the pinned
  code and models before claiming compatibility.
- The current runtime README advertises a C ABI (`qt_*`), shared-library
  builds (`QWEN_SHARED=ON`), streaming callbacks and reference extraction.
  HEAD documentation is not a pinned ABI or verified Windows/macOS pack.
- The README's `qwen.h` example does not establish the header's repository
  location: a direct root-level lookup returned 404. Phase 1 must inspect
  the pinned source tree, not invent ctypes layouts from prose examples.
- Runtime documentation mentions more languages than the app's current
  ten-language table. This track does not expand languages based on that
  claim; it validates mappings and represents unsupported ones honestly.
- The user approved retaining actual official precision (CPU/MPS FP32,
  CUDA BF16) and labeling the format Official full weights.
- The user explicitly excluded per-phase manual verification. Automated
  task gates and scripted real-model release checks remain required;
  do not insert Conductor manual-verification checkpoints.
- Native Metal is not PyTorch MPS. Selection, protocol, diagnostics, and
  readiness must distinguish backend identity even on the same device.
- Recent local commits fix GUI-blocking cancellation and host footprint
  growth. These are regression constraints for native integration, not
  optional future optimizations.

## Implementation discoveries

No implementation work has started. Append verified task discoveries here.

## Track-creation validation (2026-09-23)

- Epic: `VieNeuTTSApp-ysl8`; six phases and 15 implementation tasks remain
  open. Parent relationships, sequential blockers, metadata mappings,
  acceptance coverage, relative links, and whitespace were validated.
- `ruff check .` passed. `ruff format --check .` failed on the unchanged
  `src/vienetts_app/core/text_metrics.py` regular-expression formatting.
- Full offscreen/ffmpeg pytest: 1,844 passed, two failed in 45.02 seconds
  with 14 xdist workers. `TestFrameLoop::test_fatal_error_stops_the_host`
  crashed its worker; the CLI import-check test expected exit 1 but got
  exit 0 with `ok=true`. These are observations, not diagnosed causes.
- Baseline follow-up: `VieNeuTTSApp-55ln`. No application or test files were
  changed. Planning files remain uncommitted because repository gates
  failed; no gate was bypassed and no remote synchronization was run.
