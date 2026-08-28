import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import ".."

// Standard interactive button. Variants express the task hierarchy while the
// shared state treatment keeps every app surface predictable.
Button {
    id: root

    property string variant: "secondary"
    property string size: "md"
    property string iconKind: ""
    property bool tactile: true
    property bool busy: false
    property string disabledReason: ""
    property string accessibleLabel: text

    readonly property int _h: size === "sm" ? Theme.controlHeightSm
        : (size === "lg" ? Theme.controlHeightLg : Theme.controlHeightMd)
    readonly property int _f: size === "sm" ? Theme.fontSizeSm : Theme.fontSizeBase

    implicitHeight: _h
    implicitWidth: Math.max(size === "sm" ? 64 : 84,
                            contentLayout.implicitWidth + (size === "lg" ? Theme.spacingXl : Theme.spacingLg))
    leftPadding: 0
    rightPadding: 0

    scale: (tactile && down) ? 0.98 : 1.0
    Behavior on scale { NumberAnimation { duration: 80; easing.type: Easing.OutQuad } }

    readonly property bool _keyboardFocus: activeFocus
        && (focusReason === Qt.TabFocusReason || focusReason === Qt.BacktabFocusReason)

    readonly property color contentTextColor: {
        if (!enabled) return Theme.controlDisabledText
        if (variant === "primary") return Theme.accentText
        if (variant === "danger") return Theme.isDark ? Theme.errorText : "#ffffff"
        if (variant === "ghost" || variant === "quiet") return hovered ? Theme.accent : Theme.text
        return Theme.text
    }

    readonly property color buttonBgColor: {
        if (!root.enabled) return Theme.controlDisabledBg
        if (variant === "primary") return down ? Theme.accentHover : (hovered ? Theme.accentHover : Theme.accent)
        if (variant === "danger") return Theme.isDark ? (hovered || down ? Theme.error : Theme.errorSubtle) : Theme.error
        if (variant === "ghost" || variant === "quiet" || variant === "icon")
            return hovered ? Theme.surfaceHover : "transparent"
        // secondary
        return down ? Theme.surfaceHover : (hovered ? Theme.surfaceHover : Theme.surfaceAlt)
    }

    readonly property color buttonBorderColor: {
        if (!enabled) return Theme.controlDisabledBorder
        if (variant === "primary" || variant === "ghost" || variant === "quiet" || variant === "icon")
            return "transparent"
        if (variant === "danger") return Theme.isDark ? Theme.error : "transparent"
        if (variant === "secondary") return hovered ? Theme.borderFocus : Theme.border
        return "transparent"
    }

    contentItem: RowLayout {
        id: contentLayout
        spacing: Theme.spacingSm
        anchors.centerIn: parent

        AppIcon {
            visible: root.iconKind !== ""
            kind: root.iconKind
            iconColor: root.contentTextColor
            Layout.preferredWidth: 18
            Layout.preferredHeight: 18
        }

        Label {
            text: root.busy ? qsTr("Đang xử lý…") : root.text
            visible: text !== ""
            color: root.contentTextColor
            font.family: Theme.fontFamily
            font.pixelSize: root._f
            font.weight: root.variant === "primary" ? Theme.fontWeightHeading : Theme.fontWeightMedium
            horizontalAlignment: Text.AlignHCenter
            verticalAlignment: Text.AlignVCenter
        }
    }

    background: Rectangle {
        implicitWidth: 84
        implicitHeight: root._h
        radius: Theme.radiusMd
        color: root.buttonBgColor
        border.color: root.buttonBorderColor
        border.width: (root.variant === "secondary" || !root.enabled)
            && root.buttonBorderColor !== "transparent" ? 1 : 0

        // Keyboard focus ring — drawn outside the fill via a padded border rect.
        Rectangle {
            anchors.fill: parent
            anchors.margins: -2
            radius: parent.radius + 2
            color: "transparent"
            border.color: Theme.accent
            border.width: Theme.focusRingWidth
            visible: root._keyboardFocus
        }

        Behavior on color { ColorAnimation { duration: Theme.durationFast } }
        Behavior on border.color { ColorAnimation { duration: Theme.durationFast } }
    }

    ToolTip.text: root.disabledReason
    ToolTip.visible: root.hovered && !root.enabled && root.disabledReason !== ""
    ToolTip.delay: 350

    Accessible.name: root.accessibleLabel
    Accessible.description: !root.enabled ? root.disabledReason : ""
}
