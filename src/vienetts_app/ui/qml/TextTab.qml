// Text tab (FR-3.2, FR-4.3, FR-UX-4): free-text synthesis studio.
// PageShell/PageHeader scaffold, editor with focus glow + live metrics footer,
// emotion chips, shared VoicePicker, AppButton action hierarchy, keyboard
// shortcuts (Ctrl+Return generate · Ctrl+E quick export · Escape cancel).
//
// objectNames are the tested contract (tests/smoke/test_ui_tabs.py):
// textEditor, voicePicker, generateButton, waveformIndicator, progressBar,
// busyLabel, cancelButton, playButton, exportButton, quickExportButton,
// errorLabel, toastLabel. Pinned copy: "Tạo âm thanh", "Đã hủy", the editor
// placeholder, and a visible "[cười]" hint.
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
        color: Theme.bg
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
        if (!s.startsWith("file://"))
            return s;
        let path = decodeURIComponent(s.substring(7));
        // Windows: toString() is file:///C:/... — drop the stray slash the
        // empty host slot leaves before the drive letter, or downstream
        // slots receive /C:/... and every filesystem call fails.
        if (/^\/[A-Za-z]:\//.test(path))
            path = path.substring(1);
        return path;
    }

    // Flat picker model from controller.voices, preserving group order
    // (tested seam — format "▸ group" / "— voice" is pinned).
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
        if (textEditor.text.trim() === "" || controller.busy)
            return;
        const voice = voicePicker.selectedVoice !== ""
            ? voicePicker.selectedVoice
            : controller.defaultVoice;
        controller.generateStream(textEditor.text, voice);
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

    // --- Keyboard shortcuts (additive; buttons remain the primary path) ------
    Shortcut {
        sequence: "Ctrl+Return"
        enabled: textEditor.text.trim() !== "" && !controller.busy
        onActivated: root.submitForSynthesis()
        context: Qt.WindowShortcut
    }
    Shortcut {
        sequence: "Ctrl+E"
        enabled: controller.hasAudio && !controller.busy
        onActivated: controller.exportWav("")
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
            iconKind: "text"
            title: qsTr("Studio Tổng hợp Văn bản")
            subtitle: qsTr("Nhập văn bản tiếng Việt hoặc Anh, gắn thẻ biểu cảm và trải nghiệm giọng đọc AI chất lượng cao.")
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
                AppButton {
                    variant: "ghost"
                    size: "sm"
                    text: qsTr("Xóa")
                    visible: textEditor.text.length > 0
                    onClicked: textEditor.text = ""
                }
            }

            ColumnLayout {
                Layout.fillWidth: true
                spacing: Theme.spacingMd

                // Main Text Editor
                ScrollView {
                    Layout.fillWidth: true
                    Layout.minimumHeight: 160
                    Layout.preferredHeight: 200

                    ScrollBar.vertical: ScrollBar {
                        implicitWidth: 8
                        contentItem: Rectangle { radius: 4; color: Theme.border; opacity: 0.7 }
                    }

                    TextArea {
                        id: textEditor

                        objectName: "textEditor"
                        placeholderText: qsTr("Nhập hoặc dán văn bản tiếng Việt / English…")
                        placeholderTextColor: Theme.textSubtle
                        wrapMode: TextArea.Wrap
                        color: Theme.text
                        selectedTextColor: Theme.accentText
                        selectionColor: Theme.accent
                        font.family: Theme.fontFamily
                        font.pixelSize: Theme.fontSizeMd
                        selectByMouse: true
                        leftPadding: Theme.spacingMd
                        rightPadding: Theme.spacingMd
                        topPadding: Theme.spacingMd
                        bottomPadding: Theme.spacingMd
                        background: Rectangle {
                            radius: Theme.radiusMd
                            color: Theme.surface
                            border.width: textEditor.activeFocus ? Theme.focusRingWidth : 1
                            border.color: textEditor.activeFocus ? Theme.accent : Theme.borderSubtle
                            Behavior on border.color { ColorAnimation { duration: Theme.durationFast } }
                        }
                    }
                }

                // Emotion Tag Chips Toolbar
                ColumnLayout {
                    Layout.fillWidth: true
                    spacing: Theme.spacingSm

                    RowLayout {
                        Layout.fillWidth: true
                        spacing: Theme.spacingSm

                        SectionLabel {
                            text: qsTr("Biểu cảm")
                        }

                        Label {
                            Layout.fillWidth: true
                            text: qsTr("nhấn để chèn tại con trỏ: [cười] [thở dài] [hắng giọng] [ngập ngừng] [thì thầm]")
                            color: Theme.textSubtle
                            font.family: Theme.fontFamily
                            font.pixelSize: Theme.fontSizeXs
                            elide: Text.ElideRight
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
            title: qsTr("Giọng đọc & Điều khiển")

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
                        iconKind: "wave"
                        text: qsTr("Tạo âm thanh")
                        enabled: textEditor.text.trim() !== "" && !controller.busy
                        busy: controller.busy
                        disabledReason: textEditor.text.trim() === ""
                            ? qsTr("Nhập văn bản để tạo âm thanh.") : ""
                        ToolTip.text: qsTr("Tổng hợp phát trực tiếp (Ctrl+Return)")
                        ToolTip.visible: hovered
                        ToolTip.delay: 500

                        onClicked: root.submitForSynthesis()
                    }

                    AppButton {
                        id: playBtn
                        objectName: "playButton"
                        variant: "secondary"
                        size: "lg"
                        text: controller.replayActive ? qsTr("Dừng") : qsTr("Phát")
                        iconKind: controller.replayActive ? "stop" : "play"
                        enabled: controller.hasAudio && !controller.busy
                                  && controller.audioAvailable
                        disabledReason: !controller.hasAudio
                            ? qsTr("Tạo âm thanh trước khi phát.")
                            : qsTr("Không phát hiện thiết bị âm thanh.")
                        ToolTip.text: controller.replayActive
                            ? qsTr("Dừng phát lại")
                            : qsTr("Phát lại âm thanh vừa tạo")
                        ToolTip.visible: hovered && !enabled
                        ToolTip.delay: 200

                        onClicked: {
                            if (controller.replayActive)
                                controller.stopReplay();
                            else
                                controller.replay();
                        }
                    }

                    AppButton {
                        id: exportBtn
                        objectName: "exportButton"
                        variant: "secondary"
                        size: "lg"
                        text: qsTr("Xuất WAV")
                        iconKind: "download"
                        enabled: controller.hasAudio && !controller.busy
                        disabledReason: qsTr("Tạo âm thanh trước khi xuất WAV.")
                        ToolTip.text: qsTr("Chọn vị trí lưu tệp")
                        ToolTip.visible: hovered

                        onClicked: root.openExportDialog()
                    }

                    AppButton {
                        id: quickExportBtn
                        objectName: "quickExportButton"
                        variant: "quiet"
                        size: "lg"
                        text: qsTr("Lưu nhanh")
                        iconKind: "download"
                        enabled: controller.hasAudio && !controller.busy
                        disabledReason: qsTr("Tạo âm thanh trước khi lưu.")
                        ToolTip.text: qsTr("Lưu vào thư mục xuất mặc định (Ctrl+E)")
                        ToolTip.visible: hovered

                        onClicked: controller.exportWav("")
                    }

                    Item { Layout.fillWidth: true }
                }

                Label {
                    id: textActionHint
                    objectName: "textActionHint"
                    Layout.fillWidth: true
                    text: textEditor.text.trim() === ""
                        ? qsTr("Nhập văn bản để tạo âm thanh.")
                        : (!controller.hasAudio
                            ? qsTr("Tạo âm thanh trước khi phát hoặc xuất.")
                            : "")
                    color: Theme.textMuted
                    font.family: Theme.fontFamily
                    font.pixelSize: Theme.fontSizeSm
                    visible: text !== ""
                }

                // Live waveform while synthesis streams (visibility is the
                // tested contract); replay hands the slot to the overview.
                WaveformIndicator {
                    objectName: "waveformIndicator"
                    Layout.fillWidth: true
                    Layout.preferredHeight: 56
                    visible: controller.streamActive && !controller.replayActive
                    active: controller.streamActive
                    level: controller.streamLevel
                }

                // Finished-audio overview + replay playhead ("Phát" feedback):
                // dim shape when idle, accent-filled up to the playhead while
                // replaying, with elapsed/total time labels.
                PlaybackWaveform {
                    objectName: "playbackWaveform"
                    Layout.fillWidth: true
                    Layout.preferredHeight: 56
                    visible: controller.hasAudio && (!controller.streamActive || controller.replayActive)
                    envelope: controller.waveformEnvelope
                    position: controller.replayPosition
                    active: controller.replayActive
                    durationMs: controller.replayDurationMs
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

                        background: Rectangle {
                            implicitHeight: 6
                            radius: 3
                            color: Theme.surfaceAlt
                        }
                        contentItem: Item {
                            clip: true

                            // Determinate fill
                            Rectangle {
                                visible: !progressBar.indeterminate
                                width: progressBar.visualPosition * parent.width
                                height: parent.height
                                radius: 3
                                color: Theme.accent
                            }

                            // Indeterminate sweep
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
                        ToolTip.text: qsTr("Dừng tổng hợp (Esc)")
                        ToolTip.visible: hovered

                        onClicked: controller.cancel()
                    }
                }
            }
        }

        // ── Error Notice ────────────────────────────────────────────────
        AppNotice {
            objectName: "textErrorNotice"
            Layout.fillWidth: true
            tone: "error"
            title: qsTr("Không thể tạo âm thanh")
            message: controller.errorText
            messageObjectName: "errorLabel"
            visible: controller.errorText !== ""
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
                    toastLabel.text = qsTr("Đã hủy")
                    toastLabel.visible = true
                    toastTimer.restart()
                }
                function onLastExportPathChanged() {
                    if (controller.lastExportPath !== "") {
                        toastLabel.text = qsTr("Đã xuất WAV")
                        toastLabel.visible = true
                        toastTimer.restart()
                    }
                }
            }
        }
    }
}
