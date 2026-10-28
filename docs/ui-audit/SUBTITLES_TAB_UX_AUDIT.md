# UI/UX Audit — "Lồng tiếng & Phụ đề" (Dubbing & Subtitles)

**Verdict: YES — this tab needs a redesign.** It is the only top-level destination that never received the 2026-10-10 shell redesign treatment, and it is now structurally the weakest page in the app.

> **Status: resolved 2026-10-23 / 2026-10-28** by tracks `subtitles_ux_overhaul_20261023`
> (commits `da92163`, `601f981`, `6e22bb1`, `0d3f935` and the phase-5 commits) and
> `video_preview_panel_20261028` (`VieNeuTTSApp-t80a`).
> Every finding below is now addressed; the audit is kept as the historical
> record of the pre-redesign surface.
>
> | Finding | Resolution |
> |---|---|
> | 0.1 policy knobs invalidate a render | Staged invalidation gate: setters queue the change, the card names the loss (track/retimed SRT) and only `confirmRenderInvalidation()` applies it; rejection snaps back. |
> | 0.2 cue edit destroys track + retimed SRT | Same gate for `updateCue`/diarization/LLM; the dialog names both artifacts. |
> | 0.3 re-ASR overwrites the managed SRT | Same gate before `transcribeVideo`. |
> | 0.4 no project lifecycle | `Projects` dialog: list/open/delete workspaces, `New project` closes via `clear()`, stale workspaces deletable; project id stays path-safe. |
> | 0.5 no export cancel / transcript gap cap | New `cancelExport` slot + dock cancel; transcript default `max_gap_ms=1500`. |
> | 1 root cause (mode promoted, page never designed) | `SubtitlesTab` is a real destination: page header, stage stepper, pinned dock, empty launcher. |
> | 2.1 primary buried | `SubtitleActionDock` outside the `PageShell`; visible at 640×420 (asserted). |
> | 2.2 grid breaks 760–1080 | Window-keyed `wideLayout` (≥900), wrapping speaker `Flow`, page max width 960. |
> | 2.2a speaker field 0 px | Replaced by the catalog `VoicePicker` (≥120 px at 640×420, asserted). |
> | 2.3 blank empty state | Two explicit entry cards + drag-and-drop, prerequisites stated. |
> | 2.4 four stacked toolbars | Source/Prepare/Produce stages show one toolbar group each. |
> | 3.1 implicit pipeline | Ordered AI group labelled "theo thứ tự" with step tooltips. |
> | 3.2 silent prerequisites | Named prerequisite row with a deep-link to Settings › Models. |
> | 3.3 duplicate diarization | One affordance per stage (speaker bar), deduplicated and asserted. |
> | 3.4 raw voice IDs | `VoicePicker` per speaker; "assign current voice" shortcut. |
> | 3.5 no way to start over | `New project` + projects dialog. |
> | 3.6 no video preview | Resolved by `video_preview_panel_20261028` (`VieNeuTTSApp-t80a`): extracted `SubtitleVideoPreview.qml` with dual-mode `MediaPlayer` + `VideoOutput` (Source audio preview & Dub sync), live caption pill overlay, mini transport/scrubber, unrendered cue seeking, and Windows file-handle release (`videoReleaseRequested`). |
> | 3.7 export has no cancel/progress escape | `cancelExport` + export progress + cancel button in the dock. |
> | 4.1 five independent booleans | Controller `state` + `busyOwner`; one progress indicator per owner. |
> | 4.2 invisible behaviour | Hints channel (`hintText`), dismissible tone-aware banner, stale chip. |
> | 4.3 thin tests | Every listed action clicked with controller-call assertions; real-SRT import asserted; screenshot matrix. |
> | 4.4 design-system drift | `AppCard`/`AppProgressBar`/`AppTextField`/`StatusBadge`/`SectionLabel`; `Theme.warningText`. |
> | 4.5 accessibility | Cue list Tab stop + Up/Down seek, accessible names on every control, tooltips on AI/render/export, focus-follow scrolling. |
> | Suggested extraction | `SubtitleCard` split into Source/Prepare/Cue/Cast panels (Task 5.1). |
>
> Fresh evidence: `docs/screenshots/redesign/phase-5/subtitles-{dark,light}-{640x420,760x600,1120x740,1440x900}.png`
> (populated with six real cues). The old `phase-3/4/create-subtitles-*` grabs
> were deleted — they predate the tab's promotion and show a layout that no
> longer exists.

**But the layout is not the most serious problem.** The tab also **silently destroys completed work** — a finished render, an exported SRT, and hand-edited cues can all be lost with no confirmation and no undo. Those findings are in §0 and should be triaged before any visual work.

Evidence base: full source read of `SubtitlesTab.qml` (109 lines) + `SubtitleCard.qml` (1,289 lines) + `subtitle_controller.py` (2,321 lines) + the core layers it drives (`align.py`, `subtitle_project.py`, `video_dub.py`, `asr.py`, `llm_refine.py`, `diarization.py`), rendered screenshots at 1440×900 / 1080×800 / 760×600 / 640×420 in both themes, and comparison against `CreateTab` / `StudioTab` / `AudiobookTab`.

---

## 0. CRITICAL — silent data loss (triage before any redesign)

Four defects let the user destroy minutes-to-hours of work without a confirmation dialog. Each is verified against the source.

### 0.1 Touching any policy knob invalidates a completed render

`mode`, `rateCap`, `maxGapMs`, `offsetMs`, `mergeSentences` and `voice` all call `_rebuild_for_policy()` (`subtitle_controller.py:459, 473, 490, 507, 521, 535`). That re-derives the render fingerprint and emits `renderedChanged` (`:839-847`), so `rendered` flips to false, the stats label empties (`:413-433`), and the Export buttons disable (`SubtitleCard.qml:1183, 1201`).

There is **no confirmation and no "your export is now stale" indicator**. Worse, the rate cap is a *drag* control with a 300 ms debounce (`SubtitleCard.qml:804-809`) — a user idly dragging it after a 40-minute render has destroyed the result.

*Nuance:* moving a knob away and back **does** re-adopt a stored render if the fingerprint matches (`:824-833`), so the loss is not always permanent — but it is invisible either way, and the cache only survives while the workspace directory does.

### 0.2 Editing one cue destroys the render *and* the retimed SRT

`_update_project_cues()` (`:2230-2240`) unconditionally sets `total_ms=0, stats=None, adjusted=()`, then rebuilds. It is called by `updateCue`, `runDiarization` and `refineWithLlm`.

Because `adjusted` is what `exportSrt` writes (`:1613-1625`), **fixing a single typo re-invalidates both the audio track and the retimed subtitle file**. For a 90-minute video this is the most expensive UX failure in the tab.

### 0.3 Re-running ASR overwrites the managed SRT and discards edits

`transcribeVideo`'s `done()` unconditionally writes `_managed_srt_path(vpath)` and re-activates it (`:1918-1922`). Any `updateCue` edits, speaker→voice assignments, or LLM refinements on the current project are replaced with zero confirmation. The button stays enabled whenever `hasVideo && !busy` (`SubtitleCard.qml:186`).

### 0.4 There is no project lifecycle at all

`clear()` (`:2286`) has **zero QML callers** — there is no "close project" / "new project" action anywhere. `SubtitleProjectStore.list_projects()` (`subtitle_project.py:571`) and `remove()` (`:583`) also have zero callers, so workspaces accumulate under `<data>/subtitles/` forever with **no way to list, reopen, or delete them**.

A user who imports the wrong file can only escape by importing a different one.

### 0.5 Two supporting defects

- **No export cancel.** There is no `cancelExport` slot; `_exporting` is only cleared when the callback lands (`:2252-2265`). A hung ffmpeg leaves the tab permanently disabled.
- **Transcript mode's gap cap is disabled by a parameter mismatch.** `FitPolicy.transcript()` defaults `max_gap_ms=1500` (`align.py:132`), but the controller always passes its own `_max_gap_ms = 0` (`subtitle_controller.py:232`, `:715`) — and `align.py:76` documents `0` as **"uncapped"**. So "Bản thoại tự nhiên" reproduces every subtitle pause at full length (a 6-second dramatic pause becomes 6 seconds of silence), the exact opposite of the mode's stated purpose.

**Recommended order of work:** 0.1–0.4 are confirmation/undo/project-lifecycle gaps, not visual ones. They can be fixed without the redesign, and should be.

---

## 1. Why this tab stands out (the root cause)

The tab was **promoted from a mode to a top-level destination** but never redesigned as a page.

| Era | Structure |
|---|---|
| Before | A *mode* inside `CreateTab` (`bridge.createMode === "subtitles"`), sharing Create's header, inspector and **pinned `TransportDock`** |
| Now | Own `SubtitlesTab` → own `PageHeader` + `PageShell` → but the body is still the old `SubtitleCard` |

`CreateTab.qml:1-18` documents its own four-part architecture (header / workspace / inspector / **pinned dock**). `SubtitlesTab.qml` is 109 lines that put `SubtitleCard` inside a `PageShell` and stop. Everything the redesign gave the other pages — a pinned action bar, an inspector column, mode segmentation, a real empty state — is missing.

The 2026-10-10 redesign archive confirms the gap: `conductor/archive/ui_shell_redesign_20261010/design/` contains `Proposed-Create`, `Proposed-Studio`, `Proposed-Audiobook`, `Proposed-Settings` — **there is no `Proposed-Subtitles`.** The tab was out of scope and inherited only the generic shell.

---

## 2. The three structural defects

### 2.1 The primary action is buried and scrolls away

`CreateTab` pins its CTA in a `TransportDock` **outside** the scroll area (`CreateTab.qml:515`, documented as "so Tạo âm thanh never scrolls away (AC-3)"). `StudioTab` does the same (`studioTransportDock`). `AudiobookTab` builds an equivalent pinned player dock.

`SubtitleCard` puts **every action, including "Tạo âm thanh", inside the scrolling `PageShell` column** (`SubtitleCard.qml:1098-1243`). Measured geometry (offscreen QML tree probe, one consistent convention across all rows; ±~90 px absolute offset depending on ancestor-origin convention, which does not change any verdict):

| Window | Page scroll: content vs viewport | "Tạo âm thanh" Y | Inside the window? |
|---|---|---|---|
| 640×420 | 731 px vs 202 px (**3.6× overflow**) | 972 px | **no — 2.3× past the window** |
| 760×600 | 731 px vs 382 px (**1.9×**) | 872 px | **no** |
| 820×600 | 731 px vs 330 px (**2.2×**) | 972 px | **no** |
| 980×700 | 759 px vs 374 px (**2.0×**) | 856 px | **no** |
| 1120×740 (default) | no page overflow | 688 px | yes — but only 8 px above the 696 px status bar line |
| 1440×900 | no page overflow | 716 px | yes, comfortable |

So the primary CTA is **completely unreachable at every window size below ~1100 px wide**, and at the default 1120×740 it sits flush against the bottom edge — a single inline hint appearing above it (`subtitleOverrunHint`, `subtitleEmbedHint`) pushes it out of view.

This directly violates the project's own recorded rule:

> **Tall-tab layout rule**: a single stacked content column past the viewport buries the primary CTA behind scroll (ParagraphTab was 948 px tall against a 652 px viewport → CTA 208 px below the fold). Split mutually exclusive workflows into segmented modes and move global actions into a docked bar.
> — `conductor/patterns.md`

**At 640×420 the situation is worst:** the page content is 731 px against a 202 px viewport (**3.6× overflow**) and the CTA sits at Y=972, more than twice the window height. Screenshot `subtitles-loaded-dark-640x420.png` confirms the viewport ends mid-cue-3 with the AI toolbar consuming the top and no primary action anywhere on screen.

#### ⚠️ The current no-dock state is documented as intentional

`conductor/patterns.md:96` explicitly records it:

> "One shared bottom `TransportDock` on the CREATE destination … Studio and Audiobook own purpose-built docks (`studioTransportDock`, `playerDock`), and **SubtitlesTab keeps its actions inside `SubtitleCard`** (`subtitleRenderButton`/`subtitleExportVideoButton`/`subtitleExportWavButton`/`subtitleExportSrtButton`)."

So the dock is not an oversight — it was a deliberate scope decision during the `ui_shell_redesign_20261010` track. **This audit argues that decision should be reversed**, for three reasons:

1. **The tab is now the tallest in the app.** It was a `CreateTab` mode when the decision was made; the promotion (`aef1d87`) gave it a full page without giving it a page's chrome.
2. **The tall-tab rule's own worked example is 208 px below the fold; this tab is 702 px** at the minimum window — the rule was written for exactly this failure at 3.4× smaller magnitude.
3. Every sibling destination now has one, so the absence is no longer an isolated choice but an inconsistency.

If the redesign proceeds, `patterns.md:96` must be updated in the same change — otherwise the next agent will read the pattern file and reinstate the defect.

### 2.2 The workspace grid breaks between 760 px and 1080 px

`wideLayout: width >= 760` (`SubtitleCard.qml:41`) measures the **card's** width, not the window's — but the card is inset by the nav rail (232 px) plus Pane padding (32 px). Measured card widths:

| Window | Card width | `wideLayout` | Result |
|---|---|---|---|
| 640×420 | **544 px** | false | 1 column |
| 760×600 | **664 px** | false | 1 column |
| 820×600 | **556 px** | false | 1 column |
| 980×700 | **716 px** | false | 1 column |
| 1120×740 (default) | **856 px** | **true** | 2 columns |
| 1440×900 | **1080 px** | true | 2 columns |

So the two-column layout only appears between a 980 px and 1120 px window, and the captures at 760 px show the worst of both worlds: **a clipped single-column cue list whose settings card overflows the viewport** (`subtitles-loaded-dark-760x600.png` — the cue list is cut mid-row at cue 4 and the "Chế độ / Nén tối đa / Giọng đọc" block is sliced off at the bottom).

Note the non-monotonic widths above: the nav rail switches from compact/icon-only (~56 px) to full (~232 px) at an 800 px window (`Main.qml:90`, `:208`), so **growing the window from 760 → 820 px actually makes the card narrower (664 → 556 px)**. The two-column layout therefore does not arrive at a predictable window width — it first engages at a ~**1030 px** window, and the total content height jumps ~170 px across that 50 px band (measured: 1000 px window → 736 px card, 1 column, 924 px content; 1030 px window → 766 px card, 2 columns, 756 px content). A user resizing the window sees a card that *shrinks* in the 760–820 px range and then reflows wholesale near 1030 px, for no visible reason.

### 2.2a Horizontal overflow: the speaker voice field collapses to 0 px

At 640×420 the speaker row is a `RowLayout` (`SubtitleCard.qml:956`) whose children sum to an implicit width of **524 px inside a 492 px content box** — a 32 px overflow. The `subtitleSpeakerVoiceField` (`:995-1020`) declares `Layout.fillWidth: true` with **no `Layout.minimumWidth`**, so the layout squeezes it to **0.0 px on the first row and 3.0 px on the second**. The field is effectively invisible and unclickable at the app's documented minimum window (it measures 280 px at 1120).

This is a known trap in this codebase: `AppSlider.qml:12-22` documents exactly this failure ("a layout will happily squeeze a fillWidth item all the way to 0 px (measured at the 640 px minimum window width)") and ships a `minimumTrackWidth` handle to floor it. The `TextField` has no equivalent, and the row cannot wrap because it is a `RowLayout` rather than a `Flow`.

### 2.3 The empty state is a blank page

The first-run experience (`subtitles-empty-dark-1440x900.png`) is a title, one 1080 px-wide card, and **~700 px of pure void**. The only guidance is a single muted sentence. There is:

- no explanation of what this page does
- no distinction between the two entry paths (video→ASR vs. SRT→narrate)
- no hint that ASR needs a Whisper model, or that the AI buttons need an LLM server
- no drag-and-drop, although every sibling tab has it (`CreateTab.qml:409`, `BatchQueueCard.qml:122`, `DocumentEditorCard.qml:180`, `AudiobookTab.qml:334`)

For a user landing here, the app's most technically complex feature has the *least* onboarding of any tab.

### 2.4 Four toolbars stack before any content

Even in the populated state, a user must scroll past **four parallel control bands** before reaching the cue list: the video bar (2 rows: file + mix/embed options), the AI toolbar (4 buttons), the timing/voice panel, and then the action row. At 760×600 the cue list is squeezed to ~2.5 visible rows. At 640 px the labels break: `Nhập Video…` / `Nhập .srt…` truncate to ellipsis, `Bật/tắt trong trình phát` is clipped mid-word, and `Video: su…xture.mp4` elides in the middle.

There is also **large vertical dead space at wide sizes**: at 1440×900 the card fills the top ~72 % of the window and the bottom third is empty, while the CTA still sits flush at the card's bottom edge (§2.1). The page is simultaneously too tall and too empty — a symptom of stacking everything vertically instead of using the two-column space.

---

## 3. Workflow problems

### 3.1 The pipeline is implicit — users must guess the order

The real pipeline is:

```
import video → ASR transcribe → [diarize] → [LLM clean] → [translate] → cast voices → render → export
```

Nothing in the UI expresses this sequence. The four AI actions sit in a flat `Flow` row (`SubtitleCard.qml:347-415`) with no numbering, no dependency indication, and no disabled-with-reason explanation. A first-time user cannot tell whether they need "AI Làm sạch phụ đề" before "AI Dịch lồng tiếng", or what "AI Rút gọn khớp thời gian" does relative to the "Nén tối đa" slider.

**Only 2 of 41 controls use `disabledReason`** (`SubtitleCard.qml:1113`, `1145`) — the other 39 go grey with no explanation of what prerequisite is missing. `AppButton` fully supports explaining itself (`AppButton.qml:253-260`), it just isn't used here.

### 3.2 Silent prerequisites

The AI buttons require an LLM server, configured in **Settings → Models → "AI Phụ đề & Lồng tiếng Video"** (`SettingsModels.qml:371`, `llmBaseUrlField` at `:914`). The Subtitles tab never references this. A user with no Ollama/LM Studio running clicks "AI Dịch lồng tiếng" and gets an opaque failure instead of "Configure an LLM server in Settings" plus a jump link.

Same for ASR: device/threads/model live in Settings (`asrDeviceCombo` at `SettingsModels.qml:512`), invisible from the tab where ASR is actually invoked.

### 3.3 Duplicate diarization affordances

"Phân tách người nói" appears **twice** with different semantics:

- `subtitleDiarizeToggle` (`:171`) — a toggle inside the video bar, read only at ASR time
- `subtitleDiarizeButton` (`:356`) — a button that re-runs diarization on an existing project

Two identical labels doing different things, ~185 lines apart. Users will reasonably assume one is a duplicate or that they're linked.

### 3.4 Speaker casting requires typing a raw voice ID

`subtitleSpeakerVoiceField` (`:995-1020`) is a plain `TextField` with placeholder *"ID giọng đọc (để trống = giọng mặc định)"* — the user must **know and type an internal voice identifier**. Meanwhile the same file already uses the rich `VoicePicker` (`:889`) for the global voice.

Sibling work buttons partially compensate ("Gán giọng đang chọn", "Lấy mẫu giọng từ video"), but the primary affordance is a raw ID field — a debugging control shipped as UI.

### 3.5 No way to start over

`SubtitleController.clear()` exists (`subtitle_controller.py:2287`) but **is never wired to any control**. Once a project is loaded there is no "close project" / "new project" action — only "Bỏ đính kèm video" (`clearVideo`), which detaches the video but keeps the cues. Users cannot return to the empty state.

### 3.6 No video preview in a video-dubbing tab

`SubtitleCard.qml` contains **no `VideoOutput` / `MediaPlayer` / frame preview** — confirmed by grep. The user is dubbing a video they cannot see. The cue list is the only spatial reference, and cue timings are shown as text (`00:00:01,000 → 00:00:04,200`) with no timeline or waveform.

For contrast, `StudioTab` has a full timeline + waveform (`StudioTimeline.qml`, `studioWaveform`), and `AudiobookTab` has a player dock.

### 3.7 Export has no cancel and no progress escape

`exportVideo()` sets `_exporting` and reports progress, but **there is no `cancelExport` slot** in the controller (the slot list contains only `cancelRender` and `cancelAiTask`). Burn-in re-encodes every frame, so the user waits — with an indeterminate bar once progress is 0 — and can only quit the app. The code comment at `:1065-1066` even acknowledges the "app just looked frozen for minutes" problem it never solved.

---

## 4. State & gating inconsistencies (bug-adjacent)

These are correctness issues surfaced by the audit:

| # | Issue | Evidence |
|---|---|---|
| 1 | **`render()` does not guard `aiBusy`.** The button is gated on `!busy && !exporting` only, and `render()` checks `project is None or self._rendering` only — so a render can be started while an ASR/LLM task is running. | `SubtitleCard.qml:1111`, `subtitle_controller.py:1089` |
| 2 | **`exportTrack`/`exportSrt`/`exportVideo` also omit `aiBusy`**, while the *video* export button does check it (`:1167`). Gating is inconsistent within the same button row. | `:1183`, `:1201`, `:1167` |
| 3 | **Error banner is never dismissible.** `AppNotice` supports `actionText` + `actionTriggered` (`AppNotice.qml:15-17`), but the banner uses neither — the only way to clear an error is to trigger a new action that calls `_set_error("")`. | `SubtitlesTab.qml:83-95` |
| 4 | **The banner conflates two error sources** with a `tone` decided by which one is non-empty — a subtitle warning ("Không phân tách được người nói…") renders under the title "Cần chú ý" while a synthesis error renders "Không thể tạo âm thanh". Warnings raised after a *successful* ASR (`subtitle_controller.py:1919`) appear as an error banner. | `SubtitlesTab.qml:87-94` |
| 5 | **`takesLanguage` at `SubtitleCard.qml:902` is an unqualified implicit lookup** that only resolves because `LanguagePicker` happens to declare it. It works by accident; a rename in `LanguagePicker` silently breaks it. | `:902` vs `LanguagePicker.qml:41` |
| 6 | **Two strings missing from `vienetts_en.ts`** — the empty-state hint and the AVI soft-subtitle warning are the only 2 of 59 untranslated strings in the file. | `:124`, `:319` |

### 4.1 There is no state model — only five independent booleans

The tab's real state is the **cross-product of five independent axes**, each surfaced to QML as its own flag (`SubtitleCard.qml:34-40`):

```
project   : none ──import/ASR──> loaded
video     : none ──import/attach──> attached ──clearVideo──> none
render    : unrendered ──render()──> rendering ──> rendered | error
ai        : idle ──transcribe|diarize|refine|clone──> aiBusy ──> idle | error
export    : idle ──export*──> exporting ──> done | error
```

Because there is **no derived `state` property**, every one of the 41 controls re-derives its own boolean. That is the structural reason the gating in the table above is inconsistent: there is no single place that can be correct.

It also produces two contradictory signals at once — the card renders **two separate progress bars**, one for AI (`:431-437`) and one for render/export (`:1060-1073`), which can animate simultaneously with nothing indicating which one owns the engine. And during a *cache-hit* `render()` (`subtitle_controller.py:1118-1129`, which returns before `_rendering` is ever set) **neither bar appears**, so the primary button looks broken.

A single `state` enum (plus a `busyOwner` for the engine) would collapse most of §4 into one correct implementation.

### 4.2 Behaviour the user cannot see

Beyond the state model, a significant amount of consequential logic is invisible from the UI:

| Behaviour | Where | Why it matters |
|---|---|---|
| **`play()` silently starts a full render** if no track is cached — the button merely hints at it ("Tạo và phát"). | `subtitle_controller.py:1459-1471`, `SubtitleCard.qml:1137` | A 40-minute render can start from a play click. |
| **Clicking a cue before rendering does nothing, silently** — `seekToCue` returns early when there is no timeline, but the row has a pointing-hand cursor inviting the click. | `:1516-1520`, `SubtitleCard.qml:486-493` | Reads as a broken app during cue review. |
| **Default export embeds subtitles** (`embed_subtitles=True`, mode `soft`). | `subtitle_project.py:275, 278` | Every first export ships subtitles the user never opted into. |
| **Default mix is `replace`** — original audio destroyed, not ducked. | `subtitle_project.py:273` | Destructive default on a "dub" workflow. |
| **AVI soft subtitles are silently skipped** — `soft_codec` is `None`, so the mux ships **no subtitle stream at all** while reporting success. | `video_dub.py:62-68, 689-691` | Silent wrong output; only warned when the video is attached. |
| **Switching to transcript mode force-sets `mergeSentences = true`** and never restores it. | `subtitle_controller.py:453-456` | An unrelated checkbox changes by itself. |
| **ASR auto-fallback chain** (`whisper.cpp` → `faster-whisper` → `openai_api`) reorders by what is installed; the `asrBackend` setting is ignored when `"auto"`. | `asr.py:772-838` | Users cannot predict which engine ran. |
| **Diarization silently degrades to a NumPy acoustic clusterer** when the Nemotron GGUF is absent, surfacing only as a note pushed through `errorText`. | `diarization.py:925-943`, `subtitle_controller.py:1903` | A quality caveat renders as a **red error** (§4 table #4). |
| **Project id is content-derived** (`name + spoken text`), so editing any cue creates a *new* workspace directory and orphans the previous render on disk forever. | `subtitle_project.py:233-237` | Compounds 0.2 — the old track is not even recoverable. |
| **`clamp_to_video` is dead code** — the muxer supports trimming the dub to the picture, but no caller passes it, so an overrunning dub always produces a frozen tail. | `video_dub.py:651, 779` (no external callers) | The fix exists and is unreachable. |
| **Karaoke word-highlighting is fully implemented and entirely unbound** — `activeCharStart`/`activeCharEnd`/`positionMs`/`durationMs` have **zero** QML references on this tab. | `subtitle_controller.py:388-394, 439-441, 1539-1571` | The tab can play audio but has no scrubber, no time readout, and no per-word highlight. |
| **`videoOverrunMs` runs two synchronous `ffprobe` subprocesses on the GUI thread** every time it is read — and it is bound in a `visible:` and a `text:` expression, re-evaluated on every `videoChanged`. | `subtitle_controller.py:651-666`, `video_dub.py:340-357`, `SubtitleCard.qml:335, 337` | A GUI-thread stall hidden inside a binding. |
| **`SubtitleProjectStore.save()` is synchronous on the GUI thread** on every knob change, with up to 4 retries × 250 ms on a locked file. | `subtitle_controller.py:584, 605, 622, 641, 2018, 2242`, `subtitle_project.py:84-87, 367-374` | Up to ~1 s of frozen UI per slider tick. |
| **LLM refinement is lossy on failure** — batches 1..n−1 are discarded if any later batch fails; no retry, no backoff, fixed 25-cue batches. | `llm_refine.py:184, 196-222` | Minutes of work lost to one transient HTTP error. |
| **`exportSrt` requires a render.** A user who only wants their SRT re-timed (`offsetMs`) must first synthesize the entire file. | `subtitle_controller.py:1623-1625`, `SubtitleCard.qml:1201` | Forces the most expensive step for the cheapest task. |


### 4.3 Test coverage is much thinner than it looks

`SubtitleCard.qml` declares **58 `objectName`s** (63 across the tab + card), but only **26 are referenced anywhere in `tests/`** — leaving **37 unpinned**. The card-level names with no test at all include nearly every control added for the AI/video workflow:

`subtitleTranscribeButton`, `subtitleDiarizeToggle`, `subtitleDiarizeButton`, `subtitleRefineAsrButton`, `subtitleFitTimingButton`, `subtitleTranslateDubButton`, `subtitleCancelAiButton`, `subtitleAiStageLabel`, `subtitleAiProgressBar`, `subtitleVideoMixCombo`, `subtitleVideoBgGainField`, `subtitleEmbedSrtToggle`, `subtitleOverrunHint`, `subtitleClearVideoButton`, `subtitleSpeakerRow`, `subtitleSpeakerVoiceField`, `subtitleSpeakerSampleButton`, `subtitleUseCurrentVoiceForSpeakerButton`, `subtitleCloneSpeakerButton`, `subtitleCueEditField`, `subtitleCueEditSpeakerField`, `subtitleCueSaveEditButton`, `subtitleCueSpeakerBadge`, `subtitleCueFitBadge`, `subtitleProgressBar`, `subtitleExportProgressLabel`, `subtitleExportSpeakersToggle`, `subtitleStatsLabel`, `subtitleStatusLabel`, `subtitleVideoLabel`, `subtitleImportDialog`, `subtitleVideoDialog`, `subtitleCancelButton`, `subtitleMaxGapField` — plus the page-level `subtitlesPageHeader`, `subtitlesWorkspace` and `subtitleErrorBanner`.

The smoke suite pins existence and a few behaviors for the *original SRT-era* controls (mode combo, rate slider, offset, merge, voice picker, render/play/export buttons, cue list). **The entire ASR → diarization → LLM → multi-speaker-casting half of the feature set has no automated UI test at all** — which is consistent with the gating inconsistencies in §4 having gone unnoticed.

Three further coverage gaps worth naming:

- **No subtitle action button is ever *clicked*** except `subtitleStudioButton`. `render`, `play`, `exportWav`, `exportSrt`, `exportVideo`, `transcribeVideo`, `runDiarization`, `updateCue`, `setSpeakerVoice`, `playSpeakerSample`, `cloneSpeakerFromVideo`, `refineWithLlm` and `cancelAiTask` are stubbed in `FakeSubtitle` and never invoked from QML (they are covered at controller level in `tests/unit/test_subtitle_controller.py`).
- **No test loads an SRT into the real controller and then asserts UI state**; the shell suite scans this tab only in its empty state.
- **The `wideLayout` 760 px breakpoint is never asserted**, and the screenshot matrix only renders 1120×740 and 640×420 — so the 760–1040 px band where the layout is worst (§2.2) is untested.

Any redesign should add coverage for these while refactoring, since the existing contract is the only thing preventing regressions in the tested half.

### 4.4 Design-system violations (all verified against sibling components)

`SubtitleCard.qml` is the only file in the app that re-implements the design system instead of consuming it. Each item below has an established, used-elsewhere fix:

| # | Violation | Evidence | Fix (already exists) |
|---|---|---|---|
| 1 | **Two unstyled `ProgressBar`s — the only ones in the app.** Neither sets `background`, `contentItem`, or `Accessible.name`, so both render as stock Qt Basic bars inside a themed card. | `:431-437`, `:1060-1073` | The shared 6 px accent skin used at `TransportDock.qml:502-530`, `AudiobookTab.qml:762-790`, `BatchQueueCard.qml:256` |
| 2 | **Three hand-rolled panels** re-implementing `radiusMd` + `surfaceAlt` + 1 px `borderSubtle`. | `:134`, `:694`, `:923` | `AppCard { elevation: 0; cardColor: Theme.surfaceAlt; cardBorderColor: Theme.borderSubtle; cardRadius: Theme.radiusMd }` — exactly what `StudioTab.qml:734-745` does |
| 3 | **Three inline `TextField` skins, two of them inconsistent** — only `:995` sets `selectedTextColor`/`selectionColor`, so one file ships two skins for one control. | `:617-634`, `:642-661`, `:995-1015` | No `AppTextField` exists in the library — a genuine gap worth adding |
| 4 | **Zero `SectionLabel` in 1,289 lines**; the only visible section title is a plain 12 px `textMuted` label. | `:945` | `SectionLabel` — 4 uses in `StudioEffectsPanel.qml`, 1 in `ComposeEditorCard.qml` |
| 5 | **Three hand-rolled badges** instead of `StatusBadge`. | `:533-551`, `:553-582`, `:964-980` | `StatusBadge` — used in `AudiobookTab`, `BatchQueueCard`, `DocumentEditorCard` |
| 6 | **Contrast failure: `Theme.warning` used as *text*.** Measured **3.19:1 in light theme** (10.4:1 dark) — fails WCAG AA 4.5:1 for 12 px text. | `:577` | `Theme.warningText` (`Theme.qml:76`), which the same file already uses correctly at `:321` and `:338` |
| 7 | **Hardcoded sizes bypassing Theme tokens**: `implicitHeight: 32` (should be `Theme.controlHeightSm`, which neighbouring fields at `:622`/`:647` do use), `preferredWidth: 110`/`160`, `minimumHeight: 140`/`preferredHeight: 160 : 196`, `404`/`424`, `implicitHeight: 18`, margins `3`, `comboWidth: 210`/`165`. | `:998`, `:646`, `:433`, `:456-457`, `:696-697`, `:539`/`:565`, `:501-502`, `:225`/`:291`/`:729` | Theme tokens; Studio uses a named `panelWidth` property (`StudioTab.qml:54`) instead of literals |
| 8 | **Border hierarchy inverted** — nested panels use `borderSubtle` (1.01:1 dark vs `surfaceAlt`) while the outer card uses `border` (1.15:1), so sub-panels are *less* separated than their container. | `:144`, `:703`, `:933` | — |

Two structural containers also lack `objectName` (`:441` workspace grid, `:694` settings column, `:1098` action flow), so tests cannot address them — the file's own header comment (`:18-25`) promises a tested contract.

**Checked and clean:** no hardcoded `#` colour literals anywhere in the file; every *interactive* control carries an `objectName`; all other spacing/radius/typography comes from `Theme`; the empty state, `AppNotice` error banner (`SubtitlesTab.qml:83-95`), Escape shortcut (`:35-56`) and active-cue follow-scroll (`:682-690`) are all correct and consistent with the shell.

### 4.5 Accessibility

| Finding | Evidence |
|---|---|
| **13 focusable nodes for 26+ visible controls.** The cue list has `focusPolicy 0` and no `Accessible.role`/`name`, so a keyboard user **cannot reach, select, or edit a single cue** — only the row `MouseArea` (`:486`) seeks, and only by mouse. | `:450`, `:486` |
| **The 3 `AppNumberField`s report `Accessible.name == ""`** while their labels are separate `Label`s, so they are announced as unlabeled spin boxes. | `:250`/`:829`/`:857` vs `:244`/`:822`/`:850` |
| **`AppSlider` also has `Accessible.name == ""`**; its label at `:762` is not associated. | `:769` |
| **The 3 `AppCombo`s announce only the current *value*** ("Lồng tiếng (theo SRT)"), never the field label ("Chế độ:"), because `accessibleLabel` defaults to `displayText`. `VoicePicker` avoids this via `fieldLabel`. | `:723`/`:222`/`:288` vs `:716` |
| **Tooltips on 2 of 39 controls.** Zero on the four AI actions, the three export buttons, the render button (no `Ctrl+Enter` hint, unlike `TransportDock.qml:299`), or Studio. | `:197`, `:604` only |
| **No focus-follow scrolling** — focusing a control below the fold leaves `contentY` at 0, so keyboard users tab into controls they cannot see. | measured |

On **i18n**, the audit found the code is in better shape than the guidelines imply: coverage for this file is complete (57 `qsTr` vs 58 catalog messages, 0 unfinished, 0 vanished; the Python side has 44 translated messages). Two gaps remain: the `FileDialog` name filters at `:67` and `:80` are bare literals (a repo-wide pattern also present in `BatchQueueCard:54`, `CreateTab:228`, `AudiobookTab:198`, `TransportDock:192`), and `qsTr("Dài")` → "Long" (`:575`) is ambiguous beside its siblings "+320 ms" and "1.08×".

Note also that `product-guidelines.md:11-12` still claims "English-first in v1, Vietnamese is a follow-up" — the shipped reality is the inverse (QML source is Vietnamese, English ships as a catalog, `resolve_language()` defaults to `vi`). **The guideline is stale and should be corrected.**

---

## 5. What is actually good (preserve these)

The audit should not throw away the solid parts:

- **The two-policy model is genuinely well designed.** `dub` (SRT clock is master, rate-compress then push cues) vs `transcript` (voice is master, cap pauses) is a real insight, and `statsSummary` (`subtitle_controller.py:414-433`) reports "N phụ đề · N nén (tối đa 1.5×) · N tràn" — excellent, honest feedback.
- **Per-cue badges are informative.** `subtitleCueSpeakerBadge` and `subtitleCueFitBadge` (`:533-582`) surface overflow (+ms), fit ratio (×) and over-budget state right on the row.
- **Inline cue editing works** and writes through `updateCue` (`:617-675`).
- **Active-cue follow during playback** is correctly implemented with transition-only scrolling (`:682-690`) so the user's scroll is never yanked.
- **The rate slider debounce** (`:782-809`) correctly distinguishes drag from keyboard stepping.
- **Theming is clean** — the light-theme capture is genuinely well executed; no hardcoded colors, all `Theme.*` tokens.
- **Export-format awareness** — the WAV button relabels to MP3 based on `controller.exportFormat` (`:1181`).

---

## 6. Recommended redesign

### Direction: promote the pipeline to a **guided stepper** with a pinned action dock

The page has three genuinely distinct phases that the current UI interleaves into one scroll:

**Phase A — Source** → import video/SRT, transcribe, diarize
**Phase B — Prepare** → clean, translate, fit timing, edit cues, cast voices
**Phase C — Produce** → render, preview, export

These are mutually exclusive in practice: you cannot cast voices before cues exist, and you cannot render before casting. That makes them ideal `AppSegmented` steps — the component already exists and is used for exactly this purpose in `CreateTab` (`createModeSwitch`) and `VoicesTab`, and is used **zero** times in `SubtitleCard`.

### Concrete changes, ordered by impact

**Tier 0 — data safety (do first, independent of the redesign)**

| # | Change | Fixes |
|---|---|---|
| 1 | **Decouple render from cue editing.** Either keep the track when cues change (render is per-unit, so it can be reconciled) or show an explicit "Cue changes will discard the rendered audio and retimed SRT — re-render?" confirmation. At minimum add a stale badge instead of silently emptying the stats label. | 0.2, 0.1 |
| 2 | **Confirm before destroying a render** when a policy knob changes after a successful render; treat the rate slider's release (not each debounce tick) as the confirmation point. | 0.1 |
| 3 | **Confirm before re-running ASR** when the current project has user edits (cue text, speaker voices, or LLM passes). | 0.3 |
| 4 | **Wire project lifecycle**: `clear()` → "Dự án mới", plus a project picker backed by `list_projects()`/`remove()` so workspaces can be reopened and deleted. | 0.4 |
| 5 | **Add `cancelExport()`** to the controller and a Cancel button on the export progress row. | 0.5 |
| 6 | **Fix the transcript gap-cap mismatch** — pass a real default (`1500`) instead of the controller's `0`. | 0.5 |

**Tier 1 — structure (the redesign proper)**

| # | Change | Fixes |
|---|---|---|
| 7 | **Add a pinned dock** below the `PageShell` (mirror `CreateTab.qml:515`): primary "Tạo âm thanh", play, export menu, render/export progress + cancel. Never scrolls. | 2.1, 3.7 |
| 8 | **Replace the five booleans with one derived `state`** (+ a `busyOwner` for the engine), and let the UI read that instead of re-deriving per control. | 4.1 |
| 9 | **Segment the body into Source / Prepare / Produce** with `AppSegmented`; keep the cue list visible in Prepare and Produce. | 3.1, 2.1 |
| 10 | **Rebuild the empty state** as a two-choice launcher: "Video → phụ đề tự động" vs "Tệp .srt → lồng tiếng", each a large `AppCard` stating its prerequisites (Whisper model / LLM server) with a Settings jump. | 2.3, 3.2 |
| 11 | **Replace the speaker voice `TextField` with a compact `VoicePicker`** per speaker row (or a "Chọn giọng…" button opening the existing picker). | 3.4 |
| 12 | **Add `disabledReason` to every gated control**, with Settings deep-links for LLM/ASR prerequisites. | 3.1, 3.2 |

**Tier 2 — craft**

| # | Change | Fixes |
|---|---|---|
| 13 | **Add a video preview panel** (`VideoOutput` + transport) in Source/Produce, or at minimum a waveform/timeline strip synced to the cue list. | 3.6 |
| 14 | **Bind the existing karaoke engine** — `positionMs`/`durationMs` → a scrubber, `activeCharStart`/`activeCharEnd` → per-word highlight. The code is already written and unused. | 4.2 |
| 15 | **Merge the two diarization controls** into one, or clearly relabel them ("Phân tách khi tạo phụ đề" vs "Phân tách lại"). | 3.3 |
| 16 | **Fix `wideLayout`** to key off the page width with a corrected breakpoint (~900 px card width), and set an explicit `Layout.minimumHeight` on the cue list so it never collapses below 3 rows. | 2.2 |
| 17 | **Make the error banner dismissible** (`actionText="Đóng"`), split warning vs error tones, and auto-dismiss success. | §4 #3, #4 |
| 18 | **Gate render/export on `!aiBusy`** in both QML and controller. | §4 #1, #2 |
| 19 | **Move `videoOverrunMs` off the GUI thread** (cache it on `videoChanged`, or compute it in the export path). | §4.2 |
| 20 | **Add drag-and-drop** for video/SRT files, matching every sibling tab. | 2.3 |
| 21 | **Fix the 2 missing translation strings.** | §4 #6 |

**Tier 3 — design-system conformance (cheap, mechanical, high polish-per-hour)**

| # | Change | Fixes |
|---|---|---|
| 22 | **Fix the contrast failure** — `Theme.warning` → `Theme.warningText` on the fit badge. One-line change, WCAG AA failure. | §4.4 #6 |
| 23 | **Give the cue list a scrollbar** — declare `ScrollBar.vertical`, delete the dead `cueList.ScrollBar.vertical` reference at `:479`, and let `fillHeight` beat the 160 px cap so the list isn't a 5-row keyhole. | §4.4, §2.2 |
| 24 | **Fix the speaker-field collapse** — `Layout.minimumWidth: 120` (mirror `AppSlider.minimumTrackWidth`) and convert the speaker row to a `Flow` so it wraps at 640 px. | §2.2a |
| 25 | **Make the cue Save button `secondary`** so only one `primary` is visible at a time. | §4.4 |
| 26 | **Adopt `AppCard { elevation: 0 }`** for the three hand-rolled panels; one skin change then propagates. | §4.4 #2 |
| 27 | **Apply the shared 6 px accent ProgressBar skin** + `Accessible.name` to both bars. | §4.4 #1 |
| 28 | **Add `AppTextField`** to the component library (replacing 3 inline skins) and use `Theme.controlHeightSm` instead of the hardcoded `32`. | §4.4 #3 |
| 29 | **Use `SectionLabel`** for the one section heading; use `StatusBadge` for the three hand-rolled badges. | §4.4 #4, #5 |
| 30 | **Accessibility pass** — make the cue list focusable with an `Accessible.role`, label the 3 number fields + slider, fix the 3 combos' accessible names, add focus-follow scrolling. | §4.5 |
| 31 | **Add tooltips** to the 4 AI actions, 3 exports, render (`Ctrl+Enter`), and Studio. | §4.5 |
| 32 | **Add a `DropArea`** to the empty state for .srt/video. | §2.3 |
| 33 | **Correct `product-guidelines.md:11-12`** — the shipped i18n reality is Vietnamese-source with an English catalog, the inverse of what the guideline claims. | §4.5 |

### Suggested component extraction

`SubtitleCard.qml` at 1,289 lines with 41 controls is beyond the point where the project's own "keep reusable pieces in `components/`, declare explicit rows" rule (`conductor/patterns.md`) is being honoured. Split into:

- `SubtitleSourcePanel.qml` — video bar, ASR, diarization
- `SubtitleCueEditor.qml` — the cue list + inline edit (already self-contained)
- `SubtitlePreparePanel.qml` — AI toolbar, mode/rate/offset, language
- `SubtitleCastPanel.qml` — speaker→voice mapping
- `SubtitleActionDock.qml` — the new pinned dock

This also makes each piece independently testable against the existing objectName contract.

---

## 7. Effort estimate

| Phase | Scope | Estimate |
|---|---|---|
| **P0** | **Data safety: render/cue decoupling, destroy-confirmations, project lifecycle, `cancelExport`, gap-cap fix** | **3–4 days** |
| P1 | Structural: pinned dock, derived `state`, segmented steps, empty-state launcher | 2–3 days |
| P2 | Workflow: `disabledReason` everywhere, merge diarize controls, LLM/ASR Settings links | 1–2 days |
| P3 | Craft: voice picker per speaker, video preview, karaoke binding, banner fixes, i18n | 2–3 days |
| P3b | Design-system conformance (Tier 3, items 22–33) — mostly mechanical | 1–1.5 days |
| P4 | Component extraction + test updates | 1–2 days |

Roughly **2.5–3 weeks** of focused work.

Three notes on sequencing:

- **P0 is independent of the redesign** and should ship first. It is the only phase where delay actively costs users work, so it must not be batched behind visual changes.
- **Item 22 (the contrast fix) is a one-line change that should not wait for anything.** It is the only WCAG failure found.
- P1 alone delivers ~70 % of the *perceived* improvement; P0 delivers most of the *actual* improvement; P3b is where the "feels finished" polish comes from, at the lowest risk per hour.

---

## 8. Screenshot references

All captures are offscreen renders via `scripts/generate_screenshots.py`'s fake-controller harness, written to `docs/ui-audit/`:

- `subtitles-empty-dark-1440x900.png` — empty state, ~700 px void
- `subtitles-loaded-dark-1440x900.png` — populated, CTA at bottom edge
- `subtitles-loaded-dark-760x600.png` — broken intermediate width
- `subtitles-loaded-dark-640x420.png` — CTA entirely unreachable
- `subtitles-loaded-scrolled-dark-1440x900.png` — AI toolbar + cue list dominate; actions scroll away
- `subtitles-loaded-light-1440x900.png` — light theme (clean, confirms theming is not the problem)

### Screenshots (regenerated 2026-10-23)

`docs/screenshots/redesign/phase-5/subtitles-{dark,light}-{640x420,760x600,1120x740,1440x900}.png`
are the current evidence: a real six-cue project is loaded by the screenshot
harness, so each size shows the populated Prepare stage (AI group, cue list,
speaker casting) plus the pinned dock. The pre-promotion
`phase-3/4/create-subtitles-*` files were removed.

### How to run the subtitle test suites

```bash
cd /home/ubuntu/Documents/project/VieNeuTTSApp
QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest -k "subtitle or srt" -q
```

(119 selected tests at 2026-10-23 — the coverage added by the track. Note `pyproject.toml` hardcodes `-n auto --dist worksteal`; override with `-o addopts=""` if you need a serial run.)
