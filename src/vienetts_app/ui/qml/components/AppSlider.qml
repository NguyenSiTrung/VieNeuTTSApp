import QtQuick
import QtQuick.Controls
import ".."

// Slider-derived transport control with the same focus and disabled treatments
// as field controls.
Slider {
    id: root

    readonly property string controlKind: "slider"
    property string accessibleLabel: ""

    // A slider needs a real default width: hosts put it in a RowLayout with
    // `Layout.fillWidth: true` beside fixed-size siblings, and a layout will
    // happily squeeze a fillWidth item all the way to 0 px (measured at the
    // 640 px minimum window width: the Studio rack's speed/fade sliders
    // collapsed and their handles landed on top of the preset buttons).
    // Layout attached properties cannot be set from inside the component's own
    // definition, so `minimumTrackWidth` is the documented handle each host
    // binds — `Layout.minimumWidth: slider.minimumTrackWidth` — which is what
    // actually floors the squeeze.
    readonly property int minimumTrackWidth: 120

    implicitWidth: 160
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
