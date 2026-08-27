import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import ".."

Rectangle {
    id: root

    property string tag: ""
    property string label: ""
    property string emoji: ""
    
    signal chipClicked(string insertedTag)

    implicitHeight: 28
    implicitWidth: chipRow.implicitWidth + Theme.spacingMd * 2
    radius: Theme.radiusPill

    color: mouseArea.pressed ? Theme.accentSubtle : (mouseArea.containsMouse ? Theme.accentSubtle : Theme.surfaceAlt)
    border.color: mouseArea.containsMouse ? Theme.accent : Theme.border
    border.width: 1

    scale: mouseArea.pressed ? 0.96 : (mouseArea.containsMouse ? 1.02 : 1.0)
    Behavior on scale { NumberAnimation { duration: 100; easing.type: Easing.OutQuad } }
    Behavior on color { ColorAnimation { duration: 120 } }
    Behavior on border.color { ColorAnimation { duration: 120 } }

    RowLayout {
        id: chipRow
        anchors.centerIn: parent
        spacing: Theme.spacingXs

        Label {
            text: root.emoji
            visible: root.emoji !== ""
            font.pixelSize: Theme.fontSizeSm
        }

        Label {
            text: root.label !== "" ? root.label : root.tag
            color: mouseArea.containsMouse ? Theme.accent : Theme.text
            font.family: Theme.fontFamily
            font.pixelSize: Theme.fontSizeSm
            font.weight: Theme.fontWeightMedium
        }
    }

    MouseArea {
        id: mouseArea
        anchors.fill: parent
        hoverEnabled: true
        cursorShape: Qt.PointingHandCursor
        onClicked: root.chipClicked(root.tag)
    }
}
