// Paragraph/File tab: long-text and .txt import workflow. Placeholder
// content only (FR-2.6) — file import and per-paragraph queueing arrive later.
import QtQuick
import QtQuick.Controls
import "."

Pane {
    id: root

    objectName: "paragraphTab"
    padding: Theme.spacingLg

    background: Rectangle {
        color: Theme.surface
    }

    Column {
        spacing: Theme.spacingMd
        anchors.fill: parent

        Label {
            text: qsTr("Paragraph / File")
            color: Theme.text
            font.family: Theme.fontFamily
            font.pixelSize: Theme.fontSizeXl
            font.weight: Theme.fontWeightHeading
        }

        Label {
            width: parent.width
            text: qsTr("Nhập văn bản dài từ tệp .txt để tổng hợp theo từng đoạn.")
            color: Theme.textMuted
            font.family: Theme.fontFamily
            font.pixelSize: Theme.fontSizeBase
            wrapMode: Text.Wrap
        }
    }
}
