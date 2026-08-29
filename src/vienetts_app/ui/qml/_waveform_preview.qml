// Design-preview harness loaded ONLY by scripts/preview_waveform_widgets.py
// (dark shell + the waveform widgets at review size). Not shipped UI.
import QtQuick
import QtQuick.Layouts
import "."

Rectangle {
    width: 720
    height: 240
    color: "#0f1117"

    ColumnLayout {
        anchors.fill: parent
        anchors.margins: 24
        spacing: 20

        WaveformIndicator {
            id: meter
            objectName: "meter"
            Layout.fillWidth: true
            Layout.preferredHeight: 64
            active: true
        }

        PlaybackWaveform {
            objectName: "overview"
            Layout.fillWidth: true
            Layout.preferredHeight: 64
        }

        // Audiobook-transport posture: wide, seekable, paused mid-chapter.
        PlaybackWaveform {
            id: abWave
            objectName: "abWave"
            Layout.fillWidth: true
            Layout.preferredHeight: 52
        }
    }
}
