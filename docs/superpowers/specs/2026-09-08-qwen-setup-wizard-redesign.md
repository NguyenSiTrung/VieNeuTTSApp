# Qwen setup wizard redesign

## Problem

The current wizard asks users to reconfirm that they want Qwen after they explicitly opened setup or selected a Qwen engine. Two binary questions and a hardware acknowledgement consume three pages before any useful action. Runtime choice appears only on the action page, the standalone-build limitation appears too late, missing optional components are presented as failures, and the fixed-height content has no scrolling strategy.

## Goals

- Make the setup path correspond to three user decisions: pack, runtime, and installation.
- Explain the consequence of each choice before installation.
- Show only requirements for the selected pack.
- Preserve in-app checkpoint downloads, cancellation, progress, errors, terminal fallbacks, clipboard copy, readiness refresh, and automatic prompting.
- Keep VieNeu as the clear Vietnamese path and Qwen as an optional multilingual pack.

## Flow

### Stage 1: Choose pack

Present two explicit choices:

1. **CustomVoice**: nine fixed multilingual voices, style instructions, approximately 1.5 GB of model data.
2. **CustomVoice + Base**: everything above plus reference-audio voice cloning, approximately 3 GB of model data.

State that Qwen supports ten non-Vietnamese languages and that Vietnamese synthesis should use VieNeu.

### Stage 2: Choose runtime

Present CPU and NVIDIA CUDA 12.8 choices. CPU is the safe default and works broadly but may be slower than real time. CUDA is available only for supported NVIDIA systems and needs approximately 4 GB VRAM.

Explain here that standalone application packages exclude PyTorch. Users must run the generated command in the source/package Python environment and launch the app from that environment.

### Stage 3: Install and verify

Show an ordered readiness checklist for the selected runtime and pack. Missing required pieces use neutral pending treatment; detected pieces use success treatment; actual operation failures use error treatment.

Offer one primary checkpoint action for the currently missing required model. If both models are required, CustomVoice is downloaded first and Base remains as the next action. Keep individual model identity visible so retry and progress remain unambiguous. Keep the offline fetch command and runtime command copy controls contextual.

When all selected requirements are ready, replace setup actions with a concise success state that tells the user to close the wizard and select the Qwen engine.

## Components

- `QwenSetupWizard.qml`: transient `packVariant`, `runtimeVariant`, and three-stage navigation; scrollable installation content; generated commands and conditional actions.
- `QwenStatusList.qml`: reusable readiness rows parameterized by whether Base is required and whether missing rows are actionable. No backend logic.
- `SettingsTab.qml`: concise pack summary, selected-engine-relevant checkpoint status, and setup entry point. It does not duplicate installation instructions.
- `docs/qwen-setup.md`: same three-stage decision order, followed by terminal and offline alternatives.

No controller or backend API changes are required. Existing download slots remain one-model-at-a-time, which avoids adding orchestration state and preserves clear retries.

## Accessibility and layout

- Use descriptive actions such as “Choose CustomVoice”, “Choose cloning pack”, and “Continue to install”, not ambiguous Yes/No controls.
- Preserve keyboard focus, modal behavior, accessible progress naming, and existing theme contrast.
- Constrain dialog height relative to the available overlay and put long installation content inside a `ScrollView`.
- Keep command text selectable or copyable and wrap long commands without forcing horizontal overflow.

## Verification

Extend the existing offscreen `settings_qwen_wizard` scenario to verify:

- both pack choices and runtime choices;
- stage transitions and back navigation;
- Base requirements hidden for CustomVoice and visible for the cloning pack;
- CPU/CUDA command generation and clipboard actions;
- model download, progress, cancellation, errors, and refresh;
- ready-state replacement;
- automatic opening when a missing Qwen engine is selected;
- compact Settings summary behavior.

Run the focused QML smoke scenario, relevant controller tests, QML diagnostics/load checks available in the project, and the repository quality gates.