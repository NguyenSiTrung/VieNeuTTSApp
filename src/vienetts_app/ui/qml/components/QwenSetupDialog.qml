import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import ".."

Dialog {
    id: root

    objectName: "qwenSetupDialog"
    title: qsTr("Cài đặt Qwen")
    modal: true
    focus: true
    anchors.centerIn: Overlay.overlay
    width: Math.min(
        (Overlay.overlay ? Overlay.overlay.width : 640) - Theme.spacingXl * 2,
        680
    )
    height: Math.min(
        (Overlay.overlay ? Overlay.overlay.height : 420) - Theme.spacingXl * 2,
        620
    )
    padding: Theme.spacingLg

    property bool isCompact: false

    header: Label {
        text: root.title
        color: Theme.text
        font.family: Theme.fontFamily
        font.pixelSize: Theme.fontSizeMd
        font.weight: Theme.fontWeightHeading
        wrapMode: Text.Wrap
        leftPadding: Theme.spacingLg
        rightPadding: Theme.spacingLg
        topPadding: Theme.spacingLg
    }

    background: Rectangle {
        radius: Theme.radiusLg
        color: Theme.surfaceCard
        border.color: Theme.border
        border.width: 1
    }

    contentItem: ScrollView {
        id: setupScroll

        clip: true
        ScrollBar.horizontal.policy: ScrollBar.AlwaysOff

        ColumnLayout {
            width: setupScroll.availableWidth
            spacing: Theme.spacingMd

            Label {
                Layout.fillWidth: true
                text: qsTr("Hồ sơ Qwen đã chọn cần runtime và mô hình trước khi tổng hợp. Hoàn tất các bước bên dưới — hồ sơ vẫn được giữ nếu bạn đóng cửa sổ này.")
                color: Theme.textMuted
                font.family: Theme.fontFamily
                font.pixelSize: Theme.fontSizeSm
                wrapMode: Text.Wrap
                lineHeight: 1.25
            }

            Label {
                objectName: "qwenSetupProfileLabel"
                Layout.fillWidth: true
                text: qsTr("Hồ sơ: %1").arg(EngineState.profileLabel)
                color: Theme.text
                font.family: Theme.fontFamily
                font.pixelSize: Theme.fontSizeSm
                font.weight: Theme.fontWeightMedium
                wrapMode: Text.Wrap
            }

            QwenVariantPicker {
                Layout.fillWidth: true
                objectNamePrefix: "qwenSetup"
            }

            QwenDevicePicker {
                Layout.fillWidth: true
                objectNamePrefix: "qwenSetup"
                isCompact: root.isCompact
            }

            QwenInstallCards {
                Layout.fillWidth: true
                objectNamePrefix: "qwenSetup"
                selectedOnly: true
                allowRemoval: false
                isCompact: true
            }
        }
    }

    footer: Pane {
        objectName: "qwenSetupFooter"
        leftPadding: Theme.spacingLg
        rightPadding: Theme.spacingLg
        topPadding: Theme.spacingMd
        bottomPadding: Theme.spacingLg

        background: Item {
            Rectangle {
                anchors.top: parent.top
                width: parent.width
                height: 1
                color: Theme.borderSubtle
            }
        }

        contentItem: RowLayout {
            spacing: Theme.spacingSm

            Item { Layout.fillWidth: true }

            AppButton {
                objectName: "qwenSetupCloseButton"
                variant: "quiet"
                text: qsTr("Đóng")
                accessibleLabel: qsTr("Đóng cài đặt Qwen")
                onClicked: root.close()
            }

            AppButton {
                objectName: "qwenSetupFinishButton"
                variant: "primary"
                text: qsTr("Hoàn tất")
                accessibleLabel: qsTr("Hoàn tất cài đặt Qwen")
                enabled: controller ? controller.profileReady === true : false
                onClicked: root.close()
            }
        }
    }
}
