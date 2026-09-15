import QtQuick
import QtQuick.Layouts
import ".."

// One FX-rack module: an inset surfaceAlt strip with a tracked micro-label
// header (channel-strip style). Used by StudioTab's rack so every module
// shares the same chrome instead of repeating the Rectangle+header boilerplate.
Rectangle {
    id: root

    property string title: ""

    default property alias content: moduleColumn.data

    radius: Theme.radiusMd
    color: Theme.surfaceAlt
    border.color: Theme.borderSubtle
    border.width: 1
    implicitHeight: moduleColumn.implicitHeight + Theme.spacingMd * 2
    implicitWidth: moduleColumn.implicitWidth + Theme.spacingMd * 2

    ColumnLayout {
        id: moduleColumn
        anchors.fill: parent
        anchors.margins: Theme.spacingMd
        spacing: Theme.spacingMd

        SectionLabel {
            Layout.fillWidth: true
            text: root.title
            color: Theme.accent
            visible: root.title !== ""
        }
    }
}
