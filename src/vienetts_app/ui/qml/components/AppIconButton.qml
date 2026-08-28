import QtQuick
import QtQuick.Controls
import "."
import ".."

// Accessible icon-only control for compact row actions and transport.
AppButton {
    id: root

    property string tooltipText: ""

    variant: "icon"
    size: "md"
    text: ""
    implicitWidth: Theme.controlHitTarget
    iconKind: ""

    ToolTip.text: root.tooltipText
    ToolTip.visible: root.hovered && root.tooltipText !== ""
    ToolTip.delay: 350
}
