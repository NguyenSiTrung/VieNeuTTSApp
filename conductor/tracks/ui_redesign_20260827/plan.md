# Implementation Plan: UI/UX Redesign & Modern Desktop Refactor (`ui_redesign_20260827`)

## Phase 1: Design Tokens & Reusable Component Library
<!-- execution: sequential -->

- [x] Task 1.1: Refactor `Theme.qml` to support dynamic Dark and Light themes
  - Bind color tokens (`bg`, `surface`, `surfaceAlt`, `surfaceHover`, `border`, `text`, `textMuted`, `accent`, `accentHover`, `accentSubtle`, `success`, `warning`, `error`) to `bridge.effectiveTheme`
  - Ensure high-contrast, WCAG AA compliance in both Dark (Obsidian) and Light (Porcelain) modes
- [x] Task 1.2: Build Reusable QML Component Primitives in `src/vienetts_app/ui/qml/components/`
  - Create `AppCard.qml` with customizable title, header action, border, and elevation
  - Create `AppButton.qml` supporting primary/secondary/ghost/danger variants with icons, hover and pressed transitions
  - Create `EmotionChip.qml` with smooth hover animations and click handlers for inserting tags
  - Create `StatusBadge.qml` for hardware and voice status indicators
- [x] Task 1.3: Update `qmldir` to export new component primitives and verify theme integration
  - Update `src/vienetts_app/ui/qml/qmldir`
  - Run unit and smoke tests to ensure zero regressions
- [x] Task: Conductor - User Manual Verification 'Phase 1: Design Tokens & Component Library'

---

## Phase 2: Shell & Navigation Rail Redesign (`Main.qml`)
<!-- execution: sequential -->

- [x] Task 2.1: Redesign Navigation Sidebar in `Main.qml`
  - Add stylized Brand Header with micro-waveform badge and version tag
  - Upgrade tab buttons with active pill indicator, crisp SVG-style geometric icons, and subtle hover animations
  - Convert `engineReadout` into a compact, formatted hardware status card at the bottom of the sidebar
- [x] Task 2.2: Polish Overlays (`modelsMissingOverlay` & `exportOnlyNotice`)
  - Redesign `modelsMissingOverlay` with modal card styling, clear typography, and a copy-command button
  - Polish `exportOnlyNotice` as an elegant amber status strip with refresh button
- [x] Task 2.3: Verify Shell Smoke Tests
  - Run `pytest tests/smoke/test_ui_shell.py` and confirm all 10 smoke tests pass
- [x] Task: Conductor - User Manual Verification 'Phase 2: Shell & Navigation Rail'

---

## Phase 3: Synthesis Studios Refactor (`TextTab.qml` & `ParagraphTab.qml`)
<!-- execution: sequential -->

- [ ] Task 3.1: Upgrade `WaveformIndicator.qml`
  - Support both live streaming animation and retention of the completed waveform curve for replay
- [ ] Task 3.2: Redesign `TextTab.qml`
  - Wrap editor in `AppCard` with live word, character, and estimated duration metrics
  - Add interactive `EmotionChip` toolbar for one-click emotion insertion (`[cười]`, `[thở dài]`, `[hắng giọng]`, `[ngập ngừng]`, `[thì thầm]`) + clear action
  - Upgrade voice picker styling with group labels and visual hierarchy
  - Upgrade action bar: Primary Accent "Tạo âm thanh" CTA, inline progress indicator, and streamlined Play/Export buttons
- [ ] Task 3.3: Redesign `ParagraphTab.qml`
  - Create styled Document Ingestion card with drag-and-drop / file format info (`.txt`, `.md`, `.docx`, `.pdf`)
  - Add segment progress readout (`Đang xử lý đoạn 3/12...`) alongside progress bar
  - Polish error and import status banners
- [ ] Task 3.4: Verify Synthesis Tabs Smoke Tests
  - Run `pytest tests/smoke/test_ui_tabs.py -k "Text or Para"` and confirm all tests pass
- [ ] Task: Conductor - User Manual Verification 'Phase 3: Synthesis Studios'

---

## Phase 4: Voice Cloning & Settings Studios (`CloningTab.qml` & `SettingsTab.qml`)
<!-- execution: sequential -->

- [ ] Task 4.1: Redesign `CloningTab.qml`
  - Redesign `consentPanel` as an on-device privacy & ethics trust card with security badge and clean affirmative CTA
  - Upgrade reference audio clip inspector with Denoise preview button and audio duration guidelines
  - Upgrade cloned voices catalog (`clonedVoiceList`) to rich voice cards with avatars and delete actions
- [ ] Task 4.2: Redesign `SettingsTab.qml`
  - Group settings into 4 clean cards: *Engine & Hardware*, *Voice & Audio*, *Storage & Output*, *Appearance*
  - Add visual temperature slider with qualitative expression guide (0.1–0.3 Tin tức / 0.4–0.7 Tự nhiên / 0.8+ Biểu cảm)
  - Add theme selection cards for System / Light / Dark
- [ ] Task 4.3: Verify Cloning & Settings Smoke Tests
  - Run `pytest tests/smoke/test_ui_tabs.py -k "Clon or Settings"` and confirm all tests pass
- [ ] Task: Conductor - User Manual Verification 'Phase 4: Cloning & Settings Studios'

---

## Phase 5: End-to-End Quality Gates & Visual Polish
<!-- execution: sequential -->

- [ ] Task 5.1: Run Full Quality Gate Suite
  - Run `.venv/bin/ruff check .`
  - Run `.venv/bin/ruff format --check .`
  - Run `.venv/bin/pytest` (all 479+ tests must pass green)
- [ ] Task 5.2: Final Visual & Theme Inspection
  - Verify seamless switching between Dark and Light modes
  - Verify responsiveness across various window sizes (min 640x420 up to 1440x900+)
- [ ] Task: Conductor - User Manual Verification 'Phase 5: Quality Gates & Final Delivery'
