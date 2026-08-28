import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import ".."

// Studio button — clear hierarchy, tactile feedback, accessible.
// Variants: primary (filled accent) · secondary (surface + border) ·
//           quiet/ghost/icon (transparent text action) · danger
// Sizes: sm 32 · md 40 · lg 44.  Busy shows a spinner and "Đang xử lý…".
Button {
    id: root

    property string variant: "secondary" // primary | secondary | quiet | ghost | danger | icon
    property string size: "md"            // sm | md | lg
    property string iconKind: ""
    property bool tactile: true
    property bool busy: false
    property string disabledReason: ""
    property string accessibleLabel: text

    readonly property string _v: (variant === "ghost" ? "quiet" : variant)

    readonly property int _h: size === "sm" ? Theme.controlHeightSm
        : (size === "lg" ? Theme.controlHeightLg : Theme.controlHeightMd)
    readonly property int _f: size === "sm" ? Theme.fontSizeSm : Theme.fontSizeBase
    readonly property int _r: size === "sm" ? Theme.radiusSm + 2 : Theme.radiusMd
    // Side padding — keep compact so 4× lg buttons fit at 640 px min width.
    // Totals 16/16/24 px match the original compact spec, now symmetric.
    readonly property int _padH: size === "lg" ? Theme.spacingMd : Theme.spacingSm
    readonly property int _iconS: size === "sm" ? 16 : 18

    implicitHeight: _h
    implicitWidth: Math.max(_minW, contentLayout.implicitWidth + _padH * 2)
    readonly property int _minW: {
        if (_v === "icon") return Theme.controlHitTarget
        if (size === "sm") return 64
        return 84
    }

    leftPadding: _padH
    rightPadding: _padH
    topPadding: 0
    bottomPadding: 0

    scale: (tactile && down && enabled) ? 0.97 : 1.0
    Behavior on scale { NumberAnimation { duration: 90; easing.type: Easing.OutQuad } }

    readonly property bool _keyboardFocus: activeFocus
        && (focusReason === Qt.TabFocusReason || focusReason === Qt.BacktabFocusReason)

    readonly property color contentTextColor: {
        if (!enabled) return Theme.controlDisabledText
        if (_v === "primary") return Theme.accentText
        if (_v === "danger") {
            if (Theme.isDark) return (hovered || down) ? "#ffffff" : Theme.errorText
            return "#ffffff"
        }
        if (_v === "quiet" || _v === "icon") return Theme.text
        // secondary
        return Theme.text
    }

    readonly property color buttonBgColor: {
        if (!enabled) {
            if (_v === "quiet" || _v === "icon") return "transparent"
            if (_v === "primary") return Theme.isDark ? "#132d2b" : "#dff6f1"
            if (_v === "danger") return Theme.isDark ? "#231515" : "#fef2f2"
            return Theme.controlDisabledBg
        }
        if (_v === "primary") {
            if (down) return Theme.isDark ? "#14b8a6" : "#0d5c57"
            if (hovered) return Theme.accentHover
            return Theme.accent
        }
        if (_v === "danger") {
            if (down) return Theme.isDark ? "#dc2626" : "#991b1b"
            if (hovered) return Theme.error
            // idle danger: subtle in dark, solid in light
            return Theme.isDark ? Theme.errorSubtle : Theme.error
        }
        if (_v === "quiet" || _v === "icon") {
            if (down) return Theme.isDark ? "#262d3d" : "#e2e8f0"
            if (hovered) return Theme.isDark ? "#1f2535" : "#f1f5f9"
            return "transparent"
        }
        // secondary
        if (down) return Theme.isDark ? "#252c3c" : "#e2e8f0"
        if (hovered) return Theme.surfaceHover
        return Theme.surfaceAlt
    }

    readonly property color buttonBorderColor: {
        if (!enabled) {
            if (_v === "quiet" || _v === "icon") return "transparent"
            if (_v === "primary" || _v === "danger") return Theme.isDark ? "#243a38" : "#cbd5e1"
            return Theme.controlDisabledBorder
        }
        if (_v === "primary" || _v === "quiet" || _v === "icon") return "transparent"
        if (_v === "danger") return hovered || down ? "transparent" : (Theme.isDark ? "#7f1d1d" : "#fecaca")
        // secondary — teal-tinted focus border on hover gives clear affordance
        if (hovered || down) return Theme.borderFocus
        return Theme.border
    }

    readonly property int buttonBorderWidth: {
        if (buttonBorderColor === "transparent") return 0
        // secondary + danger idle + disabled outlined states
        if (_v === "secondary") return 1
        if (_v === "danger" && !hovered && !down) return 1
        if (!enabled && (_v === "primary" || _v === "secondary" || _v === "danger")) return 1
        return 0
    }

    HoverHandler {
        id: hoverHandler
        cursorShape: root.enabled ? Qt.PointingHandCursor : Qt.ArrowCursor
    }

    contentItem: RowLayout {
        id: contentLayout
        spacing: Theme.spacingSm
        anchors.centerIn: parent

        AppIcon {
            id: normalIcon
            visible: root.iconKind !== "" && !root.busy
            kind: root.iconKind
            iconColor: root.contentTextColor
            Layout.preferredWidth: root._iconS
            Layout.preferredHeight: root._iconS
            opacity: root.enabled ? 1.0 : 0.55
        }

        AppIcon {
            id: busySpinner
            visible: root.busy
            kind: "spinner"
            iconColor: root.contentTextColor
            Layout.preferredWidth: 16
            Layout.preferredHeight: 16
            opacity: root.enabled ? 1.0 : 0.6

            RotationAnimation on rotation {
                running: root.busy
                loops: Animation.Infinite
                from: 0
                to: 360
                duration: 800
            }
        }

        Label {
            text: root.busy ? qsTr("Đang xử lý…") : root.text
            visible: text !== ""
            color: root.contentTextColor
            font.family: Theme.fontFamily
            font.pixelSize: root._f
            font.weight: _v === "primary" ? Theme.fontWeightHeading : Theme.fontWeightMedium
            font.letterSpacing: _v === "primary" ? 0.15 : 0
            horizontalAlignment: Text.AlignHCenter
            verticalAlignment: Text.AlignVCenter
            elide: Text.ElideRight
            opacity: root.enabled ? 1.0 : 0.85
        }
    }

    background: Item {
        implicitWidth: root._minW
        implicitHeight: root._h

        // Subtle elevation for the primary CTA only
        Rectangle {
            anchors.fill: bgRect
            anchors.topMargin: 2
            radius: bgRect.radius
            color: Theme.shadowColor
            opacity: (_v === "primary" && root.enabled) ? (root.hovered ? 0.22 : 0.16) : 0
            visible: _v === "primary" && root.enabled
            Behavior on opacity { NumberAnimation { duration: Theme.durationFast } }
        }

        Rectangle {
            id: bgRect
            anchors.fill: parent
            radius: root._r
            color: root.buttonBgColor
            border.color: root.buttonBorderColor
            border.width: root.buttonBorderWidth

            Behavior on color { ColorAnimation { duration: Theme.durationFast } }
            Behavior on border.color { ColorAnimation { duration: Theme.durationFast } }
        }

        // Inset focus ring — stays inside the button bounds so it never clips
        Rectangle {
            anchors.fill: parent
            anchors.margins: 1
            radius: root._r - 1
            color: "transparent"
            border.color: Theme.borderFocus
            border.width: Theme.focusRingWidth
            visible: root._keyboardFocus
            opacity: 0.95
        }

        // Pressed inner shade for depth
        Rectangle {
            anchors.fill: bgRect
            radius: bgRect.radius
            color: "#0a0f1a"
            opacity: (root.down && root.enabled && (_v === "primary" || _v === "secondary")) ? 0.08 : 0
            Behavior on opacity { NumberAnimation { duration: 80 } }
        }
    }

    ToolTip.text: root.disabledReason
    ToolTip.visible: root.hovered && !root.enabled && root.disabledReason !== ""
    ToolTip.delay: 350

    Accessible.name: root.accessibleLabel
    Accessible.description: !root.enabled ? root.disabledReason : ""
}
