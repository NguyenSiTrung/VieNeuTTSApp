// Text tab (FR-3.2, FR-4.3, FR-UX-4): free-text synthesis studio.
// Features integrated text editor with metrics, quick emotion tag chips,
// grouped voice selector, waveform visualizer, and streamlined playback/export.
//
// objectNames are the tested contract (tests/smoke/test_ui_tabs.py):
// textEditor, voicePicker, generateButton, waveformIndicator, progressBar,
// busyLabel, cancelButton, playButton, exportButton, quickExportButton,
// errorLabel, toastLabel.
import QtQuick
import QtQuick.Controls
import QtQuick.Dialogs
import QtQuick.Layouts
import "."
import "components"

Pane {
    id: root

    objectName: "textTab"
    padding: Theme.spacingLg

    background: Rectangle {
        color: Theme.surface
    }

    // Helper to calculate word count
    function countWords(str) {
        if (!str || str.trim() === "")
            return 0;
        const matches = str.trim().match(/\S+/g);
        return matches ? matches.length : 0;
    }

    // Helper to estimate duration (~150 wpm -> ~2.5 words/sec)
    function estimateDurationSeconds(str) {
        const words = countWords(str);
        if (words === 0)
            return 0;
        return Math.max(1, Math.round(words / 2.5));
    }

    // QUrl → local path string for controller.exportWav
    function toLocalPath(url) {
        const s = url.toString();
        return s.startsWith("file://") ? decodeURIComponent(s.substring(7)) : s;
    }

    // Flat picker model from controller.voices, preserving group order
    function buildFlatModel(groups) {
        const rows = [];
        for (let i = 0; i < groups.length; i++) {
            rows.push({ id: "", label: "▸ " + groups[i].label });
            const inner = groups[i].voices;
            for (let j = 0; j < inner.length; j++)
                rows.push({ id: inner[j].id, label: "— " + inner[j].label });
        }
        return rows;
    }

    FileDialog {
        id: exportDialog

        fileMode: FileDialog.SaveFile
        title: qsTr("Xuất âm thanh WAV")
        nameFilters: ["WAV files (*.wav)"]
        defaultSuffix: "wav"
        onAccepted: controller.exportWav(root.toLocalPath(exportDialog.selectedFile))
    }

    function openExportDialog() {
        if (controller.outputDir !== "")
            exportDialog.currentFolder = "file://" + controller.outputDir;
        exportDialog.open();
    }

    ScrollView {
        anchors.fill: parent
        contentWidth: availableWidth

        ColumnLayout {
            width: parent.width
            spacing: Theme.spacingLg

            // ── Studio Header ───────────────────────────────────────────────
            ColumnLayout {
                Layout.fillWidth: true
                spacing: Theme.spacingXs

                Label {
                    text: qsTr("Studio Tổng hợp Văn bản")
                    color: Theme.text
                    font.family: Theme.fontFamily
                    font.pixelSize: Theme.fontSizeXl
                    font.weight: Theme.fontWeightHeading
                }

                Label {
                    Layout.fillWidth: true
                    text: qsTr("Nhập văn bản tiếng Việt hoặc Anh, gắn thẻ biểu cảm và trải nghiệm giọng đọc AI chất lượng cao.")
                    color: Theme.textMuted
                    font.family: Theme.fontFamily
                    font.pixelSize: Theme.fontSizeSm
                    wrapMode: Text.Wrap
                }
            }

            // ── Editor Card ─────────────────────────────────────────────────
            AppCard {
                Layout.fillWidth: true
                title: qsTr("Nội dung văn bản")
                subtitle: qsTr("Hỗ trợ tiếng Việt đa vùng miền và tiếng Anh xen kẽ")

                headerAction: RowLayout {
                    spacing: Theme.spacingSm

                    // Metric chips
                    Rectangle {
                        radius: Theme.radiusSm
                        color: Theme.surface
                        border.color: Theme.borderSubtle
                        border.width: 1
                        implicitHeight: 24
                        implicitWidth: metricsText.implicitWidth + Theme.spacingMd

                        Label {
                            id: metricsText
                            anchors.centerIn: parent
                            text: qsTr("%1 từ · %2 ký tự · ~%3s").arg(root.countWords(textEditor.text)).arg(textEditor.length).arg(root.estimateDurationSeconds(textEditor.text))
                            color: Theme.textMuted
                            font.family: Theme.fontFamily
                            font.pixelSize: Theme.fontSizeXs
                            font.weight: Theme.fontWeightMedium
                        }
                    }

                    // Clear button
                    Button {
                        flat: true
                        implicitHeight: 24
                        visible: textEditor.text.length > 0
                        contentItem: Text {
                            text: qsTr("Xóa")
                            color: Theme.textMuted
                            font.family: Theme.fontFamily
                            font.pixelSize: Theme.fontSizeXs
                        }
                        onClicked: textEditor.text = ""
                    }
                }

                ColumnLayout {
                    Layout.fillWidth: true
                    spacing: Theme.spacingMd

                    // Main Text Editor
                    ScrollView {
                        Layout.fillWidth: true
                        Layout.minimumHeight: 140
                        Layout.preferredHeight: 180

                        TextArea {
                            id: textEditor

                            objectName: "textEditor"
                            placeholderText: qsTr("Nhập hoặc dán văn bản tiếng Việt / English…")
                            wrapMode: TextArea.Wrap
                            color: Theme.text
                            selectedTextColor: Theme.accentText
                            selectionColor: Theme.accent
                            font.family: Theme.fontFamily
                            font.pixelSize: Theme.fontSizeBase
                            selectByMouse: true
                            background: Rectangle {
                                radius: Theme.radiusSm
                                color: Theme.surface
                                border.width: 1
                                border.color: textEditor.activeFocus ? Theme.accent : Theme.borderSubtle
                                Behavior on border.color { ColorAnimation { duration: 150 } }
                            }
                        }
                    }

                    // Emotion Tag Chips Toolbar
                    ColumnLayout {
                        Layout.fillWidth: true
                        spacing: Theme.spacingXs

                        RowLayout {
                            Layout.fillWidth: true
                            spacing: Theme.spacingXs

                            Label {
                                text: qsTr("Thẻ biểu cảm [cười] [thở dài] [hắng giọng]:")
                                color: Theme.textMuted
                                font.family: Theme.fontFamily
                                font.pixelSize: Theme.fontSizeXs
                                font.weight: Theme.fontWeightMedium
                            }

                            Label {
                                text: qsTr("(nhấn để chèn vào vị trí con trỏ)")
                                color: Theme.textSubtle
                                font.family: Theme.fontFamily
                                font.pixelSize: Theme.fontSizeXs
                            }
                        }

                        Flow {
                            Layout.fillWidth: true
                            spacing: Theme.spacingSm

                            EmotionChip {
                                tag: "[cười]"
                                label: qsTr("Cười")
                                onClicked: textEditor.insert(textEditor.cursorPosition, tag + " ")
                            }

                            EmotionChip {
                                tag: "[thở dài]"
                                label: qsTr("Thở dài")
                                onClicked: textEditor.insert(textEditor.cursorPosition, tag + " ")
                            }

                            EmotionChip {
                                tag: "[hắng giọng]"
                                label: qsTr("Hắng giọng")
                                onClicked: textEditor.insert(textEditor.cursorPosition, tag + " ")
                            }

                            EmotionChip {
                                tag: "[ngập ngừng]"
                                label: qsTr("Ngập ngừng")
                                onClicked: textEditor.insert(textEditor.cursorPosition, tag + " ")
                            }

                            EmotionChip {
                                tag: "[thì thầm]"
                                label: qsTr("Thì thầm")
                                onClicked: textEditor.insert(textEditor.cursorPosition, tag + " ")
                            }
                        }
                    }
                }
            }

            // ── Voice & Audio Controls Card ─────────────────────────────────
            AppCard {
                Layout.fillWidth: true
                title: qsTr("Cấu hình Giọng đọc & Điều khiển")

                ColumnLayout {
                    Layout.fillWidth: true
                    spacing: Theme.spacingMd

                    // Voice Selector Row
                    RowLayout {
                        Layout.fillWidth: true
                        spacing: Theme.spacingMd

                        Label {
                            text: qsTr("Giọng đọc:")
                            color: Theme.text
                            font.family: Theme.fontFamily
                            font.pixelSize: Theme.fontSizeBase
                            font.weight: Theme.fontWeightMedium
                        }

                        ComboBox {
                            id: voicePicker

                            objectName: "voicePicker"
                            Layout.fillWidth: true
                            textRole: "label"
                            property var flatModel: root.buildFlatModel(controller.voices)
                            model: flatModel
                            property string selectedVoice: ""
                            onCurrentIndexChanged: {
                                const row = currentIndex >= 0 ? flatModel[currentIndex] : null;
                                selectedVoice = row && row.id !== "" ? row.id : "";
                            }
                            onActivated: function(index) {
                                const row = index >= 0 ? flatModel[index] : null;
                                if (row && row.id !== "")
                                    selectedVoice = row.id;
                            }
                            Component.onCompleted: {
                                const target = controller.defaultVoice;
                                for (let i = 0; i < flatModel.length; i++) {
                                    if (flatModel[i].id !== "" && flatModel[i].id === target) {
                                        currentIndex = i;
                                        selectedVoice = target;
                                        break;
                                    }
                                }
                            }

                            contentItem: Label {
                                leftPadding: Theme.spacingMd
                                rightPadding: Theme.spacingMd
                                text: voicePicker.displayText
                                font.family: Theme.fontFamily
                                font.pixelSize: Theme.fontSizeBase
                                color: Theme.text
                                verticalAlignment: Text.AlignVCenter
                                elide: Text.ElideRight
                            }

                            background: Rectangle {
                                implicitHeight: 38
                                radius: Theme.radiusSm
                                color: Theme.surface
                                border.color: voicePicker.activeFocus ? Theme.accent : Theme.borderSubtle
                                border.width: 1
                            }
                            delegate: ItemDelegate {
                                id: voiceRow

                                required property var modelData
                                required property int index

                                width: ListView.view.width
                                text: voiceRow.modelData ? voiceRow.modelData.label : ""
                                enabled: voiceRow.modelData ? voiceRow.modelData.id !== "" : false
                                highlighted: voicePicker.highlightedIndex === voiceRow.index

                                contentItem: Label {
                                    leftPadding: Theme.spacingMd
                                    text: voiceRow.text
                                    color: voiceRow.enabled ? Theme.text : Theme.textMuted
                                    font.family: Theme.fontFamily
                                    font.pixelSize: Theme.fontSizeBase
                                    font.weight: voiceRow.enabled ? Theme.fontWeightRegular : Theme.fontWeightBold
                                    verticalAlignment: Text.AlignVCenter
                                }
                            }
                        }
                    }

                    // Action Controls Bar
                    RowLayout {
                        Layout.fillWidth: true
                        spacing: Theme.spacingSm

                        Button {
                            id: generateBtn
                            objectName: "generateButton"
                            text: qsTr("Tạo âm thanh")
                            enabled: textEditor.text.trim() !== "" && !controller.busy
                            visible: !controller.busy
                            font.family: Theme.fontFamily
                            font.pixelSize: Theme.fontSizeBase
                            font.weight: Theme.fontWeightBold
                            
                            contentItem: Text {
                                text: generateBtn.text
                                font: generateBtn.font
                                color: generateBtn.enabled ? Theme.accentText : Theme.textMuted
                                horizontalAlignment: Text.AlignHCenter
                                verticalAlignment: Text.AlignVCenter
                            }

                            background: Rectangle {
                                radius: Theme.radiusSm
                                color: generateBtn.enabled ? (generateBtn.down ? Theme.accentHover : Theme.accent) : Theme.surfaceAlt
                                border.color: generateBtn.enabled ? Theme.accentHover : Theme.borderSubtle
                                border.width: 1
                            }

                            onClicked: {
                                const voice = voicePicker.selectedVoice !== ""
                                    ? voicePicker.selectedVoice
                                    : controller.defaultVoice;
                                controller.generateStream(textEditor.text, voice);
                            }
                        }

                        Button {
                            id: playBtn
                            objectName: "playButton"
                            text: qsTr("Phát")
                            enabled: controller.hasAudio && !controller.busy
                                      && controller.lastExportPath !== ""
                                      && controller.audioAvailable
                            ToolTip.text: qsTr("Xuất WAV trước khi phát")
                            ToolTip.visible: hovered && !enabled
                            ToolTip.delay: 200
                            font.family: Theme.fontFamily
                            font.pixelSize: Theme.fontSizeBase

                            contentItem: Text {
                                text: playBtn.text
                                font: playBtn.font
                                color: playBtn.enabled ? Theme.text : Theme.textMuted
                                horizontalAlignment: Text.AlignHCenter
                                verticalAlignment: Text.AlignVCenter
                            }

                            background: Rectangle {
                                radius: Theme.radiusSm
                                color: playBtn.enabled ? (playBtn.hovered ? Theme.surfaceHover : Theme.surface) : Theme.surfaceAlt
                                border.color: Theme.borderSubtle
                                border.width: 1
                            }

                            onClicked: {
                                if (controller.lastExportPath !== "")
                                    playback.play(controller.lastExportPath);
                            }
                        }

                        Button {
                            id: exportBtn
                            objectName: "exportButton"
                            text: qsTr("Xuất WAV")
                            enabled: controller.hasAudio && !controller.busy
                            font.family: Theme.fontFamily
                            font.pixelSize: Theme.fontSizeBase

                            contentItem: Text {
                                text: exportBtn.text
                                font: exportBtn.font
                                color: exportBtn.enabled ? Theme.text : Theme.textMuted
                                horizontalAlignment: Text.AlignHCenter
                                verticalAlignment: Text.AlignVCenter
                            }

                            background: Rectangle {
                                radius: Theme.radiusSm
                                color: exportBtn.enabled ? (exportBtn.hovered ? Theme.surfaceHover : Theme.surface) : Theme.surfaceAlt
                                border.color: Theme.borderSubtle
                                border.width: 1
                            }

                            onClicked: root.openExportDialog()
                        }

                        Button {
                            id: quickExportBtn
                            objectName: "quickExportButton"
                            text: qsTr("Lưu nhanh")
                            enabled: controller.hasAudio && !controller.busy
                            font.family: Theme.fontFamily
                            font.pixelSize: Theme.fontSizeBase

                            contentItem: Text {
                                text: quickExportBtn.text
                                font: quickExportBtn.font
                                color: quickExportBtn.enabled ? Theme.text : Theme.textMuted
                                horizontalAlignment: Text.AlignHCenter
                                verticalAlignment: Text.AlignVCenter
                            }

                            background: Rectangle {
                                radius: Theme.radiusSm
                                color: quickExportBtn.enabled ? (quickExportBtn.hovered ? Theme.surfaceHover : Theme.surface) : Theme.surfaceAlt
                                border.color: Theme.borderSubtle
                                border.width: 1
                            }

                            onClicked: controller.exportWav("")
                        }
                    }

                    // Live Waveform visualizer
                    WaveformIndicator {
                        objectName: "waveformIndicator"
                        Layout.fillWidth: true
                        Layout.preferredHeight: 52
                        visible: controller.streamActive
                        active: controller.streamActive
                        level: controller.streamLevel
                    }

                    // Progress and Cancel Row
                    RowLayout {
                        Layout.fillWidth: true
                        spacing: Theme.spacingMd
                        visible: controller.busy

                        Label {
                            objectName: "busyLabel"
                            text: qsTr("Đang tổng hợp…")
                            visible: controller.busy
                            color: Theme.accent
                            font.family: Theme.fontFamily
                            font.pixelSize: Theme.fontSizeBase
                            font.weight: Theme.fontWeightMedium
                        }

                        ProgressBar {
                            id: progressBar

                            objectName: "progressBar"
                            Layout.fillWidth: true
                            from: 0
                            to: 1
                            value: controller.progress
                            indeterminate: controller.busy && controller.progress === 0
                            visible: controller.busy
                        }

                        Button {
                            id: cancelBtn
                            objectName: "cancelButton"
                            text: qsTr("Hủy")
                            visible: controller.busy
                            font.family: Theme.fontFamily
                            font.pixelSize: Theme.fontSizeBase

                            contentItem: Text {
                                text: cancelBtn.text
                                font: cancelBtn.font
                                color: Theme.error
                                horizontalAlignment: Text.AlignHCenter
                                verticalAlignment: Text.AlignVCenter
                            }

                            background: Rectangle {
                                radius: Theme.radiusSm
                                color: Theme.surface
                                border.color: Theme.error
                                border.width: 1
                            }

                            onClicked: controller.cancel()
                        }
                    }
                }
            }

            // ── Error Notice ────────────────────────────────────────────────
            Rectangle {
                Layout.fillWidth: true
                radius: Theme.radiusSm
                color: Theme.surfaceAlt
                border.color: Theme.error
                border.width: 1
                implicitHeight: errorLabel.implicitHeight + Theme.spacingMd * 2
                visible: controller.errorText !== ""

                Label {
                    id: errorLabel
                    objectName: "errorLabel"
                    anchors.fill: parent
                    anchors.margins: Theme.spacingMd
                    visible: controller.errorText !== ""
                    text: controller.errorText
                    color: Theme.error
                    font.family: Theme.fontFamily
                    font.pixelSize: Theme.fontSizeBase
                    wrapMode: Text.Wrap
                    verticalAlignment: Text.AlignVCenter
                }
            }

            // ── Toast Notice ────────────────────────────────────────────────
            Label {
                id: toastLabel

                objectName: "toastLabel"
                visible: false
                text: qsTr("Đã hủy")
                color: Theme.warning
                font.family: Theme.fontFamily
                font.pixelSize: Theme.fontSizeSm
                font.weight: Theme.fontWeightMedium

                Timer {
                    id: toastTimer
                    interval: 2000
                    onTriggered: toastLabel.visible = false
                }

                Connections {
                    target: controller
                    function onCancelled() {
                        toastLabel.visible = true
                        toastTimer.restart()
                    }
                }
            }
        }
    }
}
