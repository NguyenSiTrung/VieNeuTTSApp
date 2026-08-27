// Text tab: free-text TTS. Placeholder content only (FR-2.6) — real input,
// voice picker and generate controls arrive in Phase 3.
import QtQuick
import QtQuick.Controls
import "."

Pane {
    id: root

    objectName: "textTab"
    padding: Theme.spacingLg

    background: Rectangle {
        color: Theme.surface
    }

    Column {
        spacing: Theme.spacingMd
        anchors.fill: parent

        Label {
            text: qsTr("Text to Speech")
            color: Theme.text
            font.family: Theme.fontFamily
            font.pixelSize: Theme.fontSizeXl
            font.weight: Theme.fontWeightHeading
        }

        Label {
            width: parent.width
            text: qsTr("Nhập hoặc dán văn bản tiếng Việt, chọn giọng đọc và tạo âm thanh.")
            color: Theme.textMuted
            font.family: Theme.fontFamily
            font.pixelSize: Theme.fontSizeBase
            wrapMode: Text.Wrap
        }
    }
}
