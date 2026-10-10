import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "../components"
import ".."

// Cài đặt › Chung (FR-4.2): appearance and app-wide behaviour — Color mode,
// interface language, and the ONE global live-playback preference (FR-2.5:
// the Create inspector toggles the same controller.livePreview).
//
// objectNames (tested contract): settingsSection_general, themeCombo
// (AppSegmented, segments themeCombo_<value>), languageCombo,
// livePreviewToggle.
SettingsSection {
    id: section

    sectionId: "general"
    title: qsTr("Chung")
    hasMatch: anyMatch([themeRow, languageRow, liveRow])

    AppCard {
        Layout.fillWidth: true

        SettingRow {
            id: themeRow

            filter: section.filter
            compact: section.compact
            label: qsTr("Chế độ màu sắc")
            description: qsTr("Chọn giao diện Tối, Sáng hoặc theo hệ thống — áp dụng ngay lập tức")
            keywords: "theme dark light giao diện"

            // Color mode is a segmented control (FR-1.6): three short choices
            // read better side by side than behind a popup. Strict binding +
            // onActivated write-back: the bridge stays the single source of
            // truth.
            AppSegmented {
                id: themeCombo

                objectName: "themeCombo"
                Layout.fillWidth: section.compact
                Layout.preferredWidth: section.compact ? 0 : 280
                accessibleLabel: qsTr("Chế độ màu sắc")
                model: section.page ? section.page.themeOptions : []
                currentValue: bridge ? bridge.themePreference : "system"
                onActivated: (value) => {
                    if (bridge)
                        bridge.themePreference = value;
                    controller.theme = value;
                }
            }
        }

        // Language picker — applies LIVE (like the theme control above): the
        // shell swaps translators and retranslate()s on change.
        SettingRow {
            id: languageRow

            filter: section.filter
            compact: section.compact
            divider: true
            label: qsTr("Ngôn ngữ")
            description: qsTr("Ngôn ngữ hiển thị của giao diện — áp dụng ngay lập tức")
            keywords: "language English Tiếng Việt"

            AppCombo {
                id: languageCombo

                objectName: "languageCombo"
                Layout.fillWidth: section.compact
                Layout.preferredWidth: section.compact ? 0 : 280
                comboWidth: 280
                accessibleLabel: qsTr("Ngôn ngữ")
                textRole: "label"
                model: section.page ? section.page.languageOptions : []
                currentIndex: section.page
                    ? section.page.valueIndex(section.page.languageOptions,
                                              controller ? controller.language : "system")
                    : 0
                onActivated: function (index) {
                    if (controller)
                        controller.language = section.page.languageOptions[index].value;
                }
            }
        }

        // Live preview (real-time playback vs generate-then-replay).
        SettingRow {
            id: liveRow

            filter: section.filter
            compact: section.compact
            divider: true
            label: qsTr("Phát trực tiếp khi đang tạo")
            description: qsTr("Bật: nghe ngay khi tổng hợp. Tắt: tạo xong tự phát lại từ đầu")

            AppToggle {
                id: livePreviewToggle

                objectName: "livePreviewToggle"
                text: qsTr("Phát trực tiếp")
                checked: controller.livePreview === true
                onToggled: controller.livePreview = checked
                accessibleLabel: qsTr("Phát trực tiếp khi đang tạo")
            }
        }
    }
}
