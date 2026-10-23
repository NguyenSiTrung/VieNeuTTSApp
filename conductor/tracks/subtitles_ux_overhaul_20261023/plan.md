# Implementation Plan: Overhaul the Dubbing & Subtitles Tab (`subtitles_ux_overhaul_20261023`)

## Overview

5-phase TDD remediation of the 2026-10-23 Dubbing & Subtitles audit (33 findings, 4 tiers) across `src/vienetts_app/ui/subtitle_controller.py`, `src/vienetts_app/core/{subtitle_project,align,video_dub}.py`, `src/vienetts_app/ui/qml/SubtitlesTab.qml`, `src/vienetts_app/ui/qml/components/{SubtitleCard,AppTextField}.qml`, `src/vienetts_app/ui/i18n/vienetts_en.ts`, `conductor/patterns.md`, `conductor/product-guidelines.md`.

Evidence and measured geometry: `docs/ui-audit/SUBTITLES_TAB_UX_AUDIT.md`.

**Sequencing rationale:** Phase 1 (data safety) ships first and is independent of every visual change — it is the only phase where delay actively costs users work. Phase 2 introduces the dock + derived state, which Phase 3's workflow work builds on. Phase 4 is mechanical design-system conformance and can overlap Phase 3. Phase 5 is extraction + docs.

---

## Phase 1: Data Safety — Confirm-Before-Destroy, Staleness, Project Lifecycle

<!-- execution: sequential -->
<!-- depends: -->

> All six tasks touch `subtitle_controller.py` + `SubtitleCard.qml`, so this phase is
> sequential. It has no phase dependency — it is the highest-priority work and ships first.

- [x] **Task 1.1: Destroy-confirmation gate for render-invalidating actions**
  <!-- files: src/vienetts_app/ui/subtitle_controller.py, src/vienetts_app/ui/qml/components/SubtitleCard.qml, tests/unit/test_subtitle_controller.py -->
  - Red tests:
    - With a rendered project, setting `rateCap` to a different value does NOT immediately invalidate `rendered`; it raises a pending-confirmation signal carrying the reason (`"rate_cap"`) and the loss description.
    - Confirming applies the change and invalidates; cancelling reverts the knob to its previous value and leaves `rendered is True`.
    - The rate slider's debounce emits exactly ONE pending-confirmation per release gesture, never per tick.
    - The same gate covers `mode`, `maxGapMs`, `offsetMs`, `mergeSentences`, `voice`.
  - Green implementation:
    - Add a `renderInvalidationRequested(reason: str, message: str)` signal and `confirmRenderInvalidation()` / `rejectRenderInvalidation()` slots.
    - Route the six setters through a shared `_request_rebuild_for_policy(reason)` that stages the change when `rendered` is true.
    - QML: a confirm dialog (`AppNotice`-style or `RemoveConfirmDialog` pattern) bound to the signal.

- [x] **Task 1.2: Explicit stale-render state and truthful export gating**
  <!-- files: src/vienetts_app/ui/subtitle_controller.py, src/vienetts_app/ui/qml/components/SubtitleCard.qml, tests/unit/test_subtitle_controller.py -->
  - Red tests:
    - After a cue edit invalidates a render, `renderStale is True` and `statsSummary` retains the last-known stats instead of returning `""`.
    - Clearing staleness happens only on a successful re-render.
    - `canExportTrack` / `canExportSrt` are False while stale, with a distinct reason string (not the generic "Hãy tạo trước").
  - Green implementation:
    - Add `renderStale` (NOTIFY) and `renderStaleReason` properties; retain `lastStats`.
    - Surface in the dock (Phase 2) and in the export buttons' `disabledReason`.

- [x] **Task 1.3: Cue-mutation confirmations (`updateCue` / `runDiarization` / `refineWithLlm` / re-ASR)**
  <!-- files: src/vienetts_app/ui/subtitle_controller.py, src/vienetts_app/ui/qml/components/SubtitleCard.qml, tests/unit/test_subtitle_controller.py -->
  - Red tests:
    - `updateCue` on a rendered project stages the change and requires confirmation; on an unrendered project it applies immediately.
    - `runDiarization` and `refineWithLlm` follow the same rule.
    - `transcribeVideo` over a project whose cues differ from the managed SRT requires confirmation before overwriting.
    - `_update_project_cues` no longer unconditionally zeroes `stats`/`adjusted` — it zeroes only after confirmation.
  - Green implementation:
    - Extend the Task 1.1 gate to the cue-mutating slots; keep `_update_project_cues` pure and move the invalidation to the confirmed path.

- [x] **Task 1.4: Project lifecycle — "Dự án mới" and a project picker**
  <!-- files: src/vienetts_app/ui/subtitle_controller.py, src/vienetts_app/ui/qml/components/SubtitleCard.qml, tests/unit/test_subtitle_controller.py -->
  - Red tests:
    - `clear()` is reachable from a slot the QML calls; after it, `loaded is False` and the empty state returns.
    - `projects` (QVariantList) lists stored workspaces with title, cue count and modified time; `openProject(id)` loads one; `removeProject(id)` deletes it and emits `projectsChanged`.
    - Removing the active project resets to the empty state.
  - Green implementation:
    - Expose `projects`, `openProject`, `removeProject` over `SubtitleProjectStore.list_projects()` / `remove()`.
    - QML: "Dự án mới" in the page header + a picker (reuse the `RemoveConfirmDialog` pattern for delete).

- [x] **Task 1.5: `cancelExport()` and export-cancel UI**
  <!-- files: src/vienetts_app/ui/subtitle_controller.py, src/vienetts_app/ui/qml/components/SubtitleCard.qml, tests/unit/test_subtitle_controller.py -->
  - Red tests:
    - `cancelExport()` during an in-flight video export clears `exporting` within one event-loop turn and emits `exportFinished("", <cancelled reason>)`.
    - The tab's controls re-enable after cancellation.
    - Cancelling when idle is a no-op.
  - Green implementation:
    - Add the slot, bump `_export_generation`, and have the export callback treat a stale generation as cancelled.

- [x] **Task 1.6: Transcript-mode gap cap and non-destructive mix default**
  <!-- files: src/vienetts_app/core/align.py, src/vienetts_app/ui/subtitle_controller.py, src/vienetts_app/core/subtitle_project.py, tests/unit/test_align.py, tests/unit/test_subtitle_controller.py -->
  - Red tests:
    - A fresh controller in `transcript` mode produces a policy with `max_gap_ms == 1500` (not `0`).
    - `0` remains meaningful (uncapped) when explicitly set.
    - `video_mix_mode` defaults to `"duck"` for a new project, or `"replace"` requires explicit opt-in.
  - Green implementation:
    - Track an explicit `_max_gap_ms_set` flag, or default `_max_gap_ms = 1500` in transcript mode and `0` in dub mode.
    - Flip the mix default with a migration for stored projects.

---

## Phase 2: Page Structure — Pinned Dock, Derived State, Segmented Stages

<!-- execution: sequential -->
<!-- depends: phase1 -->

> Depends on Phase 1: the dock and the derived `state` must render the stale/confirmation
> states introduced there. Tasks are chained 2.1 → 2.5 because each edits `SubtitleCard.qml`.

- [x] **Task 2.1: Derived `state` + `busyOwner` replacing the five booleans**
  <!-- files: src/vienetts_app/ui/subtitle_controller.py, tests/unit/test_subtitle_controller.py -->
  - Red tests:
    - `state` returns exactly one of `empty | source_only | cues_ready | ai_busy | rendering | rendered | stale | exporting | error` for each scenario.
    - `busyOwner` is `"" | "ai" | "render" | "export"` and never reports two owners at once.
    - A cache-hit `render()` transitions `cues_ready → rendered` without ever entering `rendering`.
  - Green implementation:
    - Add a derived `state` property computed from the existing axes; keep the old booleans as compatibility aliases for one phase, then delete them.

- [x] **Task 2.2: Pinned action dock (`SubtitleActionDock`)**
  <!-- files: src/vienetts_app/ui/qml/SubtitlesTab.qml, src/vienetts_app/ui/qml/components/SubtitleActionDock.qml, src/vienetts_app/ui/qml/components/qmldir, src/vienetts_app/ui/qml/qmldir, conductor/patterns.md, tests/smoke/test_ui_tabs.py -->
  - Red tests:
    - `subtitleActionDock` exists and is NOT a descendant of `pageScrollView`.
    - At 640×420 the dock's render button is inside the window bounds.
    - The dock exposes progress + cancel, and shows the stale affordance from Task 1.2.
  - Green implementation:
    - New `components/SubtitleActionDock.qml` (skin from `TransportDock`/`studioTransportDock`).
    - `SubtitlesTab.qml` places it below the `PageShell` inside the page `ColumnLayout`.
    - Update `conductor/patterns.md:96` to record the reversal.

- [x] **Task 2.3: Source / Prepare / Produce segmented stages**
  <!-- files: src/vienetts_app/ui/qml/components/SubtitleCard.qml, tests/smoke/test_ui_tabs.py -->
  - Red tests:
    - `subtitleStageSwitch` (AppSegmented) has 3 segments; selecting a stage swaps the visible panel and calls `PageShell.scrollToTop()`.
    - Stages that are impossible for the current `state` are disabled with a non-empty `disabledReason`.
    - The cue list remains visible in Prepare and Produce.
  - Green implementation:
    - Rebuild the body as three panels gated on the stage; keep every existing `objectName` reachable.

- [x] **Task 2.4: Two-choice empty state with drag-and-drop**
  <!-- files: src/vienetts_app/ui/qml/components/SubtitleCard.qml, tests/smoke/test_ui_tabs.py -->
  - Red tests:
    - `subtitleEmptyLauncher` renders two cards; each states its prerequisite.
    - A `DropArea` exists and routes a dropped `.srt` to `importSrt` and a video to `importVideo`.
    - The launcher is hidden once `loaded` is true.
  - Green implementation:
    - Replace the single-line hint with the launcher; wire `onDropped` through a `handleDroppedUrls` seam like `CreateTab.qml:160`.

- [x] **Task 2.5: Responsive correctness — breakpoint, speaker row, cue list, page width**
  <!-- files: src/vienetts_app/ui/qml/components/SubtitleCard.qml, src/vienetts_app/ui/qml/SubtitlesTab.qml, tests/smoke/test_ui_tabs.py -->
  - Red tests:
    - `wideLayout` tracks window width monotonically across 760/820/980/1120 (no shrink at 820).
    - `subtitleSpeakerVoiceField.width >= 120` at 640×420.
    - `subtitleCueList` has a `ScrollBar.vertical` and its height grows with the viewport.
    - `pageMaxWidth` is 960.
  - Green implementation:
    - Base the breakpoint on `Window.width` (like `StudioTab.qml:53`); add `Layout.minimumWidth: 120` and convert the speaker row to a `Flow`; declare the cue-list scrollbar and drop `preferredHeight`; set `pageMaxWidth: 960`.

---

## Phase 3: Workflow Clarity

<!-- execution: sequential -->
<!-- depends: phase2 -->

> Every task edits `SubtitleCard.qml`, so within-phase execution is sequential.
> **Phase 3 and Phase 4 are mutually independent and may run in parallel with each other**
> (Phase 3 owns the card's behaviour/structure, Phase 4 owns its component skins — see the
> file-ownership split in Task 4.x; coordinate on `SubtitleCard.qml` if both run at once).

- [x] **Task 3.1: AI action group with order, scope and always-visible cancel**
  <!-- files: src/vienetts_app/ui/qml/components/SubtitleCard.qml, tests/smoke/test_ui_tabs.py -->
  - Red tests:
    - The four AI actions render in pipeline order with a group label; each has a tooltip.
    - `subtitleCancelAiButton` is visible whenever an AI action is available (not only while busy).
    - Each action carries a non-empty `disabledReason` when its prerequisite is missing.
  - Green implementation:
    - Replace the flat `Flow` with a labelled group; add tooltips and descriptions.

- [x] **Task 3.2: Target-language picker for "Dịch lồng tiếng"**
  <!-- files: src/vienetts_app/ui/qml/components/SubtitleCard.qml, src/vienetts_app/ui/subtitle_controller.py, tests/smoke/test_ui_tabs.py, tests/unit/test_subtitle_controller.py -->
  - Red tests:
    - A `LanguagePicker` sits beside the translate action and its value is passed to `refineWithLlm("translate_dub", lang)`.
    - With no explicit pick, the controller falls back to `synthesisLanguage` (existing behaviour preserved).
  - Green implementation:
    - Add a `translateTargetLanguage` property and pass it through the slot.

- [x] **Task 3.3: Merge the duplicate diarization controls**
  <!-- files: src/vienetts_app/ui/qml/components/SubtitleCard.qml, tests/smoke/test_ui_tabs.py -->
  - Red tests:
    - Only one control mentions diarization in the ASR stage; the re-run action lives with the speaker bar it produces.
    - Both code paths still reachable: `transcribeVideo(with_diarization=True)` and `runDiarization()`.
  - Green implementation:
    - Rename the toggle to "Tự tách người nói khi nhận dạng"; move the re-run button into the speaker bar.

- [x] **Task 3.4: `VoicePicker`-backed per-speaker casting**
  <!-- files: src/vienetts_app/ui/qml/components/SubtitleCard.qml, tests/smoke/test_ui_tabs.py -->
  - Red tests:
    - Each speaker row exposes a picker (not a raw ID field) whose selection calls `setSpeakerVoice(spk, id)`.
    - An invalid/unknown voice can no longer be entered.
  - Green implementation:
    - Replace the `TextField` with a compact `VoicePicker` or a "Chọn giọng…" button opening the existing picker.

- [x] **Task 3.5: Prerequisites, `disabledReason` coverage and Settings deep-links**
  <!-- files: src/vienetts_app/ui/qml/components/SubtitleCard.qml, src/vienetts_app/ui/qml/components/EngineState.qml, tests/smoke/test_ui_tabs.py -->
  - Red tests:
    - Every gated control exposes a non-empty `disabledReason` when disabled.
    - The LLM and ASR reasons include a navigation action to the Settings card.
  - Green implementation:
    - Add `disabledReason` across the 39 remaining controls; add `EngineState` reason helpers.

- [x] **Task 3.6: Feedback for silent no-ops (`seekToCue`, `play` auto-render, AI busy returns)**
  <!-- files: src/vienetts_app/ui/subtitle_controller.py, src/vienetts_app/ui/qml/components/SubtitleCard.qml, tests/unit/test_subtitle_controller.py, tests/smoke/test_ui_tabs.py -->
  - Red tests:
    - `seekToCue` before a render sets a user-visible hint instead of returning silently.
    - Pressing play on an unrendered project announces that a render will start (confirm or explicit label).
    - AI slots that refuse because busy set `errorText` (or emit a status) rather than returning bare `False`.
  - Green implementation:
    - Add a `statusText` / hint channel; update the QML cursor/label accordingly.

- [x] **Task 3.7: Error surface — dismiss, severity split, auto-clear**
  <!-- files: src/vienetts_app/ui/qml/SubtitlesTab.qml, src/vienetts_app/ui/subtitle_controller.py, tests/smoke/test_ui_tabs.py -->
  - Red tests:
    - The banner exposes a dismiss action that clears `errorText`.
    - A warning (diarization heuristic fallback) renders with warning tone and is not presented as a failure.
    - A successful action clears a prior error.
  - Green implementation:
    - Add `noticeLevel` (`info|warning|error`) to the controller; bind `AppNotice.tone` and `actionText`.

---

## Phase 4: Design-System Conformance and Accessibility

<!-- execution: sequential -->
<!-- depends: phase2 -->

> Eight tasks all edit `SubtitleCard.qml`; sequential within the phase. Independent of
> Phase 3, so the two phases can overlap if file ownership is coordinated.

- [x] **Task 4.1: Contrast fix and text-token audit**
  <!-- files: src/vienetts_app/ui/qml/components/SubtitleCard.qml, tests/unit/test_theme.py, tests/smoke/test_ui_tabs.py -->
  - Red tests:
    - `Theme.warning` is not used as a text colour in `SubtitleCard.qml`; the fit badge uses `Theme.warningText`.
    - A contrast test asserts ≥4.5:1 for every text token on every surface in both themes.
  - Green implementation:
    - Swap `:577` to `Theme.warningText`; add the guard test.

- [x] **Task 4.2: Adopt `AppCard { elevation: 0 }`, shared ProgressBar skin, `StatusBadge`, `SectionLabel`**
  <!-- files: src/vienetts_app/ui/qml/components/SubtitleCard.qml, src/vienetts_app/ui/qml/components/AppProgressBar.qml, src/vienetts_app/ui/qml/components/qmldir, src/vienetts_app/ui/qml/qmldir, tests/smoke/test_ui_tabs.py -->
  - Red tests:
    - No bare `ProgressBar` remains in `SubtitleCard.qml`; both use the shared skin and carry `Accessible.name`.
    - The three hand-rolled panels are `AppCard` instances (assertable via `metaObject().superClass()` walking, per the existing shell driver idiom).
    - `SectionLabel` and `StatusBadge` are used where hand-rolled equivalents were.
  - Green implementation:
    - Add `components/AppProgressBar.qml`; refactor the panels/badges/labels.

- [x] **Task 4.3: `AppTextField` component replacing three inline skins**
  <!-- files: src/vienetts_app/ui/qml/components/AppTextField.qml, src/vienetts_app/ui/qml/components/qmldir, src/vienetts_app/ui/qml/qmldir, src/vienetts_app/ui/qml/components/SubtitleCard.qml, tests/smoke/test_ui_tabs.py -->
  - Red tests:
    - `AppTextField` exposes `text`/`placeholderText`/`accessibleLabel` and mirrors `text` (per the text-pin rule).
    - No inline `background: Rectangle` TextField remains in `SubtitleCard.qml`.
    - Heights come from `Theme.controlHeightSm`.
  - Green implementation:
    - New component + registration in both `qmldir`s; replace the three call sites.

- [x] **Task 4.4: Single-primary rule and action-row regrouping**
  <!-- files: src/vienetts_app/ui/qml/components/SubtitleCard.qml, tests/smoke/test_ui_tabs.py -->
  - Red tests:
    - At most one `primary` button is visible in any reachable state (assert across empty / cues_ready / rendering / rendered / stale).
    - The cue Save button is `secondary`.
    - Export-content toggle and Studio navigation no longer share the primary's row.
  - Green implementation:
    - Demote the cue Save; move the toggle beside the SRT export and Studio into the dock.

- [x] **Task 4.5: Accessibility pass — focus, names, tooltips, focus-follow**
  <!-- files: src/vienetts_app/ui/qml/components/SubtitleCard.qml, src/vienetts_app/ui/qml/components/AppNumberField.qml, src/vienetts_app/ui/qml/components/AppSlider.qml, src/vienetts_app/ui/qml/components/AppCombo.qml, tests/smoke/test_ui_tabs.py -->
  - Red tests:
    - The cue list is focusable with `Accessible.role` and can be navigated by keyboard.
    - Every `AppNumberField`, `AppSlider` and `AppCombo` reports a non-empty `Accessible.name` matching its visible label.
    - Tooltips exist on all AI/export/render/Studio actions.
    - Focusing an off-screen control scrolls it into view.
  - Green implementation:
    - Add `accessibleLabel` plumbing to the components; add focus-follow in `PageShell`.

- [x] **Task 4.6: Move `videoOverrunMs` off the GUI-thread binding path**
  <!-- files: src/vienetts_app/ui/subtitle_controller.py, src/vienetts_app/ui/qml/components/SubtitleCard.qml, tests/unit/test_subtitle_controller.py -->
  - Red tests:
    - Reading `videoOverrunMs` never spawns a subprocess (cached); it recomputes only on `videoChanged`/render completion.
    - The value is still correct after a render completes.
  - Green implementation:
    - Cache the computed overrun; invalidate on the relevant NOTIFY signals.

- [x] **Task 4.7: i18n — missing strings, dialog filters, stale guideline**
  <!-- files: src/vienetts_app/ui/qml/components/SubtitleCard.qml, src/vienetts_app/ui/i18n/vienetts_en.ts, conductor/product-guidelines.md, tests/unit/test_i18n.py -->
  - Red tests:
    - The two previously-untranslated strings and the two `FileDialog` filters resolve in the English catalog.
    - No `type="unfinished"` entries.
  - Green implementation:
    - Wrap in `qsTr()`, run `scripts/update_i18n.sh`, correct the `product-guidelines.md` i18n claim.

- [x] **Task 4.8: Dead-surface cleanup — deprecated `subtitle`, unbound properties, `clamp_to_video`**
  <!-- files: src/vienetts_app/ui/qml/components/SubtitleCard.qml, src/vienetts_app/ui/subtitle_controller.py, src/vienetts_app/core/video_dub.py, tests/unit/test_subtitle_controller.py -->
  - Red tests:
    - `AppCard.subtitle` is no longer set by `SubtitleCard`.
    - Every remaining controller property has a QML consumer, or is documented as API-only.
    - `clamp_to_video` is either wired to a decision or explicitly documented as intentionally unused.
  - Green implementation:
    - Delete the dead property set; wire a scrubber/time readout if it lands with the dock; document the rest.

---

## Phase 5: Component Extraction, Test Coverage and Documentation

<!-- execution: sequential -->
<!-- depends: phase3, phase4 -->

- [x] **Task 5.1: Extract the monolithic card into focused components**
  <!-- files: src/vienetts_app/ui/qml/components/SubtitleCard.qml, src/vienetts_app/ui/qml/components/SubtitleSourcePanel.qml, src/vienetts_app/ui/qml/components/SubtitleCueEditor.qml, src/vienetts_app/ui/qml/components/SubtitlePreparePanel.qml, src/vienetts_app/ui/qml/components/SubtitleCastPanel.qml, src/vienetts_app/ui/qml/components/qmldir, src/vienetts_app/ui/qml/qmldir -->
  - Red tests:
    - Every pre-existing `objectName` still resolves from the tab root after extraction.
    - Each extracted component instantiates standalone with a fake controller.
  - Green implementation:
    - Split `SubtitleCard.qml` (1,289 L) into the five components; keep the `subtitleCard` root.

- [x] **Task 5.2: Close the test-coverage gap**
  <!-- files: tests/smoke/test_ui_tabs.py, tests/smoke/test_ui_shell.py, tests/smoke/test_screenshots.py -->
  - Red tests:
    - Every action button (`render`, `play`, `exportWav`, `exportSrt`, `exportVideo`, `transcribeVideo`, `runDiarization`, `updateCue`, `setSpeakerVoice`, `playSpeakerSample`, `cloneSpeakerFromVideo`, `refineWithLlm`, `cancelAiTask`, `cancelExport`, `clear`, `openProject`, `removeProject`) is clicked at least once and its controller call asserted.
    - An SRT is loaded into the real controller and UI state asserted (not just the empty state).
    - The screenshot matrix renders the populated tab at 640×420, 760×600, 1120×740 and 1440×900.
  - Green implementation:
    - Extend the shared driver scenarios; regenerate `docs/screenshots/redesign/` for this destination (the current `create-subtitles-*` files are stale, pre-`aef1d87`, `v0.2.0`).

- [ ] **Task 5.3: Documentation, patterns elevation and archive readiness**
  <!-- files: conductor/patterns.md, conductor/workflow.md, docs/ui-audit/SUBTITLES_TAB_UX_AUDIT.md, conductor/tracks/subtitles_ux_overhaul_20261023/learnings.md -->
  - Red tests: n/a (documentation) — validated by the i18n/pattern lint if any.
  - Green implementation:
    - Elevate new patterns (derived-state model, destroy-confirmation gate, dock convention) to `conductor/patterns.md`.
    - Refresh `conductor/workflow.md` suite counts.
    - Append track learnings; mark the audit doc's resolved findings.
