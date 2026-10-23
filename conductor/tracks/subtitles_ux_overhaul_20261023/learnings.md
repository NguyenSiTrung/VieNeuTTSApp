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

