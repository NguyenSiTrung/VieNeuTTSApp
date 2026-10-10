import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "../components"
import ".."

// Driver-guide commands are copy targets, not prose: a user pasting
// `sudo ubuntu-drivers autoinstall` should never have to retype it.
Rectangle {
    id: commandRow

    required property string command

    Layout.fillWidth: true
    implicitHeight: commandLabel.implicitHeight + Theme.spacingSm * 2
    radius: Theme.radiusSm
    // surfaceAlt, not surface: the row sits on a surfaceCard-colored card
    // and `surface` is the same white in light mode.
    color: Theme.surfaceAlt
    border.color: Theme.borderSubtle
    border.width: 1

    RowLayout {
        anchors.fill: parent
        anchors.leftMargin: Theme.spacingSm
        anchors.rightMargin: Theme.spacingXxs
        spacing: Theme.spacingXs

        Label {
            id: commandLabel
            Layout.fillWidth: true
            text: commandRow.command
            elide: Text.ElideRight
            color: Theme.text
            font.family: Theme.fontFamilyMono !== "" ? Theme.fontFamilyMono : Theme.fontFamily
            font.pixelSize: Theme.fontSizeSm
        }

        AppIconButton {
            size: "sm"
            iconKind: "copy"
            tooltipText: qsTr("Sao chép lệnh")
            accessibleLabel: qsTr("Sao chép lệnh: %1").arg(commandRow.command)
            onClicked: controller.copyText(commandRow.command)
        }
    }
}
