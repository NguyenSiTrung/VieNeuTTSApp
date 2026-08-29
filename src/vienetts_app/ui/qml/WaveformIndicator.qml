// Rolling amplitude-envelope waveform indicator (FR-4.5 & FR-UX-4.5) — shared by the
// Text and Paragraph/File tabs while a synthesis stream is live.
//
// Contract for hosts (documented so ParagraphTab reuses it identically):
//   level  real 0..1 — the LATEST window's peak amplitude, computed on the
//          Python side (ui/stream_playback.py max(|sample|) per ~120 ms
//          window); raw audio samples never reach QML — this is a
//          numeric-input-only widget.
//   active bool       — streaming session liveness (controller.streamActive).
//          Rising edge clears the history; each `level` change pushes one
//          bar while active; falling edge empties the history so the next
//          session starts clean.
// Rendering: bounded rolling history (`barCount` latest levels, oldest
// dropped) drawn right-to-left as rounded bars with a vertical accent
// gradient, mirrored around the center hairline; inactive/idle renders ONLY
// the flat baseline. Values pushed in excess of `barCount` are discarded,
// keeping memory bounded for long docs.
//
// Motion: `historyCount`/`samples` are the TARGET levels (pushed instantly —
// the tested contract); what is DRAWN is a 33 ms animation over them — bars
// attack fast and release smoothly, and each bar carries a peak-hold cap that
// lingers above the falling bar (level-meter behavior). The frame timer runs
// only while something is visibly moving, so a silent-but-active stream or a
// fully settled meter costs zero timers.
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

    // Rolling history, oldest first — plain JS array (TARGET levels).
    property var samples: []

    // Animation state, index-aligned with `samples`: drawn bar heights
    // (lerped toward targets), peak-hold cap heights, and frames since each
    // cap last rose (caps fall only after a short linger).
    property var _drawn: []
    property var _holds: []
    property var _ages: []

    onLevelChanged: {
        if (!root.active)
            return;
        const drop = root.samples.length >= root.barCount ? 1 : 0;
        const next = root.samples.slice(drop);
        next.push(Math.max(0.0, Math.min(1.0, root.level)));
        root.samples = next;

        // Keep the animation arrays aligned with the new history window: a
        // fresh slot animates up from zero; a dropped slot takes its cap too.
        const drawn = root._drawn.slice(drop);
        const holds = root._holds.slice(drop);
        const ages = root._ages.slice(drop);
        drawn.push(0.0);
        holds.push(0.0);
        ages.push(0);
        root._drawn = drawn;
        root._holds = holds;
        root._ages = ages;

        animTimer.running = true;
        canvas.requestPaint();
    }

    onActiveChanged: {
        if (!root.active) {
            root.samples = [];
            root._drawn = [];
            root._holds = [];
            root._ages = [];
            animTimer.running = false;
            canvas.requestPaint();
        }
    }

    Timer {
        id: animTimer

        interval: 33
        repeat: true
        running: false

        onTriggered: {
            const n = root.samples.length;
            if (n === 0) {
                animTimer.running = false;
                return;
            }
            let moved = false;
            for (let i = 0; i < n; i++) {
                const target = root.samples[i];
                let cur = root._drawn[i];
                // Fast attack, smooth release — a meter that jumps up but
                // eases down reads as responsive without flicker.
                const rate = target > cur ? 0.5 : 0.16;
                cur += (target - cur) * rate;
                if (Math.abs(target - cur) < 0.004) {
                    cur = target;
                } else {
                    moved = true;
                }
                root._drawn[i] = cur;

                let hold = root._holds[i];
                let age = root._ages[i];
                if (cur >= hold) {
                    hold = cur;
                    age = 0;
                } else if (age > 12) {
                    const next = Math.max(cur, hold - 0.012);
                    if (next < hold)
                        moved = true;
                    hold = next;
                }
                root._holds[i] = hold;
                root._ages[i] = age + 1;
            }
            canvas.requestPaint();
            if (!moved)
                animTimer.running = false;  // settled — no idle CPU burn
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

            const capW = Math.max(2, barW - 1);
            for (let i = 0; i < n; i++) {
                const val = root._drawn.length === n ? root._drawn[i] : root.samples[i];
                const h = Math.max(2.5, val * innerH);
                const x = width - (n - i) * (barW + gap);

                // Draw mirrored rounded bar around center line
                const y = mid - h / 2;
                root.roundedBar(ctx, x, y, barW, h);

                // Peak-hold cap: a bright tick that lingers where the bar
                // recently peaked while the bar eases back down.
                const hold = root._holds.length === n ? root._holds[i] : val;
                const capH = Math.max(2.5, hold * innerH);
                if (capH - h > 1.5) {
                    ctx.save();
                    ctx.fillStyle = String(root.barColorEnd);
                    const capY = mid - capH / 2;
                    ctx.fillRect(x + (barW - capW) / 2, capY, capW, 2);
                    ctx.fillRect(x + (barW - capW) / 2, capY + capH - 2, capW, 2);
                    ctx.restore();
                }
            }
        }
    }
}
