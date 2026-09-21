# Track Learnings: audiobook_epub_20260828

Patterns, gotchas, and context discovered during implementation.

## Codebase Patterns (Inherited)

Key inherited patterns from `conductor/patterns.md` applied in this track:

- Gates: `.venv/bin/ruff check .`, `.venv/bin/ruff format --check .`,
  `.venv/bin/pytest` with `QT_QPA_PLATFORM=offscreen`.
- Fake at the SDK layer (FakeVieneu) for e2e; injectable factories
  everywhere; construction must stay model/audio-stack-free.
- PySide Slot return types: `@Slot(str, result=bool)` (not positional).
- QML `var` property NOTIFY requires fresh-array reassignment, not in-place
  mutation.
- `setContextProperty` does not take ownership — anchor on the engine.
- Subprocess QML drivers: literal `\n`, no column-0 lines; real worker must
  `shutdown()` before quit.
- Fixture generator scripts under `tests/` must pass ruff like app code.

---

<!-- Learnings from implementation will be appended below -->

## Implementation Learnings

- **soundfile infers the container from the file EXTENSION** — an atomic
  temp file must keep the `.wav` suffix (`ch_0000.part.wav`), not
  `ch_0000.wav.tmp` (the latter raises "No format specified"). Same family
  as the BytesIO `format="WAV"` gotcha from phase01.
- **A second controller MUST NOT own a second InferenceWorker** — two
  workers = two engine inits = 2×~800 MB RSS. The job-listener seam
  (attach → submit_stream_for_listener → detach-in-handler) reuses the one
  worker; the busy-refusal in `submit_stream_for_listener` is what makes
  attachment-based routing race-free (a job submitted before attach can
  never complete after it, because attach+submit only happen while idle).
- **QML ListView delegates render but bindings of NON-CURRENT StackLayout
  tabs don't settle**: `property("visible")` of an item in a hidden tab
  reads stale False even though the expression evaluates True; always
  `bridge.setCurrentTab(...)` + processEvents before asserting hidden-tab
  UI state (extends the existing hidden-tab side-effects pattern; the
  ab_* smoke drivers do this).
- **PySide6 context-property fakes for data-driven QML need real NOTIFY
  Properties** — plain Python attributes read as `undefined` in QML
  bindings; the driver fakes declare every bound member as
  `@Property(..., notify=...)`.
- **`QMetaObject.invokeMethod(checkbox, "toggle")` is not reliable;
  `click()` works** (AbstractButton click is the invokable). Also:
  `item_walk` returns delegates in arbitrary order — select them by
  `property("modelData")` content, and filter by `visible` when counting
  conditional delegate children.
- **QML strict binding on CheckBox `checked:` + `onToggled:` write-back**
  is fine: C++ toggle() does not break the binding, onToggled syncs the
  controller, and the NOTIFY re-evaluation keeps them consistent.
- EPUB spine hrefs are percent-encoded and relative to the OPF directory:
  `unquote` + forward-slash normalize + prefix the OPF dir; nav/cover docs
  are excluded by `properties="nav"` and by empty extracted text.
- The e2e audiobook flow needs BOTH AudiobookController instances wired to
  the same recording PlaybackController, or the second session's
  resume-seek is invisible (default factory builds a real QMediaPlayer).
