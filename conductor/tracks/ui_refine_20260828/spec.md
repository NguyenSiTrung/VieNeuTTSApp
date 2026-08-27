# Specification: UI Refinement Pass 2 — "Signal" Design System (`ui_refine_20260828`)

## 1. Overview
Second-pass UI/UX refinement of the already-redesigned VieNeuTTS shell, driven by a
code + visual audit (real screenshots of all 4 tabs × 2 themes × busy/consent states).
Goal: move from "consistent but generic" to "distinctive and finished" while keeping
100% of the smoke-test objectName/copy contract green.

Audit headline findings this track fixes:
- Dead component library: `AppButton`/`StatusBadge` registered but never used; ~30
  copy-pasted inline button blocks cause inconsistent hover/disabled/focus states.
- Latent bug: `Theme.fontWeightRegular` referenced in TextTab/ParagraphTab but never
  defined in `Theme.qml` (silent `undefined`).
- ~270 lines of duplicated QML: voice-picker ComboBox (3×), action-button rows (2×),
  `buildFlatModel`/`toLocalPath`/`countWords` helpers (2–4×).
- Mixed-language shell: nav labels are English ("Text", "Paragraph"…) in a
  Vietnamese-first app; inconsistent per-tab headers (Text/Paragraph lack the icon
  treatment Cloning/Settings have; Paragraph is full-width while others are 840px
  centered); no typographic identity (`fontFamily: ""`, no tracking on the all-caps
  section label).
- Indigo/violet accent reads as the generic "AI app" fingerprint; no depth anywhere
  (`shadowColor` token defined but unused — light theme is flat/washed out).
- UX warts: emotion-tag toolbar shows redundant raw tag text; document card looks
  like a dropzone but accepts no drops; cloned-voices empty state is a bare italic
  line; no keyboard shortcuts; play-before-export is discoverable only via tooltip.

## 2. Functional Requirements

### 2.1 Design tokens (`Theme.qml`)
- **FR-R1.1**: Accent shifts from indigo to a signal-teal scale (distinctive for an
  audio tool, AA-contrast in both themes); `success` shifted green-ward to keep
  separation from the accent; all other token names unchanged.
- **FR-R1.2**: Bundle **Be Vietnam Pro** (OFL — designed for Vietnamese diacritics)
  weights 400/500/600/700 under `ui/fonts/` and load via `FontLoader`; `fontFamily`
  falls back to the system stack when unloaded.
- **FR-R1.3**: Define `fontWeightRegular` (alias of Normal), letter-spacing tokens
  (`trackingWide`), and focus-ring tokens; keep every existing token name.
- **FR-R1.4**: Zero Python-side theme-resolution behavior change (tests/unit/test_theme.py
  untouched and green).

### 2.2 Component library upgrades (`components/`)
- **FR-R2.1 (`AppButton`)**: variants `primary|secondary|ghost|danger`, sizes
  `sm|md|lg`, visible keyboard-focus ring, tactile press; replaces ALL inline
  copy-pasted button blocks in the four tabs.
- **FR-R2.2 (`AppCard`)**: soft elevation shadow (light+dark), subtitle wraps instead
  of eliding, tightened header rhythm; optional `elevation` (0|1).
- **FR-R2.3 (`StatusBadge`)**: adopted for real (sidebar engine card, catalog count,
  format chips) instead of lying unused.
- **FR-R2.4 (`EmotionChip`)**: drop vestigial `emoji`/`icon` props, keep both signals;
  add tooltip with the raw tag.
- **FR-R2.5 (`VoicePicker`, NEW)**: single shared picker (flat model with `▸`/`—`
  prefixes, `selectedVoice`, default-voice preselection, custom delegate) replacing
  the 3 copies; keeps objectName `voicePicker` + `flatModel` property contract.
- **FR-R2.6 (`PageHeader`, NEW)**: icon + title + subtitle + trailing slot; used by
  all 4 tabs for consistent page scaffolding.
- **FR-R2.7 (`PageShell`, NEW)**: ScrollView + centered content column with per-tab
  max width, shared by all tabs.
- **FR-R2.8 (`SectionLabel`, NEW)**: tracked uppercase micro-label.

### 2.3 Shell & navigation (`Main.qml`, `ui/bridge.py`)
- **FR-R3.1**: Nav labels localized to Vietnamese ("Văn bản", "Đoạn văn", "Sao chép
  giọng", "Cài đặt") — tab IDs unchanged; `tests/unit/test_bridge.py` label pins
  updated to match.
- **FR-R3.2**: Sidebar: refined brand tile, tracked section label, footer engine
  card uses `StatusBadge`; hover/active states preserved.
- **FR-R3.3**: Export-only notice and models-missing overlay restyled (icons,
  spacing); objectNames and behavior identical.

### 2.4 Text & Paragraph studios
- **FR-R4.1**: Consistent `PageHeader` + `PageShell` scaffolding; TextTab subtitle
  wraps (no truncation).
- **FR-R4.2**: Editor: focus glow, larger min height, metrics as a right-aligned
  footer row.
- **FR-R4.3**: Actions built from `AppButton` with clear hierarchy (primary CTA /
  secondary play+export / ghost quick-save) and tooltips that surface shortcuts.
- **FR-R4.4**: Keyboard shortcuts: `Ctrl+Return` generate, `Ctrl+E` quick export,
  `Escape` cancel (additive; no existing binding changes).
- **FR-R4.5**: Emotion toolbar: chips + concise hint preserving a `[cười]`-containing
  label (test pin).
- **FR-R4.6**: ParagraphTab: format chips row (`.txt .md .docx .pdf`), a real
  `DropArea` on the editor card that routes dropped files through `importPath()`.
- **FR-R4.7**: Waveform: rounded gradient bars, refined baseline; numeric contract
  (`level`/`active`/`historyCount`) and `visible: controller.streamActive` binding
  unchanged (test-pinned).
- **FR-R4.8**: Toast additionally fires on successful export ("Đã xuất WAV …");
  cancel toast text/behavior unchanged (test pin).

### 2.5 Cloning studio
- **FR-R5.1**: Numbered step badges (1/2/3) replacing "1." text prefixes.
- **FR-R5.2**: Reference-clip card accepts drag-and-drop audio files routed through
  `selectClip()`; guidance copy with "3–8 giây" preserved (test pin).
- **FR-R5.3**: Composed empty state for the cloned-voice catalog (icon + guidance);
  catalog rows styled with consistent `AppButton` ghost delete.
- **FR-R5.4**: All pinned copy preserved verbatim: consent phrases, "Tôi đồng ý",
  "Chưa chọn tệp", "Chọn tệp…", "Tạo giọng nói", "Xóa", "Sao chép giọng nói".

### 2.6 Settings studio
- **FR-R6.1**: Unified row layout (label+desc left, control right); combos keep
  objectNames, `activated` handlers, and popup functions (test-pinned).
- **FR-R6.2**: Engine note shown once on the tab (detected box); card badge no longer
  duplicates it.
- **FR-R6.3**: Theme combo delegates gain preview dots; `themeCombo` stays a ComboBox
  (tests drive `activated`).
- **FR-R6.4**: Temperature SpinBox visually styled; `displayText` formatting
  ("1.20") untouched.

## 3. Non-Functional Requirements & Invariants
- **NFR-1**: Full pytest suite green (481+ tests); the ONLY test edits allowed are
  the `test_bridge.py` tab-label pins (intentional copy change, FR-R3.1).
- **NFR-2**: Every objectName from the previous track's NFR-2 list preserved.
- **NFR-3**: QML seams (`toLocalPath`, `importPath`, `selectClip`, `setOutputDir`,
  `buildFlatModel`) remain root-function invocable via QMetaObject.
- **NFR-4**: Startup stays model-free/instant; fonts add ≤ ~1.2 MB to the wheel.
- **NFR-5**: No new Python dependencies; fonts are static assets inside the package.

## 4. Acceptance Criteria
- [ ] Screenshots (all 4 tabs × dark/light) show consistent headers, teal accent,
      card elevation, and no truncated/overlapping text.
- [ ] `grep` shows zero inline `background: Rectangle` button blocks in the four
      tabs (all buttons are `AppButton`), and `StatusBadge` used ≥ 3 places.
- [ ] `Theme.fontWeightRegular` defined; no undefined-token references remain.
- [ ] Voice picker exists once as `components/VoicePicker.qml`; 3 call sites.
- [ ] `ruff check .`, `ruff format --check .`, `pytest` all green.

## 5. Out of Scope
- Changing play-after-generation flow (export-first is test-pinned; controller
  behavior unchanged).
- Replacing the theme/voice ComboBoxes with non-ComboBox controls (test contract).
- Frameless/custom title bars, new Python features, packaging/signing work.
