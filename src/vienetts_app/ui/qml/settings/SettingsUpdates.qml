import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "../components"
import ".."
import "SettingsFilter.js" as Filter

// Cài đặt › Cập nhật (FR-4.2): GitHub Releases check — a silent auto-check at
// startup (app.py), manual refresh here. Suggests the running platform's file
// first (asset name contains windows-x64 / linux-x64 / macos-arm64); other
// files expand below. Download opens the file URL in the browser — the user
// re-extracts over the old install (Linux: re-run share/linux/install.sh).
// The status bar's update link lands here (SettingsTab.jumpToSection).
//
// objectNames (tested contract): settingsSection_updates, checkUpdatesButton,
// updateBanner, updateErrorLabel, downloadUpdateButton, viewReleaseButton,
// otherPlatformsToggle, otherPlatformsList (delegates otherPlatformAssetButton).
SettingsSection {
    id: section

    sectionId: "updates"
    title: qsTr("Cập nhật")
    hasMatch: anyMatch([updatesCard])

    AppCard {
        id: updatesCard

        // The whole card is one filter row: "cập nhật", "phiên bản", …
        property bool shown: true
        readonly property bool matches: Filter.matches(section.filter, [
            section.title, title, qsTr("Kiểm tra bản mới"), "update version release"
        ])

        objectName: "settingsUpdatesCard"
        Layout.fillWidth: true
        title: qsTr("Phiên bản hiện tại")
        // The installed version as the header badge (the subtitle line is
        // gone, FR-1.4).
        badgeText: controller && controller.appVersion !== "" ? "v" + controller.appVersion : ""

        ColumnLayout {
            Layout.fillWidth: true
            spacing: Theme.spacingMd

            // New-version banner (sticky once any check finds one).
            AppNotice {
                Layout.fillWidth: true
                tone: "info"
                title: qsTr("Có bản mới %1").arg(controller ? controller.updateLatestVersion : "")
                message: controller && controller.updateAssetName !== ""
                    ? qsTr("Bản dành cho %1: %2").arg(controller.updatePlatformLabel).arg(controller.updateAssetName)
                    : qsTr("Bản mới không có tệp cho nền tảng này — xem các tệp khác bên dưới.")
                messageObjectName: "updateBanner"
                visible: controller ? controller.updateAvailable : false
            }

            RowLayout {
                Layout.fillWidth: true
                spacing: Theme.spacingMd

                Label {
                    Layout.fillWidth: true
                    text: controller && controller.updateLatestVersion !== ""
                        ? (controller.updateAvailable
                            ? qsTr("Bản mới nhất: %1").arg(controller.updateLatestVersion)
                            : qsTr("Đã là bản mới nhất (%1)").arg(controller.appVersion))
                        : qsTr("Chưa kiểm tra cập nhật")
                    color: Theme.textMuted
                    font.family: Theme.fontFamily
                    font.pixelSize: Theme.fontSizeSm
                    wrapMode: Text.Wrap
                }

                AppButton {
                    id: checkUpdatesButton
                    objectName: "checkUpdatesButton"
                    variant: "secondary"
                    size: "sm"
                    iconKind: "refresh"
                    text: qsTr("Kiểm tra")
                    tooltipText: qsTr("Kiểm tra bản mới trên GitHub Releases")
                    accessibleLabel: qsTr("Kiểm tra bản mới")
                    busy: controller ? controller.updateChecking : false
                    onClicked: controller.checkForUpdates()
                }
            }

            // Failure note (manual check only; startup failures stay silent).
            AppNotice {
                Layout.fillWidth: true
                tone: "warning"
                title: qsTr("Không kiểm tra được")
                message: controller ? controller.updateError : ""
                messageObjectName: "updateErrorLabel"
                visible: controller
                    ? (controller.updateError !== "" && !controller.updateAvailable) : false
            }

            // Suggested platform download.
            AppButton {
                id: downloadUpdateButton
                objectName: "downloadUpdateButton"
                variant: "primary"
                size: "md"
                iconKind: "download"
                text: qsTr("Tải bản %1 cho %2").arg(controller ? controller.updateLatestVersion : "").arg(controller ? controller.updatePlatformLabel : "")
                tooltipText: controller ? controller.updateAssetName : ""
                accessibleLabel: qsTr("Tải bản cập nhật cho nền tảng này")
                visible: controller
                    ? (controller.updateAvailable && controller.updateAssetUrl !== "") : false
                onClicked: Qt.openUrlExternally(controller.updateAssetUrl)
            }

            // Link to the full release page (notes + every file) when there
            // is no matching platform file, or as a secondary path.
            AppButton {
                id: viewReleaseButton
                objectName: "viewReleaseButton"
                variant: "quiet"
                size: "sm"
                iconKind: "externalLink"
                text: qsTr("Xem ghi chú phát hành")
                accessibleLabel: qsTr("Mở trang phát hành trên GitHub")
                visible: controller
                    ? (controller.updateAvailable && controller.updateReleaseUrl !== "") : false
                onClicked: Qt.openUrlExternally(controller.updateReleaseUrl)
            }

            // Other-platform files expander.
            ColumnLayout {
                Layout.fillWidth: true
                spacing: Theme.spacingXs
                visible: controller
                    ? (controller.updateAvailable && controller.updateOtherAssets.length > 0) : false

                AppButton {
                    id: otherPlatformsToggle
                    objectName: "otherPlatformsToggle"
                    property bool showOtherPlatforms: false
                    variant: "quiet"
                    size: "sm"
                    iconKind: showOtherPlatforms ? "chevronUp" : "chevronDown"
                    text: showOtherPlatforms ? qsTr("Ẩn các bản khác") : qsTr("Tải cho nền tảng khác")
                    accessibleLabel: qsTr("Hiện các tệp cho nền tảng khác")
                    onClicked: showOtherPlatforms = !showOtherPlatforms
                }

                Repeater {
                    id: otherPlatformsList
                    objectName: "otherPlatformsList"
                    model: (controller && otherPlatformsToggle.showOtherPlatforms)
                        ? controller.updateOtherAssets : []
                    delegate: AppButton {
                        objectName: "otherPlatformAssetButton"
                        required property var modelData
                        variant: "secondary"
                        size: "sm"
                        iconKind: "download"
                        text: modelData.name
                        tooltipText: modelData.url
                        Layout.fillWidth: true
                        onClicked: Qt.openUrlExternally(modelData.url)
                    }
                }
            }
        }
    }
}
