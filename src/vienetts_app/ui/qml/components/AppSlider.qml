import QtQuick
import QtQuick.Controls
import ".."

// Slider-derived transport control with the same focus and disabled treatments
// as field controls.
Slider {
    id: root

    readonly property string controlKind: "slider"
    property string accessibleLabel: ""

    implicitHeight: Theme.controlHitTarget
    leftPadding: Theme.spacingSm
    rightPadding: Theme.spacingSm

    background: Rectangle {
        x: root.leftPadding
        y: root.topPadding + root.availableHeight / 2 - height / 2
        width: root.availableWidth
        height: 6
        radius: 3
        color: root.enabled ? Theme.surfaceAlt : Theme.controlDisabledBg

        Rectangle {
            width: root.visualPosition * parent.width
            height: parent.height
            radius: parent.radius
            color: root.enabled ? Theme.accent : Theme.controlDisabledText
        }
    }

    handle: Rectangle {
        x: root.leftPadding + root.visualPosition * (root.availableWidth - width)
        y: root.topPadding + root.availableHeight / 2 - height / 2
        width: 16
        height: 16
        radius: 8
        color: root.enabled ? Theme.accent : Theme.controlDisabledText
        border.width: root.activeFocus ? Theme.focusRingWidth : 2
        border.color: root.activeFocus ? Theme.accentHover : Theme.bg
    }

    Accessible.name: root.accessibleLabel
}
