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

    // Square 44 px hit target at every size (audit FR-1.2); `sm` keeps its
    // 32 px visual centred inside it (AppButton._visualW/_visualH).
    implicitWidth: Math.max(Theme.controlHitTarget,
        size === "lg" ? Theme.controlHeightLg : Theme.controlHitTarget)
    implicitHeight: implicitWidth

    ToolTip.text: root.tooltipText
    ToolTip.visible: root.hovered && root.tooltipText !== ""
    ToolTip.delay: 350
}
