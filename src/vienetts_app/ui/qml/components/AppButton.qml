import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import ".."

// Studio button — clear hierarchy, tactile feedback, accessible.
// Variants: primary (filled accent) · secondary (surface + border) ·
//           quiet/ghost/icon (transparent text action) · danger ·
//           chip (bordered preset/micro action — real affordance, not bare text)
// Sizes: sm 32 · md 44 · lg 44 (visual). The BUTTON ITEM — its hit target —
// is never below Theme.controlHitTarget (44) in either dimension (audit
// FR-1.2): a compact `sm` button keeps its 32 px visual centred inside a
// 44 px item, so dense rows keep their look while every target is tappable.
// Disabled buttons of every variant are never filled with the accent: they
// use the controlDisabled* tokens (FR-1.5). Hierarchy: at most ONE visible
// `primary` per screen state — everything else is secondary/quiet/chip.
// Busy shows a spinner beside the ORIGINAL label — swapping the text for
// "Đang xử lý…" changed the button's width and reflowed its Flow/Row siblings.
Button {
    id: root

    property string variant: "secondary" // primary | secondary | quiet | ghost | danger | icon | chip
    property string size: "md"            // sm | md | lg
    property string iconKind: ""
    property bool tactile: true
    property bool busy: false
    property string disabledReason: ""
    property string tooltipText: ""
    property string accessibleLabel: text
    // Icon-only posture for cramped rows (e.g. the dock on a 640×420 window):
    // the label hides but stays the accessible name and, without a tooltip,
    // the hover text; the item shrinks to the 44 px square target.
    property bool iconOnly: false
    readonly property string _v: (variant === "ghost" ? "quiet" : variant)
    // Selected look for toggles and pickers (bind `checked`; the button need
    // not be checkable): secondary/chip buttons turn accent-tinted instead of
    // borrowing `primary` — primary is reserved for THE action of a screen.
    readonly property bool _selected: checked && (_v === "secondary" || _v === "chip")

    readonly property int _h: size === "sm" ? Theme.controlHeightSm
        : (size === "lg" ? Theme.controlHeightLg : Theme.controlHeightMd)
    readonly property int _f: size === "sm" ? Theme.fontSizeSm : Theme.fontSizeBase
    readonly property int _r: _v === "chip" ? Theme.radiusPill
        : (size === "sm" ? Theme.radiusSm + 2 : Theme.radiusMd)
    // Side padding — keep compact so 4× lg buttons fit at 640 px min width.
    // Totals 16/16/24 px match the original compact spec, now symmetric.
    readonly property int _padH: (size === "lg" && !iconOnly) ? Theme.spacingMd : Theme.spacingSm
    readonly property int _iconS: size === "sm" ? 16 : 18

    implicitHeight: Math.max(Theme.controlHitTarget, _h)
    implicitWidth: Math.max(Theme.controlHitTarget, _minW,
        contentLayout.implicitWidth + _padH * 2)
    // Visual (painted) size: fills the item, except that a compact `sm` button
    // keeps its 32 px visual (and a square `sm` icon its 32 px width) centred
    // in the 44 px hit target.
    readonly property real _visualH: size === "sm" ? Math.min(height, _h) : height
    readonly property real _visualW: (size === "sm" && _v === "icon") ? Math.min(width, _h) : width
    readonly property int _minW: {
        if (iconOnly) return Theme.controlHitTarget
        if (_v === "icon") return Math.max(Theme.controlHitTarget, _h)
        if (_v === "chip") return 48
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
        if (_selected) return Theme.accent
        if (_v === "chip") return (hovered || down) ? Theme.text : Theme.textMuted
        if (_v === "quiet" || _v === "icon") return Theme.text
        // secondary
        return Theme.text
    }

    readonly property color buttonBgColor: {
        if (!enabled) {
            if (_v === "quiet" || _v === "icon") return "transparent"
            // Every filled variant (primary/danger included) greys out the
            // same way: a disabled control never looks filled or tinted.
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
        if (_selected) return Theme.accentSubtle
        if (_v === "chip") {
            if (down) return Theme.isDark ? "#262d3d" : "#e2e8f0"
            if (hovered) return Theme.surfaceHover
            return Theme.surfaceAlt
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
            return Theme.controlDisabledBorder
        }
        if (_v === "primary" || _v === "quiet" || _v === "icon") return "transparent"
        if (_selected) return Theme.accent
        if (_v === "chip") return (hovered || down) ? Theme.borderFocus : Theme.borderSubtle
        if (_v === "danger") return hovered || down ? "transparent" : (Theme.isDark ? "#7f1d1d" : "#fecaca")
        // secondary — teal-tinted focus border on hover gives clear affordance
        if (hovered || down) return Theme.borderFocus
        return Theme.border
    }

    readonly property int buttonBorderWidth: {
        if (buttonBorderColor === "transparent") return 0
        // secondary + chip + danger idle + disabled outlined states
        if (_v === "secondary" || _v === "chip") return 1
        if (_v === "danger" && !hovered && !down) return 1
        if (!enabled && (_v === "primary" || _v === "secondary" || _v === "danger" || _v === "chip")) return 1
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
            text: root.text
            visible: text !== "" && !root.iconOnly
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
            anchors.centerIn: parent
            width: root._visualW
            height: root._visualH
            radius: root._r
            color: root.buttonBgColor
            border.color: root.buttonBorderColor
            border.width: root.buttonBorderWidth

            Behavior on color { ColorAnimation { duration: Theme.durationFast } }
            Behavior on border.color { ColorAnimation { duration: Theme.durationFast } }
        }

        // Inset focus ring — stays inside the button bounds so it never clips
        Rectangle {
            anchors.fill: bgRect
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

    ToolTip.text: !root.enabled && root.disabledReason !== "" ? root.disabledReason
        : (root.tooltipText !== "" ? root.tooltipText : (root.iconOnly ? root.text : ""))
    // HoverHandler stays active when disabled; Button.hovered does not —
    // without it a disabledReason tooltip never surfaces (CUDA install case).
    ToolTip.visible: (root.hovered || hoverHandler.hovered) && (ToolTip.text !== "")

    Accessible.name: root.accessibleLabel
    Accessible.description: !root.enabled ? root.disabledReason : ""
}
