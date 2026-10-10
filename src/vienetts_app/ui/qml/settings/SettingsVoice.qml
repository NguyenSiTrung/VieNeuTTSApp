import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "../components"
import ".."

// Cài đặt › Giọng & nhịp đọc (FR-4.2): the default voice (read-only here —
// FR-3.6: it is chosen in Giọng đọc) and the synthesis pacing defaults.
//
// objectNames (tested contract): settingsSection_voice,
// settingsDefaultVoiceValue, settingsDefaultVoiceLink, defaultVoiceNote,
// temperatureSpin, temperatureNote, speedSpin, silencePSpin.
SettingsSection {
    id: section

    sectionId: "voice"
    title: qsTr("Giọng & nhịp đọc")
    hasMatch: anyMatch([voiceRow, temperatureRow, speedRow, pauseRow])

    AppCard {
        Layout.fillWidth: true

        SettingRow {
            id: voiceRow

            filter: section.filter
            compact: section.compact
            label: qsTr("Giọng đọc mặc định")
            // VieNeu's own setting: under another profile the row says where
            // that profile's voice is actually chosen instead of naming a
            // default that does not exist there (the value hides too).
            description: EngineState.defaultVoiceApplies
                ? qsTr("Giọng được tự động chọn khi mở ứng dụng")
                : EngineState.defaultVoiceNote
            descriptionObjectName: "defaultVoiceNote"
            descriptionColor: EngineState.defaultVoiceApplies ? Theme.textMuted : Theme.warningText
            keywords: "default voice"

            Label {
                objectName: "settingsDefaultVoiceValue"
                visible: EngineState.defaultVoiceApplies
                Layout.fillWidth: section.compact
                text: (section.page ? section.page.defaultVoiceName() : "") || qsTr("Chưa đặt")
                color: Theme.text
                font.family: Theme.fontFamily
                font.pixelSize: Theme.fontSizeBase
                font.weight: Theme.fontWeightMedium
                elide: Text.ElideRight
            }

            AppButton {
                objectName: "settingsDefaultVoiceLink"
                variant: "secondary"
                text: qsTr("Chọn trong Giọng đọc")
                accessibleLabel: qsTr("Mở Giọng đọc để chọn giọng mặc định")
                onClicked: {
                    bridge.setVoicesView("library");
                    bridge.setCurrentTab("voices");
                }
            }
        }

        SettingRow {
            id: temperatureRow

            filter: section.filter
            compact: section.compact
            divider: true
            label: qsTr("Temperature (Độ biến thiên)")
            // The pinned Qwen 0.6B host samples with its own fixed settings,
            // so under that profile the field is disabled and its explanation
            // states why rather than teaching a value the engine ignores.
            description: EngineState.supportsTemperature
                ? qsTr("0.6 – 0.8: Chuẩn, ổn định tự nhiên; 0.9+: Nhiều biểu cảm và ngữ điệu hơn")
                : EngineState.temperatureNote
            descriptionObjectName: "temperatureNote"
            descriptionColor: EngineState.supportsTemperature ? Theme.textMuted : Theme.warningText

            AppNumberField {
                id: temperatureSpin

                objectName: "temperatureSpin"
                from: 5            // ×100: bounds mirror Settings [0.05, 2.0]
                to: 200
                stepSize: 5
                value: Math.round(controller.temperature * 100)
                // Engine-scoped (EngineState.supportsTemperature): the stored
                // value stays for VieNeu, it just cannot be edited under a
                // profile whose engine ignores it.
                enabled: EngineState.supportsTemperature
                accessibleLabel: qsTr("Temperature")
                Layout.preferredWidth: 140
                implicitWidth: 140

                validator: DoubleValidator {
                    bottom: Math.min(temperatureSpin.from, temperatureSpin.to) / 100
                    top: Math.max(temperatureSpin.from, temperatureSpin.to) / 100
                    decimals: 2
                    locale: "C"
                }

                onRealValueChanged: controller.temperature = realValue
            }
        }

        SettingRow {
            id: speedRow

            filter: section.filter
            compact: section.compact
            divider: true
            label: qsTr("Tốc độ đọc (Speed)")
            description: qsTr("0.5× – 2.0×: Điều chỉnh tốc độ phát giọng đọc (mặc định 1.0×)")

            AppNumberField {
                id: speedSpin

                objectName: "speedSpin"
                from: 50           // ×100: bounds mirror Settings [0.5, 2.0]
                to: 200
                stepSize: 5
                value: Math.round(controller.speed * 100)
                accessibleLabel: qsTr("Tốc độ đọc")
                Layout.preferredWidth: 140
                implicitWidth: 140

                validator: DoubleValidator {
                    bottom: Math.min(speedSpin.from, speedSpin.to) / 100
                    top: Math.max(speedSpin.from, speedSpin.to) / 100
                    decimals: 2
                    locale: "C"
                }

                onRealValueChanged: controller.speed = realValue
            }
        }

        // Silence between sentences/paragraphs.
        SettingRow {
            id: pauseRow

            filter: section.filter
            compact: section.compact
            divider: true
            label: qsTr("Khoảng lặng ngắt câu (Pause)")
            description: qsTr("0.0s – 2.0s: Độ dài khoảng lặng giữa các câu và đoạn văn (mặc định 0.15s)")

            AppNumberField {
                id: silencePSpin

                objectName: "silencePSpin"
                from: 0            // ×100: bounds mirror Settings [0.0, 2.0]
                to: 200
                stepSize: 5
                value: Math.round(controller.silenceP * 100)
                accessibleLabel: qsTr("Khoảng lặng ngắt câu")
                Layout.preferredWidth: 140
                implicitWidth: 140

                validator: DoubleValidator {
                    bottom: Math.min(silencePSpin.from, silencePSpin.to) / 100
                    top: Math.max(silencePSpin.from, silencePSpin.to) / 100
                    decimals: 2
                    locale: "C"
                }

                onRealValueChanged: controller.silenceP = realValue
            }
        }
    }
}
