# Refined Studio Control-System Redesign

**Date:** 2026-08-28
**Beads:** `VieNeuTTSApp-mp2`
**Status:** Approved design, awaiting implementation-plan review

## Goal

Refine every user-facing control in VieNeuTTS into one accessible, professional
interaction system. The app remains a calm, private, on-device audio workstation:
teal stays the identifying accent, the Vietnamese-first type system stays intact,
and visual interest comes from hierarchy and responsive state feedback rather than
decoration.

The redesign applies to the text, paragraph, audiobook, cloning, and settings
studios, plus the main navigation shell.

## Problems to solve

- Disabled buttons are too pale and visually indistinct. They hide prerequisites
  instead of communicating them.
- Action rows mix generate, play, export, save, and cancel without showing which
  action is the next logical step.
- Voice selection is a dense native-looking dropdown: group rows are easy to
  mistake for choices, the selected value provides no meaningful context, and
  long labels are hard to scan.
- Checkboxes, sliders, spin boxes, icons, alert banners, and list actions vary
  too much across screens.
- Tooltips carry essential information, such as the export-before-playback flow,
  that should be visible in the workflow itself.

## Experience principles

1. **One obvious next step.** Each card or workflow stage has no more than one
   primary action.
2. **State explains itself.** Disabled, processing, completed, warning, and error
   states give a visible reason and recovery path.
3. **Controls feel related.** All interactive elements share spacing, focus,
   hover, pressed, disabled, and elevation behavior.
4. **Audio work stays calm.** The visual language is practical and polished, not a
   decorative "AI" theme or a dense studio-console imitation.
5. **Accessible by default.** Every control is keyboard reachable, has a visible
   keyboard focus state, meets contrast expectations, and gives non-text actions
   an accessible name.

## Shared QML component system

### `AppButton`

Refactor the existing control into `primary`, `secondary`, `quiet`, `danger`, and
`icon` variants, all with small, regular, and large sizing.

- A large primary button is a 44 px target and is the only filled action in its
  workflow group.
- Secondary buttons are elevated/bordered surfaces that remain readable when
  disabled.
- Quiet buttons serve non-destructive tertiary actions such as save, clear, or
  per-row actions.
- Danger buttons reserve red for confirmation, cancellation, or deletion.
- Busy primary actions replace their normal label with a progress state while
  retaining their dimensions, preventing layout jumps.
- Disabled controls expose their missing prerequisite in nearby helper text where
  it affects the task flow, and in a tooltip as a secondary aid.

### `AppCombo` and `VoicePicker`

Treat selectors as form fields, not plain buttons.

- Trigger: optional leading mark, current value, optional supporting metadata,
  explicit chevron, focus ring, and open state.
- Popup: title/header when useful, constrained height, scrolling, strong group
  headers, high-contrast hover, selected-row checkmark, and stable keyboard
  navigation.
- Voice choices retain catalog group order and IDs. They display region, gender,
  and delivery style in a scanable row while keeping the existing controller
  selection contract.
- Filtering is available when the catalog is long. It must not obscure the current
  choice or make group labels selectable.
- Existing `VoicePicker` object names, `flatModel`, `selectedVoice`, `purpose`,
  `activated`, and default-voice synchronization remain intact.

### Supporting primitives

- **`AppToggle`**: consistent 40–44 px hit target and selected track for
  `autoAdvanceToggle` and denoise preference.
- **`AppSlider`**: shared track, handle, focus ring, hover, and disabled behavior
  for audiobook seeking.
- **`AppNumberField`**: wraps the existing temperature spin-box behavior with
  clearer increment/decrement actions and a stable formatted value.
- **`AppIconButton`**: vector icon with 36–40 px target, tooltip, accessible
  name, and visible focus state. It replaces text-symbol-only icon actions.
- **`AppNotice`**: single success, warning, error, and progress presentation with
  concise supporting copy and optional recovery action.

## Screen behavior

### Text and Paragraph

Place voice selection and **Generate audio** in a compact synthesis block after
the editor. The selected voice remains clear when its popup is closed.

Generate is the primary CTA. Preview/playback, export, and quick save become a
secondary action cluster that appears or activates as audio moves through its
existing state flow. If audio is unavailable or not yet exported, a concise
visible explanation tells the user why the relevant action is unavailable.

During synthesis, the primary CTA becomes a stable progress control; waveform,
progress, and cancellation live in one unified feedback region. Editor metrics
remain in a quiet footer, while import and clear remain adjacent to their source
field.

### Audiobook

The book card presents one batch action, **Render pending**, as primary. Export
is secondary. Per-chapter render/remove controls are quiet icon actions with
clear accessible labels and tooltips.

Chapter status remains visible through badges and progress rather than relying on
all rows looking clickable. The player becomes a balanced transport bar:
previous, prominent play/pause, next, elapsed time, seek slider, and duration.

### Voice cloning

The existing workflow becomes visibly sequential: choose a source clip, name the
voice, then create it. Each stage exposes not-started, ready, processing, and
complete states without changing the controller workflow.

The consent panel has a concise acknowledgement summary and preserves the
required legal copy. Denoise and preview appear as optional enhancements. Removing
a saved cloned voice uses a danger icon action and a confirmation step, while
retaining controller behavior and the existing test seam.

### Settings and navigation

Settings use consistent label, supporting description, and fixed-width control
rows. At narrow widths controls move below copy, instead of truncating. Immediate
and restart-required effects are presented through shared notices.

Output-folder selection uses a path field with browse and reset/default
affordances. Navigation keeps a strong selected state and concise engine summary,
with usable focus behavior on compact widths.

## Compatibility constraints

The redesign must not change:

- Python controller APIs, inference, playback, export order, persistence, or file
  import behavior.
- Existing QML object names, `QMetaObject`-invocable root functions, and
  test-pinned text unless a test is deliberately updated for approved copy.
- Keyboard shortcuts: Ctrl+Return, Ctrl+E, and Escape.
- Translation behavior, dark/light live switching, voice IDs, and default-voice
  synchronization.

## Responsive and accessible behavior

At the 640 px minimum window width, button groups wrap into readable rows and
form controls move below their label/description. No label, subtitle, path, or
voice name may overlap or truncate critical meaning. Interactive targets stay at
least 36 px, primary controls and frequent touch-like actions target 44 px, and
keyboard focus is clear in both themes.

## Validation

1. Add focused smoke tests for control variants, selector popup state and
selection, disabled explanations, busy/progress behavior, accessible names, and
minimum-width wrapping.
2. Extend the QML screenshot driver for all five tabs in both themes, including
empty, populated, busy, error, and selected-control states.
3. Manually review Vietnamese diacritics, long labels, popup clipping, focus
movement, and narrow-window flow.
4. Run `ruff check .`, `ruff format --check .`, and the full offscreen `pytest`
   suite.

## Out of scope

- New backend features, model behavior, audio formats, data migrations, or
  persistence changes.
- Replacing the app shell with a custom window frame.
- Changing test-pinned playback/export sequencing.
