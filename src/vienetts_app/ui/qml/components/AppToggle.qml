import QtQuick
import QtQuick.Controls
import ".."

// CheckBox-derived setting control, keeping Qt's native checked/click contract
// while making focus, state, and hit area consistent with other controls.
CheckBox {
    id: root

    readonly property string controlKind: "toggle"
    property string accessibleLabel: text

    implicitHeight: Theme.controlHitTarget
    spacing: Theme.spacingSm

    indicator: Rectangle {
        implicitWidth: 20
        implicitHeight: 20
        x: root.leftPadding
        y: parent.height / 2 - height / 2
        radius: Theme.radiusSm
        color: root.checked ? Theme.accent : Theme.surface
        border.color: root.activeFocus ? Theme.accent : Theme.border
        border.width: root.activeFocus ? Theme.focusRingWidth : 1

        AppIcon {
            anchors.centerIn: parent
            visible: root.checked
            width: 14
            height: 14
            kind: "check"
            iconColor: Theme.accentText
        }

        Behavior on color { ColorAnimation { duration: Theme.durationFast } }
        Behavior on border.color { ColorAnimation { duration: Theme.durationFast } }
    }

    contentItem: Text {
        text: root.text
        color: root.enabled ? Theme.text : Theme.controlDisabledText
        font.family: Theme.fontFamily
        font.pixelSize: Theme.fontSizeSm
        leftPadding: root.indicator.width + root.spacing
        verticalAlignment: Text.AlignVCenter
    }

    Accessible.name: root.accessibleLabel
}
