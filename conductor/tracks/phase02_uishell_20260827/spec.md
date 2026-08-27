# Spec: phase02_uishell_20260827

## Track

Implements **Phase 2** ("UI shell") from [PROJECT_PLAN.md](../../../PROJECT_PLAN.md) §19.

## Overview

Build the PySide6 + QML application shell: GUI bootstrap, navigation between
the four top-level tabs (Text, Paragraph/File, Cloning, Settings),
dark/light/system theming with persistence, and placeholder tab content. No
engine wiring — the shell must launch and navigate **without** loading the
TTS model (lazy init is a Phase 3 concern). This track resolves open
question §21-1: **QML is confirmed** over Qt Widgets+QSS.

## Kickoff Decisions

- **UI framework: QML** (Qt Quick) — resolves §21-1; Widgets+QSS remains a
  documented fallback only if QML iteration proves untenable later.
- **UI testing: offscreen smoke** via pytest-qt (`QT_QPA_PLATFORM=offscreen`):
  launch, tab navigation, theme toggle. QML visual polish is verified
  manually outside CI per workflow.md.
- **Priority: high** (critical path — Phases 3–6 build on the shell).
- **Execution: parallel where file ownership allows.**

## Functional Requirements

- **FR-2.1** GUI entry: `python -m vienetts_app` (no args) launches the QML
  window; `vienetts-app` console script does the same. The `--smoke` CLI path
  and its exit-code contract remain unchanged.
- **FR-2.2** `app.py` + `ui/` layout per §12: QML engine bootstrap
  (QApplication + QQmlApplicationEngine), bridge `QObject` registered with
  the QML context exposing shell state (current tab, theme, detected-engine
  readout string) as properties/signals.
- **FR-2.3** `Main.qml`: window + responsive layout with navigation between
  Text, Paragraph/File, Cloning, Settings tabs (§8: 3-column wide / stacked
  narrow is indicative; shell only needs the nav scaffold).
- **FR-2.4** `Theme.qml` singleton design tokens (colors, spacing,
  typography); dark mode default per §8.
- **FR-2.5** `ui/theme.py`: theme resolution (dark/light/system) persisted
  through the existing `core/settings.py` (`Settings.theme`); changing theme
  in the shell applies immediately and survives restart.
- **FR-2.6** Placeholder tab QML files: `TextTab.qml`, `ParagraphTab.qml`,
  `CloningTab.qml`, `SettingsTab.qml` — static placeholder content only.
- **FR-2.7** Detected-engine readout string surfaced in the shell (from
  `core/detector.py` capability view — display only, no engine
  instantiation).

## Non-Functional Requirements

- **NFR-2.1** App launch must not initialize the TTS engine/model — startup
  stays under a few seconds and model files untouched.
- **NFR-2.2** UI tests run headless/offscreen in the standard pytest gate
  (no display required, no flaky pixel assertions).
- **NFR-2.3** All existing quality gates stay green: `ruff check`,
  `ruff format --check`, `pytest` (137 existing tests must not regress).

## Acceptance Criteria

- **AC-1** `python -m vienetts_app` opens the QML window locally (Linux
  workspace; per-OS verification follows the Phase 0/1 precedent — cross-OS
  gaps recorded and deferred to Phase 6 CI).
- **AC-2** All four tabs are present and navigate in the offscreen pytest-qt
  smoke test.
- **AC-3** Theme dark/light switches live via the bridge and persists across
  restart (verified via settings round-trip test).
- **AC-4** `--smoke` CLI behavior unchanged; existing smoke test still green.
- **AC-5** Offscreen UI smoke suite green within the standard gate.

## Out of Scope

- Engine/worker wiring into the UI (Phase 3), playback/export, file import,
  cloning flows, streaming (Phase 4).
- Real tab content beyond placeholders; consent notice; error/edge-case
  screens (Phase 4).
- Packaging, bundling, signing (Phase 5); cross-OS CI matrix (Phase 6).
- Any model download/load at startup.
