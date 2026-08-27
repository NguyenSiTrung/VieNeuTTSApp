// Settings tab: engine, output and appearance configuration. Placeholder
// content only (FR-2.6) — real controls bind to the settings bridge later.
import QtQuick
import QtQuick.Controls
import "."

Pane {
    id: root

    objectName: "settingsTab"
    padding: Theme.spacingLg

    background: Rectangle {
        color: Theme.surface
    }

    Column {
        spacing: Theme.spacingMd
        anchors.fill: parent

        Label {
            text: qsTr("Settings")
            color: Theme.text
            font.family: Theme.fontFamily
            font.pixelSize: Theme.fontSizeXl
            font.weight: Theme.fontWeightHeading
        }

        Label {
            width: parent.width
            text: qsTr("Cấu hình engine, thư mục xuất âm thanh và giao diện sáng/tối.")
            color: Theme.textMuted
            font.family: Theme.fontFamily
            font.pixelSize: Theme.fontSizeBase
            wrapMode: Text.Wrap
        }
    }
}
