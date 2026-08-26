# Testing Guide

## Framework
- `pytest` for all Python tests; run `pytest` at repo root in CI.
- Tests must be deterministic, isolated, and full-suite-safe. No reliance
  on order, network, or real model downloads.

## Coverage
- Target >= 80% line coverage overall (workflow default).
- Core logic (`detector`, `controllers`, data models, request queue) must
  be well covered; QML glue is smoke-tested, not unit-tested.

## Unit vs Integration
- **Unit**: pure logic without Qt event loop — engine detection matrix,
  workload heuristic, chunking, settings persistence, cancel flag.
- **Integration**: worker `QThread` + signal flow with a fake/stub engine
  (do not initialize real `Vieneu` in tests).
- **Smoke**: launch the app (offscreen `QT_QPA_PLATFORM=offscreen`) and
  verify a path renders/handles — no real synthesis in CI.

## Patterns
- Stub the `vieneu` SDK boundary (inject a fake `Vieneu`); never import
  real weights.
- Use `pytest.raises` for error paths; assert on observable behavior
  (output audio length, signal sequence, state transitions), not
  implementation detail.
- Prefer `tmp_path` for file outputs; never write to the user data dir.
