# Specification: Overhaul the "Lồng tiếng & Phụ đề" (Dubbing & Subtitles) Tab — Data Safety, Structure, Workflow & Design-System Conformance (`subtitles_ux_overhaul_20261023`)

## Overview

The 2026-10-23 UI/UX audit of the Dubbing & Subtitles destination (`SubtitlesTab.qml` 109 L, `SubtitleCard.qml` 1,289 L, `subtitle_controller.py` 2,321 L plus the core layers it drives) produced **33 findings in 4 tiers**. Full evidence, measured geometry, contrast ratios and screenshots live in [`docs/ui-audit/SUBTITLES_TAB_UX_AUDIT.md`](../../../docs/ui-audit/SUBTITLES_TAB_UX_AUDIT.md); this spec is the actionable contract derived from it.

The central finding is that **the layout is not the worst problem**. The tab can destroy a user's completed work with no confirmation and no undo:

1. **Any policy knob invalidates a completed render** (`subtitle_controller.py:459/473/490/507/521/535` → `_rebuild_for_policy` → `renderedChanged`). The rate cap is a *drag* control, so idle dragging after a 40-minute render destroys it.
2. **Editing one cue destroys the render *and* the retimed SRT** — `_update_project_cues` (`:2230-2240`) unconditionally zeroes `total_ms`, `stats`, `adjusted`.
3. **Re-running ASR overwrites the managed SRT** and discards cue edits (`:1918-1922`).
4. **No project lifecycle at all** — `clear()`, `list_projects()` and `remove()` have zero callers, so workspaces accumulate forever and cannot be reopened or deleted.

Structurally the tab was promoted from a `CreateTab` mode to a top-level destination (`aef1d87`) but never received a page's chrome: it is the **only destination without a pinned action dock**, and its primary CTA measures **702 px below the fold** at the documented 640×420 minimum (the tall-tab rule's own worked example was 208 px).

**Note on a recorded decision:** `conductor/patterns.md:96` currently states *"SubtitlesTab keeps its actions inside `SubtitleCard`"*. That was a deliberate scope call made when the tab was still a mode. **This track reverses it** and must update that line in the same change, so a future agent does not read the pattern file and reinstate the defect.

---

## Functional Requirements

### FR-1: Data safety — no silent destruction of completed work (Tier 0 / P0)

- **FR-1.1 (destroy-confirmations):** Any action that would discard a `rendered` track or a populated `adjusted` cue set must be confirmed first. Covered actions: policy knob change (`mode`, `rateCap`, `maxGapMs`, `offsetMs`, `mergeSentences`, `voice`), `updateCue`, `runDiarization`, `refineWithLlm`, and re-running `transcribeVideo` over a project with user edits.
  - The confirmation must name what will be lost (rendered audio / retimed SRT / cue edits) and the action that will cause it.
  - The rate slider confirms **on release**, not per debounce tick.
  - **Chosen approach (user-confirmed):** confirm-before-destroy + a persistent "render is stale" indicator. Do **not** attempt incremental per-cue re-rendering in this track.
- **FR-1.2 (staleness is visible):** Introduce an explicit stale state so a render that no longer matches the current cues/knobs is *shown* as stale rather than silently vanishing. The stats label must not merely empty; the dock must display a stale affordance and the export actions must explain why they are unavailable.
- **FR-1.3 (project lifecycle):** Wire `clear()` to a user-visible "Dự án mới" action, and add a project picker backed by `SubtitleProjectStore.list_projects()` / `remove()` so workspaces can be listed, reopened and deleted.
- **FR-1.4 (cancel export):** Add `cancelExport()` to `SubtitleController` and a Cancel control on the export progress surface. A hung ffmpeg must never permanently disable the tab.
- **FR-1.5 (transcript gap cap):** Fix the parameter mismatch — `FitPolicy.transcript()` defaults `max_gap_ms=1500` (`align.py:132`) but the controller always passes `_max_gap_ms = 0` (`:232`, `:715`), and `align.py:76` documents `0` as **uncapped**. Transcript mode must respect a real cap.
- **FR-1.6 (ASR overwrite guard):** `transcribeVideo` must not silently overwrite a managed SRT whose project has user edits; it must route through FR-1.1's confirmation.
- **FR-1.7 (destructive-default review):** `embed_subtitles=True` + `soft` and `video_mix_mode="replace"` are current defaults (`subtitle_project.py:273-278`). The default mix must not destroy original audio without the user choosing it.

### FR-2: Page structure — a dock, a state model, and segmented stages (Tier 1 / P1)

- **FR-2.1 (pinned action dock):** Add a docked action bar **outside** the `PageShell` scroll area (mirroring `CreateTab.qml:515` / `StudioTab.qml:1408`) holding the primary render action, transport, an export menu, and render/export progress + cancel. The primary CTA must be visible at every window size from 640×420 up.
  - `conductor/patterns.md:96` must be updated in the same change.
- **FR-2.2 (derived state):** Replace the five independent booleans (`loaded`, `hasVideo`, `rendering`, `aiBusy`, `exporting`) plus `rendered` with a single derived `state` (and a `busyOwner` identifying which operation owns the engine). All 41 controls must derive their gating from that single source instead of re-deriving locally.
  - This must resolve the two contradictory-progress-bar situation (AI bar `:431-437` vs render/export bar `:1060-1073` animating simultaneously) and the cache-hit case where neither appears.
- **FR-2.3 (segmented stages):** Split the body into **Source → Prepare → Produce** with `AppSegmented` (already used by `CreateTab.createModeSwitch` and `VoicesTab`). Cue list remains visible in Prepare and Produce.
  - Impossible stages must be disabled with a reason, not merely hidden.
- **FR-2.4 (empty-state launcher):** Replace the blank empty state (title + one card + ~700 px of void) with a two-choice launcher — "Video → phụ đề tự động" vs "Tệp .srt → lồng tiếng" — each an `AppCard` stating its prerequisites (Whisper model / LLM server) with a Settings jump. Add a `DropArea` for `.srt`/video, matching `AudiobookTab.qml:334`, `BatchQueueCard.qml:122`, `CloningTab.qml:331`, `DocumentEditorCard.qml:180`.
- **FR-2.5 (responsive correctness):**
  - `wideLayout: width >= 760` (`SubtitleCard.qml:41`) measures a **non-monotonic** card width (nav rail flips 232→56 px at an 800 px window per `Main.qml:90`), so an 820 px window yields a 556 px card while a 760 px window yields 664 px. Base the breakpoint on window/available width.
  - The speaker row must not overflow: the voice `TextField` collapses to **0–3 px** at 640×420 (`:995-1020`, `Layout.fillWidth` with no `minimumWidth`; mirror `AppSlider.minimumTrackWidth` and allow the row to wrap).
  - The cue list must not be a 5-row keyhole: it has no `ScrollBar.vertical` at all (`:479` references a non-existent one), and `preferredHeight: 160/196` defeats `Layout.fillHeight`.
  - `pageMaxWidth: 1080` (`SubtitlesTab.qml:26`) is the widest in the app and unreachable below a 1440 px window; align with the 960 convention used by Create/Voices.

### FR-3: Workflow clarity — the pipeline must be expressible (Tier 2 / P2)

- **FR-3.1 (AI actions ordered and explained):** The four AI actions are currently peers in one flat `Flow` (`:347-415`) with no order, no scope, no tooltips and no undo. Correct order is clean → translate → fit-timing (`llm_refine.py:109-136`, `maxChars` at `:206`). Each action needs a description and a scope.
- **FR-3.2 (translate target language):** "AI Dịch lồng tiếng" resolves its target from `controller.synthesisLanguage` (`subtitle_controller.py:2182-2185`) — a different tab's setting — with no affordance here. Bind a `LanguagePicker` beside the action (the component already exists and is used at `:899`).
- **FR-3.3 (duplicate diarization control):** "Phân tách người nói" exists twice with different semantics — a toggle consumed at ASR time (`:168`) and a button that re-runs diarization (`:355`), 190 px apart. Merge or relabel unambiguously.
- **FR-3.4 (voice casting):** Replace the raw voice-ID `TextField` (`:995-1020`, which fails only at render time with "Giọng đọc '{voice}' … không hợp lệ") with a `VoicePicker`-backed control per speaker.
- **FR-3.5 (prerequisites are explained):** Add `disabledReason` to every gated control (currently 2 of 41), with Settings deep-links for the LLM server (`SettingsModels.qml:914`) and ASR device/model (`:512`).
- **FR-3.6 (silent no-ops):** `seekToCue` returns silently when unrendered while the row shows a pointing-hand cursor; `play()` silently starts a full render; several AI slots `return False` with no error text. Each must surface feedback.
- **FR-3.7 (error surface):** The banner is never dismissible despite `AppNotice` supporting `actionText`, and conflates warnings with errors (a diarization heuristic-fallback note renders as a red "Cần chú ý" error). Add a dismiss action, separate warning/error channels, and clear the banner on success.

### FR-4: Craft and design-system conformance (Tier 3 / P3)

- **FR-4.1 (contrast — WCAG failure):** `Theme.warning` used as *text* at `SubtitleCard.qml:577` measures **3.19:1 in light theme** (10.4:1 dark) — fails WCAG AA 4.5:1 for 12 px text. Use `Theme.warningText` (already correct at `:321`, `:338`).
- **FR-4.2 (component conformance):** Adopt the established components instead of re-implementing them:
  - three hand-rolled `Rectangle` panels → `AppCard { elevation: 0 }` as `StudioTab.qml:734-745` does;
  - two unstyled `ProgressBar`s (the only ones in the app) → the shared 6 px accent skin (`TransportDock.qml:502-530`) + `Accessible.name`;
  - three hand-rolled badges → `StatusBadge`; zero `SectionLabel` → use it;
  - three inline `TextField` skins → add `AppTextField` to the component library;
  - hardcoded sizes (`implicitHeight: 32`, `preferredWidth: 110/160`, `comboWidth: 210/165`, `404/424`) → Theme tokens.
- **FR-4.3 (single primary):** Two `primary` buttons can be visible at once (cue Save `:666` + Render `:1106`), violating `AppButton.qml:15-16`.
- **FR-4.4 (accessibility):** 13 focusable nodes for 26+ controls; the cue list has `focusPolicy 0` so keyboard users cannot reach a single cue; 3 `AppNumberField`s and the slider report `Accessible.name == ""`; the 3 `AppCombo`s announce only their value; tooltips cover 2 of 39 controls; there is no focus-follow scrolling.
- **FR-4.5 (off-thread binding):** `videoOverrunMs` runs two synchronous `ffprobe` subprocesses on the GUI thread from a `visible:`/`text:` binding (`:651-666`); cache it or move it off the binding path.
- **FR-4.6 (i18n):** Add the 2 missing `qsTr` strings (empty-state hint `:124`, AVI soft-subtitle warning `:319`) and `qsTr()` the two `FileDialog` name filters (`:67`, `:80`).
- **FR-4.7 (dead surface):** Wire or remove unused controller surface — karaoke properties (`activeCharStart`/`activeCharEnd`/`positionMs`/`durationMs`, fully implemented and unbound), `clamp_to_video` (dead code with no callers), deprecated `AppCard.subtitle` (`:31`), and the 6 properties with zero QML references.

---

## Non-Functional Requirements

- **NFR-1 (test contract preserved):** The existing 26 pinned `objectName`s in `tests/smoke/test_ui_tabs.py`, `test_ui_shell.py`, `test_ui_studio.py` must keep working; renaming any of them is a breaking change requiring a coordinated test update in the same task.
- **NFR-2 (coverage gap closed):** 37 of 63 `objectName`s are currently unpinned and no subtitle action button is ever *clicked* except `subtitleStudioButton`. Every new/rewired action must gain a test that clicks it and asserts the controller call.
- **NFR-3 (gates):** `ruff check .`, `ruff format --check .`, `pytest` green before every commit; run `scripts/update_i18n.sh` for any user-facing string change.
- **NFR-4 (responsiveness):** The page must be usable at 640×420, 760×600, 980×700, 1120×740 and 1440×900 in both themes, with the primary action reachable at every size.
- **NFR-5 (no regressions):** The two-policy `dub`/`transcript` model, `statsSummary` reporting, per-cue overflow/fit badges, transition-only cue-follow scrolling, and the rate-slider drag/keyboard debounce are all correct today and must be preserved.
- **NFR-6 (performance):** No new synchronous GUI-thread work on bindings; `SubtitleProjectStore.save()` currently blocks the GUI thread on every knob change and must not gain new call sites.

---

## Acceptance Criteria

- **AC-1:** A completed render cannot be lost without an explicit confirmation naming the loss. Verified by driving each of the six policy knobs and the three cue-mutating actions against a rendered project.
- **AC-2:** A stale render is visibly indicated (not silently absent), and the export actions explain their state.
- **AC-3:** A project can be closed and reopened from the UI; stale workspaces can be deleted.
- **AC-4:** An in-flight export can be cancelled and the tab returns to a usable state.
- **AC-5:** The primary render action is visible without scrolling at 640×420, 760×600, 980×700, 1120×740 and 1440×900.
- **AC-6:** A single derived `state` drives control gating; at most one progress indicator is active at a time and it identifies its owner.
- **AC-7:** The empty state offers both entry paths with prerequisites stated, and accepts drag-and-drop.
- **AC-8:** The speaker voice field is usable (≥120 px) at 640×420; the speaker row does not overflow; the cue list has a working scrollbar and grows with available height.
- **AC-9:** The AI actions state their order and scope; "Dịch lồng tiếng" has a target-language control on this tab.
- **AC-10:** `Theme.warning` is not used as text anywhere; `tests/unit/test_theme.py`-style contrast measurement confirms ≥4.5:1 for every text token in both themes.
- **AC-11:** Only one `primary` button is visible per screen state.
- **AC-12:** The cue list is keyboard-reachable and editable; every form control has a non-empty accessible name; tooltips cover every AI/export/render action.
- **AC-13:** `conductor/patterns.md:96` reflects the new dock; `product-guidelines.md` i18n claim corrected.
- **AC-14:** Full gate green; subtitle suites (`pytest -k "subtitle or srt"`) green; no previously-passing test regressed.

---

## Out of Scope

- **Incremental per-cue re-rendering** (re-synthesizing only changed cues and splicing into `track.wav`). Considered and deferred: it is the best long-term UX but a large, risky change to `SubtitleTrackRenderer`/`splice_studio_window_into_subtitle`. This track ships confirm-before-destroy instead.
- **A video preview panel** (`VideoOutput` + transport). Desirable (the tab dubs video the user cannot see) but independent of the safety/structure work; file as a follow-up bead.
- **Karaoke word-highlight UI.** The controller already computes `activeCharStart`/`activeCharEnd`/`positionMs`; this track only wires a basic scrubber/time readout if it falls out of the dock work, and defers per-word highlighting.
- **LLM batching/retry hardening** (`llm_refine.py:196-222` discards batches on failure). Real, but a core concern outside this UI track.
- **Burn-in ASS restyling / `clamp_to_video` behaviour.** `clamp_to_video` is dead code; wiring it is a product decision, tracked separately.
- **Repo-wide `FileDialog` filter i18n** — only the two in this tab are in scope.
- **Replacing the Create `TransportDock`** or any other destination's dock.
