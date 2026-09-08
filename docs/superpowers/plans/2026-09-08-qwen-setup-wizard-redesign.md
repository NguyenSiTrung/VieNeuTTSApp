# Qwen setup wizard redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the current four-page Yes/No Qwen setup walk with an adaptive three-stage pack, runtime, and installation flow that preserves existing download and readiness behavior.

**Architecture:** Keep all transient wizard selection state in `QwenSetupWizard.qml`. The wizard selects the required model set and runtime presentation, while existing controller slots continue to perform one-model-at-a-time downloads. `QwenStatusList.qml` remains a reusable presentation component parameterized by requirements; `SettingsTab.qml` becomes a concise entry summary. No backend or dependency changes.

**Tech Stack:** PySide6, Qt Quick/QML, existing `Theme.qml` tokens and `App*` components, pytest offscreen QML smoke tests, Markdown documentation.

**Spec:** `docs/superpowers/specs/2026-09-08-qwen-setup-wizard-redesign.md`

## Global Constraints

- Preserve Qwen optionality: the default VieNeu installation remains torch-free.
- Preserve current controller APIs: `downloadQwenModel`, `cancelQwenModelDownload`, `refreshQwenReadiness`, and `copyToClipboard`.
- Qwen supports ten documented non-Vietnamese languages; Vietnamese remains directed to VieNeu.
- CustomVoice requires approximately 1.5 GB model data; CustomVoice + Base requires approximately 3 GB.
- CUDA guidance is NVIDIA CUDA 12.8 with approximately 4 GB VRAM; CPU remains the broad compatibility fallback.
- Standalone/frozen application packages exclude PyTorch; generated commands target the Python environment used to launch the app.
- QML smoke tests require real NOTIFY properties and offscreen subprocess handling; do not rely on hidden-tab binding state.
- Write behavior tests before production changes and observe the expected failure before implementation.
- Skip commits, formatters, linters, and project-wide tests during individual implementation tasks; run the full quality gate once at the end.

## File Map

- Modify `tests/smoke/test_ui_tabs.py`: replace the old Yes/No interaction assertions with observable three-stage flow assertions while retaining download, readiness, and auto-prompt coverage.
- Modify `src/vienetts_app/ui/qml/QwenSetupWizard.qml`: implement pack/runtime selection, stage navigation, scrollable install view, requirement-aware actions, and accessible labels.
- Modify `src/vienetts_app/ui/qml/QwenStatusList.qml`: accept selected-pack requirements and distinguish pending optional rows from required readiness rows without backend logic.
- Modify `src/vienetts_app/ui/qml/SettingsTab.qml`: reduce the Qwen card to a compact status summary and setup entry point while preserving object names needed by existing UI tests.
- Modify `docs/qwen-setup.md`: reorganize setup instructions to match the three-stage wizard and clarify source/frozen runtime constraints.
- Modify `README.md` only if the concise Qwen setup reference no longer matches the revised guide.
- Modify `conductor/tracks.md` or archived track context only if the project’s conductor workflow requires recording this follow-up track; do not use Markdown task lists as the task source of truth.

---

### Task 1: Add failing smoke assertions for the three-stage contract

**Files:**
- Modify: `tests/smoke/test_ui_tabs.py:2235-2407`
- Read: `tests/smoke/test_ui_tabs.py` fake controller and scenario helpers before editing

**Interfaces:**
- Consumes existing QML object names and fake controller slots.
- Produces assertions for the new object names and observable stage transitions that the QML implementation must satisfy.

- [ ] **Step 1: Write the failing test**

Replace only the old interaction expectations that depend on `qwenWizardYesButton` / `qwenWizardNoButton` with assertions for these stable objects:

```python
required |= {
    "qwenWizardPackCustomVoiceButton",
    "qwenWizardPackCloningButton",
    "qwenWizardCpuModeButton",
    "qwenWizardCudaModeButton",
    "qwenWizardInstallContinueButton",
    "qwenWizardStageLabel",
    "qwenWizardInstallScrollView",
    "qwenWizardDownloadNextButton",
}
```

Exercise the CustomVoice path:

```python
click_item(settings_tab.findChildren(QObject, "qwenSetupButton")[0])
app.processEvents()
assert "1/3" in step_label.property("text")
click_item(settings_tab.findChildren(QObject, "qwenWizardPackCustomVoiceButton")[0])
app.processEvents()
assert "2/3" in step_label.property("text")
click_item(settings_tab.findChildren(QObject, "qwenWizardCpuModeButton")[0])
app.processEvents()
click_item(settings_tab.findChildren(QObject, "qwenWizardInstallContinueButton")[0])
app.processEvents()
assert "3/3" in step_label.property("text")
assert not settings_tab.findChildren(QObject, "qwenStatusBase")[0].property("visible")
```

Exercise the cloning path from a fresh wizard open and verify Base is required and the generated fetch command contains both repository names. Retain existing assertions for clipboard, download, progress, cancellation, refresh, ready state, and auto-prompt, adapting only their navigation controls.

- [ ] **Step 2: Run the focused smoke test to verify it fails**

Run:

```bash
QT_QPA_PLATFORM=offscreen QT_AUDIO_BACKEND=ffmpeg .venv/bin/pytest tests/smoke/test_ui_tabs.py -k settings_qwen_wizard -n0 -vv
```

Expected: FAIL because the new object names and three-stage behavior do not yet exist.

- [ ] **Step 3: Do not modify production code in this task**

Keep this task red. The next task supplies the minimum QML implementation.

- [ ] **Step 4: Record the failing contract**

Capture the exact missing object or assertion in the task notes/track learnings; do not add a workaround to the test.

---

### Task 2: Implement adaptive pack and runtime stages

**Files:**
- Modify: `src/vienetts_app/ui/qml/QwenSetupWizard.qml:24-180,470-541`

**Interfaces:**
- Consumes existing controller readiness and clipboard properties.
- Produces `packVariant` (`"customvoice"` or `"cloning"`), `runtimeVariant` (`"cpu"` or `"cuda"`), and the stable pack/runtime/install object names used by Task 1.

- [ ] **Step 1: Add minimal state and stage labels**

Replace the Yes/No state with:

```qml
property int step: 0
property string packVariant: "customvoice"
property string runtimeVariant: "cpu"
readonly property bool needCloning: packVariant === "cloning"
```

Use `step` values 0, 1, and 2 and expose a stage label containing `1/3`, `2/3`, or `3/3`.

- [ ] **Step 2: Add explicit pack choices**

Create two `AppButton`s named `qwenWizardPackCustomVoiceButton` and `qwenWizardPackCloningButton`. Selecting either sets `packVariant` and advances to runtime selection. Include concise descriptions for fixed voices versus reference-audio cloning, model size, and the Vietnamese VieNeu rule.

- [ ] **Step 3: Add explicit runtime choices**

Create `qwenWizardCpuModeButton` and `qwenWizardCudaModeButton` using the existing command bindings. Present CPU as the default and explain CUDA 12.8/4 GB VRAM. Include the frozen-build limitation before advancing.

- [ ] **Step 4: Add install-stage navigation**

Create `qwenWizardInstallContinueButton` and update Back/Close actions to operate on three stages. Remove Yes/No controls from the live object tree or retain no obsolete object names in the test contract. Back from install returns to runtime; back from runtime returns to pack.

- [ ] **Step 5: Run the focused smoke test**

Run the command from Task 1. Expected: the new stage and selection assertions pass or fail only on install-stage behavior not yet implemented.

---

### Task 3: Refactor status and install actions around selected requirements

**Files:**
- Modify: `src/vienetts_app/ui/qml/QwenStatusList.qml:14-108`
- Modify: `src/vienetts_app/ui/qml/QwenSetupWizard.qml:160-467`

**Interfaces:**
- Consumes `showBase` / selected-pack state and `controller.qwenReadiness`.
- Produces requirement-aware rows and install actions for the selected pack without changing controller APIs.

- [ ] **Step 1: Write the failing status assertion**

Add to the smoke scenario a fresh CustomVoice path assertion that Base is not visible, then a cloning path assertion that Base is visible and the fetch command contains both repository IDs. Assert the primary next action is hidden when all selected requirements are ready.

- [ ] **Step 2: Run the focused test and verify the new assertion fails**

Run the focused smoke command. Expected: failure because status rows and install controls still use the old unconditional/Yes-No flow.

- [ ] **Step 3: Parameterize `QwenStatusList`**

Use `showBase` as the selected requirement flag. Keep runtime, torch, and CustomVoice rows always present; hide Base when `showBase` is false. Replace failure language for missing rows with neutral pending language, while retaining success colors for ready rows and error color only for actual controller errors.

- [ ] **Step 4: Add the next-required-model action**

Create `qwenWizardDownloadNextButton`. Its text and action select CustomVoice first, then Base when cloning is selected and CustomVoice is ready. Keep individual download buttons only where they improve retry clarity. Do not add a new controller coordinator.

- [ ] **Step 5: Wrap installation content in a `ScrollView`**

Create `qwenWizardInstallScrollView`, constrain its height using the available overlay/window height, and place status, actions, commands, progress, errors, and refresh controls inside it. Preserve selectable wrapped command text and copy buttons.

- [ ] **Step 6: Run the focused smoke test**

Run the focused command. Expected: status filtering, command generation, download, progress, cancellation, refresh, and ready assertions pass.

---

### Task 4: Simplify Settings summary and preserve auto-prompt behavior

**Files:**
- Modify: `src/vienetts_app/ui/qml/SettingsTab.qml:546-582,2217-2233`
- Modify: `tests/smoke/test_ui_tabs.py` only if summary-specific expectations need tightening

**Interfaces:**
- Consumes `controller.qwenReadiness` and current engine selection.
- Produces a compact Qwen summary and the existing `qwenSetupButton`/`qwenSetupWizard` entry points.

- [ ] **Step 1: Write the failing summary assertion**

Assert that the Settings card still exposes the setup button and readiness summary, while the full wizard-only install command controls are not descendants of the card.

- [ ] **Step 2: Run the focused smoke test and verify it fails**

Expected: failure while the Settings card still owns the full `QwenStatusList`/duplicated instructions.

- [ ] **Step 3: Reduce the card contents**

Keep title, readiness subtitle/badge, a concise runtime/model summary, documentation hint, and setup button. Avoid duplicating wizard commands or installation actions. Preserve the existing automatic `onTtsEngineChanged` opening behavior and guard for fake controllers.

- [ ] **Step 4: Run the focused smoke test**

Expected: summary and auto-prompt assertions pass.

---

### Task 5: Align the Qwen setup guide with the new flow

**Files:**
- Modify: `docs/qwen-setup.md:1-170`
- Modify: `README.md:113-169` only if wording or links are stale

**Interfaces:**
- Documentation only; no runtime interface changes.

- [ ] **Step 1: Rewrite the guide structure**

Lead with a short “choose your path” section matching the wizard:

- Pack: CustomVoice or CustomVoice + Base.
- Runtime: CPU or NVIDIA CUDA 12.8.
- Install: runtime command, selected checkpoint download, readiness refresh.

Move the standalone/frozen-build limitation directly into the runtime section. Preserve exact repository IDs, package pins, disk estimates, language limitations, offline `HF_HOME` instructions, CLI alternatives, voice usage, and troubleshooting.

- [ ] **Step 2: Check copy consistency**

Ensure visible QML labels and guide terminology use the same names: CustomVoice, Base, CPU, NVIDIA CUDA 12.8, checkpoint, runtime, and VieNeu for Vietnamese.

- [ ] **Step 3: Review documentation diff locally**

Read the changed guide sections and confirm no unsupported promise of CPU real-time performance and no obsolete four-step/Yes-No instructions remain.

---

### Task 6: Run verification and close the Beads task

**Files:**
- No source changes expected unless verification finds a defect.

- [ ] **Step 1: Run focused UI verification**

```bash
QT_QPA_PLATFORM=offscreen QT_AUDIO_BACKEND=ffmpeg .venv/bin/pytest tests/smoke/test_ui_tabs.py -k settings_qwen_wizard -n0 -vv
```

Expected: PASS.

- [ ] **Step 2: Run relevant controller and Qwen unit tests**

```bash
QT_QPA_PLATFORM=offscreen QT_AUDIO_BACKEND=ffmpeg .venv/bin/pytest tests/unit/test_controller_engine.py tests/unit/test_qwen_models.py tests/unit/test_qwen_runtime.py tests/unit/test_qwen_backend.py tests/unit/test_qwen_voices.py -n0 -q
```

Expected: PASS.

- [ ] **Step 3: Run repository quality gates**

```bash
.venv/bin/ruff check .
.venv/bin/ruff format --check .
QT_QPA_PLATFORM=offscreen QT_AUDIO_BACKEND=ffmpeg .venv/bin/pytest -q
```

Expected: all commands pass. If the known environment-specific app-entry failures recur, report their exact existing Beads issue rather than claiming a clean full gate.

- [ ] **Step 4: Inspect changed-file status**

```bash
git status --short
```

Expected: only the intended Qwen wizard, status, Settings, test, documentation, spec/plan, and Beads metadata changes are present.

- [ ] **Step 5: Close the implementation issue**

```bash
bd close VieNeuTTSApp-fye --reason="Implemented adaptive three-stage Qwen setup wizard, requirement-aware installation UI, compact Settings summary, aligned guide, and verification."
```

Do not commit or push without explicit authority; report the changed files and verification output in the handoff.
