import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import ".."

ColumnLayout {
    id: root

    objectName: root.named("DevicePicker")
    spacing: root.isCompact ? Theme.spacingSm : Theme.spacingMd

    property string objectNamePrefix: "qwen"
    property bool isCompact: false
    readonly property bool qwenRuntimeSupported: controller ? controller.qwenRuntimeSupported : false
    readonly property bool qwenRuntimeBusy: controller ? controller.qwenRuntimeBusy : false
    readonly property var qwenDeviceOptions: controller ? controller.qwenDeviceOptions : []

    function named(suffix) { return objectNamePrefix + suffix; }

    readonly property string qwenResolvedDevice: {
        for (let i = 0; i < qwenDeviceOptions.length; i++)
            if (qwenDeviceOptions[i].active)
                return qwenDeviceOptions[i].resolved;
        return "";
    }

    readonly property string qwenUnsupportedReasons: {
        const lines = [];
        for (let i = 0; i < qwenDeviceOptions.length; i++)
            if (!qwenDeviceOptions[i].supported)
                lines.push(qwenDeviceOptions[i].label + ": " + qwenDeviceOptions[i].reason);
        return lines.join("\n");
    }

    function qwenDeviceLabel(code) {
        if (code === "")
            return qsTr("đang kiểm tra…");
        for (let i = 0; i < qwenDeviceOptions.length; i++)
            if (qwenDeviceOptions[i].value === code)
                return qwenDeviceOptions[i].label;
        return code;
    }

    Flow {
        Layout.fillWidth: true
        spacing: Theme.spacingSm

        Repeater {
            model: root.qwenDeviceOptions

            AppButton {
                required property var modelData

                objectName: root.named("DeviceChip_") + modelData.value
                variant: modelData.active ? "primary" : "chip"
                size: "sm"
                text: modelData.label
                enabled: modelData.supported && !root.qwenRuntimeBusy
                disabledReason: modelData.reason
                tooltipText: modelData.reason
                accessibleLabel: modelData.supported
                    ? qsTr("Dùng thiết bị %1").arg(modelData.label)
                    : qsTr("%1 không khả dụng: %2").arg(modelData.label).arg(modelData.reason)
                onClicked: controller.setQwenDevice(modelData.value)
            }
        }
    }

    RowLayout {
        Layout.fillWidth: true
        spacing: Theme.spacingSm

        Label {
            id: qwenDeviceResolvedLabel

            objectName: root.named("DeviceResolvedLabel")
            Layout.fillWidth: true
            text: qsTr("Sẽ chạy trên: %1").arg(root.qwenDeviceLabel(root.qwenResolvedDevice))
            color: root.qwenResolvedDevice === "cpu" ? Theme.warningText : Theme.textMuted
            font.family: Theme.fontFamily
            font.pixelSize: Theme.fontSizeSm
            wrapMode: Text.Wrap
        }

        AppButton {
            id: qwenDeviceRefreshButton

            objectName: root.named("DeviceRefreshButton")
            variant: "secondary"
            size: "sm"
            iconKind: "refresh"
            text: qsTr("Kiểm tra lại")
            accessibleLabel: qsTr("Kiểm tra lại thiết bị và runtime Qwen")
            enabled: !root.qwenRuntimeBusy
            onClicked: controller.refreshQwenState()
        }
    }

    Label {
        id: qwenDeviceUnsupportedLabel

        objectName: root.named("DeviceUnsupportedLabel")
        Layout.fillWidth: true
        text: root.qwenUnsupportedReasons
        visible: root.qwenUnsupportedReasons !== ""
        color: Theme.warningText
        font.family: Theme.fontFamily
        font.pixelSize: Theme.fontSizeXs
        wrapMode: Text.Wrap
        lineHeight: 1.25
    }
}
