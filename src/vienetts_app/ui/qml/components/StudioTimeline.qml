import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import ".."

// Studio timeline (FR-4.3): one track. A mono time ruler, then the clip lane
// (each clip a block whose width is its share of the project length), then
// the waveform of whatever the transport describes. The lane carries its own
// selection band and playhead so the range and the play position read against
// the clips, not just the bars. The blocks are a map, not controls: a block
// can be narrower than a 44 px target, so auditioning stays on the clip
// table's play buttons.
//
// The host owns every value. The waveform reports drags and clicks back
// through rangeSelected / rangeCleared / seekRequested and never writes its
// own selection (see PlaybackWaveform's contract).
//
// objectNames: studioTimeline, studioTimeRuler, studioTimelineLane,
// studioTimelineClip (one per clip), studioTimelineSelection,
// studioTimelinePlayhead, studioWaveform.
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

    readonly property bool hasSelection: selectionStart >= 0 && selectionEnd > selectionStart
    readonly property bool auditioning: auditionClipId !== ""
    readonly property real clipSumSeconds: {
        let sum = 0;
        for (let i = 0; i < root.clips.length; i++)
            sum += Math.max(0, Number(root.clips[i].duration) || 0);
        return sum;
    }

    signal seekRequested(real fraction)
    signal rangeSelected(real start, real end)
    signal rangeCleared()

    spacing: Theme.spacingXs

    readonly property string monoFamily: Theme.fontFamilyMono !== "" ? Theme.fontFamilyMono : Theme.fontFamily

    function formatTime(ms) {
        const s = Math.max(0, Math.round(ms / 1000));
        return ("%1:%2").arg(Math.floor(s / 60)).arg(String(s % 60).padStart(2, "0"));
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

        Row {
            id: blockRow

            anchors.fill: parent
            spacing: Theme.spacingXxs

            Repeater {
                model: root.clipModel

                Rectangle {
                    id: block

                    required property var modelData
                    required property int index

                    objectName: "studioTimelineClip"

                    readonly property string labelText: qsTr("Đoạn %1").arg(index + 1)
                    readonly property bool current: root.auditionClipId !== ""
                        && root.auditionClipId === modelData.id
                    readonly property real share: root.clipSumSeconds > 0
                        ? Math.max(0, Number(modelData.duration) || 0) / root.clipSumSeconds
                        : (root.clips.length > 0 ? 1 / root.clips.length : 0)

                    width: Math.max(2, (blockRow.width - blockRow.spacing * Math.max(0, root.clips.length - 1)) * share)
                    height: blockRow.height
                    radius: Theme.radiusSm
                    color: current ? Theme.accentSubtle : Theme.surfaceAlt
                    border.color: current ? Theme.accent : Theme.borderSubtle
                    border.width: 1
                    clip: true

                    Accessible.role: Accessible.StaticText
                    Accessible.name: labelText

                    RowLayout {
                        anchors.fill: parent
                        anchors.leftMargin: Theme.spacingSm
                        anchors.rightMargin: Theme.spacingSm
                        spacing: Theme.spacingXs
                        // Too narrow for text: the block stays a plain bar.
                        visible: block.width >= 48

                        Label {
                            Layout.fillWidth: true
                            text: block.labelText
                            color: block.current ? Theme.accent : Theme.text
                            font.family: Theme.fontFamily
                            font.pixelSize: Theme.fontSizeXs
                            font.weight: Theme.fontWeightMedium
                            elide: Text.ElideRight
                        }

                        Label {
                            visible: block.width >= 110
                            text: Number(block.modelData.duration || 0).toFixed(1) + "s"
                            color: Theme.textSubtle
                            font.family: root.monoFamily
                            font.pixelSize: Theme.fontSizeXs
                        }
                    }
                }
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
