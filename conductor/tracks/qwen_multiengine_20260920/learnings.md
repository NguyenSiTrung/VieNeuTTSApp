# Track Learnings: qwen_multiengine_20260920

Patterns, gotchas, and context discovered during implementation.

## Codebase Patterns (Inherited)

`conductor/patterns.md` contains 113 inherited project patterns. The patterns
most relevant to this track are:

- Quality gate per task: `.venv/bin/ruff check .`, `.venv/bin/ruff format
  --check .`, and `.venv/bin/pytest`; commit with a conventional prefix and a
  git-note task summary.
- Engine ownership is single-owner: one model owner serializes requests, and
  model/audio stacks stay off the GUI/startup path.
- Immutable job IDs and tagged terminals are the routing contract; stale
  deliveries are dropped and every admitted job settles exactly once.
- Artifact-first synthesis writes `<job>.part.wav` and atomically promotes;
  bounded live preview carries PCM without duration-sized accumulation.
- Managed installs are checksum-pinned, staging-only, resumable, and offline
  after install; QML-facing byte counts use `qlonglong`.
- Cache fingerprints include every input that changes rendered audio; corrupt
  sidecars degrade to cache misses rather than crashes.
- Context properties require Python lifetime anchors; heavyweight one-shot
  work uses injectable background runners with stale-result guards.
- QML smoke drivers share one `QGuiApplication`, use real NOTIFY properties on
  fakes, and consolidate scenarios per subprocess.
- Repeater/ComboBox delegates declare required `modelData`/`index` properties
  on the delegate root.
- Parallel workers own disjoint files; commits and shared UI/i18n copy remain
  serialized by the lead.

---

<!-- Learnings from implementation will be appended below -->
