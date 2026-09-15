// Full-buffer waveform overview + live playhead for REPLAY of finished audio
// (the "Phát" side of synthesis — complements WaveformIndicator, which covers
// live streaming synthesis only).
//
// Contract for hosts (documented so any tab reuses it identically):
//   envelope   var list[float 0..1] — peak-normalized buckets computed on the
//              Python side (controller.waveformEnvelope); raw samples never
//              reach QML — numeric-input-only widget, same posture as
//              WaveformIndicator. Empty list renders ONLY the flat baseline.
//   position   real 0..1 — replay playhead (controller.replayPosition). The
//              drawn playhead GLIDES toward this target (33 ms lerp) so the
//              80 ms Python ticks read as continuous motion.
//   active     bool — replay liveness (controller.replayActive). While true,
//              buckets left of the playhead render in the accent gradient and
//              the rest stay dim; false renders one uniform dim overview (the
//              idle "here is your audio" shape, no playhead).
//   durationMs int — total length for the time labels (elapsed/total); <= 0
//              hides the labels row and gives the canvas the full height.
//   seekable bool — when true, clicks/drags on the canvas emit
//              seekRequested(fraction 0..1) (hosts map that to their player;
//              the app tabs leave it false — their sink cannot seek).
//   selectable bool — opt-in RANGE selection, default false so the three
//              non-Studio hosts behave exactly as before. While true:
//              * a plain click still emits seekRequested(fraction) — and, if a
//                range exists, ALSO emits selectionCleared() first: clicking
//                to move the playhead is meant to drop the stale range.
//              * a press-and-drag travelling more than ~4 px emits
//                selectionChanged(start, end) on release, normalised so
//                start < end and both clamped to 0..1; that gesture does NOT
//                also seek (one drag cannot mean two things).
//              * the widget never writes selectionStart/selectionEnd itself —
//                hosts bind them to their model, and assigning here would
//                destroy the binding — it only reports via the two signals.
//   selectionStart / selectionEnd real 0..1 — the committed range, -1 = none.
//              Painted as a translucent accent band with 1.5 px accent edge
//              rules, over the idle/played bars and under the playhead, and
//              the bars inside the range stay accent-tinted even while
//              inactive so an idle range is unmistakable.
//
// Rendering: Canvas of mirrored rounded bars around the center hairline (the
// WaveformIndicator visual language), a playhead line with a soft glow while
// active, and mm:ss labels pinned under the canvas. The glide timer runs
// ONLY while the playhead is chasing a new target — a static overview or an
// idle replay costs zero timers.
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "."

Item {
    id: root

    // ── Public API ──────────────────────────────────────────────────────────
    property var envelope: []
    property real position: 0.0
    property bool active: false
    property int durationMs: 0
    property bool seekable: false
    property bool selectable: false
    property real selectionStart: -1
    property real selectionEnd: -1
    property color playedColor: Theme.accent
    property color playedColorEnd: Theme.accentHover
    property color idleColor: Theme.waveformIdle
    property color playheadColor: Theme.accentHover
    property color baselineColor: Theme.border

    signal seekRequested(real fraction)
    signal selectionChanged(real start, real end)
    signal selectionCleared()

    readonly property int bucketCount: envelope.length

    implicitWidth: 240
    implicitHeight: 56

    // Live drag state (fractions). Kept private so an in-flight drag preview
    // never clobbers a host's selectionStart/selectionEnd binding; the
    // committed range only ever comes back through the host after
    // selectionChanged.
    property real _dragStartFraction: -1
    property real _dragEndFraction: -1

    onSelectionStartChanged: canvas.requestPaint()
    onSelectionEndChanged: canvas.requestPaint()

    // Glide state: the drawn playhead x chases position*width.
    property real _playheadX: 0.0

    onPositionChanged: {
        if (!root.active)
            return;
        glideTimer.running = true;
    }

    onEnvelopeChanged: canvas.requestPaint()

    onActiveChanged: {
        if (root.active) {
            root._playheadX = root.position * canvas.width;
        } else {
            glideTimer.running = false;
        }
        canvas.requestPaint();
    }

    function fmtTime(ms) {
        const s = Math.max(0, Math.round(ms / 1000));
        const m = Math.floor(s / 60);
        return ("%1:%2").arg(m).arg(String(s % 60).padStart(2, "0"));
    }

    Timer {
        id: glideTimer

        interval: 33
        repeat: true
        running: false

        onTriggered: {
            const target = root.position * canvas.width;
            const next = root._playheadX + (target - root._playheadX) * 0.35;
            if (Math.abs(target - next) < 0.5) {
                root._playheadX = target;
                glideTimer.running = false;  // settled — no idle CPU burn
            } else {
                root._playheadX = next;
            }
            canvas.requestPaint();
        }
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

        anchors.top: parent.top
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.bottom: root.durationMs > 0 ? labelsRow.top : parent.bottom
        anchors.margins: Theme.spacingXs
        antialiasing: true

        onWidthChanged: {
            // A resized window re-anchors the playhead so it never detaches
            // from the audio it represents.
            root._playheadX = root.position * width;
            canvas.requestPaint();
        }
        onPaint: root.paint(canvas)
    }

    // Seek by click; dragging scrubs continuously (each move re-seeks).
    // With `selectable: true` the same press-drag gesture instead defines a
    // range: once the pointer travels past `dragThreshold` the gesture is a
    // selection, the scrubbing stops, and the release reports the range
    // instead of seeking (a single gesture cannot mean both).
    // NOTE: the handler-injected `mouse` parameter ONLY exists inside the
    // handler itself — pass mouse.x explicitly, never via a named helper.
    MouseArea {
        anchors.fill: canvas
        enabled: (root.seekable || root.selectable) && root.durationMs > 0
        cursorShape: enabled ? Qt.PointingHandCursor : Qt.ArrowCursor
        acceptedButtons: Qt.LeftButton

        // Pixels of travel that separate a sloppy click from a real drag.
        readonly property int dragThreshold: 4

        // Press origin (px) + gesture classification. `_suppressClick` is
        // cleared on the NEXT press rather than inside onClicked, because a
        // drag may not produce a `clicked` at all.
        property real _pressX: 0
        property bool _isDragging: false
        property bool _suppressClick: false

        function fractionAt(x) {
            return Math.max(0.0, Math.min(1.0, x / Math.max(1, canvas.width)))
        }

        onPressed: (mouse) => {
            _pressX = mouse.x
            _isDragging = false
            _suppressClick = false
        }
        onPositionChanged: (mouse) => {
            if (!pressed)
                return;
            if (root.selectable && !_isDragging
                    && Math.abs(mouse.x - _pressX) > dragThreshold) {
                _isDragging = true;
                root._dragStartFraction = fractionAt(_pressX);
                root._dragEndFraction = root._dragStartFraction;
            }
            if (_isDragging) {
                // Preview only: the committed range is reported on release.
                root._dragEndFraction = fractionAt(mouse.x);
                canvas.requestPaint();
            } else if (root.seekable) {
                root.seekRequested(fractionAt(mouse.x));
            }
        }
        onReleased: (mouse) => {
            if (!_isDragging)
                return;
            const from = root._dragStartFraction;
            const to = fractionAt(mouse.x);
            _isDragging = false;
            _suppressClick = true;
            root._dragStartFraction = -1;
            root._dragEndFraction = -1;
            canvas.requestPaint();
            // Normalised (start < end) and clamped — the host owns the commit.
            root.selectionChanged(Math.min(from, to), Math.max(from, to));
        }
        onClicked: (mouse) => {
            if (_suppressClick) {
                _suppressClick = false;
                return;  // that gesture already reported a range — no seek
            }
            // Plain click: drop any stale range, then move the playhead. Both
            // are wanted — the click is a seek, and a range left over from an
            // earlier gesture is meaningless once the playhead moves.
            if (root.selectionStart >= 0 || root.selectionEnd >= 0)
                root.selectionCleared();
            if (root.seekable)
                root.seekRequested(fractionAt(mouse.x));
        }
    }

    RowLayout {
        id: labelsRow

        anchors.bottom: parent.bottom
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.margins: Theme.spacingXs
        spacing: Theme.spacingSm
        visible: root.durationMs > 0

        Label {
            text: root.active ? root.fmtTime(root.position * root.durationMs) : root.fmtTime(0)
            color: root.active ? Theme.accent : Theme.textSubtle
            font.family: Theme.fontFamily
            font.pixelSize: Theme.fontSizeXs
            font.weight: Theme.fontWeightMedium
        }

        Item { Layout.fillWidth: true }

        Label {
            text: root.fmtTime(root.durationMs)
            color: Theme.textSubtle
            font.family: Theme.fontFamily
            font.pixelSize: Theme.fontSizeXs
        }
    }

    // Painting lives on the root so the canvas paint callback and the glide
    // timer share one implementation; `source` is the canvas being painted.
    function paint(source) {
        const ctx = source.getContext("2d");
        ctx.reset();
        const w = source.width;
        const h = source.height;
        const mid = h / 2;

        // Flat baseline hairline
        ctx.fillStyle = String(root.baselineColor);
        ctx.fillRect(0, mid - 0.5, w, 1);

        // Selection geometry (px). The live drag preview outranks the
        // committed range so the band tracks the pointer before the host has
        // echoed selectionChanged back through its bindings.
        let selFrom = -1;
        let selTo = -1;
        if (root._dragStartFraction >= 0 && root._dragEndFraction >= 0) {
            selFrom = Math.min(root._dragStartFraction, root._dragEndFraction);
            selTo = Math.max(root._dragStartFraction, root._dragEndFraction);
        } else if (root.selectionStart >= 0 && root.selectionEnd > root.selectionStart) {
            selFrom = root.selectionStart;
            selTo = root.selectionEnd;
        }
        const selX0 = selFrom < 0 ? -1 : selFrom * w;
        const selX1 = selTo < 0 ? -1 : selTo * w;
        const hasSelection = selX0 >= 0 && selX1 > selX0;

        // A destroyed-context repaint can see `envelope` as undefined.
        const env = root.envelope || [];
        const n = env.length;
        if (n === 0) {
            // Baseline-only canvas: the band still shows so a host that
            // selects before the first envelope arrives is not left blank.
            if (hasSelection)
                paintSelection(ctx, selX0, selX1, h);
            return;
        }

        const gap = Math.max(1.5, w * 0.006);
        const barW = Math.max(1.5, (w - gap * (n - 1)) / n);
        const innerH = h - Theme.spacingXs;

        // Vertical accent gradient for the played region — same lit-meter
        // language as WaveformIndicator.
        const played = ctx.createLinearGradient(0, 0, 0, h);
        played.addColorStop(0.0, String(root.playedColorEnd));
        played.addColorStop(0.5, String(root.playedColor));
        played.addColorStop(1.0, String(root.playedColorEnd));

        const playheadX = root.active ? root._playheadX : -1;

        for (let i = 0; i < n; i++) {
            const val = Math.max(0.0, Math.min(1.0, Number(env[i]) || 0.0));
            const barH = Math.max(2, val * innerH);
            const x = i * (barW + gap);
            const center = x + barW / 2;
            // Bars inside the range stay lit even while inactive, so an idle
            // selection is unmistakable next to the dim idle overview.
            const inSelection = hasSelection && center >= selX0 && center <= selX1;
            ctx.fillStyle = ((playheadX >= 0 && center <= playheadX) || inSelection)
                ? played
                : String(root.idleColor);
            const y = mid - barH / 2;
            const r = Math.min(barW, barH) / 2;
            ctx.beginPath();
            ctx.moveTo(x + r, y);
            ctx.arcTo(x + barW, y, x + barW, y + barH, r);
            ctx.arcTo(x + barW, y + barH, x, y + barH, r);
            ctx.arcTo(x, y + barH, x, y, r);
            ctx.arcTo(x, y, x + barW, y, r);
            ctx.closePath();
            ctx.fill();
        }

        // Band + edge rules sit over the bars but under the playhead, so the
        // range and the playhead both stay readable when they overlap.
        if (hasSelection)
            paintSelection(ctx, selX0, selX1, h);

        if (playheadX < 0)
            return;

        // Playhead: soft glow bands + a crisp core line.
        ctx.fillStyle = Qt.rgba(root.playheadColor.r, root.playheadColor.g, root.playheadColor.b, 0.18);
        ctx.fillRect(playheadX - 2.5, 0, 5, h);
        ctx.fillStyle = Qt.rgba(root.playheadColor.r, root.playheadColor.g, root.playheadColor.b, 0.35);
        ctx.fillRect(playheadX - 1, 0, 2, h);
        ctx.fillStyle = String(root.playheadColor);
        ctx.fillRect(playheadX - 0.75, 0, 1.5, h);
    }

    // Translucent accent band across the full canvas height with a 1.5 px
    // accent rule at each edge — the range analogue of the playhead line.
    function paintSelection(ctx, x0, x1, h) {
        ctx.fillStyle = Qt.rgba(Theme.accent.r, Theme.accent.g, Theme.accent.b, 0.16);
        ctx.fillRect(x0, 0, x1 - x0, h);
        ctx.fillStyle = String(Theme.accent);
        ctx.fillRect(x0, 0, 1.5, h);
        ctx.fillRect(x1 - 1.5, 0, 1.5, h);
    }
}
