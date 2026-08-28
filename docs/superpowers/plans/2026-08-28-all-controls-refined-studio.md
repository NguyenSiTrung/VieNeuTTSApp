# Refined Studio Control-System Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make every VieNeuTTS control read as a coherent, accessible, professional audio-workstation interface while preserving all TTS behavior and existing UI contracts.

**Architecture:** Extend the QML design token system and component library first, then replace per-tab control skins with a small set of semantic primitives. Each studio continues calling its existing `controller`, `audiobook`, `playback`, and `bridge` APIs; the work is entirely in QML composition, visual state, accessibility metadata, and smoke/screenshot validation.

**Tech Stack:** PySide6 / Qt Quick Controls 6, QML, Qt Quick Layouts, Canvas-based `AppIcon`, pytest subprocess smoke drivers, Ruff.

**Spec:** `docs/superpowers/specs/2026-08-28-all-controls-refined-studio-design.md`

## Global Constraints

- Do not change Python controller APIs, inference, playback, export order, persistence, file import behavior, voice IDs, or default-voice synchronization.
- Preserve each current QML `objectName`, test-pinned copy, and QMetaObject-invocable root function.
- Preserve Ctrl+Return, Ctrl+E, Escape, live theme switching, and live language switching.
- Keep QML imports relative: use `import "."` from tab files and `import ".."` from components.
- `VoicePicker` must retain `flatModel`, `selectedVoice`, `purpose`, `activated`, and its non-circular default-voice synchronization.
- Do not add runtime Python dependencies.
- All user-facing copy uses `qsTr()` and English translations must be complete in `vienetts_en.ts` and compiled to `vienetts_en.qm`.
- GUI assertions run in subprocesses with `QT_QPA_PLATFORM=offscreen`; use real `create_app` wiring with fake dependencies only at existing seams.
- At 640 px width, controls must wrap or stack without overlap, inaccessible targets, or loss of critical text.
- The repository is under a conservative Git policy. Do not commit, push, or sync without explicit user authorization.

---

## File map

| File | Responsibility |
|---|---|
| `ui/qml/Theme.qml` | Semantic control, focus, disabled, and responsive-design tokens. |
| `ui/qml/components/AppIcon.qml` | Single Canvas vector set, extended for transport, chevron, close, reset, check, and status icons. |
| `ui/qml/components/AppButton.qml` | Labelled action button variants and busy/disabled semantics. |
| `ui/qml/components/AppIconButton.qml` | Accessible icon-only action button. |
| `ui/qml/components/AppCombo.qml` | Shared field-style ComboBox trigger and popup delegate. |
| `ui/qml/components/VoicePicker.qml` | Voice-specific grouped, scanable selector. |
| `ui/qml/components/AppToggle.qml` | Consistent CheckBox-derived toggle. |
| `ui/qml/components/AppSlider.qml` | Consistent Slider-derived transport control. |
| `ui/qml/components/AppNumberField.qml` | SpinBox-derived formatted numeric input. |
| `ui/qml/components/AppNotice.qml` | Reusable error, warning, success, and progress notice. |
| `ui/qml/components/qmldir`, `ui/qml/qmldir` | Register new reusable components. |
| `TextTab.qml`, `ParagraphTab.qml` | Synthesis workflow, action hierarchy, state explanation, and shared notices. |
| `AudiobookTab.qml` | Batch action hierarchy, toggles, chapter actions, and transport bar. |
| `CloningTab.qml` | Progressive source/name/create flow, toggle, accessible actions, and deletion confirmation. |
| `SettingsTab.qml`, `Main.qml` | Consistent settings controls, output-folder reset, notices, navigation, and shell actions. |
| `tests/smoke/test_ui_tabs.py`, `tests/smoke/test_ui_shell.py` | QML component, behavior, contract, narrow-layout, and screenshot smoke coverage. |
| `ui/i18n/vienetts_en.ts`, `ui/i18n/vienetts_en.qm` | Complete English translations for new QML strings. |

## Task 1: Establish control semantics, tokens, and vector iconography

**Files:**
- Modify: `src/vienetts_app/ui/qml/Theme.qml`
- Modify: `src/vienetts_app/ui/qml/components/AppIcon.qml`
- Modify: `src/vienetts_app/ui/qml/components/AppButton.qml`
- Create: `src/vienetts_app/ui/qml/components/AppIconButton.qml`
- Modify: `src/vienetts_app/ui/qml/components/qmldir`
- Modify: `src/vienetts_app/ui/qml/qmldir`
- Test: `tests/smoke/test_ui_tabs.py`

**Interfaces:**
- Consumes: Existing `Theme` color, spacing, radius, typography, and focus tokens.
- Produces: `AppButton.variant`, `AppButton.iconKind`, `AppButton.busy`, `AppButton.disabledReason`, and `AppIconButton.iconKind`, `accessibleLabel`, `tooltipText`.
- Compatibility: Keep `AppButton.glyph` temporarily as a compatibility alias during migration; remove every usage before the final task.

- [ ] **Step 1: Add a failing button-component contract**

  In `DRIVER`’s `disabled_states` branch, add:

  ```python
  out["generate_disabled_reason"] = generate.property("disabledReason")
  out["generate_min_height"] = generate.property("implicitHeight")
  ```

  Add these assertions to `TestTextTabSmoke.test_disabled_states`:

  ```python
  assert isinstance(result["generate_disabled_reason"], str)
  assert result["generate_min_height"] >= 44
  ```

- [ ] **Step 2: Run the focused test to verify it fails**

  Run:

  ```bash
  QT_QPA_PLATFORM=offscreen .venv/bin/pytest tests/smoke/test_ui_tabs.py::TestTextTabSmoke::test_disabled_states -v
  ```

  Expected: `disabledReason` is not currently exposed and the existing large
  button height is 42 px, below the new 44 px target.

- [ ] **Step 3: Add semantic control tokens and complete the icon source**

  In `Theme.qml`, add named tokens rather than inlining state colors:

  ```qml
  readonly property color controlDisabledBg: isDark ? "#202532" : "#e7edf3"
  readonly property color controlDisabledText: isDark ? "#a7b2c2" : "#5f6e80"
  readonly property color controlDisabledBorder: isDark ? "#30394a" : "#ccd7e3"
  readonly property int controlHeightSm: 32
  readonly property int controlHeightMd: 40
  readonly property int controlHeightLg: 44
  readonly property int controlHitTarget: 40
  readonly property int popupMaxHeight: 320
  ```

  Extend the one `AppIcon` Canvas `switch` with `play`, `pause`, `previous`,
  `next`, `upload`, `download`, `close`, `refresh`, `chevronDown`, `check`,
  `folder`, and `reset`. Use the same 20×20 rounded-stroke style as existing
  icons, never Unicode symbol glyphs or an icon-font dependency.

- [ ] **Step 4: Refactor `AppButton` and create `AppIconButton`**

  Make `AppButton` expose stable semantics:

  ```qml
  property string variant: "secondary" // primary|secondary|quiet|danger|icon
  property string size: "md"           // sm|md|lg
  property string iconKind: ""
  property bool busy: false
  property string disabledReason: ""
  property string accessibleLabel: text
  ```

  Render `AppIcon { kind: root.iconKind }` when `iconKind !== ""`; render the
  normal text label alongside it; suppress duplicate click handling when `busy`
  is true. Map `lg` to `Theme.controlHeightLg`, `md` to
  `Theme.controlHeightMd`, and `sm` to `Theme.controlHeightSm`. Use the
  disabled tokens for a legible disabled state, keep the existing keyboard-only
  focus-ring policy, and attach:

  ```qml
  Accessible.name: root.accessibleLabel
  Accessible.description: !root.enabled ? root.disabledReason : ""
  ```

  Create `AppIconButton.qml` as an `AppButton` specialization with no label,
  `variant: "icon"`, `implicitWidth: Theme.controlHitTarget`, an
  `accessibleLabel`, and a Qt `ToolTip` using `tooltipText`. Register it in
  both `qmldir` files.

- [ ] **Step 5: Run the focused button and QML component-load checks**

  Run the existing application-load tests. They compile and instantiate
  `Main.qml`, which imports each registered component, so malformed QML,
  misspelled imports, and invalid token bindings fail immediately:

  ```bash
  QT_QPA_PLATFORM=offscreen .venv/bin/pytest \
    tests/smoke/test_ui_tabs.py::TestTextTabSmoke::test_disabled_states \
    tests/smoke/test_ui_tabs.py::TestTextTabSmoke::test_load_objectnames_and_picker_model \
    tests/smoke/test_ui_shell.py -v
  ```

  Expected: existing UI loads without QML warnings or changed object-name contracts.

- [ ] **Step 6: Check the task diff**

  Run:

  ```bash
  .venv/bin/ruff check .
  .venv/bin/ruff format --check .
  git diff --check
  ```

  Do not commit without explicit user authorization.

## Task 2: Redesign shared select, toggle, slider, number, and notice primitives

**Files:**
- Modify: `src/vienetts_app/ui/qml/components/AppCombo.qml`
- Modify: `src/vienetts_app/ui/qml/components/VoicePicker.qml`
- Create: `src/vienetts_app/ui/qml/components/AppToggle.qml`
- Create: `src/vienetts_app/ui/qml/components/AppSlider.qml`
- Create: `src/vienetts_app/ui/qml/components/AppNumberField.qml`
- Create: `src/vienetts_app/ui/qml/components/AppNotice.qml`
- Modify: `src/vienetts_app/ui/qml/components/qmldir`
- Modify: `src/vienetts_app/ui/qml/qmldir`
- Test: `tests/smoke/test_ui_tabs.py`

**Interfaces:**
- Consumes: Task 1 tokens, `AppIcon`, and `AppIconButton`.
- Produces: CheckBox-, Slider-, and SpinBox-compatible controls so callers keep
  `checked`, `toggled`, `value`, `from`, `to`, `onMoved`, and `displayText`;
  each exposes `property string accessibleLabel`.
- Compatibility: `AppCombo.openPopup()`/`closePopup()`, ComboBox `activated`,
  and all existing `VoicePicker` public properties remain unchanged.

- [ ] **Step 1: Add failing popup and control-contract checks**

  Add a `voice_picker_popup` branch to `DRIVER` that shows the offscreen
  window, opens the text tab picker, walks every window/overlay visual tree,
  and records:

  ```python
  out["popup_open"] = bool(picker.property("popup").property("visible"))
  out["group_rows_disabled"] = [
      not bool(row.property("enabled")) for row in picker_rows if row.property("isGroup")
  ]
  out["selected_mark_count"] = len([
      item for item in picker_rows
      if item.objectName() == "voicePickerSelectedMark" and item.property("visible")
  ])
  ```

  Assert:

  ```python
  assert result["popup_open"] is True
  assert all(result["group_rows_disabled"])
  assert result["selected_mark_count"] == 1
  ```

  Type `"Eva"` into the popup’s `voicePickerFilter` field and record the
  resulting delegate rows. Assert the matching `— Eva` row is visible, the
  `— Adam` row is not visible, and only the group header containing Eva remains
  visible. Clear the field and assert the initial selected voice remains
  selected.

  Add a `settings_control_library` branch that asserts `temperatureSpin` is an
  `AppNumberField` with `displayText`, and `themeCombo` still opens all its
  delegates through `activated`.

- [ ] **Step 2: Run the new checks to verify they fail**

  Run:

  ```bash
  QT_QPA_PLATFORM=offscreen .venv/bin/pytest \
    tests/smoke/test_ui_tabs.py::TestTextTabSmoke::test_voice_picker_popup \
    tests/smoke/test_ui_tabs.py::TestSettingsTabSmoke::test_settings_control_library -v
  ```

  Expected: the newly added tests fail their assertions until the named QML
  public properties and controls are implemented.

- [ ] **Step 3: Implement form-field-style `AppCombo` and `VoicePicker`**

  Preserve `ComboBox` roots. Add the following optional `AppCombo` properties:

  ```qml
  property string leadingIconKind: ""
  property string popupTitle: ""
  property string accessibleLabel: displayText
  ```

  Give its trigger a 40 px field surface, clear chevron icon, `popup.opened`
  border state, visible keyboard focus ring, and an accessible name. Its popup
  must constrain the list to `Theme.popupMaxHeight` and scroll.

  In `VoicePicker`, give every flat row:

  ```qml
  readonly property bool isGroup: modelData && modelData.id === ""
  readonly property bool isSelected: !isGroup && modelData.id === root.selectedVoice
  ```

  Group rows are inert `SectionLabel`-style rows. Voice rows show the existing
  label as the title, a secondary parsed description where the catalog label
  contains `—` and `·`, and a visible `AppIcon { kind: "check" }` with object
  name `voicePickerSelectedMark` only for `isSelected`. Keep `width: root.width`
  and both required delegate properties to avoid the existing Qt 6
  `modelData/index` failure mode.

  Add `property string filterText: ""`. For catalogs over twelve flat rows, show
  a `TextField` with object name `voicePickerFilter` at the popup top and bind
  its `onTextChanged` to `root.filterText = text`; retain the field as an
  invisible child for smaller catalogs so the binding is always valid. Keep
  `model: flatModel`; add `groupHasMatchingVoice(groupIndex)` so the delegate
  keeps its original index but binds `visible` and `height` to the current
  filter:

  ```qml
  readonly property bool filterActive: root.filterText.trim() !== ""
  readonly property bool rowMatches: {
      const needle = root.filterText.trim().toLocaleLowerCase();
      return needle === "" || modelData.label.toLocaleLowerCase().includes(needle);
  }
  visible: !isGroup ? rowMatches : root.groupHasMatchingVoice(index)
  height: visible ? 40 : 0
  ```

  `groupHasMatchingVoice` scans forward until the next group header and returns
  true if one selectable voice label matches. This keeps group labels
  non-selectable, hides empty groups, preserves model indexes for
  `activated(index)`, and leaves the current selection unchanged when filtering
  is cleared.

- [ ] **Step 4: Implement the remaining controls as Qt Controls subclasses**

  `AppToggle` must be a `CheckBox`; `AppSlider` must be a `Slider`; and
  `AppNumberField` must be a `SpinBox`, so all existing smoke drivers can read
  and invoke their inherited properties. Style their indicator/handle/background
  with Task 1 tokens, expose `property string accessibleLabel: ""` and bind
  `Accessible.name: root.accessibleLabel`, and preserve visible focus.

  `AppNotice` must accept:

  ```qml
  property string tone: "info" // info|success|warning|error
  property string title: ""
  property string message: ""
  property string messageObjectName: ""
  property string actionText: ""
  signal actionTriggered()
  ```

  It uses the matching Theme status colors and assigns `messageObjectName` to
  the rendered message Label. This lets current smoke tests continue finding
  `errorLabel` after every banner migration.

- [ ] **Step 5: Run the focused shared-control tests**

  Run:

  ```bash
  QT_QPA_PLATFORM=offscreen .venv/bin/pytest \
    tests/smoke/test_ui_tabs.py::TestTextTabSmoke \
    tests/smoke/test_ui_tabs.py::TestSettingsTabSmoke -v
  ```

  Expected: current picker, settings ComboBox, temperature formatting, and
  original object-name assertions remain green, plus the new popup/control
  checks pass.

- [ ] **Step 6: Check lint, formatting, and the component registration diff**

  Run:

  ```bash
  .venv/bin/ruff check .
  .venv/bin/ruff format --check .
  git diff --check
  ```

  Do not commit without explicit user authorization.

## Task 3: Apply the synthesis workflow to Text and Paragraph

**Files:**
- Modify: `src/vienetts_app/ui/qml/TextTab.qml`
- Modify: `src/vienetts_app/ui/qml/ParagraphTab.qml`
- Test: `tests/smoke/test_ui_tabs.py`

**Interfaces:**
- Consumes: Task 1 `AppButton`; Task 2 `VoicePicker` and `AppNotice`.
- Produces: `textActionHint` and `paragraphActionHint` state labels, with every
  pre-existing text/paragraph object name and controller call unchanged.
- Compatibility: `submitForSynthesis()`, `openExportDialog()`, `importPath()`,
  `toLocalPath()`, `buildFlatModel()`, generate/export/quick-export methods,
  and the current streaming state behavior remain intact.

- [ ] **Step 1: Expand behavior tests before altering layout**

  Add to the text `export_flow` driver state:

  ```python
  out["hint_before_audio"] = find("textActionHint").property("text")
  controller.hasAudio = True
  app.processEvents()
  out["hint_before_export"] = find("textActionHint").property("text")
  ```

  Assert:

  ```python
  assert result["hint_before_audio"] == "Tạo âm thanh trước khi phát hoặc xuất."
  assert result["hint_before_export"] == "Xuất WAV để phát lại."
  ```

  Mirror this in paragraph scenarios with `paragraphActionHint`. Extend
  `generate_flow` and `para_generate` to assert busy progress remains visible
  without hiding the `generateButton` object, because the new control keeps its
  place and becomes busy rather than disappearing:

  ```python
  assert result["busy_generate_visible"] is True
  assert result["busy_generate_busy"] is True
  ```

- [ ] **Step 2: Run the focused tests to confirm the expected failure**

  Run:

  ```bash
  QT_QPA_PLATFORM=offscreen .venv/bin/pytest \
    tests/smoke/test_ui_tabs.py::TestTextTabSmoke \
    tests/smoke/test_ui_tabs.py::TestParagraphTabSmoke -v
  ```

  Expected: failures on the new helper-label and busy-button assertions.

- [ ] **Step 3: Replace the two flat action rows with a shared hierarchy**

  In each tab, retain the existing voice `RowLayout`, then introduce a
  synthesis action region immediately below it:

  ```qml
  AppButton {
      id: generateBtn
      objectName: "generateButton"
      variant: "primary"
      size: "lg"
      iconKind: "wave"
      text: qsTr("Tạo âm thanh")
      busy: controller.busy
      visible: true
      enabled: editor.text.trim() !== "" && !controller.busy
      disabledReason: editor.text.trim() === ""
          ? qsTr("Nhập văn bản để tạo âm thanh.") : ""
      onClicked: root.submitForSynthesis()
  }
  ```

  Keep playback, export, and quick export as secondary/quiet controls with
  `iconKind` values. Replace the Unicode `glyph` values. Use a single
  `textActionHint`/`paragraphActionHint` Label that binds in this order:
  blank editor, no audio, audio awaiting export, then empty string. Keep
  importing and clear actions by the editor, not in the synthesis group.

  Move waveform, progress, and cancel into one feedback container. Bind the
  existing `progressBar`, `busyLabel`/`paraBusyLabel`, and `cancelButton`
  object names to their new elements; their property values and controller
  actions must stay unchanged.

- [ ] **Step 4: Convert error/success feedback to `AppNotice`**

  Replace each hand-drawn error panel with `AppNotice`, setting:

  ```qml
  tone: "error"
  message: controller.errorText
  messageObjectName: "errorLabel"
  visible: controller.errorText !== ""
  ```

  Preserve ParagraphTab’s `errorBanner` object name on a wrapping Item, its
  `controller.errorText || root.importError` behavior, and TextTab’s cancel and
  export toast behavior. Do not change the strings checked by the existing
  tests.

- [ ] **Step 5: Run the synthesis smoke suites**

  Run:

  ```bash
  QT_QPA_PLATFORM=offscreen .venv/bin/pytest \
    tests/smoke/test_ui_tabs.py::TestTextTabSmoke \
    tests/smoke/test_ui_tabs.py::TestParagraphTabSmoke \
    tests/smoke/test_ui_tabs.py::TestTextStreamE2E \
    tests/smoke/test_ui_tabs.py::TestParagraphStreamSmoke \
    tests/smoke/test_ui_tabs.py::TestParagraphStreamE2E \
    tests/smoke/test_ui_tabs.py::TestCrossTabStreamLifecycle -v
  ```

  Expected: the new action-state assertions pass; streaming, import, export,
  and cancellation behavior remains unchanged.

- [ ] **Step 6: Check the task diff**

  Run:

  ```bash
  .venv/bin/ruff check .
  .venv/bin/ruff format --check .
  git diff --check
  ```

  Do not commit without explicit user authorization.

## Task 4: Refine the audiobook and cloning task flows

**Files:**
- Modify: `src/vienetts_app/ui/qml/AudiobookTab.qml`
- Modify: `src/vienetts_app/ui/qml/CloningTab.qml`
- Test: `tests/smoke/test_ui_tabs.py`

**Interfaces:**
- Consumes: `AppButton`, `AppIconButton`, `AppToggle`, `AppSlider`, and
  `AppNotice`.
- Produces: unchanged audiobook and cloning controller calls; the remove-voice
  operation gains a local confirmation UI before it invokes `removeVoice(id)`.
- Compatibility: keep `autoAdvanceToggle`, `seekSlider`, `playPauseButton`,
  `cloneRemoveButton`, and all existing object names and root entry functions.

- [ ] **Step 1: Add failing interaction assertions**

  In the audiobook driver’s `ab_interact` branch, read:

  ```python
  out["auto_toggle_height"] = afind("autoAdvanceToggle")[0].property("implicitHeight")
  out["seek_accessible_label"] = afind("seekSlider")[0].property("accessibleLabel")
  ```

  Assert a toggle hit target of at least 40 px and the Vietnamese seek label
  `Vị trí phát`.

  In cloning’s `clone_remove` scenario, make the first click record
  `removeConfirmDialog.visible is True` and `remove_calls == []`; invoke
  `cloneRemoveConfirmButton.click()` and then assert `remove_calls == ["my_clone"]`.

- [ ] **Step 2: Run the focused tests to verify they fail**

  Run:

  ```bash
  QT_QPA_PLATFORM=offscreen .venv/bin/pytest \
    tests/smoke/test_ui_tabs.py::TestAudiobookTabSmoke::test_ab_interactions_reach_controller_slots \
    tests/smoke/test_ui_tabs.py::TestCloningTabSmoke::test_clone_remove_voice -v
  ```

  Expected: current controls do not expose the required semantic properties or
  confirmation sequence.

- [ ] **Step 3: Rebuild audiobook controls around task hierarchy**

  Use `AppToggle` for `autoAdvanceToggle` and `AppSlider` for `seekSlider`;
  preserve their inherited `checked`, `click`, `value`, and `onMoved` behavior.
  Set:

  ```qml
  objectName: "seekSlider"
  accessibleLabel: qsTr("Vị trí phát")
  ```

  Make **Tạo tất cả** the one primary batch action, retain export as secondary,
  and replace row remove, render, previous, next, and play/pause Unicode glyphs
  with `AppIconButton` or `AppButton.iconKind`. The play/pause button remains
  the larger primary transport control and must continue calling the current
  pause/resume/play methods exactly as it does today.

- [ ] **Step 4: Make cloning visibly sequential and confirm deletion**

  Keep its current three cards but add a non-interactive status mark to each:
  source is ready when `clipPath !== ""`, name/create is ready when the trimmed
  name is non-empty, and catalog is complete when a cloned voice exists.
  Replace `denoiseCheck` with `AppToggle`; keep `checked` defaulting to true.

  Add a QML `Dialog` with:

  ```qml
  objectName: "removeConfirmDialog"
  property string voiceId: ""
  AppButton {
      objectName: "cloneRemoveConfirmButton"
      variant: "danger"
      text: qsTr("Xóa giọng")
      onClicked: {
          controller.removeVoice(removeConfirmDialog.voiceId)
          removeConfirmDialog.close()
      }
  }
  ```

  Each existing `cloneRemoveButton` opens this dialog and assigns its row’s
  voice ID. Preserve the visible `Xóa` text on the trigger so current copy
  contracts remain valid.

  Convert cloning’s bespoke error block to `AppNotice`, retaining `errorLabel`.

- [ ] **Step 5: Run the studio-specific smoke tests**

  Run:

  ```bash
  QT_QPA_PLATFORM=offscreen .venv/bin/pytest \
    tests/smoke/test_ui_tabs.py::TestAudiobookTabSmoke \
    tests/smoke/test_ui_tabs.py::TestCloningTabSmoke -v
  ```

  Expected: chapter rendering, player controls, toggles, cloning enrollment,
  preview, confirmation, and catalog updates all pass.

- [ ] **Step 6: Check the task diff**

  Run:

  ```bash
  .venv/bin/ruff check .
  .venv/bin/ruff format --check .
  git diff --check
  ```

  Do not commit without explicit user authorization.

## Task 5: Unify Settings and shell controls

**Files:**
- Modify: `src/vienetts_app/ui/qml/SettingsTab.qml`
- Modify: `src/vienetts_app/ui/qml/Main.qml`
- Test: `tests/smoke/test_ui_tabs.py`
- Test: `tests/smoke/test_ui_shell.py`

**Interfaces:**
- Consumes: `AppCombo`, `AppNumberField`, `AppIconButton`, `AppNotice`, and
  Task 1 button/icon tokens.
- Produces: existing settings write paths through `controller`/`bridge`, with
  one additional QML-only reset route `controller.outputDir = ""`.
- Compatibility: preserve all IDs/object names, `setOutputDir(path)`,
  ComboBox `activated` handlers, and current restart/theme/language behavior.

- [ ] **Step 1: Add failing settings and shell assertions**

  Add a `settings_output_reset` driver branch:

  ```python
  reset = settings_tab.findChildren(QObject, "outputDirResetButton")[0]
  controller.outputDir = str(tmp / "custom")
  app.processEvents()
  out["reset_visible_with_custom_path"] = bool(reset.property("visible"))
  reset.click()
  app.processEvents()
  out["output_dir_after_reset"] = controller.outputDir
  ```

  Assert:

  ```python
  assert result["reset_visible_with_custom_path"] is True
  assert result["output_dir_after_reset"] == ""
  ```

  In `test_ui_shell.py`, assert `audioRefreshButton` is an `AppButton`-style
  control with an accessible label and that the engine status card remains
  discoverable after a live theme switch.

- [ ] **Step 2: Run the focused tests to verify failure**

  Run:

  ```bash
  QT_QPA_PLATFORM=offscreen .venv/bin/pytest \
    tests/smoke/test_ui_tabs.py::TestSettingsTabSmoke \
    tests/smoke/test_ui_shell.py -v
  ```

  Expected: the reset object and shell accessibility assertions fail.

- [ ] **Step 3: Replace isolated settings skins and surface apply timing**

  Replace the raw `SpinBox` with `AppNumberField`, retaining:

  ```qml
  id: temperatureSpin
  objectName: "temperatureSpin"
  from: 5
  to: 200
  stepSize: 5
  value: Math.round(controller.temperature * 100)
  ```

  Preserve `textFromValue`, `valueFromText`, validator, and
  `onRealValueChanged`. Do not alter its localized `displayText` behavior.

  Give every `AppCombo` a form-field trigger and set a descriptive accessible
  name. Keep the theme preview delegate and all `activated` behavior. Convert
  the restart and error rectangles to `AppNotice`.

  Add an `AppIconButton` reset action beside the output-folder browse action:

  ```qml
  objectName: "outputDirResetButton"
  iconKind: "reset"
  accessibleLabel: qsTr("Khôi phục thư mục mặc định")
  visible: controller.outputDir !== ""
  onClicked: controller.outputDir = ""
  ```

- [ ] **Step 4: Finish shell navigation and notices**

  Keep navigation as a `Button`-derived control but use the Task 1 control
  state tokens and `Accessible.name: modelData.label`. Replace
  `audioRefreshButton` with a quiet `AppButton`, preserving its object name and
  `controller.refreshAudioAvailability()` call. Convert the export-only and
  model-missing panels to `AppNotice`-based composition while retaining
  `exportOnlyNotice`, `modelsMissingOverlay`, `modelsMissingCommand`, and
  `modelsRetryButton`.

- [ ] **Step 5: Run settings and shell smoke suites**

  Run:

  ```bash
  QT_QPA_PLATFORM=offscreen .venv/bin/pytest \
    tests/smoke/test_ui_tabs.py::TestSettingsTabSmoke \
    tests/smoke/test_ui_shell.py \
    tests/smoke/test_e2e_flows.py -v
  ```

  Expected: output reset, translations, settings persistence, model-missing,
  export-only, and existing end-to-end UI flows pass.

- [ ] **Step 6: Check the task diff**

  Run:

  ```bash
  .venv/bin/ruff check .
  .venv/bin/ruff format --check .
  git diff --check
  ```

  Do not commit without explicit user authorization.

## Task 6: Complete translations, responsive smoke coverage, and visual audit

**Files:**
- Modify: `src/vienetts_app/ui/i18n/vienetts_en.ts`
- Modify: `src/vienetts_app/ui/i18n/vienetts_en.qm`
- Modify: `tests/smoke/test_ui_tabs.py`
- Modify: `tests/smoke/test_ui_shell.py`

**Interfaces:**
- Consumes: final QML component/translation strings from Tasks 1–5.
- Produces: no unfinished translation entries, generated screenshot evidence,
  and a full green test gate.
- Compatibility: English live-switch regression behavior and all existing
  Vietnamese copy pins remain intact.

- [ ] **Step 1: Add a failing 640 px layout scenario**

  Add a `narrow_layout` branch to the main QML smoke driver. Resize the visible
  offscreen window and inspect both text and settings tabs:

  ```python
  window.resize(640, 740)
  window.show()
  app.processEvents()
  text_generate = find("generateButton")
  settings_tab = find("settingsTab")
  out["window_width"] = window.width()
  out["generate_width"] = text_generate.width()
  out["generate_height"] = text_generate.height()
  out["settings_visible"] = bool(settings_tab.property("visible"))
  ```

  Capture a `QQuickWindow.grabWindow()` image after switching each of the five
  tabs and each theme. Save them under the per-test `tmp_path` by default, or
  under `Path(os.environ["UI_AUDIT_DIR"])` when that environment variable is
  set. Assert each image is non-null and at least 640 px wide:

  ```python
  image = window.grabWindow()
  out[f"{theme}_{tab}_image"] = not image.isNull() and image.width() >= 640
  ```

  Add assertions that every one of the ten capture flags is true, then manually
  inspect the saved images before final completion.

- [ ] **Step 2: Run the narrow-layout test to verify failure**

  Run:

  ```bash
  QT_QPA_PLATFORM=offscreen .venv/bin/pytest \
    tests/smoke/test_ui_shell.py::TestShellSmoke::test_narrow_layout_screenshots -v
  ```

  Expected: failure until the scenario and its test class are added.

- [ ] **Step 3: Finish responsive layouts and create the screenshot harness**

  Add `Layout.minimumWidth`, `Layout.maximumWidth`, `Layout.fillWidth`, and
  explicit wrap/stack behavior to action and settings rows. Use a
  `ColumnLayout` at narrow widths where a row cannot retain 40 px targets and
  readable text. Do not condition this on platform or language.

  Put the capture scenario in the existing subprocess test architecture so it
  uses real `create_app` wiring and fake controller/playback seams. It must
  capture the five tab baselines in both themes, plus busy, error, and populated
  selector states. It writes test images below `tmp_path` unless `UI_AUDIT_DIR`
  is explicitly set for a manual visual review; do not add binary screenshot
  artifacts to the repository.

- [ ] **Step 4: Update and compile translations**

  Run:

  ```bash
  ./scripts/update_i18n.sh
  ```

  Translate every newly generated unfinished source entry in
  `src/vienetts_app/ui/i18n/vienetts_en.ts`, preserving placeholders and
  accessible labels. Run the script again:

  ```bash
  ./scripts/update_i18n.sh
  ```

  Confirm no unfinished entries remain:

  ```bash
  ! grep -n 'type="unfinished"' src/vienetts_app/ui/i18n/vienetts_en.ts
  ```

- [ ] **Step 5: Run targeted visual, localization, and smoke validation**

  Run:

  ```bash
  audit_dir=$(mktemp -d /tmp/vieneu-ui-audit.XXXXXX)
  printf 'UI audit images: %s\n' "$audit_dir"
  UI_AUDIT_DIR="$audit_dir" QT_QPA_PLATFORM=offscreen .venv/bin/pytest \
    tests/smoke/test_ui_tabs.py \
    tests/smoke/test_ui_shell.py \
    tests/unit/test_i18n.py -v
  ```

  Inspect the printed audit directory for the ten baseline screenshots plus busy,
  error, and selector images. Check Vietnamese diacritics, long strings, popup
  clipping, selected-row visibility, focus, and 640 px wrapping in both themes.

- [ ] **Step 6: Run the complete quality gate and record task status**

  Run:

  ```bash
  .venv/bin/ruff check .
  .venv/bin/ruff format --check .
  QT_QPA_PLATFORM=offscreen .venv/bin/pytest
  git diff --check
  git status --short
  ```

  When every check passes, update the `VieNeuTTSApp-mp2` Beads issue with the
  validation result and close it only after the user accepts the implemented
  redesign. Do not commit, push, or run `bd dolt push` without explicit user
  authorization.

## Coverage review

- Design tokens, contrast-safe disabled states, focus, and icons: Task 1.
- Field-style controls, grouped voice picker, toggles, slider, number input,
  and notices: Task 2.
- Text/paragraph synthesis hierarchy and state recovery: Task 3.
- Audiobook batch/player controls and cloning confirmation flow: Task 4.
- Settings controls, output reset, shell actions, and overlays: Task 5.
- Translation completeness, 640 px behavior, screenshots, and full gate: Task 6.
