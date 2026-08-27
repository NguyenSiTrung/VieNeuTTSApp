// Paragraph/File tab (FR-3.3, FR-4.4, FR-UX-5): long-text & document synthesis studio.
// Features document ingestion dropzone/card, format chips (.txt, .md, .docx, .pdf),
// character & paragraph metrics, waveform visualization, and segment progress.
//
// objectNames are the tested contract (tests/smoke/test_ui_tabs.py):
// paragraphTab, paragraphEditor, importButton, importDialog, charCountLabel,
// voicePicker, generateButton, playButton, exportButton, waveformIndicator,
// paraBusyLabel, progressBar, cancelButton, errorBanner, errorLabel.
import QtQuick
import QtQuick.Controls
import QtQuick.Dialogs
import QtQuick.Layouts
import "."
import "components"

Pane {
    id: root

    objectName: "paragraphTab"
    padding: Theme.spacingLg

    background: Rectangle {
        color: Theme.surface
    }

    property string importError: ""

    // QUrl → local path string for controller.importDocument
    function toLocalPath(url) {
        const s = url.toString();
        return s.startsWith("file://") ? decodeURIComponent(s.substring(7)) : s;
    }

    function importPath(path) {
        importError = "";
        if (typeof controller.importDocument !== "function") {
            importError = qsTr("Không thể nhập tệp");
            return;
        }
        const text = controller.importDocument(path);
        if (typeof text !== "string" || text === "") {
            const reason = typeof controller.errorText === "string"
                ? controller.errorText : "";
            importError = reason !== "" ? reason : qsTr("Không thể nhập tệp");
            return;
        }
        paragraphEditor.text = text;
    }

    // Helper to calculate word count
    function countWords(str) {
        if (!str || str.trim() === "")
            return 0;
        const matches = str.trim().match(/\S+/g);
        return matches ? matches.length : 0;
    }

    // Helper to estimate duration (~150 wpm -> ~2.5 words/sec)
    function estimateDurationMinutes(str) {
        const words = countWords(str);
        if (words === 0)
            return 0;
        return (words / 150).toFixed(1);
    }

    // Flat picker model from controller.voices
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
        id: importDialog

        objectName: "importDialog"
        fileMode: FileDialog.OpenFile
        title: qsTr("Chọn tệp văn bản")
        nameFilters: ["Văn bản (*.txt *.md *.docx *.pdf)"]
        onAccepted: root.importPath(root.toLocalPath(importDialog.selectedFile))
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
                    text: qsTr("Đoạn văn / Tệp")
                    color: Theme.text
                    font.family: Theme.fontFamily
                    font.pixelSize: Theme.fontSizeXl
                    font.weight: Theme.fontWeightHeading
                }

                Label {
                    Layout.fillWidth: true
                    text: qsTr("Dán văn bản dài hoặc nhập tệp tài liệu (.txt, .md, .docx, .pdf). Hệ thống tự động phân đoạn thông minh và truyền phát âm thanh tức thì.")
                    color: Theme.textMuted
                    font.family: Theme.fontFamily
                    font.pixelSize: Theme.fontSizeSm
                    wrapMode: Text.Wrap
                }
            }

            // ── Document Ingestion & Editor Card ────────────────────────────
            AppCard {
                Layout.fillWidth: true
                title: qsTr("Nội dung tài liệu")
                subtitle: qsTr("Hỗ trợ tệp văn bản định dạng .txt, .md, .docx, .pdf")

                headerAction: RowLayout {
                    spacing: Theme.spacingSm

                    Button {
                        id: importBtn
                        objectName: "importButton"
                        text: qsTr("Nhập tệp…")
                        enabled: !controller.busy
                        font.family: Theme.fontFamily
                        font.pixelSize: Theme.fontSizeSm

                        contentItem: Text {
                            text: "📂 " + importBtn.text
                            font: importBtn.font
                            color: importBtn.enabled ? Theme.text : Theme.textMuted
                            horizontalAlignment: Text.AlignHCenter
                            verticalAlignment: Text.AlignVCenter
                        }

                        background: Rectangle {
                            radius: Theme.radiusSm
                            color: importBtn.enabled ? (importBtn.hovered ? Theme.surfaceHover : Theme.surface) : Theme.surfaceAlt
                            border.color: Theme.borderSubtle
                            border.width: 1
                        }

                        onClicked: importDialog.open()
                    }

                    Rectangle {
                        radius: Theme.radiusSm
                        color: Theme.surface
                        border.color: Theme.borderSubtle
                        border.width: 1
                        implicitHeight: 28
                        implicitWidth: metricsRow.implicitWidth + Theme.spacingMd

                        RowLayout {
                            id: metricsRow
                            anchors.centerIn: parent
                            spacing: Theme.spacingSm

                            Label {
                                id: charCountLabel
                                objectName: "charCountLabel"
                                text: qsTr("%1 ký tự").arg(paragraphEditor.length)
                                color: Theme.textMuted
                                font.family: Theme.fontFamily
                                font.pixelSize: Theme.fontSizeXs
                                font.weight: Theme.fontWeightMedium
                            }

                            Label {
                                text: "·"
                                color: Theme.textMuted
                                font.family: Theme.fontFamily
                                font.pixelSize: Theme.fontSizeXs
                            }

                            Label {
                                text: qsTr("%1 từ (~%2 phút)").arg(root.countWords(paragraphEditor.text)).arg(root.estimateDurationMinutes(paragraphEditor.text))
                                color: Theme.textMuted
                                font.family: Theme.fontFamily
                                font.pixelSize: Theme.fontSizeXs
                                font.weight: Theme.fontWeightMedium
                            }
                        }
                    }

                    Button {
                        flat: true
                        implicitHeight: 28
                        visible: paragraphEditor.text.length > 0
                        contentItem: Text {
                            text: qsTr("Xóa")
                            color: Theme.textMuted
                            font.family: Theme.fontFamily
                            font.pixelSize: Theme.fontSizeXs
                        }
                        onClicked: paragraphEditor.text = ""
                    }
                }

                ColumnLayout {
                    Layout.fillWidth: true
                    spacing: Theme.spacingMd

                    // Editor Area
                    ScrollView {
                        Layout.fillWidth: true
                        Layout.minimumHeight: 220
                        Layout.preferredHeight: 260

                        TextArea {
                            id: paragraphEditor

                            objectName: "paragraphEditor"
                            placeholderText: qsTr("Dán văn bản dài / nhiều đoạn văn vào đây, hoặc nhấn 'Nhập tệp…' để tải tài liệu…")
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
                                border.color: paragraphEditor.activeFocus ? Theme.accent : Theme.borderSubtle
                                Behavior on border.color { ColorAnimation { duration: 150 } }
                            }
                        }
                    }
                }
            }

            // ── Voice & Audio Controls Card ─────────────────────────────────
            AppCard {
                Layout.fillWidth: true
                title: qsTr("Cấu hình Giọng đọc & Tổng hợp")

                ColumnLayout {
                    Layout.fillWidth: true
                    spacing: Theme.spacingMd

                    // Voice Selector
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

                            background: Rectangle {
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
                            enabled: paragraphEditor.text.trim() !== "" && !controller.busy
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
                                controller.generateStream(paragraphEditor.text, voice);
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
                                text: "▶ " + playBtn.text
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
                                text: "⚡ " + exportBtn.text
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

                    // Progress & Cancel Row
                    RowLayout {
                        Layout.fillWidth: true
                        spacing: Theme.spacingMd
                        visible: controller.busy

                        Label {
                            objectName: "paraBusyLabel"
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

            // ── Error Banner ────────────────────────────────────────────────
            Rectangle {
                id: errorBanner

                objectName: "errorBanner"
                Layout.fillWidth: true
                radius: Theme.radiusSm
                color: Theme.surfaceAlt
                border.width: 1
                border.color: Theme.warning
                implicitHeight: errorLabel.implicitHeight + Theme.spacingMd * 2
                visible: (controller.errorText || root.importError) !== ""

                Rectangle {
                    anchors {
                        left: parent.left
                        top: parent.top
                        bottom: parent.bottom
                        leftMargin: Theme.spacingSm
                        topMargin: Theme.spacingSm
                        bottomMargin: Theme.spacingSm
                    }
                    width: 3
                    radius: 1
                    color: Theme.warning
                }

                Label {
                    id: errorLabel

                    objectName: "errorLabel"
                    visible: errorBanner.visible
                    text: controller.errorText || root.importError
                    color: Theme.text
                    font.family: Theme.fontFamily
                    font.pixelSize: Theme.fontSizeBase
                    wrapMode: Text.Wrap
                    anchors {
                        left: parent.left
                        right: parent.right
                        verticalCenter: parent.verticalCenter
                        leftMargin: Theme.spacingLg
                        rightMargin: Theme.spacingMd
                    }
                }
            }
        }
    }
}
