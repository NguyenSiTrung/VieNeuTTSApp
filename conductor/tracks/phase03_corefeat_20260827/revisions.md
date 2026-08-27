# Revisions: phase03_corefeat_20260827

## Revision 1 — 2026-08-27 (Plan)

- **Trigger:** Task phase1_task1 kickoff analysis. The controller must dispatch
  `add_voice`/`remove_voice`/`denoise` through the single engine-owning worker
  thread (NFR-3.1 / §5 single-owner rule) and pass per-request temperature
  (FR-3.5), but `core/models.py` (TTSRequest has no temperature field) and
  `workers/inference_worker.py` (queue accepts only TTSRequest) were not in
  Task 1's owned files — no task owned them.
- **Phase/task when found:** Phase 1, Task 1 (before implementation).
- **Change:** Task phase1_task1 file set extended with
  `src/vienetts_app/core/models.py`, `src/vienetts_app/workers/inference_worker.py`,
  `tests/unit/test_models.py`, `tests/unit/test_inference_worker.py`.
  Controller adds voice-op submission (`VoiceOp`) and the worker gains a
  `voice_op_done` signal; `TTSRequest.temperature` flows into `engine.infer`.
- **Rationale:** keeps the one-thread-owns-engine invariant intact instead of
  calling engine voice APIs from the UI thread; the smallest seam change is in
  the worker queue union type, owned by the same task that needs it.
