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

    up.indicator: AppIcon {
        x: root.width - width - Theme.spacingSm
        y: root.height / 2 - height - 1
        width: 12
        height: 12
        kind: "chevronDown"
        rotation: 180
        iconColor: root.enabled ? Theme.textMuted : Theme.controlDisabledText
    }

    down.indicator: AppIcon {
        x: root.width - width - Theme.spacingSm
        y: root.height / 2 + 1
        width: 12
        height: 12
        kind: "chevronDown"
        iconColor: root.enabled ? Theme.textMuted : Theme.controlDisabledText
    }

    Accessible.name: root.accessibleLabel
}
