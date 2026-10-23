# Track Learnings: subtitles_ux_overhaul_20261023

Patterns, gotchas, and context discovered during implementation.

## Codebase Patterns (Inherited)

📚 **194 patterns** available in `conductor/patterns.md`. The ones that bear directly on this track:

- **Tall-tab layout rule** — a single stacked content column past the viewport buries the primary CTA behind scroll (ParagraphTab was 948 px tall against a 652 px viewport → CTA 208 px below the fold). Split mutually exclusive workflows into segmented modes and move global actions into a docked bar. **This track measures the same failure at 702 px below the fold — 3.4× the magnitude the rule was written for.**
- **`conductor/patterns.md:96` records the absence of a SubtitlesTab dock as deliberate.** This track reverses that decision; the pattern line must be updated in the same commit as the dock (Task 2.2) or the next agent will reinstate the defect.
- **One skin per control** — every button goes through `AppButton`, every combo through `AppCombo`, voice lists through `VoicePicker`; `AppIcon` is the only icon source. `SubtitleCard.qml` currently violates this with 3 hand-rolled panels, 3 inline `TextField` skins, 3 hand-rolled badges and 2 unstyled `ProgressBar`s.
- **Text-pin rule for custom control skins** — a custom `contentItem` must MIRROR `control.text`; the smoke suites read the control's own `text` property. Applies to the new `AppTextField` (Task 4.3).
- **QML `var` property signals do NOT fire on in-place mutation** — reassign a fresh array per update.
- **QML Property API traps** — `TextArea` has `lineSpacing` not `lineHeight`; `RowLayout` has no padding properties; from inside `components/` import only `".."` for Theme.
- **A QML singleton must be declared in BOTH `qmldir`s** (root with a relative path, plus `components/`). Applies to any new singleton; new *components* need registration in both as well.
- **Row-level list models** — `subtitleCueList` already uses one; keep delegates stable when adding the scrollbar and stage panels.
- **Hidden StackLayout tabs have UNSETTLED bindings** — `property("visible")` reads stale values until the tab becomes current; activate the tab + `processEvents` before asserting UI state in drivers.
- **QML teardown must be DETERMINISTIC** — call `_teardown_qml()` after `app.exec()` while the context-property anchors are still alive.
- **`setContextProperty` does NOT take ownership** — anchor the object (`engine._bridge = bridge`) or GC collects it.
- **Shared-subprocess QML smoke drivers** — one `QGuiApplication` per process; batch scenarios; print one `RESULT:<json>` line; merge N launches into one to save ~1 s each.
- **Text-pin / objectName contract** — the smoke suites pin `objectName`s; renaming is a breaking change (see NFR-1).
- **Loaded-host flake family** — a full `-n auto` run on a busy machine fails 2–4 varying tests that pass in isolation; re-run a suspect test alone before calling it a regression.
- **The format gate is `ruff format --check`**, not just `ruff check`.
- **`scripts/update_i18n.sh`** must run whenever user-facing strings change (Task 4.7).

## Audit Context (2026-10-23)

Full evidence: [`docs/ui-audit/SUBTITLES_TAB_UX_AUDIT.md`](../../../docs/ui-audit/SUBTITLES_TAB_UX_AUDIT.md). Six rendered screenshots live alongside it under `docs/ui-audit/`.

Key measured facts to re-verify rather than assume:

| Fact | Value | How measured |
|---|---|---|
| Primary CTA below fold at 640×420 | 702 px | offscreen QML tree probe |
| Page content vs viewport at 640×420 | 731 px vs 202 px (3.6×) | same |
| Card width at 760 / 820 px window | 664 / 556 px (non-monotonic) | same; nav rail flips at 800 px |
| Two-column layout first engages | ~1030 px window | same |
| Speaker voice field width at 640×420 | 0.0 / 3.0 px | same |
| `Theme.warning` as text, light theme | **3.19:1** (fails AA) | computed WCAG ratio |
| Pinned objectNames / total | 26 / 63 | grep across `tests/` |
| Controls in the card | 41 (39 unique + per-row templates) | source count |
| `disabledReason` coverage | 2 of 41 | grep |

## Track Decisions

1. **Scope:** all 4 tiers (P0 → P3), ~2.5–3 weeks.
2. **Cue-edit decoupling:** confirm-before-destroy + a stale-render indicator. **Not** incremental per-cue re-render (deferred; see spec Out of Scope).
3. **Dock:** add the pinned action dock and reverse `patterns.md:96`.

---

<!-- Learnings from implementation will be appended below -->

## [2026-10-23] - Phase 1: Data safety (Tasks 1.1–1.6)

- **Implemented:** A staged `_PendingInvalidation` gate on the controller (`renderInvalidationRequested` + confirm/reject) now protects every live render from the six policy knobs and from cue edits/ASR/diarization/LLM re-runs. `renderStale`/`renderStaleReason`/`canExportTrack`/`canExportSrt`/`exportBlockedReason` make the loss visible and truthful; `lastStats` survives an invalidation. Project lifecycle (`projects`, `openProject`, `removeProject`, header "Dự án mới"/picker) closes and reopens workspaces. `cancelExport()` terminates ffmpeg through a new `cancel_check` in `video_dub._run_ffmpeg`. Transcript mode uses its real 1500 ms gap default (explicit 0 stays uncapped) and new projects default to `video_mix_mode="duck"`.
- **Files changed:** `ui/subtitle_controller.py`, `core/subtitle_project.py`, `core/video_dub.py`, `ui/qml/SubtitlesTab.qml`, `ui/qml/components/SubtitleCard.qml`, `ui/i18n/vienetts_en.ts/.qm`, `tests/unit/test_subtitle_controller.py`, `tests/unit/test_video_dub.py`, `tests/smoke/test_ui_tabs.py`.
- **Learnings:**
  - **Patterns:** A destructive-change gate is a *staged command* (`reason`, `message`, `apply`, `revert_signals`) held on the owner; property getters keep returning the old value until `confirm`, so a rejected change needs only the property's notify signal to snap controls back. Existing tests that mutated a rendered project must call `confirmRenderInvalidation()`.
  - **Gotchas:** `_update_project_cues` zeroed `stats` *before* `_rebuild_for_policy` could capture them — capture the last stats at the start of every mutating path, not inside the rebuild. `exportBlockedReason` is the single source for both the button gate and its tooltip; do not reintroduce a bare "Hãy tạo trước" message.
  - **Gotchas:** `Dialog.close()` runs before the confirm action in `RemoveConfirmDialog`, so a generic `onClosed: reject()` would discard the change being confirmed — the subtitle dialog uses an explicit `confirming` flag and its own cancel button.
  - **Context:** QML smoke `FakeSubtitle` must mirror every new controller property; `canExportTrack`/`canExportSrt` bind to the fake's `rendered` so the existing export-gating assertions stay valid.
  - **Context:** `lupdate` leaves new strings `type="unfinished"` and the `test_i18n` integrity check rejects the same source translated two different ways — translate duplicates identically.
---


## [2026-10-23] - Phase 2: Page structure (Tasks 2.1–2.5)

- **Implemented:** `state`/`busyOwner` derived axes on the controller (`exporting` folded into the busy axis); `SubtitleActionDock` pinned below the `PageShell` with render/transport/export menu/progress/cancel/stale hint; a three-stage `AppSegmented` (`source`/`prepare`/`produce`) with per-stage disabled reasons and auto-selection on load; a two-card empty launcher with one `handleDroppedUrls` seam; window-keyed `wideLayout`, wrapping speaker rows, a real cue-list scrollbar, and `pageMaxWidth: 960`.
- **Files changed:** `ui/subtitle_controller.py`, `ui/qml/SubtitlesTab.qml`, `ui/qml/components/SubtitleActionDock.qml` (new), `ui/qml/components/SubtitleCard.qml`, both `qmldir`s, `ui/i18n/vienetts_en.ts/.qm`, `tests/unit/test_subtitle_controller.py`, `tests/unit/test_i18n.py`, `tests/smoke/test_ui_tabs.py`, `conductor/patterns.md`.
- **Learnings:**
  - **Patterns:** QML smoke probes must snapshot values **immediately**; holding a `QQuickItem` reference across a `loaded`/stage toggle risks `libshiboken: Internal C++ object already deleted`. This hit `popup_item` (voice-picker popup destroyed on close) and `edit_btns` (cue delegates reflowed) — store scalars, never items.
  - **Gotchas:** Repeater delegates have no QObject parent, so `findChildren(QObject, name)` cannot see them (`subtitleSpeakerVoiceField`); use the visual-tree `item_walk`/`ifind` helper.
  - **Gotchas:** `Layout.preferredHeight` on the cue list competed with `fillHeight`; dropping it plus `Layout.minimumHeight: 180` gives three rows at 640×420 and unbounded growth above.
  - **Gotchas:** `wideLayout` must key off `Window.window.width`: the tab/card width shrinks at 760→820 because the nav rail switches from compact to full at 800 px.
  - **Context:** moving the export actions into `SubtitleActionDock` moves their catalog entries to the `SubtitleActionDock` context; `test_i18n` must assert against the new context. `update_i18n.sh` re-emits dock strings as unfinished when their source file changed — retranslate before compiling.
  - **Context:** the full parallel suite reported two loaded-host flakes (`test_worker_thread_safety_smoke`, `test_qwen_host::test_session_lifecycle`); both pass in isolation.

## [2026-10-23] - Phase 3: Workflow clarity (Tasks 3.1–3.7)

- **Implemented:** AI actions now form a labelled, ordered pipeline (clean → translate → fit) with per-step tooltips, an explicit target-language combo passed to `refineWithLlm("translate_dub", lang)`, and a visibly-present/disabled cancel. Diarization has one control per stage (ASR toggle renamed, re-run moved into the speaker bar). Per-speaker casting uses `VoicePicker` with a new `voiceActivated` signal, so no unknown voice ID can be typed. Every gated shared control (`AppButton`, `AppToggle`, `AppCombo`, `AppSlider`, `AppNumberField`) carries `disabledReason` → tooltip + `Accessible.description`; the AI group links to Settings. A `hintText` channel reports silent no-ops (seek-before-render, play-that-renders, busy AI), and `noticeLevel` splits warnings from errors; the banner has an "Đã hiểu" dismiss.
- **Files changed:** `ui/subtitle_controller.py`, `ui/qml/components/{SubtitleCard,VoicePicker,AppToggle,AppCombo,AppSlider,AppNumberField}.qml`, `ui/qml/SubtitlesTab.qml`, `ui/i18n/vienetts_en.ts/.qm`, `tests/unit/test_subtitle_controller.py`, `tests/smoke/test_ui_tabs.py`.
- **Learnings:**
  - **Patterns:** A component that auto-selects a fallback (VoicePicker's `syncSelection`) must expose a *user-only* signal; a `onSelectedVoiceChanged` write would silently rewrite every speaker mapping on first paint. `voiceActivated(id)` is emitted solely from the click/keyboard path.
  - **Patterns:** `disabledReason` belongs on the shared control, not at 40 call sites: each control renders the same tooltip + accessible description, and hosts only supply the sentence.
  - **Gotchas:** Repeater delegates are invisible to `findChildren(QObject, …)`; the smoke reason sweep needed the visual-tree `ifind` fallback. A sweep like this also catches reasons that are empty because only one of several disable axes was covered (`subtitleDiarizeToggle` initially ignored busy).
  - **Gotchas:** A `_set_error("")` used as a "clear on start" also clears the notice level — reset it to `info` there, or a dismissed banner and a fresh success never leave the warning tone.
  - **Context:** `update_i18n.sh` re-emits ~30 new strings as unfinished per phase; translate before compiling, and `test_i18n` needs the new context (`SubtitlesTab` "Lưu ý"/"Đã hiểu") only when asserting it.

## [2026-10-23] - Phase 4: Design-system conformance & accessibility (Tasks 4.1–4.8)

- **Implemented:** Fit badge uses `Theme.warningText` and the contrast test now covers every status text token on every surface in both themes; the three hand-rolled panels are `AppCard { elevation: 0 }`, both progress bars use the new `AppProgressBar` (accessible name + one skin), badges are `StatusBadge`, and the timing panel has a `SectionLabel`. Cue edits use the new `AppTextField` (32 px, one border, accessible name). Only one primary is visible per state (cue Save demoted). Cue list is a Tab stop with `Accessible.List`, Up/Down navigate+seek, all fields/sliders/combos have accessible names, AI/render/Studio/export actions have tooltips, and `PageShell.ensureFocusVisible()` scrolls a focused control into view. `videoOverrunMs` is cached under (video, wav mtime/size) and invalidated by `renderedChanged`/`videoChanged` (new `overrunChanged`). File-dialog filters are translated, the English catalog has 0 unfinished entries, and the product guidelines now describe the real vi→en direction. `SubtitleCard` no longer sets the deprecated `AppCard.subtitle`; `clamp_to_video` is documented as an intentional API-only opt-in.
- **Files changed:** `ui/subtitle_controller.py`, `core/video_dub.py`, `ui/qml/SubtitlesTab.qml`, components (`SubtitleCard`, `SubtitleActionDock`, `PageShell`, `AppProgressBar` new, `AppTextField` new, `AppToggle/AppCombo/AppSlider/AppNumberField`), both `qmldir`s, `ui/i18n/vienetts_en.ts/.qm`, `conductor/product-guidelines.md`, `tests/unit/{test_theme,test_i18n,test_subtitle_controller}.py`, `tests/smoke/test_ui_tabs.py`.
- **Learnings:**
  - **Patterns:** Accessibility is cheaper as a component contract than a call-site sweep — `AppProgressBar`/`AppTextField`/`AppCombo`/`AppSlider`/`AppNumberField` own `Accessible.name`/`description`, so the card only supplies a label.
  - **Gotchas:** `isAncestorOf()` exists only in C++; QML focus-follow needs an explicit parent walk (`ownsFocusItem`). Extract the scroll math into a public `ensureFocusVisible(item)` so it is testable without a real active window.
  - **Gotchas:** Offscreen Qt does not deliver `forceActiveFocus` to the window's `activeFocusItem`, so a keyboard test must `app.sendEvent(item, QKeyEvent(...))`; `QTest.keyClick` only accepts a QWidget/QWindow.
  - **Gotchas:** `findChildren` cannot see Repeater delegates; `save_variant`/speaker pickers need `ifind`. Counting "visible primaries" must (a) obey the visual-parent visibility chain and (b) be scoped to the tab — `Main.qml`'s global `modelDownloadButton` is also primary.
  - **Context:** The plan's "three inline TextFields" had become two after Task 3.4 replaced the speaker field with a picker; the guard test asserts the absence of the pattern, not the count.

## [2026-10-23] - Phase 5 / Task 5.1: Component extraction

- **Implemented:** `SubtitleCard.qml` (1,473 → 646 lines) now composes four extracted components: `SubtitleSourcePanel` (empty launcher, file dialogs, video/ASR bar), `SubtitlePreparePanel` (ordered AI group, prerequisites link, progress), `SubtitleCueEditor` (cue list, keyboard seek, inline editing), `SubtitleCastPanel` (speaker → voice mapping). Each takes its gate state through properties (`available`/`busy`/`aiBusy`/`exporting`/reasons) and keeps its `objectName`s. The card keeps the public drop seam, which forwards to the panel.
- **Files changed:** four new components, both `qmldir`s, `SubtitleCard.qml`, `tests/smoke/test_ui_tabs.py`, `tests/unit/test_theme.py`, new `tests/unit/test_subtitle_components.py`.
- **Learnings:**
  - **Gotchas:** A `ColumnLayout` child that keeps a non-zero height after its own children turn invisible does not always shrink on a stage switch; the stale height eats the slack that used to flow into the cue list. Bind the panel's own `visible` to "this stage owns me" so the parent layout drops the whole subtree.
  - **Gotchas:** `findChildren(QObject)` from the tab root reaches dialogs owned by a component (FileDialog is not an Item) but still misses Repeater delegates; the NFR-1 contract check must union `findChildren` with `item_walk`.
  - **Patterns:** Standalone instantiation is a cheap real test when the components read context globals: a stub controller with the exact Q_PROPERTY/Slot surface plus a `QQmlEngine` in a subprocess catches borrowed ids and missing imports without loading the app.
  - **Gotchas:** `Layout.fillHeight` on a composite instance applies to its root item normally; the earlier "not filling" symptom was the stale-height sibling, not the attached property.

## [2026-10-23] - Phase 5 / Task 5.2: Coverage gap closed

- **Implemented:** `FakeSubtitle` now records every action (`action_calls`) and exposes two projects; the para_group driver clicks all 17 listed actions once and the test asserts the exact call map, including render/cancelRender, play, the three exports through the dock's opened menu (overlay walk), export cancel, clear, and project open/delete through the real dialogs. A real `.srt` is imported into the real `SubtitleController` in the Studio smoke and the populated tab asserts loaded/stage/cue rows/text/launcher-hidden/stats-hidden. `generate_screenshots.py` seeds a six-cue project so the `subtitles` destination is shot populated; `docs/screenshots/redesign/phase-5/` holds both themes at 640×420, 760×600, 1120×740 and 1440×900, pinned by a new matrix test.
- **Files changed:** `tests/smoke/test_ui_tabs.py`, `tests/smoke/test_ui_studio.py`, `tests/smoke/test_screenshots.py`, `scripts/generate_screenshots.py`, `docs/screenshots/redesign/phase-5/*.png`.
- **Learnings:**
  - **Gotchas:** a driver template is a normal (non-raw) Python string — `\n` inside inserted driver code is expanded when the template is built; escape it as `\\n` or the generated file loses all indentation after that literal.
  - **Gotchas:** `subtitlesTab.findChildren` reaches the source panel's FileDialogs (QObject children) but not Repeater delegates; delegate actions (`speakerSample`, `speakerVoicePicker`, export menu items) need `ifind`/overlay walks.
  - **Patterns:** `click()` invokes the handler unconditionally, so a coverage driver must select an enabled instance (the active project row's "Mở" is disabled) instead of assuming row order.
  - **Context:** export menu items live in the window overlay; each selection closes the menu, so the harness must reopen it per item.
