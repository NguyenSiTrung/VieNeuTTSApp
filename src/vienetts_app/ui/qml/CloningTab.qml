// Voice cloning tab: record/select a reference clip to build a custom voice.
// Placeholder content only (FR-2.6) — recording, preview and consent flow
// arrive in a later phase.
import QtQuick
import QtQuick.Controls
import "."

Pane {
    id: root

    objectName: "cloningTab"
    padding: Theme.spacingLg

    background: Rectangle {
        color: Theme.surface
    }

    Column {
        spacing: Theme.spacingMd
        anchors.fill: parent

        Label {
            text: qsTr("Voice Cloning")
            color: Theme.text
            font.family: Theme.fontFamily
            font.pixelSize: Theme.fontSizeXl
            font.weight: Theme.fontWeightHeading
        }

        Label {
            width: parent.width
            text: qsTr("Ghi hoặc chọn đoạn âm thanh tham chiếu (3–8 giây) để tạo giọng đọc tùy chỉnh.")
            color: Theme.textMuted
            font.family: Theme.fontFamily
            font.pixelSize: Theme.fontSizeBase
            wrapMode: Text.Wrap
        }
    }
}
