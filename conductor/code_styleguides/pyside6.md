# PySide6 / QML Style Guide

## Architecture
- Single-process app (no IPC/port). UI in QML (Qt Quick); Python bridge
  via `QObject` signals/slots.
- Keep UI-framework-agnostic layers (`TTSEngine`, `detector`, controllers)
  separate from QML/Widgets so they can be tested in isolation.
- The `Vieneu()` instance lives in one worker `QThread` and is never
  touched from the main thread.

## Threading
- All inference/blocking work runs on the worker thread.
- Commands flow main-thread → thread-safe request queue → worker; results
  flow back via signals (`progress`, `chunkReady`, `done`, `error`).
- Never emit cross-thread signals with large mutable buffers without a
  documented ownership transfer (use `np.float32` chunks that are
  immutable per-emit).

## Signals & Slots
- Prefer typed `Signal(...)` on a `QObject` we own; keep connection types
  explicit (`Qt.QueuedConnection` for worker→UI).
- UI updates must happen on the main thread only.

## QML Conventions
- Design tokens in a single `Theme.qml` (colors, spacing, typography).
  Never hardcode colors/margins in components.
- Components are small, reusable, named per directory convention; keep
  model/data out of views.
- Guard against re-entrancy: disable Generate while a job is running;
  cooperative cancel flag checked between chunks.

## Resource Lifecycle
- Close/stop audio sinks and release model references on shutdown.
- Lazy-init the engine on first request; show a "Loading model…" state,
  never a frozen UI.
