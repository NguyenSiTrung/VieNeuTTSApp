// Rolling amplitude-envelope waveform indicator (FR-4.5 & FR-UX-4.5) — shared by the
// Text and Paragraph/File tabs while a synthesis stream is live.
//
// Contract for hosts (documented so ParagraphTab reuses it identically):
//   level  real 0..1 — the LATEST chunk's peak amplitude, computed on the
//          Python side (ui/stream_playback.py max(|sample|)); raw audio
//          samples never reach QML — this is a numeric-input-only widget.
//   active bool       — streaming session liveness (controller.streamActive).
//          Rising edge clears the history; each `level` change pushes one
//          bar while active; falling edge empties the history so the next
//          session starts clean.
// Rendering: bounded rolling history (`barCount` latest levels, oldest
// dropped) drawn right-to-left as rounded bars with a vertical accent
// gradient, mirrored around the center hairline; inactive/idle renders ONLY
// the flat baseline. Values pushed in excess of `barCount` are discarded,
// keeping memory bounded for long docs.
import QtQuick
import "."

Item {
    id: root

    // ── Public API ──────────────────────────────────────────────────────────
    property real level: 0.0
    property bool active: false
    property int barCount: 48
    property color barColor: Theme.accent
    property color barColorEnd: Theme.accentHover
    property color baselineColor: Theme.border

    readonly property int historyCount: samples.length

    implicitWidth: 240
    implicitHeight: 48

    // Rolling history, oldest first — plain JS array.
    property var samples: []

    onLevelChanged: {
        if (!root.active)
            return;
        const next = root.samples.slice(
            root.samples.length >= root.barCount ? 1 : 0);
        next.push(Math.max(0.0, Math.min(1.0, root.level)));
        root.samples = next;
        canvas.requestPaint();
    }

    onActiveChanged: {
        if (!root.active && root.samples.length > 0) {
            root.samples = [];
            canvas.requestPaint();
        }
    }

    function roundedBar(ctx, x, y, w, h) {
        const r = Math.min(w, h) / 2;
        ctx.beginPath();
        ctx.moveTo(x + r, y);
        ctx.arcTo(x + w, y, x + w, y + h, r);
        ctx.arcTo(x + w, y + h, x, y + h, r);
        ctx.arcTo(x, y + h, x, y, r);
        ctx.arcTo(x, y, x + w, y, r);
        ctx.closePath();
        ctx.fill();
    }

    Rectangle {
        anchors.fill: parent
        radius: Theme.radiusMd
        color: Theme.surfaceAlt
        border.color: Theme.borderSubtle
        border.width: 1
    }

    Canvas {
        id: canvas

        anchors.fill: parent
        anchors.margins: Theme.spacingXs
        antialiasing: true

        onPaint: {
            const ctx = getContext("2d");
            ctx.reset();
            const mid = height / 2;

            // Flat baseline hairline
            ctx.fillStyle = String(root.baselineColor);
            ctx.fillRect(0, mid - 0.5, width, 1);

            if (!root.active || root.samples.length === 0)
                return;

            const n = root.samples.length;
            const gap = Math.max(2, width * 0.012);
            const totalGaps = gap * (n - 1);
            const barW = Math.max(2.5, (width - totalGaps) / n);
            const innerH = height - Theme.spacingSm;

            // Vertical accent gradient: deeper tone at the edges, brighter at
            // the peaks — reads like a lit level meter rather than flat bars.
            const grad = ctx.createLinearGradient(0, 0, 0, height);
            grad.addColorStop(0.0, String(root.barColorEnd));
            grad.addColorStop(0.5, String(root.barColor));
            grad.addColorStop(1.0, String(root.barColorEnd));
            ctx.fillStyle = grad;

            for (let i = 0; i < n; i++) {
                const val = root.samples[i];
                const h = Math.max(2.5, val * innerH);
                const x = width - (n - i) * (barW + gap);

                // Draw mirrored rounded bar around center line
                const y = mid - h / 2;
                root.roundedBar(ctx, x, y, barW, h);
            }
        }
    }
}
