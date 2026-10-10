# Design reference — ui_shell_redesign_20261010

Exported 2026-10-10 from the Design canvas "VieNeuTTS UI/UX Audit"
(https://claude.ai/artifact/LACfJsyU8LZMdPdj7BcwAw). The canvas is the live
source; these files are a snapshot so tasks can refer to the design offline.

Each `.dc.html` is a Design Component page: plain HTML with inline styles plus a
small `<script type="text/x-dc">` block whose `renderVals()` holds the repeated
data (issue lists, chapter rows, waveform bars). It needs the canvas runtime
(`support.js`) to render, so **read it as source**. The layout, copy and colour
values are all in the markup.

## Boards

| File | What it is | Used by |
|---|---|---|
| `Main.dc.html` | Audit verdict, assessment, top 8 issues | spec Overview |
| `IA.dc.html` | Old tabs → new destinations, the shared shell anatomy | Phase 2, 3 |
| `Current-Text.dc.html` | v0.1.14 Văn bản with 8 annotated problems | Phase 1, 2 |
| `Current-Audiobook.dc.html` | v0.1.14 Sách nói with 7 annotated problems | Phase 2, 4.1 |
| `Current-Settings.dc.html` | v0.1.14 Cài đặt with 8 annotated problems | Phase 1.4, 4.2 |
| `Proposed-Create.dc.html` | Tạo giọng đọc: mode switch, editor + emotion chips, inspector, TransportDock, StatusBar | Phase 2, 3 |
| `Proposed-Audiobook.dc.html` | Sách nói master–detail + player dock | Phase 2.4, 4.1 |
| `Proposed-Studio.dc.html` | Studio timeline, history chips, clip table, effects panel with A/B + single apply | Phase 4.3, 4.4 |
| `Proposed-Settings.dc.html` | Cài đặt sub-nav + filter, engine summary card, Nâng cao disclosures | Phase 4.2 |

The `Current-*` boards reference `docs/screenshots/*.png` by relative path.

## Mockup colours → Theme tokens

The mockups use the existing dark Signal palette, so implement with
`Theme.*` tokens and never copy the hex values into QML.

| Mockup hex | Theme token |
|---|---|
| `#0f1117` | `bg` |
| `#171a23` | `surface` / `surfaceCard` |
| `#1f2430` | `surfaceAlt` (selected segment, chips, secondary buttons) |
| `#282e3e` | `border` |
| `#1f232f` | `borderSubtle` (in-card separators) |
| `#f8fafc` | `text` |
| `#a3b0c2` / `#cbd5e1` | `textMuted` (the mockups lighten it slightly; check contrast in Task 1.1) |
| `#8a97ab` | **new** `textSubtle` value for dark (Task 1.1: ≥4.5:1 on bg/surface) |
| `#2dd4bf` | `accent`; `#052e2b` text on it = `accentText` |
| `#0f2e2a` / `#5eead4` | `accentSubtle` fill / `accentHover` text on it |
| `#4ade80`, `#86efac`, `#0e2f1c` | `success`, `successText`, `successSubtle` |
| `#fbbf24`, `#fde68a` | `warning`, `warningText` |
| `#fca5a5` | `errorText` (destructive quiet buttons) |
| `#6e7c92` | `waveformIdle` |
| `#0b0d12` | **new** status-bar background (one step darker than `bg`) |

## Measurements to carry over

- Page header: one row, ~56 px, title 22 px bold. No icon tile, no subtitle.
- Nav items 44 px tall; the sidebar is about 200–232 px, and Cài đặt is pinned to the bottom.
- TransportDock: 44 px controls, radius 16 card, primary Generate on the left after the voice chip, an Export split button, and "Mở trong Studio" as a quiet link.
- StatusBar: 34 px, 12 px text, dot + label groups, with the update link on the right.
- Inspector: about 280–320 px wide, stacking below the editor on narrow widths.
- Minimum text is 12 px. Mono numerals (timecodes, counters) are the only place a second font appears.
  The mockups use JetBrains Mono; in QML use `Theme.fontFamilyMono` (system mono fallback).

The mockups use web controls (`<input type=range>`, native checkboxes) as
stand-ins. Implement them with the project's `AppSlider`, `AppToggle`,
`AppCombo`, `AppButton` and the new `AppSegmented`, following the
one-skin-per-control pattern in `conductor/patterns.md`.
