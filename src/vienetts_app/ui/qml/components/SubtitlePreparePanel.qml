import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import ".."

// Prepare-stage AI tools (FR-3.1/3.2/3.5): one labelled group whose buttons
// read in pipeline order, an explicit translation target, a cancel that stays
// visible while idle (disabled with a reason), and the prerequisites link.
//
// The host passes the shared busy state and the gating sentence; the component
// owns the ordering, the language list and the slot arguments.
ColumnLayout {
    id: root

    property bool shown: false
    property bool available: false
    property bool busy: false
    property bool aiBusy: false
    property bool exporting: false
    property string llmReason: ""
    property string targetLanguage: ""
    property bool glossaryExpanded: false

    signal languageSelected(string code)

    objectName: "subtitleAiBar"
    visible: root.shown
    spacing: Theme.spacingXs

    readonly property string currentGlossary: root.available
        && typeof subtitleController.llmGlossary === "string"
        ? subtitleController.llmGlossary : ""

    function commitGlossary() {
        if (root.available && typeof subtitleController.setLlmGlossary === "function")
            subtitleController.setLlmGlossary(subtitleGlossaryField.text);
    }

    Connections {
        target: root.available ? subtitleController : null

        function onLlmGlossaryChanged() {
            const next = subtitleController.llmGlossary || "";
            if (!subtitleGlossaryField.activeFocus && subtitleGlossaryField.text !== next)
                subtitleGlossaryField.text = next;
        }
    }

    readonly property var languageOptions: {
        const rows = [{ code: "", label: qsTr("Theo ngôn ngữ tổng hợp") }];
        const langs = root.available ? (subtitleController.profileLanguages || []) : [];
        for (let i = 0; i < langs.length; ++i)
            rows.push({ code: langs[i].code, label: langs[i].label });
        return rows;
    }
    readonly property int languageIndex: {
        const rows = root.languageOptions;
        for (let i = 0; i < rows.length; ++i)
            if (rows[i].code === root.targetLanguage)
                return i;
        return 0;
    }

    ColumnLayout {
        Layout.fillWidth: true
        spacing: Theme.spacingXs

        RowLayout {
            Layout.fillWidth: true
            spacing: Theme.spacingSm

            SectionLabel {
                objectName: "subtitleAiGroupLabel"
                Layout.fillWidth: true
                text: qsTr("CÔNG CỤ AI · THEO THỨ TỰ")
            }

            AppButton {
                objectName: "subtitleCancelAiButton"
                variant: "danger"
                size: "sm"
                text: qsTr("Hủy AI")
                enabled: root.aiBusy
                disabledReason: root.aiBusy ? "" : qsTr("Không có tác vụ AI nào đang chạy.")
                onClicked: {
                    if (root.available)
                        subtitleController.cancelAiTask();
                }
            }
        }

        Flow {
            Layout.fillWidth: true
            spacing: Theme.spacingXs

            AppButton {
                objectName: "subtitleRefineAsrButton"
                variant: "secondary"
                size: "sm"
                text: qsTr("AI Làm sạch phụ đề")
                tooltipText: qsTr("Bước 1 · Sửa chính tả và nhãn người nói trên toàn bộ phụ đề.")
                enabled: root.available && !root.busy && !root.aiBusy && !root.exporting
                disabledReason: root.llmReason
                onClicked: {
                    if (root.available)
                        subtitleController.refineWithLlm("refine_asr", "");
                }
            }

            AppButton {
                objectName: "subtitleTranslateDubButton"
                variant: "secondary"
                size: "sm"
                text: qsTr("AI Dịch lồng tiếng")
                tooltipText: qsTr("Bước 2 · Dịch phụ đề sang ngôn ngữ đích đã chọn.")
                enabled: root.available && !root.busy && !root.aiBusy && !root.exporting
                disabledReason: root.llmReason
                onClicked: {
                    if (root.available)
                        subtitleController.refineWithLlm(
                            "translate_dub", root.targetLanguage);
                }
            }

            AppCombo {
                objectName: "subtitleTranslateLanguageCombo"
                comboWidth: 200
                accessibleLabel: qsTr("Ngôn ngữ đích khi dịch")
                textRole: "label"
                model: root.languageOptions
                enabled: root.available && !root.busy && !root.aiBusy && !root.exporting
                currentIndex: root.languageIndex
                onActivated: function (index) {
                    const rows = root.languageOptions;
                    if (index >= 0 && index < rows.length)
                        root.languageSelected(rows[index].code);
                }
            }

            AppButton {
                objectName: "subtitleFitTimingButton"
                variant: "secondary"
                size: "sm"
                text: qsTr("AI Rút gọn khớp thời gian")
                tooltipText: qsTr("Bước 3 · Rút ngắn câu cho vừa tốc độ đọc tối đa.")
                enabled: root.available && !root.busy && !root.aiBusy && !root.exporting
                disabledReason: root.llmReason
                onClicked: {
                    if (root.available)
                        subtitleController.refineWithLlm("fit_timing", "");
                }
            }
        }

        // FR-3.5: the AI prerequisites live in one named place with a
        // deep-link, instead of being discovered only when a run fails.
        RowLayout {
            objectName: "subtitleAiPrereqRow"
            Layout.fillWidth: true
            spacing: Theme.spacingSm

            Label {
                Layout.fillWidth: true
                text: qsTr("ASR cần mô hình Whisper; AI cần máy chủ LLM tương thích OpenAI.")
                color: Theme.textMuted
                font.family: Theme.fontFamily
                font.pixelSize: Theme.fontSizeXs
                wrapMode: Text.WordWrap
            }

            AppButton {
                objectName: "subtitleGlossaryToggle"
                variant: "secondary"
                size: "sm"
                iconKind: root.glossaryExpanded ? "chevronUp" : "chevronDown"
                text: qsTr("Thuật ngữ & cách xưng hô")
                tooltipText: qsTr("Ghi chú tên riêng, thuật ngữ và quy tắc xưng hô giữa các nhân vật cho AI")
                onClicked: root.glossaryExpanded = !root.glossaryExpanded
            }

            AppButton {
                objectName: "subtitleAiSettingsButton"
                variant: "secondary"
                size: "sm"
                text: qsTr("Mở Cài đặt › Mô hình")
                tooltipText: qsTr("Mở phần cài đặt mô hình AI và nhận dạng giọng nói")
                onClicked: {
                    if (typeof bridge !== "undefined" && bridge !== null)
                        bridge.setCurrentTab("settings");
                }
            }
        }

        // FR-7.2: Collapsible project glossary & pronoun-rules section.
        ColumnLayout {
            objectName: "subtitleGlossarySection"
            Layout.fillWidth: true
            visible: root.glossaryExpanded
            spacing: Theme.spacingXxs

            Label {
                Layout.fillWidth: true
                text: qsTr("Ghi chú tên riêng, thuật ngữ và quy tắc xưng hô (tối đa 4000 ký tự, gửi kèm mọi tác vụ AI).")
                color: Theme.textMuted
                font.family: Theme.fontFamily
                font.pixelSize: Theme.fontSizeXs
                wrapMode: Text.WordWrap
            }

            TextArea {
                id: subtitleGlossaryField

                objectName: "subtitleGlossaryField"
                Layout.fillWidth: true
                implicitHeight: 72
                enabled: root.available && !root.busy && !root.aiBusy && !root.exporting
                text: root.currentGlossary
                placeholderText: qsTr("Ví dụ: Minh -> Minh (giữ nguyên); SPEAKER_01 -> SPEAKER_02: xưng anh - gọi em")
                placeholderTextColor: Theme.textSubtle
                color: enabled ? Theme.text : Theme.controlDisabledText
                selectedTextColor: Theme.accentText
                selectionColor: Theme.accent
                font.family: Theme.fontFamily
                font.pixelSize: Theme.fontSizeSm
                wrapMode: TextArea.Wrap
                selectByMouse: true
                leftPadding: Theme.spacingSm
                rightPadding: Theme.spacingSm
                topPadding: Theme.spacingXs
                bottomPadding: Theme.spacingXs
                Accessible.name: qsTr("Thuật ngữ & cách xưng hô")

                background: Rectangle {
                    radius: Theme.radiusSm
                    color: subtitleGlossaryField.enabled ? Theme.surface : Theme.controlDisabledBg
                    border.width: subtitleGlossaryField.activeFocus ? Theme.focusRingWidth : 1
                    border.color: subtitleGlossaryField.activeFocus ? Theme.accent
                        : (subtitleGlossaryField.enabled ? Theme.borderSubtle : Theme.controlDisabledBorder)
                }

                onEditingFinished: root.commitGlossary()
                onActiveFocusChanged: {
                    if (!activeFocus)
                        root.commitGlossary();
                }
            }
        }
    }

    RowLayout {
        Layout.fillWidth: true
        visible: root.aiBusy
        spacing: Theme.spacingSm

        Label {
            objectName: "subtitleAiStageLabel"
            Layout.fillWidth: true
            text: root.available ? subtitleController.aiStageText : ""
            color: Theme.accent
            font.family: Theme.fontFamily
            font.pixelSize: Theme.fontSizeSm
        }

        AppProgressBar {
            objectName: "subtitleAiProgressBar"
            Layout.preferredWidth: 160
            accessibleLabel: qsTr("Tiến độ tác vụ AI")
            value: root.available ? subtitleController.aiProgress : 0.0
        }
    }
}
