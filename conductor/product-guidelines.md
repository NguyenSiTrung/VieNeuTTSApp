# VieNeuTTS Desktop App — Product Guidelines

## Visual Identity
- Dark-first minimalist design, GPU-rendered QML (Qt Quick).
- Design tokens in a single `Theme.qml` (colors, spacing, typography);
  dark mode default. Settings exposes system / light / dark.
- Modern, clean, content-forward — the audio/voice is the hero, not chrome.

## UI Copy Tone
- Minimal, technical, precise but clear; avoid jargon.
- Vietnamese is the **source** language (`qsTr()` literals); English ships as a
  complete catalog (`ui/i18n/vienetts_en.ts` → `.qm`). No entry may stay
  `type="unfinished"` — run `scripts/update_i18n.sh` after any copy change.

## Brand Messaging
- Privacy-first headline: *"Your voice never leaves your device."*
- Quality claim: 48 kHz studio-grade Vietnamese voices, fully offline.

## Localization
- Vietnamese source copy must render with full diacritics (bundled Be Vietnam
  Pro; system fallback when the fonts are absent).
- English is a shipped translation, not a follow-up: `vienetts_en.ts` is the
  single catalog and must stay free of unfinished or context-drifted strings
  (the i18n pytest guard was removed in the 2026-10-28 test-suite diet).
- Add future locales as sibling `vienetts_<lang>.ts` catalogs; never fork copy
  inside QML.

## Motion & Interactivity
- Professional but expressive: subtle transitions, animated waveform and
  level indicator during synthesis/playback.
- No decorative animation on constrained hardware; keep streaming first
  audio in ~300 ms.
