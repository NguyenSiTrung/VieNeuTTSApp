"""Screenshot harness: render WaveformIndicator + PlaybackWaveform offscreen.

Loads the two widgets directly (no engine/audio stack) with synthetic data at
three visual checkpoints and saves PNGs for design review:

    build/waveform_preview_live.png   — live meter (bars + peak-hold caps)
    build/waveform_preview_replay.png — envelope overview, playhead mid-file
    build/waveform_preview_idle.png   — idle dim overview + time labels

Usage: .venv/bin/python scripts/preview_waveform_widgets.py [outdir]
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

from PySide6.QtCore import QTimer, QUrl
from PySide6.QtGui import QGuiApplication
from PySide6.QtQuick import QQuickView

QML_DIR = Path(__file__).resolve().parents[1] / "src" / "vienetts_app" / "ui" / "qml"

# Envelope shaped like real speech: syllable bursts inside phrase gates,
# gentle fade-in/out, floor near zero for pauses.
_ENVELOPE = [
    max(
        0.04,
        min(
            1.0,
            (0.55 + 0.45 * math.sin(t * 46.0) ** 2)
            * (0.15 + 0.85 * (0.5 + 0.5 * math.sin(t * 6.2)))
            * min(1.0, t / 0.04)
            * min(1.0, (1.0 - t) / 0.05),
        ),
    )
    for t in (i / 160 for i in range(160))
]


def main() -> int:
    out_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("build")
    out_dir.mkdir(parents=True, exist_ok=True)

    app = QGuiApplication(sys.argv[:1])
    view = QQuickView()
    view.engine().addImportPath(str(QML_DIR))
    view.setSource(QUrl.fromLocalFile(str(QML_DIR / "_waveform_preview.qml")))
    if view.status() != QQuickView.Status.Ready:
        print("load errors:", view.errors(), file=sys.stderr)
        return 1
    view.show()
    root = view.rootObject()
    meter = root.findChild(object, "meter")  # type: ignore[arg-type]
    overview = root.findChild(object, "overview")  # type: ignore[arg-type]
    ab_wave = root.findChild(object, "abWave")  # type: ignore[arg-type]
    assert meter is not None and overview is not None and ab_wave is not None

    levels = [
        max(0.05, min(1.0, abs(math.sin(t * 9.0)) * (0.5 + 0.5 * math.sin(t * 3.1))))
        for t in (i / 48 for i in range(48))
    ]

    def push_levels():
        for v in levels:
            meter.setProperty("level", v)
        QTimer.singleShot(450, capture_live)

    def capture_live():
        save(view, out_dir / "waveform_preview_live.png")
        overview.setProperty("envelope", _ENVELOPE)
        overview.setProperty("durationMs", 154_000)
        overview.setProperty("position", 0.58)
        overview.setProperty("active", True)
        QTimer.singleShot(550, capture_replay)

    def capture_replay():
        save(view, out_dir / "waveform_preview_replay.png")
        overview.setProperty("active", False)
        overview.setProperty("position", 0.0)
        QTimer.singleShot(450, capture_idle)

    def capture_idle():
        save(view, out_dir / "waveform_preview_idle.png")
        # Audiobook-transport posture: seekable, paused mid-chapter.
        ab_wave.setProperty("envelope", _ENVELOPE)
        ab_wave.setProperty("durationMs", 612_000)
        ab_wave.setProperty("position", 0.34)
        ab_wave.setProperty("active", True)
        ab_wave.setProperty("seekable", True)
        QTimer.singleShot(500, capture_audiobook)

    def capture_audiobook():
        save(view, out_dir / "waveform_preview_audiobook.png")
        app.quit()

    QTimer.singleShot(300, push_levels)
    return app.exec()


def save(view: QQuickView, path: Path) -> None:
    image = view.grabWindow()
    ok = image.save(str(path))
    print(f"{'saved' if ok else 'FAILED'}: {path}")


if __name__ == "__main__":
    raise SystemExit(main())
