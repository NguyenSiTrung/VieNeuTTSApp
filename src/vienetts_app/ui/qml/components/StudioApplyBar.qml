import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import ".."

// Commit row under the Hiệu ứng panel: "Bỏ" drops every staged edit,
// "Áp dụng N thay đổi" pushes them onto the op stack as ONE undo step. The
// apply button is the Studio screen's only primary action.
//
// objectNames: studioApplyBar (root), studioClearPendingButton,
// studioApplyButton.
RowLayout {
    id: bar

    objectName: "studioApplyBar"

    property bool rackEnabled: false
    readonly property int count: controller.studioPendingCount || 0
    readonly property bool applying: controller.studioBusyKind === "apply"

    spacing: Theme.spacingSm

    Item { Layout.fillWidth: true }

    AppButton {
        objectName: "studioClearPendingButton"
        variant: "secondary"
        text: qsTr("Bỏ")
        tooltipText: qsTr("Bỏ mọi thay đổi chưa áp dụng")
        enabled: bar.count > 0 && !bar.applying
        onClicked: controller.studioClearPending()
    }

    AppButton {
        objectName: "studioApplyButton"
        variant: "primary"
        text: bar.count > 0 ? qsTr("Áp dụng %n thay đổi", "", bar.count) : qsTr("Áp dụng thay đổi")
        tooltipText: qsTr("Áp dụng mọi thay đổi đang chờ trong một bước (hoàn tác được)")
        enabled: bar.count > 0 && bar.rackEnabled
        busy: bar.applying
        onClicked: controller.studioApplyPending()
    }
}
