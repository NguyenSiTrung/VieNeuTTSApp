// Rolling amplitude-envelope waveform indicator (FR-4.5) — shared by the
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
// dropped) drawn right-to-left as vertical bars mirrored around the center
// hairline; inactive/idle renders ONLY the flat baseline. Values pushed in
// excess of `barCount` are discarded, keeping memory bounded for long docs.
//
// NOTE: the history is stored as a NEW array per push (slice + push +
// reassign), not an in-place mutation — QML change signals do not fire for
// in-place JS-array edits, which would leave derived readonly properties
// (historyCount) silently stale.
//
// Hosts own visibility (bind `visible: controller.streamActive`) — the
// component itself always paints something (baseline or bars).
import QtQuick
import "."

Item {
    id: root

    // ── Public API ──────────────────────────────────────────────────────────
    // Newest amplitude 0..1 (already clamped by the Python emitter; still
    // re-clamped defensively below so a bad value cannot break painting).
    property real level: 0.0
    // Stream session liveness: gates pushing AND clears history on edges.
    property bool active: false
    // History capacity in bars (rolling window length).
    property int barCount: 48
    // Themable paint tokens; overridden only by special host backgrounds.
    property color barColor: Theme.accent
    property color baselineColor: Theme.border

    // Test surface: how many bars the rolling window currently holds.
    readonly property int historyCount: samples.length

    implicitWidth: 240
    implicitHeight: 48

    // Rolling history, oldest first — plain JS array, no timers, no audio.
    property var samples: []

    onLevelChanged: {
        if (!root.active)
            return;
        // Drop the OLDEST sample once the window is full, then append the
        // newest. Reassignment (not in-place mutation) is required so the
        // property change signal fires — see the header note above.
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

    Canvas {
        id: canvas

        anchors.fill: parent
        antialiasing: true

        onPaint: {
            const ctx = getContext("2d");
            ctx.reset();
            const mid = height / 2;
            // Flat baseline hairline — the idle/idle-drain state.
            ctx.fillStyle = String(root.baselineColor);
            ctx.fillRect(0, mid - 0.5, width, 1);
            if (!root.active || root.samples.length === 0)
                return;
            // Bars fill from the RIGHT edge backwards: newest level nearest
            // the write head, oldest scrolled off to the left.
            const gap = Math.max(1, width * 0.01);
            const barW = Math.max(1, (width - gap * root.samples.length) / root.samples.length);
            const innerH = height - Theme.spacingSm;
            ctx.fillStyle = String(root.barColor);
            for (let i = 0; i < root.samples.length; i++) {
                const h = root.samples[i] * innerH;
                const x = width - (root.samples.length - i) * (barW + gap);
                ctx.fillRect(x, mid - h / 2, barW, Math.max(1, h));
            }
        }
    }
}
