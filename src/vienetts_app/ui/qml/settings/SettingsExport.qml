import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "../components"
import ".."

// Cài đặt › Xuất tệp (FR-4.2): where exports land and their container.
// The FolderDialog lives on the page (SettingsTab.outputDirDialog); the
// tested seam for a chosen folder is SettingsTab.setOutputDir(path).
//
// objectNames (tested contract): settingsSection_export, outputDirLabel,
// outputDirBrowseButton, outputDirResetButton, exportFormatCombo,
// m4bBitrateRow, m4bBitrateCombo.
SettingsSection {
    id: section

    sectionId: "export"
    title: qsTr("Xuất tệp")
    hasMatch: anyMatch([outputRow, formatRow, m4bBitrateRow])

    AppCard {
        Layout.fillWidth: true

        // Full-width path field: the label always stacks above it.
        SettingRow {
            id: outputRow

            filter: section.filter
            compact: true
            label: qsTr("Thư mục xuất âm thanh")
            description: qsTr("Vị trí lưu trữ các tệp âm thanh xuất ra (.wav/.mp3)")
            keywords: "output folder"

            Rectangle {
                Layout.fillWidth: true
                implicitHeight: 52
                radius: Theme.radiusMd
                color: Theme.surfaceAlt
                border.color: Theme.borderSubtle
                border.width: 1

                RowLayout {
                    anchors.fill: parent
                    anchors.margins: Theme.spacingSm
                    anchors.leftMargin: Theme.spacingMd
                    spacing: Theme.spacingSm

                    Label {
                        id: outputDirLabel

                        objectName: "outputDirLabel"
                        Layout.fillWidth: true
                        text: controller.outputDir !== ""
                            ? controller.outputDir
                            : qsTr("Mặc định: ~/Music/VieNeuTTS")
                        elide: Text.ElideMiddle
                        color: controller.outputDir !== "" ? Theme.text : Theme.textMuted
                        font.family: Theme.fontFamilyMono !== "" ? Theme.fontFamilyMono : Theme.fontFamily
                        font.pixelSize: Theme.fontSizeSm
                    }

                    AppButton {
                        id: outputDirBrowseButton

                        objectName: "outputDirBrowseButton"
                        variant: "secondary"
                        size: "sm"
                        text: qsTr("Thay đổi…")
                        iconKind: "folder"
                        onClicked: section.page.openOutputDirDialog()
                    }

                    AppIconButton {
                        id: outputDirResetButton

                        objectName: "outputDirResetButton"
                        iconKind: "reset"
                        tooltipText: qsTr("Khôi phục thư mục mặc định")
                        accessibleLabel: qsTr("Khôi phục thư mục mặc định")
                        visible: controller.outputDir !== ""
                        onClicked: controller.outputDir = ""
                    }
                }
            }
        }

        // Batch + audiobook container; Save dialogs still pick per file.
        SettingRow {
            id: formatRow

            filter: section.filter
            compact: section.compact
            divider: true
            label: qsTr("Định dạng xuất âm thanh")
            description: qsTr("Hàng loạt và sách nói dùng định dạng này (WAV ~10 MB/phút, MP3 ~1 MB/phút)")
            keywords: "export format wav mp3"

            AppCombo {
                id: exportFormatCombo

                objectName: "exportFormatCombo"
                Layout.fillWidth: section.compact
                Layout.preferredWidth: section.compact ? 0 : 280
                comboWidth: 280
                accessibleLabel: qsTr("Định dạng xuất âm thanh")
                textRole: "label"
                model: section.page ? section.page.exportFormatOptions : []
                currentIndex: section.page
                    ? section.page.valueIndex(section.page.exportFormatOptions, controller.exportFormat)
                    : 0
                onActivated: function (index) {
                    controller.exportFormat = section.page.exportFormatOptions[index].value;
                }
            }
        }

        // M4B audiobook AAC bitrate (FR-5.1).
        SettingRow {
            id: m4bBitrateRow

            objectName: "m4bBitrateRow"
            filter: section.filter
            compact: section.compact
            divider: true
            label: qsTr("Chất lượng âm thanh M4B")
            description: qsTr("Tốc độ bit AAC khi xuất sách nói M4B (.m4b).")
            keywords: "m4b aac bitrate audio quality audiobook"

            AppCombo {
                id: m4bBitrateCombo

                objectName: "m4bBitrateCombo"
                Layout.fillWidth: section.compact
                Layout.preferredWidth: section.compact ? 0 : 280
                comboWidth: 280
                accessibleLabel: qsTr("Chất lượng âm thanh M4B")
                textRole: "label"
                model: section.page ? section.page.m4bBitrateOptions : []
                currentIndex: section.page
                    ? section.page.valueIndex(
                        section.page.m4bBitrateOptions,
                        controller ? controller.m4bBitrate : "96k"
                    )
                    : 0
                onActivated: function (index) {
                    if (controller && section.page && section.page.m4bBitrateOptions[index]) {
                        controller.setM4bBitrate(section.page.m4bBitrateOptions[index].value);
                    }
                    // A user pick assigns currentIndex; re-bind it so controller updates stay synced.
                    currentIndex = Qt.binding(function () {
                        return section.page
                            ? section.page.valueIndex(
                                section.page.m4bBitrateOptions,
                                controller ? controller.m4bBitrate : "96k"
                            )
                            : 0;
                    });
                }
            }
        }
    }
}
