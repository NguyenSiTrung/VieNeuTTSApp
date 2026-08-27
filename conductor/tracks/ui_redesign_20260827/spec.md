# Specification: UI/UX Redesign & Modern Desktop Refactor (`ui_redesign_20260827`)

## 1. Overview
Transform VieNeuTTS into a modern, visually stunning on-device AI Audio Workstation desktop application (inspired by Descript, Linear, and macOS native audio apps), featuring a dynamic Light/Dark design system, modular reusable QML components, and refined workflows for text-to-speech, document reading, voice cloning, and engine configuration.

---

## 2. Functional Requirements

### 2.1. Dynamic Light/Dark Theme System (`Theme.qml`)
- **FR-UX-1.1**: `Theme.qml` must dynamically compute color tokens based on `bridge.effectiveTheme` ("dark" vs "light") rather than hardcoding static values.
- **FR-UX-1.2**: Tokens must include rich gradients, subtle card borders, surface elevations (`surface`, `surfaceAlt`, `surfaceHover`), high-contrast text (`text`, `textMuted`), accent colors (`accent`, `accentHover`, `accentSubtle`), and status colors (`success`, `warning`, `error`).
- **FR-UX-1.3**: Zero regression for theme switching signals, persistence in `settings.json`, and automatic OS palette following.

### 2.2. Reusable Component Library (`src/vienetts_app/ui/qml/components/`)
- **FR-UX-2.1 (`AppCard.qml`)**: Card container with subtle hairline border (`Theme.border`), adaptive surface background, rounded corners (`radius: 10`), and optional header/badge slot.
- **FR-UX-2.2 (`AppButton.qml`)**: Modern interactive button with variants (`primary`, `secondary`, `ghost`, `danger`), icon support, tactile pressed feedback (`scale: 0.98`), and smooth hover transitions.
- **FR-UX-2.3 (`EmotionChip.qml`)**: Interactive clickable chip for quick insertion of speech emotion tags (`[cười]`, `[thở dài]`, `[hắng giọng]`, `[ngập ngừng]`, `[thì thầm]`) directly into text editors.
- **FR-UX-2.4 (`StatusBadge.qml`)**: Compact status pill with dot indicator (e.g. `🟢 Sẵn sàng`, `⚡ ONNX Int8`, `📦 Sẵn sàng`).

### 2.3. Shell & Navigation Rail (`Main.qml`)
- **FR-UX-3.1**: Brand header with styled icon, gradient accent, and `v3 Turbo` badge.
- **FR-UX-3.2**: Navigation rail with active pill indicator, icon + label layout for all 4 tabs, and subtle hover animations.
- **FR-UX-3.3**: Engine readout formatted as a sleek hardware status card at the bottom of the sidebar.
- **FR-UX-3.4**: Models-missing overlay (`modelsMissingOverlay`) redesigned with copy-command button and refined modal card layout.
- **FR-UX-3.5**: Export-only banner (`exportOnlyNotice`) updated to a clean amber status bar.

### 2.4. Text-to-Speech Studio (`TextTab.qml`)
- **FR-UX-4.1**: Integrated text editor inside `AppCard` with live word count, character count, and estimated reading time (`~15s`).
- **FR-UX-4.2**: Quick emotion tags toolbar (`EmotionChip` bar) with one-click insertion and clear text action.
- **FR-UX-4.3**: Polished Voice Selector with regional/gender visual indicators.
- **FR-UX-4.4**: Action bar with Primary Accent CTA ("Tạo âm thanh"), smooth progress indicator, and streamlined Play/Export controls.
- **FR-UX-4.5**: Audio Waveform visualizer that operates during live streaming and remains visible for playback replay.

### 2.5. Long Text & Document Tab (`ParagraphTab.qml`)
- **FR-UX-5.1**: Document Ingestion card with dropzone styling, supported formats banner (`.txt`, `.md`, `.docx`, `.pdf`), and document metadata chip.
- **FR-UX-5.2**: Visual chunk/segment progress indicator for long document synthesis.

### 2.6. Voice Cloning Studio (`CloningTab.qml`)
- **FR-UX-6.1**: Redesigned Privacy & Ethics Trust Card (`consentPanel`) emphasizing 100% on-device privacy guarantee and legal consent.
- **FR-UX-6.2**: Reference clip inspector with Denoise preview button and duration guidance.
- **FR-UX-6.3**: Cloned voices catalog rendered as cards with avatar, metadata, and voice deletion action.

### 2.7. Settings Studio (`SettingsTab.qml`)
- **FR-UX-7.1**: Organized into 4 distinct setting cards: *Động cơ & Phần cứng*, *Giọng đọc & m thanh*, *Lưu trữ & Xuất tệp*, *Giao diện & Chủ đề*.
- **FR-UX-7.2**: Temperature slider/spinbox with qualitative expression guidance labels (Ổn định ⟷ Sống động).
- **FR-UX-7.3**: Visual theme selector cards for System / Light / Dark modes.

---

## 3. Non-Functional Requirements & Invariants

- **NFR-1 (Zero Test Regression)**: All 479 existing unit and smoke tests must continue to pass 100%.
- **NFR-2 (Strict ObjectName Preservation)**: Every `objectName` referenced in tests (`textEditor`, `voicePicker`, `generateButton`, `playButton`, `exportButton`, `quickExportButton`, `progressBar`, `busyLabel`, `cancelButton`, `errorLabel`, `toastLabel`, `waveformIndicator`, `importDialog`, `importButton`, `charCountLabel`, `paragraphEditor`, `errorBanner`, `clipDialog`, `consentPanel`, `consentText`, `consentAcceptButton`, `clonePanel`, `clipPathLabel`, `clipBrowseButton`, `denoiseCheck`, `denoiseButton`, `previewPlayButton`, `voiceNameField`, `cloneButton`, `clonedVoiceList`, `clonedVoiceName`, `cloneRemoveButton`, `cloneBusyLabel`, `backendCombo`, `detectedEngineLabel`, `precisionCombo`, `needsRestartBanner`, `defaultVoiceCombo`, `outputDirLabel`, `outputDirBrowseButton`, `outputDirDialog`, `temperatureSpin`, `themeCombo`, `mainWindow`, `navBar`, `engineReadout`, `tabStack`, `exportOnlyNotice`, `audioRefreshButton`, `modelsMissingOverlay`, `modelsMissingCommand`, `modelsRetryButton`) must be preserved identically.
- **NFR-3 (Seams & Properties Preservation)**: Public QML methods (`toLocalPath`, `importPath`, `selectClip`, `setOutputDir`, `buildFlatModel`) and property bindings must remain fully functional.
- **NFR-4 (Performance)**: UI startup remains instantaneous and model-free (NFR-2.1/NFR-3.1); animations run smoothly at 60 FPS GPU-accelerated.

---

## 4. Acceptance Criteria
- [ ] `Theme.qml` delivers fully styled, high-contrast themes for both Dark and Light modes when `bridge.effectiveTheme` switches.
- [ ] Components directory `src/vienetts_app/ui/qml/components/` is established with `AppCard`, `AppButton`, `EmotionChip`, `StatusBadge` and registered in `qmldir`.
- [ ] Shell (`Main.qml`) features a polished sidebar with brand icon, active navigation indicators, and engine status chip.
- [ ] `TextTab.qml` features clickable EmotionChips, text metrics (words/chars/duration), hero primary CTA, and persistent waveform visualizer.
- [ ] `ParagraphTab.qml` features document ingestion banner and segment progress display.
- [ ] `CloningTab.qml` features privacy trust card, reference clip preview, and cloned voice cards.
- [ ] `SettingsTab.qml` is organized into clean functional cards with temperature guidance.
- [ ] Full quality gates pass (`ruff check .`, `ruff format --check .`, `pytest`).

---

## 5. Out of Scope
- Backend model changes or new TTS model weights.
- Network-based features or cloud telemetry (the app remains 100% offline & on-device).
