# Track Learnings: ui_refine_20260828

Patterns, gotchas, and context discovered during the UI refinement pass 2.

## Codebase Patterns (Inherited)

- **QML Theme Tokens**: QML files live in `src/vienetts_app/ui/qml/`, `import "."`
  for the Theme singleton; engine needs `addImportPath(qml_dir)`.
- **Context Property Lifetime**: anchor objects set via `setContextProperty` on the
  engine (`engine._bridge = bridge`) or GC nulls them in QML.
- **Smoke contract**: objectNames + exact Vietnamese copy pins live in
  `tests/smoke/test_ui_tabs.py` / `test_ui_shell.py`; `tests/unit/test_bridge.py`
  pins TABS labels. ComboBoxes are driven by emitting `activated(i)`.
- **One QGuiApplication per process**: GUI-tree assertions run in subprocesses with
  a `RESULT:` JSON line; `QT_QPA_PLATFORM=offscreen`.
- **QML Array Reactivity**: reassign fresh arrays (no in-place mutation) so NOTIFY fires.
- **Quality Gates**: `ruff check .` + `ruff format --check .` + `pytest` green
  before each commit; conventional prefix + `git notes add` task summary.
- **Screenshot harness**: `QQuickWindow.grabWindow()` captures the QML scene on
  macOS WITHOUT screen-recording TCC permission (`QScreen.grabWindow` needs it and
  silently returns empty); driver script pattern parked at /tmp/vieneu_ui_shots/driver.py
  (FakeController + FakePlayback through `create_app` factories).

---

<!-- Learnings from implementation will be appended below -->
