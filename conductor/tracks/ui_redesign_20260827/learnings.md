# Track Learnings: ui_redesign_20260827

Patterns, gotchas, and context discovered during the UI/UX redesign and refactor.

## Codebase Patterns (Inherited)

- **QML Theme Tokens**: QML files live in `src/vienetts_app/ui/qml/` and use same-directory `import "."` for the `Theme` singleton (registered in `qmldir`); the engine requires `addImportPath(qml_dir)` — bare absolute-path imports are rejected.
- **Context Property Lifetime**: `setContextProperty` does NOT take ownership of the Python object — anchor it (e.g. `engine._bridge = bridge`) or GC collects it and QML bindings see `null`.
- **QML Object Tree Smoke Assertions**: Qt allows ONE `QGuiApplication` per process and QML aborts the interpreter if only a headless `QCoreApplication` exists — GUI-object-tree assertions in pytest must run in a subprocess (`sys.executable -c` + `RESULT:` JSON line).
- **QML Array Reactivity**: QML `var` property signals do NOT fire on in-place array mutation (`samples.push(...)` leaves derived bindings stale) — reassign a fresh array per update so `NOTIFY` fires.
- **Offscreen UI Testing**: `findChildren(QObject, name)` returns generic QObject wrappers — read QML-declared properties via `.property("name")`, not attribute access; `StackLayout` instantiates ALL children, so use `currentIndex` for visibility, not existence.
- **Dialog Seams**: Qt6 QML dialogs: FileDialog has no `folder` (Qt5 name) — set `currentFolder` imperatively before `open()`. Expose each dialog's logic as a QML root function (`importPath`/`selectClip`/`setOutputDir`) so tests invoke via `QMetaObject` with `Q_ARG("QVariant", ...)`.
- **Quality Gates**: `ruff check .` + `ruff format --check .` + `pytest` must all pass before completing any task.

---

<!-- Learnings from implementation will be appended below -->
