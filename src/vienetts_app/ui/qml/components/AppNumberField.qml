import QtQuick
import QtQuick.Controls
import ".."

// SpinBox-derived numeric field that preserves the native value/displayText
// interface while presenting a consistent field surface.
SpinBox {
    id: root

    readonly property string controlKind: "number"
    property string accessibleLabel: ""
    property int decimals: 2
    property int scaleFactor: 100
    readonly property real realValue: value / scaleFactor

    implicitWidth: 140
    implicitHeight: Theme.controlHeightMd
    editable: true

    textFromValue: function(value, locale) {
        return Number(value / root.scaleFactor).toLocaleString(locale, "f", root.decimals);
    }

    valueFromText: function(text, locale) {
        return Math.round(Number.fromLocaleString(locale, text) * root.scaleFactor);
    }

    contentItem: TextInput {
        z: 2
        text: root.textFromValue(root.value, root.locale)
        font.family: Theme.fontFamily
        font.pixelSize: Theme.fontSizeBase
        color: root.enabled ? Theme.text : Theme.controlDisabledText
        selectionColor: Theme.accent
        selectedTextColor: Theme.accentText
        horizontalAlignment: Qt.AlignHCenter
        verticalAlignment: Qt.AlignVCenter
        leftPadding: root.mirrored ? 28 : Theme.spacingSm
        rightPadding: root.mirrored ? Theme.spacingSm : 28
        readOnly: !root.editable
        validator: root.validator
        inputMethodHints: Qt.ImhFormattedNumbersOnly
    }

    background: Rectangle {
        implicitWidth: root.implicitWidth
        implicitHeight: root.implicitHeight
        radius: Theme.radiusSm
        color: root.enabled ? Theme.surface : Theme.controlDisabledBg
        border.width: root.activeFocus ? Theme.focusRingWidth : 1
        border.color: root.activeFocus ? Theme.accent
            : (root.enabled ? Theme.borderSubtle : Theme.controlDisabledBorder)
    }

    up.indicator: Item {
        x: root.mirrored ? 0 : root.width - width
        y: 0
        implicitWidth: 28
        implicitHeight: root.height / 2
        width: 28
        height: root.height / 2

        AppIcon {
            anchors.centerIn: parent
            width: 12
            height: 12
            kind: "chevronUp"
            iconColor: !root.enabled ? Theme.controlDisabledText
                : (root.up.pressed ? Theme.accent
                : (root.up.hovered ? Theme.text : Theme.textMuted))
        }
    }

    down.indicator: Item {
        x: root.mirrored ? 0 : root.width - width
        y: root.height / 2
        implicitWidth: 28
        implicitHeight: root.height / 2
        width: 28
        height: root.height / 2

        AppIcon {
            anchors.centerIn: parent
            width: 12
            height: 12
            kind: "chevronDown"
            iconColor: !root.enabled ? Theme.controlDisabledText
                : (root.down.pressed ? Theme.accent
                : (root.down.hovered ? Theme.text : Theme.textMuted))
        }
    }

    Accessible.name: root.accessibleLabel
}
