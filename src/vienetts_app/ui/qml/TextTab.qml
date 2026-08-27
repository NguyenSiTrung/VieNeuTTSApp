// Text tab (FR-3.2, FR-4.3): free-text synthesis — editor with emotion-cue
// hint, grouped voice picker, generate/cancel with progress, play + WAV
// export, error banner and a transient "Đã hủy" toast. All state flows
// through the `controller` / `playback` context properties registered by
// app.py — there are no Component.onCompleted Python round-trips.
//
// Streaming (FR-4.3): the Generate button submits through
// controller.generateStream so playback starts as chunks arrive (§6.2's
// streaming→onnx heuristic is decided Python-side, not here). While
// controller.streamActive is live, the shared WaveformIndicator rolls the
// Python-computed peak-envelope (FR-4.5); the progress bar + cancel stay
// the busy-state contract. On done the retained audio keeps replay/export
// working exactly as before.
//
// objectNames are the tested contract (tests/smoke/test_ui_tabs.py):
// textEditor, voicePicker, generateButton, waveformIndicator, progressBar,
// busyLabel, cancelButton, playButton, exportButton, quickExportButton,
// errorLabel, toastLabel.
//
// The FileDialog is authored but deliberately NOT exercised offscreen (native
// save dialogs are unreliable headless); export coverage goes through
// quickExportButton, which exports to the default output dir.
import QtQuick
import QtQuick.Controls
import QtQuick.Dialogs
import QtQuick.Layouts
import "."

Pane {
    id: root

    objectName: "textTab"
    padding: Theme.spacingLg

    background: Rectangle {
        color: Theme.surface
    }

    // QUrl → local path string for controller.exportWav (FileDialog gives a
    // url; the controller takes a filesystem path). %XX escapes decoded.
    function toLocalPath(url) {
        const s = url.toString();
        return s.startsWith("file://") ? decodeURIComponent(s.substring(7)) : s;
    }

    // Flat picker model from controller.voices, preserving group order:
    // group headers carry id "" ("▸ <group>" — non-selectable, guarded in
    // the generate handler), inner voices are prefixed "— ".
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

    // Seed the dialog with the configured output dir when one exists; the
    // dialog keeps its own default location otherwise.
    function openExportDialog() {
        if (controller.outputDir !== "")
            exportDialog.currentFolder = "file://" + controller.outputDir;
        exportDialog.open();
    }

    ColumnLayout {
        anchors.fill: parent
        spacing: Theme.spacingMd

        Label {
            text: qsTr("Text to Speech")
            color: Theme.text
            font.family: Theme.fontFamily
            font.pixelSize: Theme.fontSizeXl
            font.weight: Theme.fontWeightHeading
        }

        Label {
            Layout.fillWidth: true
            text: qsTr("Nhập hoặc dán văn bản tiếng Việt, chọn giọng đọc và tạo âm thanh.")
            color: Theme.textMuted
            font.family: Theme.fontFamily
            font.pixelSize: Theme.fontSizeBase
            wrapMode: Text.Wrap
        }

        ScrollView {
            Layout.fillWidth: true
            Layout.fillHeight: true

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
                background: Rectangle {
                    radius: 6
                    color: Theme.surfaceAlt
                    border.width: 1
                    border.color: Theme.border
                }
            }
        }

        Label {
            Layout.fillWidth: true
            text: qsTr("Mẹo: chèn cảm xúc vào văn bản — [cười] [thở dài] [hắng giọng]")
            color: Theme.textMuted
            font.family: Theme.fontFamily
            font.pixelSize: Theme.fontSizeSm
            wrapMode: Text.Wrap
        }

        Label {
            text: qsTr("Giọng đọc")
            color: Theme.text
            font.family: Theme.fontFamily
            font.pixelSize: Theme.fontSizeBase
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
            // Keyboard selection of a header row must not change the voice.
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

            delegate: ItemDelegate {
                id: voiceRow

                required property var modelData
                required property int index

                width: ListView.view.width
                text: voiceRow.modelData ? voiceRow.modelData.label : ""
                // Header rows (id "") are display-only: greyed + unclickable.
                enabled: voiceRow.modelData ? voiceRow.modelData.id !== "" : false
                highlighted: voicePicker.highlightedIndex === voiceRow.index

                contentItem: Label {
                    leftPadding: Theme.spacingMd
                    text: voiceRow.text
                    color: voiceRow.enabled ? Theme.text : Theme.textMuted
                    font.family: Theme.fontFamily
                    font.pixelSize: Theme.fontSizeBase
                    verticalAlignment: Text.AlignVCenter
                }
            }
        }

        RowLayout {
            Layout.fillWidth: true
            spacing: Theme.spacingSm

            Button {
                objectName: "generateButton"
                text: qsTr("Tạo âm thanh")
                enabled: textEditor.text.trim() !== "" && !controller.busy
                visible: !controller.busy
                onClicked: {
                    // Header rows carry id "" — fall back to the configured
                    // default voice instead of synthesizing with a group label.
                    const voice = voicePicker.selectedVoice !== ""
                        ? voicePicker.selectedVoice
                        : controller.defaultVoice;
                    controller.generateStream(textEditor.text, voice);
                }
            }

            Button {
                objectName: "playButton"
                text: qsTr("Phát")
                // Play needs an exported file: simplest correct UX is
                // "export first, then play". No audio output device →
                // export-only mode (FR-4.6a): playback controls disabled.
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

            Button {
                objectName: "exportButton"
                text: qsTr("Xuất WAV")
                enabled: controller.hasAudio && !controller.busy
                onClicked: root.openExportDialog()
            }

            Button {
                objectName: "quickExportButton"
                text: qsTr("Lưu nhanh")
                enabled: controller.hasAudio && !controller.busy
                onClicked: controller.exportWav("")
            }
        }

        // Live rolling envelope (FR-4.5): bars mirror the recent peak
        // amplitudes computed Python-side (no samples reach QML); only the
        // flat baseline would show during the quiet head of a session.
        WaveformIndicator {
            objectName: "waveformIndicator"
            Layout.fillWidth: true
            Layout.preferredHeight: 48
            visible: controller.streamActive
            active: controller.streamActive
            level: controller.streamLevel
        }

        RowLayout {
            Layout.fillWidth: true
            spacing: Theme.spacingMd

            Label {
                objectName: "busyLabel"
                text: qsTr("Đang tổng hợp…")
                visible: controller.busy
                color: Theme.textMuted
                font.family: Theme.fontFamily
                font.pixelSize: Theme.fontSizeBase
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
                objectName: "cancelButton"
                text: qsTr("Hủy")
                visible: controller.busy
                onClicked: controller.cancel()
            }
        }

        Label {
            objectName: "errorLabel"
            Layout.fillWidth: true
            visible: controller.errorText !== ""
            text: controller.errorText
            color: Theme.error
            font.family: Theme.fontFamily
            font.pixelSize: Theme.fontSizeBase
            wrapMode: Text.Wrap
        }

        Label {
            id: toastLabel

            objectName: "toastLabel"
            visible: false
            text: qsTr("Đã hủy")
            color: Theme.warning
            font.family: Theme.fontFamily
            font.pixelSize: Theme.fontSizeSm

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
