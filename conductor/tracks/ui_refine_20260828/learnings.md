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

## [2026-08-28] — Implementation learnings

### Gotchas discovered
- **MultiEffect (Qt 6.11)** has NO `autoShadowColor` property (compile error);
  `shadowEnabled` + `shadowColor` alone are the valid API.
- **Inline components in tabs are trap-prone**: a `component SettingsRow`
  template with `parent.parent.label` lookups + default-property aliasing was
  fragile; explicit rows were safer. Same for anchors on children that an
  AppCard's `default property alias` funnels into a ColumnLayout — a DropArea
  declared as a direct AppCard child becomes layout-managed → "Detected
  anchors on an item that is managed by a layout" (undefined behavior).
  Wrap DropAreas in a plain `Item` child or attach inside a Rectangle.
- **QML property API mismatches bite at load time**: `Label`/`Text` have
  `lineHeight` but `TextArea` (TextEdit) does NOT (use `lineSpacing`);
  `underline` is `font.underline`; `RowLayout` has no `leftPadding`;
  components in the SAME directory resolve implicitly (no `import
  "components"` from inside `components/` — only `import ".."` for Theme).
- **FontLoader source** resolves relative to the QML file: fonts must sit
  under `ui/qml/fonts/` for `"fonts/X.ttf"` to resolve; a `pragma Singleton`
  QtObject may own FontLoader children and expose the loaded family (name is
  NOTIFY — `fontFamily` binding flips when the font registers).
- **Text-pin gotcha**: tests read the CONTROL's `text` property (e.g.
  `denoiseCheck.property("text")`), so custom `contentItem` must mirror
  `control.text`, not replace the property's value.
- **Keyboard-focus ring pattern**: `activeFocus && (focusReason ===
  Qt.TabFocusReason || focusReason === Qt.BacktabFocusReason)` shows the ring
  for keyboard nav only, keeping click-focus clean.
- **VoicePicker default-purpose reactivity**: a direct `currentIndex` binding
  to `controller.defaultVoice` is circular; imperative sync in
  `Component.onCompleted` + a `Connections` handler on
  `onDefaultVoiceChanged` is the non-circular equivalent.

### Patterns established
- Screenshot harness: FakeController/FakePlayback through `create_app`
  factories + `QQuickWindow.grabWindow()` (no macOS TCC permission, unlike
  `QScreen.grabWindow`) — 9 states (4 tabs × themes + busy + consent) per run.
- Single button skin (`AppButton`) + single combo skin (`AppCombo`) + shared
  `VoicePicker`; tabs contain zero inline Button backgrounds now.
- `AppIcon` Canvas switch is the only icon source (kind string → 20×20 vector).

## [2026-08-28] — Track close-out
- All 9 plan tasks complete; 481 tests green; ruff clean; visual audit pass
  (no truncation/overlap; both themes verified; polish 8.5/10 by vision audit).
- Beads epic VieNeuTTSApp-xd7 closed with all 6 phases.
