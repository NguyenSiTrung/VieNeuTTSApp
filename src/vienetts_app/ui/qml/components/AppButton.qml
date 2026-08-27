import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import ".."

// Standard interactive button — the ONLY button skin in the app (tabs must not
// hand-roll inline Button backgrounds). Variants carry the visual hierarchy:
//   primary   — filled accent, the single hero action of a region
//   secondary — bordered quiet surface for regular actions
//   ghost     — borderless, for tertiary/inline actions
//   danger    — error-toned, for destructive actions
// Sizes: "sm" (28px, compact rows) | "md" (36px, default) | "lg" (42px, hero CTA).
// Keyboard users get a focus ring on Tab navigation only (click focus stays clean).
Button {
    id: root

    property string variant: "secondary"
    property string size: "md"
    property string glyph: ""   // optional leading vector glyph (▶ ✕ …), not emoji
    property bool tactile: true

    readonly property int _h: size === "sm" ? 28 : (size === "lg" ? 42 : 36)
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
        if (!enabled) return Theme.textSubtle
        if (variant === "primary") return Theme.accentText
        if (variant === "danger") return Theme.isDark ? Theme.errorText : "#ffffff"
        if (variant === "ghost") return hovered ? Theme.accent : Theme.text
        return Theme.text
    }

    readonly property color buttonBgColor: {
        if (!root.enabled) return Theme.isDark ? "#1f232d" : "#e9edf2"
        if (variant === "primary") return down ? Theme.accentHover : (hovered ? Theme.accentHover : Theme.accent)
        if (variant === "danger") return Theme.isDark ? (hovered || down ? Theme.error : Theme.errorSubtle) : Theme.error
        if (variant === "ghost") return hovered ? Theme.surfaceHover : "transparent"
        // secondary
        return down ? Theme.surfaceHover : (hovered ? Theme.surfaceHover : Theme.surfaceAlt)
    }

    readonly property color buttonBorderColor: {
        if (!enabled) return "transparent"
        if (variant === "primary" || variant === "ghost") return "transparent"
        if (variant === "danger") return Theme.isDark ? Theme.error : "transparent"
        if (variant === "secondary") return hovered ? Theme.borderFocus : Theme.border
        return "transparent"
    }

    contentItem: RowLayout {
        id: contentLayout
        spacing: Theme.spacingSm
        anchors.centerIn: parent

        Label {
            text: root.glyph
            visible: root.glyph !== ""
            color: root.contentTextColor
            font.family: Theme.fontFamily
            font.pixelSize: root._f - 2
        }

        Label {
            text: root.text
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
        border.width: root.buttonBorderColor !== "transparent" ? 1 : 0

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
}
