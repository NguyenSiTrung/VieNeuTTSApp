import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import ".."

Button {
    id: root

    property string variant: "secondary" // "primary" | "secondary" | "ghost" | "danger" | "success"
    property string iconText: ""
    property color customColor: "transparent"
    property color customTextColor: "transparent"
    property int customRadius: Theme.radiusMd
    property bool tactile: true

    implicitHeight: 36
    implicitWidth: Math.max(80, contentLayout.implicitWidth + Theme.spacingXl)

    scale: (tactile && down) ? 0.98 : 1.0
    Behavior on scale { NumberAnimation { duration: 80; easing.type: Easing.OutQuad } }

    contentItem: RowLayout {
        id: contentLayout
        spacing: Theme.spacingSm
        anchors.centerIn: parent

        Label {
            text: root.iconText
            font.family: Theme.fontFamily
            font.pixelSize: Theme.fontSizeBase
            visible: root.iconText !== ""
            color: root.contentTextColor
        }

        Label {
            text: root.text
            font.family: Theme.fontFamily
            font.pixelSize: Theme.fontSizeBase
            font.weight: root.variant === "primary" ? Theme.fontWeightHeading : Theme.fontWeightMedium
            color: root.contentTextColor
            horizontalAlignment: Text.AlignHCenter
            verticalAlignment: Text.AlignVCenter
        }
    }

    readonly property color contentTextColor: {
        if (!root.enabled) return Theme.textSubtle
        if (customTextColor !== "transparent") return customTextColor
        if (variant === "primary") return Theme.accentText
        if (variant === "danger") return Theme.isDark ? Theme.errorText : "#ffffff"
        if (variant === "success") return "#ffffff"
        if (variant === "ghost") return hovered ? Theme.accent : Theme.text
        return Theme.text
    }

    readonly property color buttonBgColor: {
        if (!root.enabled) return Theme.isDark ? "#1f232d" : "#e2e8f0"
        if (customColor !== "transparent") return customColor
        if (variant === "primary") return down ? Theme.accentHover : (hovered ? Theme.accentHover : Theme.accent)
        if (variant === "danger") return down ? Theme.error : (hovered ? Theme.error : (Theme.isDark ? Theme.errorSubtle : Theme.error))
        if (variant === "success") return down ? Theme.success : (hovered ? Theme.success : Theme.success)
        if (variant === "ghost") return hovered ? Theme.surfaceHover : "transparent"
        // secondary
        return down ? Theme.surfaceHover : (hovered ? Theme.surfaceHover : Theme.surfaceAlt)
    }

    readonly property color buttonBorderColor: {
        if (!root.enabled) return "transparent"
        if (variant === "primary" || variant === "ghost") return "transparent"
        if (variant === "secondary") return hovered ? Theme.borderFocus : Theme.border
        if (variant === "danger") return Theme.isDark ? Theme.error : "transparent"
        return "transparent"
    }

    background: Rectangle {
        implicitWidth: 80
        implicitHeight: 36
        radius: root.customRadius
        color: root.buttonBgColor
        border.color: root.buttonBorderColor
        border.width: root.buttonBorderColor !== "transparent" ? 1 : 0

        Behavior on color { ColorAnimation { duration: 120 } }
        Behavior on border.color { ColorAnimation { duration: 120 } }
    }
}
