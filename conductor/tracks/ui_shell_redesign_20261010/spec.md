# Spec: UI/UX shell redesign (ui_shell_redesign_20261010)

Source audit: Design canvas "VieNeuTTS UI/UX Audit"
(https://claude.ai/artifact/LACfJsyU8LZMdPdj7BcwAw, 2026-10-10) — verdict board,
annotated v0.1.14 screenshots, and four proposed screens (Tạo giọng đọc,
Sách nói, Studio, Cài đặt). A snapshot of every board lives in `design/`
next to this spec; `design/README.md` indexes the boards and maps mockup
colours to `Theme` tokens. The mockups set layout, hierarchy and copy; QML
implements them with existing components and tokens, not pixel copies.

## Overview

The Signal design system (slate + teal tokens, Be Vietnam Pro, the AppButton /
AppCard / VoicePicker component kit, accessibility work) stays. This track
fixes the problems the audit ranked as structural:

1. Navigation is split by input length (Văn bản vs Đoạn văn do the same job).
2. The primary action (Tạo âm thanh) moves between screens and sits at the fold
   on Văn bản.
3. The voice — the product's core object — is a form-field dropdown repeated on
   five screens.
4. Chrome (icon-tile page headers, card subtitles, dividers, per-row icon
   tiles) crowds out content; text goes down to 10 px; hit targets are 40 px.
5. Status is scattered over four surfaces (floating export-only pill, sidebar
   engine card that can read "…", status sentence under actions, header
   badges).
6. Studio is a vertical stack of cards; Sách nói nests a 360 px chapter scroll
   inside the page scroll; Cài đặt is one 2,420-line scroll with no
   sub-navigation.

Delivered in four phases, each shippable on its own.

## Functional Requirements

### FR-1 Design-system refactor (Phase 1)
- FR-1.1 Type floor: no rendered text below 12 px (`Theme.fontSizeXs` 10 → 12;
  audit every literal `pixelSize` < 12).
- FR-1.2 `Theme.controlHitTarget` 40 → 44; every icon-only button and nav item
  meets it.
- FR-1.3 `Theme.textSubtle` passes 4.5:1 against `bg`, `surface` and
  `surfaceCard` in both themes (dark is ~#64748b today and fails on small text).
- FR-1.4 `PageHeader` becomes a single compact row (title + optional trailing
  actions/counters); no icon tile, no subtitle line. `AppCard` drops the
  subtitle line and header divider by default. Properties stay for source
  compatibility (no-op/deprecated) so callers compile unchanged.
- FR-1.5 Button hierarchy: exactly one `primary` AppButton visible per screen
  state; disabled controls never look filled; the ambiguous "Studio…" label
  becomes "Mở trong Studio"; clearing editor text is undoable (Ctrl+Z restores)
  or confirmed.
- FR-1.6 Settings rows lose the decorative icon tile; Color mode becomes a
  segmented control (new `AppSegmented` component, also used later by modes
  and device pickers).

### FR-2 Shared shell (Phase 2)
- FR-2.1 `StatusBar` component pinned to the window bottom: model/engine
  readiness, engine note (backend · precision · mode), audio-output state with
  "Kiểm tra lại", update indicator. It replaces the floating `exportOnlyNotice`
  pill and the sidebar "Phần cứng & Engine" card. It never shows an empty or
  "…" readout — it falls back to the model state text.
- FR-2.2 `TransportDock` component (generalized from `SynthesisBar`): voice
  chip → opens voice selection; Tạo âm thanh primary with Ctrl+Enter; Dừng
  (Esc) replacing Tạo while busy (not a second always-visible button); Play;
  waveform + timecodes; Export split button (default format, menu: WAV/MP3,
  Lưu nhanh, Lưu thành…); "Mở trong Studio".
- FR-2.3 Văn bản adopts the dock (the "Giọng đọc & Điều khiển" card is
  removed); Đoạn văn's `SynthesisBar` is replaced by the same dock.
- FR-2.4 Sách nói's player adopts the dock skin: 44 px prev/next icon buttons
  around a larger play, "Tự chuyển chương" moves from the book header into the
  player.
- FR-2.5 The live-playback toggle appears in one place per screen (inspector
  in Phase 3; dock overflow until then) — it is already one global
  `controller.livePreview` setting, so this is de-duplication, not a behaviour
  change.

### FR-3 Information architecture (Phase 3)
- FR-3.1 `bridge.TABS` becomes `create`, `audiobook`, `voices`, `studio`,
  `settings`. Legacy ids stay accepted by `setCurrentTab`: `text` →
  create/compose, `paragraph` → create/document, `cloning` → voices/clone.
  A new `bridge.createMode` (`compose|document|files|subtitles`) and
  `bridge.voicesView` (`library|clone`) carry the sub-destination.
- FR-3.2 `CreateTab` = header (title, mode segmented control
  Soạn thảo | Tài liệu | Nhiều tệp | Phụ đề, counters, Nhập tệp…) + workspace
  (editor or the existing paragraph/batch/subtitle cards) + inspector + the
  shared TransportDock. Emotion tags render as chips in the compose toolbar.
- FR-3.3 Create inspector: current voice card (name, gender · region · style,
  Nghe thử, Đổi giọng…), up to 3 recent voices with audition, per-run
  Tốc độ / Ngắt giữa câu sliders and the Phát trực tiếp toggle. Collapses
  under the editor below 1000 px window width.
- FR-3.4 Recent voices: persisted `Settings.recent_voices` (max 3, most recent
  first, de-duplicated, filtered to voices valid for the active engine
  profile); updated on successful synthesis submit.
- FR-3.5 `VoicesTab` (Giọng đọc): preset library grouped by region
  (Bắc/Trung/Nam) with gender/style filters, search and audition (reusing
  VoicePicker's data + `auditionVoice`); cloned voices section; "Đặt làm mặc
  định" on any voice writes `default_voice`; "Tạo giọng mới" opens the existing
  cloning flow (CloningTab content, consent notice preserved) as the `clone`
  view.
- FR-3.6 Settings' default-voice picker is replaced by a read-only row linking
  to Giọng đọc.
- FR-3.7 Sidebar: five destinations, Cài đặt pinned to the bottom with the
  update dot; compact rail below 800 px keeps accessible names.

### FR-4 Screen layouts (Phase 4)
- FR-4.1 Sách nói master–detail: library column (book tiles with progress +
  drop target), chapter list filling the available height (no 360 px cap, no
  nested flickable), reader panel visible beside the list at ≥1200 px and
  behind the "Văn bản" toggle below that. Chapter row actions are labelled or
  have tooltips + accessible names; status chips ≥12 px.
- FR-4.2 Cài đặt sub-navigation: Chung · Giọng & nhịp đọc · Xuất tệp ·
  Engine & phần cứng · Mô hình · Cập nhật, plus a filter field that narrows rows
  by label. Engine section leads with a summary card (state, active
  backend/precision, why it was chosen, Tự động/CPU/GPU segmented); precision,
  managed CUDA runtime, Qwen device and diagnostics sit under "Nâng cao"
  disclosures. `SettingsTab.qml` is split into per-section files.
- FR-4.3 Studio: timeline (clips as blocks on one track, selection range,
  playhead) over a history chip row and a clip table; right-hand Hiệu ứng
  panel with grouped controls, a Gốc / Đã chỉnh A/B listen toggle and one
  "Áp dụng N thay đổi" commit (replacing per-row Áp dụng). Pending edits are
  previewed without being pushed onto the op stack until applied.
- FR-4.4 Responsive pass: every redesigned screen works at the 640×420 minimum
  (rail nav, stacked inspector/panels, wrapping toolbars, no horizontal
  scroll).

## Non-Functional Requirements
- NFR-1 Every tested `objectName` contract survives; where an element moves
  (e.g. `exportOnlyNotice`, `engineReadout`, `generateButton`,
  `livePreviewToggle`), the name moves with it. Smoke tests change only where
  navigation ids or deliberately removed elements require it.
- NFR-2 Dark and light themes both pass WCAG 2.1 AA for text (4.5:1, 3:1 at
  ≥24 px) and 3:1 for non-text UI.
- NFR-3 No regression of the perf-hardening work: async tab Loaders stay;
  landing tab first paint not slower than today; idle CPU unchanged (no
  always-running timers).
- NFR-4 All user-facing strings via `qsTr`, `scripts/update_i18n.sh` run on
  every string change, vi + en catalogs complete.
- NFR-5 Gates per task: `ruff check .`, `ruff format --check .`, `pytest`.

## Acceptance Criteria
- AC-1 No text below 12 px and no interactive target below 44 px in any tab
  (checked by a smoke scan of rendered items).
- AC-2 One status surface: no floating banner, no sidebar engine card; the
  status bar shows engine + audio state and the "Kiểm tra lại" action.
- AC-3 Văn bản/compose, Tài liệu, Nhiều tệp, Phụ đề and Sách nói all generate
  and export through the same TransportDock; Tạo âm thanh is visible without
  scrolling at 1120×740.
- AC-4 `setCurrentTab("text"|"paragraph"|"cloning")` lands on the right
  destination/mode; the sidebar shows five destinations.
- AC-5 Giọng đọc lists all 20 presets grouped by region with audition,
  sets the default voice, shows cloned voices and hosts the cloning flow.
- AC-6 Create inspector shows the current voice and up to 3 recent voices that
  persist across restarts.
- AC-7 Sách nói has no nested scroll region; Cài đặt has sub-nav + filter;
  Studio has timeline + effects panel with A/B and a single apply.
- AC-8 Screenshot sets (dark + light, 1120×740 + 640×420, every destination)
  are captured automatically with `scripts/generate_screenshots.py` at the end
  of each phase (artifact only, no manual approval gate); `docs/screenshots/`
  refreshed at the end.
- AC-9 Full gate green (with the known device-less `QAudioSink` smoke
  exception and the 7 linux-aarch64 Qwen/GGUF baseline failures).

## Out of Scope
- New synthesis features, engine/runtime changes, or model changes.
- Rich-text editing beyond rendering emotion tags as chips.
- Multi-track Studio (one track only), waveform-level editing beyond the
  existing ops.
- macOS/Windows native menu bar integration.
- A light-theme visual rework beyond contrast fixes.
