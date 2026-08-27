// Paragraph/File tab (FR-3.3, FR-4.4, FR-UX-5): long-text & document synthesis studio.
// PageShell/PageHeader scaffold, format chips, drag-and-drop document import,
// shared VoicePicker, AppButton actions, streaming waveform + segment progress.
//
// objectNames are the tested contract (tests/smoke/test_ui_tabs.py):
// paragraphTab, paragraphEditor, importButton, importDialog, charCountLabel,
// voicePicker, generateButton, playButton, exportButton, waveformIndicator,
// paraBusyLabel, progressBar, cancelButton, errorBanner, errorLabel.
// Pinned copy: header "Đoạn văn / Tệp", a ".pdf" mention, "Nhập tệp…",
// "%1 ký tự", "Không thể nhập tệp".
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
        color: Theme.bg
    }

    property string importError: ""
    property bool dragOver: false

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

    // Flat picker model from controller.voices (tested seam; same format as
    // TextTab/VoicePicker — "▸ group" / "— voice" rows).
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

    function submitForSynthesis() {
        if (paragraphEditor.text.trim() === "" || controller.busy)
            return;
        const voice = voicePicker.selectedVoice !== ""
            ? voicePicker.selectedVoice
            : controller.defaultVoice;
        controller.generateStream(paragraphEditor.text, voice);
    }

    FileDialog {
        id: importDialog

        objectName: "importDialog"
        fileMode: FileDialog.OpenFile
        title: qsTr("Chọn tệp văn bản")
        nameFilters: ["Văn bản (*.txt *.md *.docx *.pdf)"]
        onAccepted: root.importPath(root.toLocalPath(importDialog.selectedFile))
    }

    // --- Keyboard shortcuts (additive) ----------------------------------------
    Shortcut {
        sequence: "Ctrl+Return"
        enabled: paragraphEditor.text.trim() !== "" && !controller.busy
        onActivated: root.submitForSynthesis()
        context: Qt.WindowShortcut
    }
    Shortcut {
        sequence: "Escape"
        enabled: controller.busy
        onActivated: controller.cancel()
        context: Qt.WindowShortcut
    }

    PageShell {
        anchors.fill: parent
        maxWidth: 960

        // ── Studio Header ───────────────────────────────────────────────
        PageHeader {
            Layout.fillWidth: true
            iconKind: "paragraph"
            title: qsTr("Đoạn văn / Tệp")
            subtitle: qsTr("Dán văn bản dài hoặc nhập tệp tài liệu. Hệ thống tự động phân đoạn thông minh và truyền phát âm thanh tức thì.")
        }

        // ── Document Ingestion & Editor Card ────────────────────────────
        AppCard {
            id: editorCard
            Layout.fillWidth: true
            title: qsTr("Nội dung tài liệu")
            subtitle: qsTr("Kéo thả tệp vào đây, hoặc dán văn bản trực tiếp")

            headerAction: RowLayout {
                spacing: Theme.spacingSm

                AppButton {
                    id: importBtn
                    objectName: "importButton"
                    variant: "secondary"
                    size: "sm"
                    glyph: "↑"
                    text: qsTr("Nhập tệp…")
                    enabled: !controller.busy
                    onClicked: importDialog.open()
                }

                Rectangle {
                    radius: Theme.radiusSm
                    color: Theme.surface
                    border.color: Theme.borderSubtle
                    border.width: 1
                    implicitHeight: 24
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

                AppButton {
                    variant: "ghost"
                    size: "sm"
                    text: qsTr("Xóa")
                    visible: paragraphEditor.text.length > 0
                    onClicked: paragraphEditor.text = ""
                }
            }

            // Drag & drop document import (same seam as the file dialog).
            DropArea {
                anchors.fill: parent
                onEntered: if (drag.hasUrls) root.dragOver = true
                onExited: root.dragOver = false
                onDropped: if (drop.hasUrls && drop.urls.length > 0) {
                    root.dragOver = false;
                    root.importPath(root.toLocalPath(drop.urls[0]));
                }
            }

            ColumnLayout {
                Layout.fillWidth: true
                spacing: Theme.spacingMd

                // Supported format chips
                RowLayout {
                    Layout.fillWidth: true
                    spacing: Theme.spacingXs

                    Label {
                        text: qsTr("Hỗ trợ:")
                        color: Theme.textSubtle
                        font.family: Theme.fontFamily
                        font.pixelSize: Theme.fontSizeXs
                    }

                    StatusBadge { text: ".txt"; status: "neutral" }
                    StatusBadge { text: ".md"; status: "neutral" }
                    StatusBadge { text: ".docx"; status: "neutral" }
                    StatusBadge { text: ".pdf"; status: "neutral" }

                    Item { Layout.fillWidth: true }
                }

                // Editor Area
                ScrollView {
                    Layout.fillWidth: true
                    Layout.minimumHeight: 240
                    Layout.preferredHeight: 280

                    TextArea {
                        id: paragraphEditor

                        objectName: "paragraphEditor"
                        placeholderText: qsTr("Dán văn bản dài / nhiều đoạn văn vào đây, hoặc kéo thả tệp tài liệu vào khung này…")
                        placeholderTextColor: Theme.textSubtle
                        wrapMode: TextArea.Wrap
                        color: root.dragOver ? Theme.accent : Theme.text
                        selectedTextColor: Theme.accentText
                        selectionColor: Theme.accent
                        font.family: Theme.fontFamily
                        font.pixelSize: Theme.fontSizeBase
                        selectByMouse: true
                        leftPadding: Theme.spacingMd
                        rightPadding: Theme.spacingMd
                        topPadding: Theme.spacingMd
                        bottomPadding: Theme.spacingMd
                        background: Rectangle {
                            radius: Theme.radiusMd
                            color: root.dragOver ? Theme.accentSubtle : Theme.surface
                            border.width: paragraphEditor.activeFocus || root.dragOver ? Theme.focusRingWidth : 1
                            border.color: root.dragOver ? Theme.accent : (paragraphEditor.activeFocus ? Theme.accent : Theme.borderSubtle)
                            Behavior on border.color { ColorAnimation { duration: Theme.durationFast } }
                            Behavior on color { ColorAnimation { duration: Theme.durationFast } }
                        }
                    }
                }
            }
        }

        // ── Voice & Audio Controls Card ─────────────────────────────────
        AppCard {
            Layout.fillWidth: true
            title: qsTr("Giọng đọc & Tổng hợp")

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

                    VoicePicker {
                        id: voicePicker
                        Layout.fillWidth: true
                    }
                }

                // Action Controls Bar
                RowLayout {
                    Layout.fillWidth: true
                    spacing: Theme.spacingSm

                    AppButton {
                        id: generateBtn
                        objectName: "generateButton"
                        variant: "primary"
                        size: "lg"
                        text: qsTr("Tạo âm thanh")
                        enabled: paragraphEditor.text.trim() !== "" && !controller.busy
                        visible: !controller.busy
                        ToolTip.text: qsTr("Tổng hợp phát trực tiếp (Ctrl+Return)")
                        ToolTip.visible: hovered

                        onClicked: root.submitForSynthesis()
                    }

                    AppButton {
                        id: playBtn
                        objectName: "playButton"
                        variant: "secondary"
                        text: qsTr("Phát")
                        glyph: "▶"
                        enabled: controller.hasAudio && !controller.busy
                                  && controller.lastExportPath !== ""
                                  && controller.audioAvailable
                        ToolTip.text: qsTr("Xuất WAV trước khi phát")
                        ToolTip.visible: hovered && !enabled
                        ToolTip.delay: 200

                        onClicked: {
                            if (controller.lastExportPath !== "")
                                playback.play(controller.lastExportPath);
                        }
                    }

                    AppButton {
                        id: exportBtn
                        objectName: "exportButton"
                        variant: "secondary"
                        text: qsTr("Xuất WAV")
                        enabled: controller.hasAudio && !controller.busy
                        onClicked: controller.exportWav("")
                    }

                    Item { Layout.fillWidth: true }
                }

                // Live Waveform visualizer (visibility is the tested contract)
                WaveformIndicator {
                    objectName: "waveformIndicator"
                    Layout.fillWidth: true
                    Layout.preferredHeight: 56
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

                        background: Rectangle {
                            implicitHeight: 6
                            radius: 3
                            color: Theme.surfaceAlt
                        }
                        contentItem: Item {
                            clip: true

                            Rectangle {
                                visible: !progressBar.indeterminate
                                width: progressBar.visualPosition * parent.width
                                height: parent.height
                                radius: 3
                                color: Theme.accent
                            }

                            Rectangle {
                                id: indetBar
                                visible: progressBar.indeterminate
                                width: parent.width * 0.3
                                height: parent.height
                                radius: 3
                                color: Theme.accent

                                XAnimator on x {
                                    from: -indetBar.width
                                    to: indetBar.parent.width
                                    duration: 900
                                    loops: Animation.Infinite
                                    running: progressBar.indeterminate
                                }
                            }
                        }
                    }

                    AppButton {
                        id: cancelBtn
                        objectName: "cancelButton"
                        variant: "danger"
                        size: "sm"
                        text: qsTr("Hủy")
                        visible: controller.busy
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
            radius: Theme.radiusMd
            color: Theme.warningSubtle
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
                radius: 1.5
                color: Theme.warning
            }

            Label {
                id: errorLabel

                objectName: "errorLabel"
                visible: errorBanner.visible
                text: controller.errorText || root.importError
                color: Theme.warningText
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
