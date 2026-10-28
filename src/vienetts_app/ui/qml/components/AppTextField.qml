import QtQuick
import QtQuick.Controls
import ".."

// Shared single-line field: one border, one focus ring, one disabled/selection
// treatment. Hosts pass `accessibleLabel` so every field has a spoken name.
//
// The height comes from Theme.controlHeightSm (32 px); the surrounding layout
// supplies the width.
TextField {
    id: root

    property string accessibleLabel: ""
    readonly property bool passwordEcho: root.echoMode === TextInput.Password

    implicitHeight: Theme.controlHeightSm
    leftPadding: Theme.spacingSm
    rightPadding: Theme.spacingSm
    color: Theme.text
    placeholderTextColor: Theme.textSubtle
    selectedTextColor: Theme.accentText
    selectionColor: Theme.accent
    font.family: Theme.fontFamily
    font.pixelSize: Theme.fontSizeSm
    Accessible.name: root.accessibleLabel

    background: Rectangle {
        radius: Theme.radiusSm
        color: Theme.surface
        border.width: 1
        border.color: root.activeFocus ? Theme.accent : Theme.borderSubtle
    }
}
