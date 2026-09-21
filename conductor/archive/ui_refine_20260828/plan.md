# Plan: `ui_refine_20260828`

Sequential execution (shared QML files across tasks make parallel edits conflict-prone;
quality gate runs after each phase). Workflow per task: patterns → (tests where
contracts change) → implement → `ruff check` + `ruff format --check` + `pytest` →
commit + git note → learnings.

## Phase 1: Tokens, fonts, base components
<!-- execution: sequential -->
<!-- files: src/vienetts_app/ui/qml/Theme.qml, src/vienetts_app/ui/qml/components/AppButton.qml, src/vienetts_app/ui/qml/components/AppCard.qml, src/vienetts_app/ui/qml/components/StatusBadge.qml, src/vienetts_app/ui/qml/components/EmotionChip.qml, src/vienetts_app/ui/fonts/* -->

- [x] Task 1: Download Be Vietnam Pro (400/500/600/700) into `ui/fonts/`; load in
  `Theme.qml` via FontLoader with system fallback; add `fontWeightRegular`,
  `tracking` tokens, focus-ring tokens; shift accent→teal + success→green.
- [x] Task 2: Upgrade `AppButton` (variants/sizes/focus ring), `AppCard` (elevation
  shadow, wrapping subtitle), `StatusBadge` (status→tone map), `EmotionChip`
  (drop vestigial props, tooltip). No tab changes yet (components stay unused-safe).

## Phase 2: Shell & bridge
<!-- execution: sequential -->
<!-- files: src/vienetts_app/ui/qml/Main.qml, src/vienetts_app/ui/bridge.py, tests/unit/test_bridge.py -->

- [x] Task 3: Localize bridge TABS labels to Vietnamese; update `test_bridge.py`
  label pins; nav rail: SectionLabel, refined brand tile, StatusBadge engine card;
  restyle export-only notice + models-missing overlay. Run full gates.

## Phase 3: Shared studio components
<!-- execution: sequential -->
<!-- files: src/vienetts_app/ui/qml/components/VoicePicker.qml, src/vienetts_app/ui/qml/components/PageHeader.qml, src/vienetts_app/ui/qml/components/PageShell.qml, src/vienetts_app/ui/qml/components/SectionLabel.qml, src/vienetts_app/ui/qml/WaveformIndicator.qml, src/vienetts_app/ui/qml/qmldir -->

- [x] Task 4: Create `SectionLabel`, `PageHeader`, `PageShell`; extract
  `VoicePicker` (flat-model contract, preselect, delegate); register in qmldir.
  Refine `WaveformIndicator` rendering (rounded gradient bars) keeping the numeric
  contract.

## Phase 4: Text & Paragraph studios
<!-- execution: sequential -->
<!-- files: src/vienetts_app/ui/qml/TextTab.qml, src/vienetts_app/ui/qml/ParagraphTab.qml -->

- [x] Task 5: TextTab: PageHeader/PageShell, editor focus glow + metrics footer,
  emotion toolbar, AppButton action hierarchy, shortcuts (Ctrl+Return/Ctrl+E/Esc),
  export-success toast, styled progress/error.
- [x] Task 6: ParagraphTab: same scaffold; format chips; DropArea import; AppButton
  actions; keep pinned copy (`"Đoạn văn / Tệp"`, `"%1 ký tự"`, `"Nhập tệp…"`).

## Phase 5: Cloning & Settings studios
<!-- execution: sequential -->
<!-- files: src/vienetts_app/ui/qml/CloningTab.qml, src/vienetts_app/ui/qml/SettingsTab.qml -->

- [x] Task 7: CloningTab: step badges, drag-drop clip card, composed empty state,
  AppButton adoption; pinned consent/copy preserved.
- [x] Task 8: SettingsTab: unified rows, single engine note, theme-combo preview
  delegates, styled SpinBox; combo contracts intact.

## Phase 6: Verification & close-out
<!-- execution: sequential -->
<!-- files: conductor/tracks/ui_refine_20260828/* -->

- [x] Task 9: Re-run screenshot driver (all tabs × themes); visually verify no
  truncation/overlap; full gates (`ruff`, `pytest`); update learnings; close beads.
