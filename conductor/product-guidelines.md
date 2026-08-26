# VieNeuTTS Desktop App — Product Guidelines

## Visual Identity
- Dark-first minimalist design, GPU-rendered QML (Qt Quick).
- Design tokens in a single `Theme.qml` (colors, spacing, typography);
  dark mode default. Settings exposes system / light / dark.
- Modern, clean, content-forward — the audio/voice is the hero, not chrome.

## UI Copy Tone
- Minimal, technical, precise but clear; avoid jargon.
- English-first in v1; bilingual (vi/en) copy is a follow-up (see
  Localization).

## Brand Messaging
- Privacy-first headline: *"Your voice never leaves your device."*
- Quality claim: 48 kHz studio-grade Vietnamese voices, fully offline.

## Localization
- English UI for v1, but guarantee full Vietnamese diacritic rendering
  (system font fallback / proper `Font` selection).
- Vietnamese localization tracked as a follow-up.

## Motion & Interactivity
- Professional but expressive: subtle transitions, animated waveform and
  level indicator during synthesis/playback.
- No decorative animation on constrained hardware; keep streaming first
  audio in ~300 ms.
