import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import ".."

// Studio timeline (FR-4.3): one track. A mono time ruler, then the clip lane
// (each clip drawn as a proportional segment on a single Canvas), then the
// waveform of whatever the transport describes. The lane carries its own
// selection band and playhead as scene items so playhead ticks never repaint
// the clip Canvas. The blocks are a map, not controls: a block can be narrower
// than a 44 px target, so auditioning stays on the clip table's play buttons.
//
// The host owns every value. The waveform reports drags and clicks back
// through rangeSelected / rangeCleared / seekRequested and never writes its
// own selection (see PlaybackWaveform's contract).
//
// objectNames: studioTimeline, studioTimeRuler, studioTimelineLane,
// studioTimelineCanvas, studioTimelineSelection, studioTimelinePlayhead,
// studioWaveform.
ColumnLayout {
    id: root

    objectName: "studioTimeline"

    // studioClipModel rows ({id, label, duration, ...}); durations in seconds.
    property var clipModel: null
    property var clips: []
    property string auditionClipId: ""
    // Length (ms) of what the waveform shows; labels the ruler.
    property int totalMs: 0
    property var envelope: []
    property real position: 0
    property bool active: false
    property bool seekable: false
    property bool selectable: false
    property real selectionStart: -1
    property real selectionEnd: -1
    property int waveformHeight: 72

    // Canvas repaint counter — probe read by offscreen smoke assertions to
    // verify zero repaints on playhead ticks and visibility-gated painting.
    property int paintCount: 0

    readonly property bool hasSelection: selectionStart >= 0 && selectionEnd > selectionStart
    readonly property bool auditioning: auditionClipId !== ""

    // Reactive revision bumped whenever clipModel mutates; never touched by
    // playhead position ticks.
    property int _modelRev: 0
    property bool _canvasDirty: true
    property int _hoveredSegmentIndex: -1

    // Theme colour bindings so dark/light switches trigger a single repaint.
    readonly property color _blockBg: Theme.surfaceAlt
    readonly property color _blockHoverBg: Theme.surfaceHover
    readonly property color _blockBorder: Theme.borderSubtle
    readonly property color _activeBg: Theme.accentSubtle
    readonly property color _activeBorder: Theme.accent
    readonly property color _labelColor: Theme.text
    readonly property color _subtleColor: Theme.textSubtle

    readonly property bool _useClipModel: Boolean(
        root.clipModel && typeof root.clipModel.get === "function"
    )

    readonly property real clipSumSeconds: {
        void root._modelRev;
        let sum = 0;
        if (root._useClipModel) {
            const m = root.clipModel;
            const n = m.count || 0;
            for (let i = 0; i < n; i++)
                sum += Math.max(0, Number(m.get(i).duration) || 0);
            return sum;
        }
        for (let i = 0; i < root.clips.length; i++)
            sum += Math.max(0, Number(root.clips[i].duration) || 0);
        return sum;
    }

    // Proportional segment geometry for the clip Canvas and smoke probes.
    // Re-evaluates only on clip model, lane width, or auditionClipId changes.
    readonly property var clipSegments: {
        void root._modelRev;
        const useModel = root._useClipModel;
        const m = root.clipModel;
        const n = useModel ? (m.count || 0) : root.clips.length;
        const w = lane.width;
        if (n <= 0 || w <= 0)
            return [];
        const sum = root.clipSumSeconds;
        const gap = n > 1
            ? Math.min(Theme.spacingXxs, Math.max(0, (w * 0.35) / (n - 1)))
            : 0;
        const availW = Math.max(0, w - gap * (n - 1));
        const minSegW = availW >= 2 * n ? 2 : 1;
        const out = [];
        let cursorX = 0;
        for (let i = 0; i < n; i++) {
            const row = useModel ? m.get(i) : root.clips[i];
            const id = String(row.id || "");
            const dur = Math.max(0, Number(row.duration) || 0);
            const share = sum > 0 ? dur / sum : 1 / n;
            const segW = Math.max(minSegW, availW * share);
            out.push({
                id: id,
                index: i,
                label: qsTr("Đoạn %1").arg(i + 1),
                duration: dur,
                x: cursorX,
                width: segW,
                current: root.auditionClipId !== "" && root.auditionClipId === id
            });
            cursorX += segW + gap;
        }
        return out;
    }

    signal seekRequested(real fraction)
    signal rangeSelected(real start, real end)
    signal rangeCleared()

    spacing: Theme.spacingXs

    readonly property string monoFamily: Theme.fontFamilyMono !== "" ? Theme.fontFamilyMono : Theme.fontFamily

    function _markDirty() {
        root._canvasDirty = true;
        if (!root.visible || !clipCanvas.visible || clipCanvas.width <= 0 || clipCanvas.height <= 0)
            return;
        root._canvasDirty = false;
        clipCanvas.requestPaint();
    }

    onClipModelChanged: {
        root._modelRev += 1;
        root._markDirty();
    }
    onClipsChanged: {
        if (!root._useClipModel) {
            root._modelRev += 1;
            root._markDirty();
        }
    }
    onAuditionClipIdChanged: root._markDirty()
    on_HoveredSegmentIndexChanged: root._markDirty()
    on_BlockBgChanged: root._markDirty()
    on_BlockHoverBgChanged: root._markDirty()
    on_BlockBorderChanged: root._markDirty()
    on_ActiveBgChanged: root._markDirty()
    on_ActiveBorderChanged: root._markDirty()
    on_LabelColorChanged: root._markDirty()
    on_SubtleColorChanged: root._markDirty()

    onVisibleChanged: {
        if (root.visible && root._canvasDirty && clipCanvas.width > 0 && clipCanvas.height > 0) {
            root._canvasDirty = false;
            clipCanvas.requestPaint();
        }
    }

    Connections {
        target: root._useClipModel ? root.clipModel : null
        function onRowsInserted() {
            root._modelRev += 1;
            root._markDirty();
        }
        function onRowsRemoved() {
            root._modelRev += 1;
            root._markDirty();
        }
        function onDataChanged() {
            root._modelRev += 1;
            root._markDirty();
        }
        function onModelReset() {
            root._modelRev += 1;
            root._markDirty();
        }
    }

    function formatTime(ms) {
        const s = Math.max(0, Math.round(ms / 1000));
        return ("%1:%2").arg(Math.floor(s / 60)).arg(String(s % 60).padStart(2, "0"));
    }

    function _segmentIndexAt(px) {
        const segs = root.clipSegments;
        for (let i = 0; i < segs.length; i++) {
            if (px >= segs[i].x && px <= segs[i].x + segs[i].width)
                return i;
        }
        return -1;
    }

    function _fillRoundedRect(ctx, x, y, w, h, radius) {
        const r = Math.max(0, Math.min(radius, w / 2, h / 2));
        ctx.beginPath();
        ctx.moveTo(x + r, y);
        ctx.arcTo(x + w, y, x + w, y + h, r);
        ctx.arcTo(x + w, y + h, x, y + h, r);
        ctx.arcTo(x, y + h, x, y, r);
        ctx.arcTo(x, y, x + w, y, r);
        ctx.closePath();
    }

    function _elideText(ctx, text, maxWidth) {
        if (maxWidth <= 0)
            return "";
        if (ctx.measureText(text).width <= maxWidth)
            return text;
        const ellipsis = "…";
        const ellipsisW = ctx.measureText(ellipsis).width;
        if (ellipsisW >= maxWidth)
            return "";
        let lo = 0;
        let hi = text.length;
        while (lo < hi) {
            const mid = Math.floor((lo + hi + 1) / 2);
            if (ctx.measureText(text.slice(0, mid)).width + ellipsisW <= maxWidth)
                lo = mid;
            else
                hi = mid - 1;
        }
        return text.slice(0, lo) + ellipsis;
    }

    function _paintClips(source) {
        root.paintCount += 1;
        const ctx = source.getContext("2d");
        ctx.reset();
        const w = source.width;
        const h = source.height;
        const segs = root.clipSegments;
        if (!segs || segs.length === 0)
            return;

        const blockBg = String(root._blockBg);
        const blockHoverBg = String(root._blockHoverBg);
        const blockBorder = String(root._blockBorder);
        const activeBg = String(root._activeBg);
        const activeBorder = String(root._activeBorder);
        const labelColor = String(root._labelColor);
        const subtleColor = String(root._subtleColor);
        const labelFont = "500 " + Theme.fontSizeXs + "px \"" + Theme.fontFamily + "\"";
        const monoFont = Theme.fontSizeXs + "px \"" + root.monoFamily + "\"";
        const padX = Theme.spacingSm;
        const midY = h / 2;

        ctx.textBaseline = "middle";
        for (let i = 0; i < segs.length; i++) {
            const seg = segs[i];
            const x = seg.x;
            const drawW = Math.min(seg.width, Math.max(0, w - x));
            if (drawW <= 0)
                continue;
            const hovered = i === root._hoveredSegmentIndex && !seg.current;
            const inset = drawW >= 3 ? 0.5 : 0;
            const rectW = Math.max(1, drawW - inset * 2);
            const rectH = Math.max(1, h - inset * 2);

            _fillRoundedRect(ctx, x + inset, inset, rectW, rectH, Theme.radiusSm);
            ctx.fillStyle = seg.current ? activeBg : (hovered ? blockHoverBg : blockBg);
            ctx.fill();
            if (drawW >= 3) {
                ctx.lineWidth = 1;
                ctx.strokeStyle = seg.current ? activeBorder : blockBorder;
                ctx.stroke();
            }

            // Too narrow for text: the block stays a plain bar.
            if (drawW < 48)
                continue;

            let rightReserve = padX;
            if (drawW >= 110) {
                const durText = Number(seg.duration || 0).toFixed(1) + "s";
                ctx.font = monoFont;
                ctx.fillStyle = subtleColor;
                ctx.textAlign = "right";
                ctx.fillText(durText, x + drawW - padX, midY);
                rightReserve += ctx.measureText(durText).width + Theme.spacingXs;
            }

            const maxLabelW = drawW - padX - rightReserve;
            if (maxLabelW > 4) {
                ctx.font = labelFont;
                ctx.fillStyle = seg.current ? activeBorder : labelColor;
                ctx.textAlign = "left";
                const drawnLabel = _elideText(ctx, seg.label, maxLabelW);
                if (drawnLabel !== "")
                    ctx.fillText(drawnLabel, x + padX, midY);
            }
        }
    }

    // Ruler ticks: the smallest round step that keeps at most ~6 labels.
    readonly property var ticks: {
        const total = root.totalMs;
        if (total <= 0)
            return [];
        const steps = [1000, 2000, 5000, 10000, 15000, 30000, 60000, 120000, 300000, 600000];
        let step = steps[steps.length - 1];
        for (let i = 0; i < steps.length; i++) {
            if (total / steps[i] <= 5) {
                step = steps[i];
                break;
            }
        }
        const out = [];
        for (let t = 0; t <= total; t += step)
            out.push(t);
        return out;
    }

    // ── Ruler ────────────────────────────────────────────────────────────
    Item {
        objectName: "studioTimeRuler"
        Layout.fillWidth: true
        Layout.leftMargin: Theme.spacingXs
        Layout.rightMargin: Theme.spacingXs
        implicitHeight: 16
        visible: root.ticks.length > 0

        Repeater {
            model: root.ticks

            Label {
                required property var modelData

                // The last label right-aligns so it never overflows the track.
                x: Math.min(parent.width - width,
                    root.totalMs > 0 ? modelData / root.totalMs * parent.width : 0)
                text: root.formatTime(modelData)
                color: Theme.textSubtle
                font.family: root.monoFamily
                font.pixelSize: Theme.fontSizeXs
            }
        }
    }

    // ── Clip lane ────────────────────────────────────────────────────────
    Item {
        id: lane

        objectName: "studioTimelineLane"
        Layout.fillWidth: true
        Layout.leftMargin: Theme.spacingXs
        Layout.rightMargin: Theme.spacingXs
        implicitHeight: 40

        Canvas {
            id: clipCanvas

            objectName: "studioTimelineCanvas"
            anchors.fill: parent
            antialiasing: true

            onWidthChanged: root._markDirty()
            onHeightChanged: root._markDirty()
            onVisibleChanged: {
                if (visible && root._canvasDirty && width > 0 && height > 0) {
                    root._canvasDirty = false;
                    requestPaint();
                }
            }
            onPaint: {
                if (!root.visible || !visible || width <= 0 || height <= 0) {
                    root._canvasDirty = true;
                    return;
                }
                root._canvasDirty = false;
                root._paintClips(clipCanvas);
            }
        }

        // Hover feedback + click seek across the clip lane (mirrors waveform
        // seek when seekable; drag selection stays on the waveform).
        MouseArea {
            anchors.fill: parent
            hoverEnabled: true
            acceptedButtons: Qt.LeftButton
            cursorShape: root.seekable ? Qt.PointingHandCursor : Qt.ArrowCursor

            onPositionChanged: (mouse) => {
                root._hoveredSegmentIndex = root._segmentIndexAt(mouse.x);
            }
            onExited: root._hoveredSegmentIndex = -1
            onClicked: (mouse) => {
                if (!root.seekable || lane.width <= 0)
                    return;
                if (root.selectionStart >= 0 || root.selectionEnd >= 0)
                    root.rangeCleared();
                root.seekRequested(Math.max(0.0, Math.min(1.0, mouse.x / lane.width)));
            }
        }

        // Range band across the lane — the same range the waveform paints.
        Rectangle {
            objectName: "studioTimelineSelection"
            visible: root.hasSelection && !root.auditioning
            x: Math.max(0, root.selectionStart) * lane.width
            width: Math.max(0, root.selectionEnd - root.selectionStart) * lane.width
            height: lane.height
            color: Qt.rgba(Theme.accent.r, Theme.accent.g, Theme.accent.b, 0.18)
            border.color: Theme.accent
            border.width: 1
        }

        // Playhead over the mix (a clip audition plays one block, so the
        // lane's mix position would be wrong there).
        Rectangle {
            objectName: "studioTimelinePlayhead"
            visible: root.active && !root.auditioning
            x: Math.max(0, Math.min(1, root.position)) * lane.width - width / 2
            y: -Theme.spacingXs
            width: 2
            height: lane.height + Theme.spacingXs * 2
            radius: 1
            color: Theme.accentHover
        }
    }

    PlaybackWaveform {
        id: wave

        objectName: "studioWaveform"
        Layout.fillWidth: true
        Layout.preferredHeight: root.waveformHeight
        envelope: root.envelope
        position: root.position
        active: root.active
        durationMs: root.totalMs
        seekable: root.seekable
        selectable: root.selectable
        selectionStart: root.selectionStart
        selectionEnd: root.selectionEnd
        onSeekRequested: (fraction) => root.seekRequested(fraction)
        onSelectionChanged: (start, end) => root.rangeSelected(start, end)
        onSelectionCleared: root.rangeCleared()
    }
}
