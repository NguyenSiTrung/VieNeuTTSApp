# Plan: UI/UX shell redesign (ui_shell_redesign_20261010)

Per-task workflow (conductor/workflow.md): read patterns.md + learnings.md →
failing test first → implement → `ruff check .`, `ruff format --check .`,
`pytest` → commit + `git notes add` → append learnings. String changes run
`scripts/update_i18n.sh`. QML assertions run in subprocess smoke drivers
(patterns: one QGuiApplication per process, shared-window scenario groups).

Design reference: `design/` in this track (snapshot of the audit canvas —
read `design/README.md` first for the board index and hex → Theme token map).
Each task names the board it implements.

Screenshots: `scripts/generate_screenshots.py` (extended in Task 1.6) captures
every destination × {dark, light} × {1120×740, 640×420} into
`docs/screenshots/redesign/phase-N/` at the end of each phase as an artifact —
no manual approval gate; phases close on the automated gate alone.

---

## Phase 1: Design-system refactor

Design: `design/Current-Text.dc.html`, `design/Current-Settings.dc.html` (problems), `design/Proposed-*.dc.html` (target density/type).

- [x] Task 1.1: Theme tokens — type floor, hit target, subtle-text contrast `25658c9`
  - [x] Test: smoke reads Theme → `fontSizeXs >= 12`, `controlHitTarget >= 44`;
        unit contrast check of `textSubtle` vs `bg`/`surface`/`surfaceCard`
        in both themes ≥ 4.5:1
  - [x] Update `Theme.qml` (and `ui/theme.py` mirror if present); grep and fix
        literal `pixelSize` < 12 across `ui/qml/`
- [x] Task 1.2: Rendered-size scan helper
  - [x] Test: smoke driver walks each tab's item tree and reports any visible
        Text with pixelSize < 12 or visible AbstractButton < 44 px tall/wide
        (initially xfail-listed offenders, list must shrink to empty by 1.5)
- [x] Task 1.3: Compact `PageHeader` and quiet `AppCard`
  - [x] Test: header height ≤ 56 px; `subtitle` no longer rendered; existing
        header objectNames still resolve
  - [x] Single-row PageHeader (title + trailing); AppCard drops subtitle line
        and header divider by default (properties kept as no-ops)
- [x] Task 1.4: `AppSegmented` component + Settings rows
  - [x] Test: segmented control exposes `currentValue`, keyboard arrows move
        selection, accessible names per segment; Color mode drives
        `bridge` theme preference through it
  - [x] Remove decorative icon tiles from Settings rows
- [x] Task 1.5: Button hierarchy and copy
  - [x] Test: per tab, at most one visible `variant: "primary"` AppButton in
        idle state; disabled button background uses `controlDisabledBg`;
        `studioButton` text = "Mở trong Studio"; clear-text is undoable
  - [x] Apply across Text/Paragraph/Audiobook/Studio/Cloning; run
        `update_i18n.sh`
- [ ] Task 1.6: Screenshot harness
  - [ ] Extend `scripts/generate_screenshots.py` (grabWindow, fake
        controller) for destination × theme × size matrix; smoke test that it
        runs offscreen and writes the expected file set

## Phase 2: Shared shell — status bar and transport dock

Design: `design/IA.dc.html` (shell anatomy), dock + status bar in `design/Proposed-Create.dc.html` and `design/Proposed-Audiobook.dc.html`.

- [ ] Task 2.1: `StatusBar` component
  - [ ] Test: shows model state text, engine note, audio warning +
        `audioRefreshButton` when `!controller.audioAvailable`, update
        indicator; never renders empty/"…" readout; `exportOnlyNotice` and
        `engineReadout` objectNames now live inside it
  - [ ] Wire into `Main.qml`; remove floating pill and sidebar engine card
- [ ] Task 2.2: `TransportDock` component
  - [ ] Test: voice chip, Generate (Ctrl+Enter) swaps to Stop (Esc) while
        busy, Play, waveform, Export split menu (format, Lưu nhanh, Lưu
        thành…), Mở trong Studio; carries `SynthesisBar`'s objectNames
  - [ ] Generalize from `SynthesisBar.qml` (keep it as a thin wrapper until
        Phase 3 removes the last caller)
- [ ] Task 2.3: Văn bản adopts the dock
  - [ ] Test: `generateButton` visible without scrolling at 1120×740; the
        "Giọng đọc & Điều khiển" card is gone; existing Text-tab flows in
        `test_ui_tabs.py` / `test_e2e_flows.py` stay green
- [ ] Task 2.4: Đoạn văn and Sách nói adopt the dock skin
  - [ ] Test: paragraph flows unchanged via dock; audiobook prev/next are
        44 px icon buttons with accessible names; `Tự chuyển chương` toggle
        moved into the player and still bound to the same controller flag
- [ ] Task 2.5: Single live-playback toggle per screen
  - [ ] Test: exactly one visible `livePreviewToggle` per tab, bound to
        `controller.livePreview`

## Phase 3: Information architecture — Tạo giọng đọc and Giọng đọc

Design: `design/IA.dc.html` (mapping), `design/Proposed-Create.dc.html` (mode switch, editor, inspector). No Voices board exists — follow the Create inspector's voice card + recent list styling.

- [ ] Task 3.1: Bridge navigation model
  - [ ] Test (`test_bridge.py`): TABS = create/audiobook/voices/studio/
        settings; `setCurrentTab("text")` → create + `createMode == "compose"`;
        `"paragraph"` → create + `document`; `"cloning"` → voices +
        `voicesView == "clone"`; unknown ids still ignored; labels retranslate
  - [ ] Implement `TAB_ALIASES`, `createMode`, `voicesView` with NOTIFY
- [ ] Task 3.2: Recent voices persistence
  - [ ] Test (`test_settings` / `test_controller`): `recent_voices` max 3,
        MRU order, de-dup, filtered by active engine profile, survives
        reload, updated on successful submit only
  - [ ] Add to `Settings`, expose `controller.recentVoices`
- [ ] Task 3.3: `CreateTab` shell
  - [ ] Test: mode segmented control switches compose/document/files/
        subtitles and syncs `bridge.createMode`; each mode keeps its existing
        objectNames (editor, import, batch queue, subtitle card); one dock
  - [ ] Compose TextTab/ParagraphTab content into CreateTab; emotion chips in
        compose toolbar
- [ ] Task 3.4: Create inspector
  - [ ] Test: voice card shows name + "gender · region · style"; audition calls
        `auditionVoice`; recent list renders `controller.recentVoices`;
        speed/pause sliders write the existing settings; collapses under the
        editor below 1000 px
- [ ] Task 3.5: `VoicesTab` library
  - [ ] Test: 20 presets grouped Bắc/Trung/Nam, filter + search narrow the
        list, audition per row, "Đặt làm mặc định" writes `default_voice`,
        cloned voices listed, engine-profile capability respected (Qwen
        profiles show their own voice source)
- [ ] Task 3.6: Cloning moves into Giọng đọc
  - [ ] Test: `voicesView == "clone"` shows the cloning flow with consent
        notice and all existing cloning objectNames; Settings default-voice
        row becomes a link to Giọng đọc
- [ ] Task 3.7: Sidebar and smoke migration
  - [ ] Test: five nav items, Cài đặt pinned bottom with update dot, compact
        rail accessible names; update smoke navigation to new ids where
        tests navigate by id (legacy ids still covered by alias tests)
  - [ ] Delete `SynthesisBar` wrapper if unused; run `update_i18n.sh`

## Phase 4: Screen layouts — Sách nói, Cài đặt, Studio
<!-- execution: parallel -->

Design: `design/Proposed-Audiobook.dc.html` (4.1), `design/Proposed-Settings.dc.html` (4.2), `design/Proposed-Studio.dc.html` (4.3–4.4).

Parallel streams: 4.1 Sách nói · 4.2 Cài đặt · 4.3→4.4 Studio. Shared files
(`tests/smoke/test_ui_tabs.py`, `ui/i18n/*.ts`) are edited by all three — the
orchestrator serializes commits and runs `update_i18n.sh` + the full gate
before each (patterns: index.lock + shared-copy ping-pong).

- [ ] Task 4.1: Sách nói master–detail
  <!-- files: src/vienetts_app/ui/qml/AudiobookTab.qml -->
  - [ ] Test: no Flickable/ListView nested inside the page scroll (chapter list
        fills height); reader panel visible at ≥1200 px, toggle below;
        chapter row actions have accessible names; all audiobook objectNames
        resolve
- [ ] Task 4.2: Cài đặt sub-navigation and split
  <!-- files: src/vienetts_app/ui/qml/SettingsTab.qml, src/vienetts_app/ui/qml/settings/ -->
  - [ ] Test: six sections selectable; filter narrows rows by label; engine
        summary card shows state/backend/precision/reason; advanced
        disclosures collapsed by default (CUDA auto-expand rules from v0.1.14
        preserved); all 21 settings smoke objectNames resolve
  - [ ] Split `SettingsTab.qml` into `settings/*.qml` section files
- [ ] Task 4.3: Studio timeline + effects panel (controller seam)
  <!-- files: src/vienetts_app/core/studio.py, src/vienetts_app/ui/controller.py (studio slots only), tests/unit/test_studio.py -->
  - [ ] Test (`test_studio.py` / controller): pending-ops preview renders
        without mutating the op stack; "apply N" pushes them as one history
        step; A/B toggle switches playback between base and pending render
- [ ] Task 4.4: Studio timeline + effects panel (QML)
  <!-- files: src/vienetts_app/ui/qml/StudioTab.qml, src/vienetts_app/ui/qml/components/Studio*.qml -->
  <!-- depends: task3 -->
  - [ ] Test: clips render as proportional blocks; selection range and
        playhead; history chips with undo/reset; single apply button with
        count; existing Studio objectNames resolve
- [ ] Task 4.5: Responsive pass at 640×420
  <!-- depends: task1, task2, task4 -->
  - [ ] Test: size scan from 1.2 passes at 640×420 for every destination; no
        horizontal overflow (content width ≤ window width)
- [ ] Task 4.6: Docs
  <!-- depends: task5 -->
  - [ ] Refresh `docs/screenshots/*.png`, update `conductor/product.md`
        feature list for the new IA
