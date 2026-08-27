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
## [2026-08-27 12:15] - Phase 1: Design Tokens & Component Library
- **Implemented:** Refactored Theme.qml to dynamic Light/Dark design system (`Theme.isDark` reacting to `bridge.effectiveTheme`); created reusable component primitives `AppCard`, `AppButton`, `EmotionChip`, `StatusBadge` in `src/vienetts_app/ui/qml/components/`; registered them in `qmldir`.
- **Files changed:** `src/vienetts_app/ui/qml/Theme.qml`, `src/vienetts_app/ui/qml/qmldir`, `src/vienetts_app/ui/qml/components/*`, `tests/unit/test_theme.py`
- **Commit:** Pending
- **Learnings:**
  - Patterns: Components import `".."`, exposing standard theme properties with smooth `Behavior on color` transitions.
  - Gotchas: When defining singletons in `qmldir`, subfolder components can be exposed cleanly at the root module level or via relative import.

## [2026-08-27 12:25] - Phase 2: Shell & Navigation Rail Redesign
- **Implemented:** Redesigned `Main.qml` with modern desktop audio workstation shell: brand header with micro-waveform badge and v3 Turbo pill; tab navigation with active accent indicators, icons, and hover feedback; hardware engine status card at sidebar base; modal card styling for modelsMissingOverlay; amber status bar for exportOnlyNotice.
- **Files changed:** `src/vienetts_app/ui/qml/Main.qml`
- **Commit:** Pending
- **Learnings:**
  - Patterns: Preserving all `objectName` identifiers and property bindings (`engineReadout`, `navBar`, `tabStack`, `exportOnlyNotice`, `modelsMissingOverlay`, `modelsMissingCommand`, `modelsRetryButton`) guarantees 100% compatibility with smoke test suites.
  - Gotchas: When nesting `Repeater` inside `ColumnLayout`, wrap delegates in simple controls (`Button`) with explicit `Layout.fillWidth` to prevent layout collapse.
---
### [2026-08-27 12:35] - Phase 3: Synthesis Studios Refactor (TextTab & ParagraphTab)
- **Implemented**: Upgraded `TextTab.qml` and `ParagraphTab.qml` with `AppCard` layout containers, live text and document metrics, quick emotion chips with icon and tag insertion, grouped voice picker hierarchy, responsive action rows, and styled status banners.
- **Learnings**:
  - `AppCard.qml` header items and action layouts require explicit `Layout.fillWidth: true` and flexible content handling to support custom header widgets seamlessly without layout clipping.
  - Emotion hints in `test_ui_tabs.py` check for the literal string `"[cười]"` in any QObject's `text` property — ensuring the toolbar title and chips preserve these exact cue strings keeps accessibility and smoke assertions 100% compliant.
  - Signal handlers on custom QML components (`EmotionChip`) should declare both `signal clicked()` and `signal chipClicked(string insertedTag)` with alias `icon: emoji` to allow both standard and semantic event handling.

### [2026-08-27 12:45] - Phase 4: Voice Cloning & Settings Studios (CloningTab & SettingsTab)
- **Implemented**: Upgraded `CloningTab.qml` and `SettingsTab.qml` to full desktop studio aesthetics with modular `AppCard` containers, privacy trust badges, interactive voice catalog cards, organized hardware/synthesis/appearance configuration cards, and qualitative temperature guidance notes.
- **Learnings**:
  - `ItemDelegate` custom delegates in `ComboBox` popups require explicit binding of `required property var modelData` and `required property int index`, plus `width: combo.width` to ensure popup item clicks and highlights align properly under offscreen and GUI event dispatchers.
  - Wrapping tab contents in a responsive `ScrollView` with `contentWidth: availableWidth` and centered max-width layouts (`Math.min(840, root.availableWidth - Theme.spacingLg * 2)`) gives modern desktop feel across standard (640x420 min) and high-res (1440x900+) displays without horizontal scroll clipping.
