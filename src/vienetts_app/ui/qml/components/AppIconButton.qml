import QtQuick
import QtQuick.Controls
import "."
import ".."

// Compact icon-only action — same skin as AppButton variant "icon" but
// with a guaranteed hit-target and an accessible tooltip.
AppButton {
    id: root

    property string tooltipText: ""

    variant: "icon"
    size: "md"
    text: ""
    iconKind: ""

    // Keep square hit-target per size; let AppButton's _minW handle md=40,
    // but override for sm/lg so icon transport stays balanced.
    implicitWidth: size === "sm" ? Theme.controlHeightSm
        : (size === "lg" ? Theme.controlHeightLg : Theme.controlHitTarget)
    implicitHeight: size === "sm" ? Theme.controlHeightSm
        : (size === "lg" ? Theme.controlHeightLg : Theme.controlHitTarget)

    ToolTip.text: root.tooltipText
    ToolTip.visible: root.hovered && root.tooltipText !== ""
    ToolTip.delay: 350
}
