import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import ".."

// Clickable pill that inserts a speech-emotion tag into the focused editor.
// Both signals fire on click: `clicked()` for generic handling and
// `chipClicked(tag)` for semantic tag-aware handling.
Rectangle {
    id: root

    property string tag: ""
    property string label: ""

    signal clicked()
    signal chipClicked(string insertedTag)

    // Screen-reader contract: the pill behaves as a button that inserts
    // the emotion tag (same label the sighted user sees).
    Accessible.role: Accessible.Button
    Accessible.name: root.label !== "" ? root.label : root.tag
    Accessible.description: root.tag
    Accessible.onPressAction: {
        root.clicked()
        root.chipClicked(root.tag)
    }

    implicitHeight: 28
    implicitWidth: chipRow.implicitWidth + Theme.spacingMd * 2
    radius: Theme.radiusPill

    color: mouseArea.pressed ? Theme.accentSubtle : (mouseArea.containsMouse ? Theme.accentSubtle : Theme.surfaceAlt)
    border.color: mouseArea.containsMouse ? Theme.accent : Theme.border
    border.width: 1

    scale: mouseArea.pressed ? 0.96 : (mouseArea.containsMouse ? 1.02 : 1.0)
    Behavior on scale { NumberAnimation { duration: 100; easing.type: Easing.OutQuad } }
    Behavior on color { ColorAnimation { duration: Theme.durationFast } }
    Behavior on border.color { ColorAnimation { duration: Theme.durationFast } }

    RowLayout {
        id: chipRow
        anchors.centerIn: parent
        spacing: Theme.spacingXs

        // Subtle accent dot
        Rectangle {
            implicitWidth: 6
            implicitHeight: 6
            radius: 3
            color: mouseArea.containsMouse ? Theme.accent : Theme.textSubtle
            Layout.alignment: Qt.AlignVCenter
            Behavior on color { ColorAnimation { duration: Theme.durationFast } }
        }

        Label {
            text: root.label !== "" ? root.label : root.tag
            color: mouseArea.containsMouse ? Theme.accent : Theme.text
            font.family: Theme.fontFamily
            font.pixelSize: Theme.fontSizeSm
            font.weight: Theme.fontWeightMedium
        }
    }

    ToolTip {
        text: root.tag
        delay: 400
        timeout: 2200
    }

    MouseArea {
        id: mouseArea
        anchors.fill: parent
        hoverEnabled: true
        cursorShape: Qt.PointingHandCursor
        onClicked: {
            root.clicked()
            root.chipClicked(root.tag)
        }
    }
}
