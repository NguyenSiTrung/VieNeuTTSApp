import QtQuick
import QtQuick.Controls
import QtQuick.Dialogs
import QtQuick.Layouts
import ".."
import "."

// Docked synthesis bar for the Paragraph studio: voice choice, transport and the
// mode-aware primary action. Docked rather than scrolled because the audit found
// the primary CTA ("Tạo âm thanh") 208 px below the fold on the default
// 1120x740 window — a 948 px single column with the action card last.
//
// It owns the controller-facing transport (generate is delegated to the tab via
// `generateRequested` because only the tab holds the editor text) plus the batch
// run controls, so the file queue card stays a list rather than a second
// control surface.
//
// objectNames are the tested contract (tests/smoke/test_ui_tabs.py):
// generateButton, playButton, exportButton, studioButton, livePreviewToggle,
// paragraphActionHint, longParagraphNotice, artifactPlaybackState,
// waveformIndicator, playbackWaveform, paraBusyLabel, progressBar, cancelButton,
// exportDialog, runAllButton, batchCancelButton, batchRunSummary.
// Pinned copy: "Tạo âm thanh", "Nhập văn bản để tạo âm thanh.",
// "Tạo âm thanh trước khi phát hoặc xuất.", "%1/%2 tệp".
Rectangle {
    id: root

    property string mode: "text"      // "text" | "files"
    property bool editorReady: false  // editor holds non-blank text
    property int editorLength: 0

    readonly property string selectedVoice: voicePicker.selectedVoice
    readonly property bool batchAvailable: typeof batchController !== "undefined"
                                           && batchController !== null

    signal generateRequested()
    signal studioRequested()

    color: Theme.surfaceCard
    radius: Theme.radiusLg
    border.width: 1
    border.color: Theme.border
    implicitHeight: barLayout.implicitHeight + Theme.spacingLg * 2

    // Local path string → valid QUrl string for FileDialog currentFolder
    function toFolderUrl(path) {
        if (!path || path.trim() === "")
            return "";
        if (typeof controller !== "undefined" && controller && typeof controller.pathToUrl === "function") {
            const u = controller.pathToUrl(path);
            if (u !== "")
                return u;
        }
        if (path.startsWith("file://"))
            return path;
        const clean = path.replace(/\\/g, "/");
        if (/^[A-Za-z]:\//.test(clean))
            return "file:///" + clean;
        if (clean.startsWith("//"))
            return "file:" + clean;
        if (clean.startsWith("/"))
            return "file://" + clean;
        return "file:///" + clean;
    }

    // QUrl → local path string for controller.exportAudio
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

    // Single-type filter wins for bare names; combined/unknown falls through
    // to the controller (exportFormat setting). Bare names are normally
    // completed by the dialog itself via defaultSuffix (bound to the setting
    // below); this helper only covers backends that return the name as-is.
    // NOTE: FileDialog.selectedNameFilter is read-only (no select method),
    // so the visible filter cannot be pre-selected — do not assign it here.
    function exportPathForFilter(url, filter) {
        const path = root.toLocalPath(url);
        const lower = path.toLowerCase();
        if (lower.endsWith(".wav") || lower.endsWith(".mp3"))
            return path;
        const f = String(filter || "");
        const hasMp3 = f.indexOf("*.mp3") !== -1;
        const hasWav = f.indexOf("*.wav") !== -1;
        if (hasMp3 && !hasWav)
            return path + ".mp3";
        if (hasWav && !hasMp3)
            return path + ".wav";
        return path;
    }

    function openExportDialog() {
        const folder = (controller.outputDir !== "")
            ? root.toFolderUrl(controller.outputDir)
            : (controller.outputDirUrl || "");
        if (folder !== "")
            exportDialog.currentFolder = folder;
        exportDialog.open();
    }

    FileDialog {
        id: exportDialog

        objectName: "exportDialog"
        fileMode: FileDialog.SaveFile
        title: qsTr("Xuất âm thanh")
        nameFilters: ["Âm thanh (*.wav *.mp3)", "WAV (*.wav)", "MP3 (*.mp3)"]
        defaultSuffix: controller.exportFormat
        onAccepted: controller.exportAudio(root.exportPathForFilter(exportDialog.selectedFile, exportDialog.selectedNameFilter))
    }

    // The batch run speaks with the tab's picker voice (one shared voice for
    // the whole run — per-file voices are a non-goal).
    Connections {
        target: voicePicker

        function onSelectedVoiceChanged() {
            if (root.batchAvailable)
                batchController.renderVoice = voicePicker.selectedVoice;
        }
    }

    ColumnLayout {
        id: barLayout

        anchors.fill: parent
        anchors.margins: Theme.spacingLg
        spacing: Theme.spacingMd

        // ── Voice row: who reads it, plus the artifact-level Studio entry ──
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

            // Studio is an artifact action, not page navigation: icon-sized so
            // it never competes with the primary CTA.
            AppButton {
                id: studioBtn

                objectName: "studioButton"
                variant: "icon"
                size: "md"
                iconKind: "studio"
                enabled: controller.hasArtifact && !controller.busy
                disabledReason: qsTr("Tạo âm thanh trước khi mở Studio.")
                tooltipText: qsTr("Mở Studio để chỉnh sửa âm thanh")
                accessibleLabel: qsTr("Mở Studio")
                onClicked: root.studioRequested()
            }
        }

        Rectangle {
            Layout.fillWidth: true
            height: 1
            color: Theme.borderSubtle
        }

        // ── Action row: one primary action per mode ────────────────────────
        // A Flow, not a RowLayout: at the 640 px minimum width four labelled
        // controls overflowed the bar (the live-preview toggle ended up flush
        // against the window edge). Wrapping keeps every control inside the
        // bar, and the toggle now sits next to the Generate button it modifies.
        Flow {
            Layout.fillWidth: true
            spacing: Theme.spacingSm

            AppButton {
                id: generateBtn

                objectName: "generateButton"
                visible: root.mode === "text"
                variant: "primary"
                size: "lg"
                iconKind: "wave"
                text: qsTr("Tạo âm thanh")
                enabled: root.editorReady && !controller.busy
                busy: controller.busy
                disabledReason: root.editorReady ? "" : qsTr("Nhập văn bản để tạo âm thanh.")
                ToolTip.text: qsTr("Tổng hợp phát trực tiếp (Ctrl+Return)")
                ToolTip.visible: hovered

                onClicked: root.generateRequested()
            }

            AppButton {
                id: playBtn

                objectName: "playButton"
                visible: root.mode === "text"
                variant: controller.replayActive ? "primary" : "secondary"
                size: "lg"
                text: controller.replayActive ? qsTr("Dừng") : qsTr("Phát")
                iconKind: controller.replayActive ? "stop" : "play"
                enabled: controller.hasArtifact && controller.audioAvailable
                disabledReason: !controller.hasArtifact
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
                visible: root.mode === "text"
                variant: "secondary"
                size: "lg"
                text: qsTr("Xuất âm thanh")
                iconKind: "download"
                enabled: controller.hasArtifact
                disabledReason: qsTr("Tạo âm thanh trước khi xuất.")
                onClicked: root.openExportDialog()
            }

            // Files mode: the queue's run controls live beside the same voice
            // picker the run uses, instead of a second footer inside the card.
            AppButton {
                id: runAllBtn

                objectName: "runAllButton"
                visible: root.mode === "files"
                variant: "primary"
                size: "lg"
                iconKind: "wave"
                text: qsTr("Tạo tất cả")
                enabled: root.batchAvailable && batchController.hasPending
                         && !batchController.running
                disabledReason: qsTr("Thêm tệp vào hàng đợi để tạo âm thanh.")
                ToolTip.text: qsTr("Tổng hợp lần lượt mọi tệp đang chờ")
                ToolTip.visible: hovered
                onClicked: batchController.runAll()
            }

            AppButton {
                id: batchCancelBtn

                objectName: "batchCancelButton"
                visible: root.mode === "files" && root.batchAvailable
                         && batchController.running
                variant: "danger"
                size: "sm"
                text: qsTr("Hủy")
                onClicked: batchController.cancel()
            }

            Label {
                objectName: "batchRunSummary"
                visible: root.mode === "files" && root.batchAvailable
                         && batchController.runAllTotal > 0
                text: qsTr("%1/%2 tệp").arg(root.batchAvailable ? batchController.runAllDone : 0)
                    .arg(root.batchAvailable ? batchController.runAllTotal : 0)
                color: Theme.textMuted
                font.family: Theme.fontFamily
                font.pixelSize: Theme.fontSizeSm
            }

            // Live vs generate-then-replay (global livePreview setting)
            AppToggle {
                id: livePreviewToggle

                objectName: "livePreviewToggle"
                visible: root.mode === "text"
                text: qsTr("Phát trực tiếp")
                checked: controller.livePreview === true
                enabled: !controller.busy
                onToggled: controller.livePreview = checked
                accessibleLabel: qsTr("Phát trực tiếp khi đang tạo")
                ToolTip.text: qsTr("Tắt: tạo xong tự phát lại từ đầu")
                ToolTip.visible: hovered
            }
        }

        // ── Progress & cancel (foreground synthesis) ──────────────────────
        RowLayout {
            Layout.fillWidth: true
            spacing: Theme.spacingMd
            visible: controller.busy

            Label {
                objectName: "paraBusyLabel"
                text: controller.foregroundJobState === "queued"
                    ? qsTr("Đang chờ xử lý…")
                    : controller.foregroundJobState === "cancel_requested"
                        ? qsTr("Đang hủy…")
                        : qsTr("Đang tổng hợp…")
                visible: controller.busy
                color: controller.foregroundJobState === "cancel_requested"
                    ? Theme.warning
                    : Theme.accent
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
                text: controller.foregroundJobState === "cancel_requested"
                    ? qsTr("Đang hủy…")
                    : qsTr("Hủy")
                visible: controller.busy
                enabled: controller.foregroundJobState !== "cancel_requested"
                busy: controller.foregroundJobState === "cancel_requested"
                ToolTip.text: qsTr("Dừng tổng hợp (Esc)")
                ToolTip.visible: hovered
                onClicked: controller.cancel()
            }
        }

        Label {
            objectName: "artifactPlaybackState"
            Layout.fillWidth: true
            visible: controller.playbackState !== "idle"
            text: controller.playbackState === "prebuffering"
                ? qsTr("Đệm âm thanh…")
                : controller.playbackState === "generating"
                    ? qsTr("Đang tạo và phát")
                    : qsTr("Đang phát phần còn lại…")
            color: Theme.textMuted
            font.family: Theme.fontFamily
            font.pixelSize: Theme.fontSizeSm
        }

        // Live waveform while synthesis streams (visibility is the tested
        // contract); replay hands the slot to the overview.
        WaveformIndicator {
            objectName: "waveformIndicator"
            Layout.fillWidth: true
            Layout.preferredHeight: 56
            visible: (controller.playbackState === "prebuffering"
                      || controller.playbackState === "generating")
                     && !controller.replayActive
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
            visible: controller.hasArtifact && controller.waveformEnvelope.length > 0
            envelope: controller.waveformEnvelope
            position: controller.replayPosition
            active: controller.replayActive
            durationMs: controller.replayDurationMs
        }

        // One visible guidance line for the disabled transport. The disabled
        // buttons keep their own reason tooltips for hover/a11y; this line is
        // the always-visible half.
        Label {
            id: paragraphActionHint

            objectName: "paragraphActionHint"
            Layout.fillWidth: true
            text: !root.editorReady
                ? qsTr("Nhập văn bản để tạo âm thanh.")
                : (!controller.hasArtifact
                    ? qsTr("Tạo âm thanh trước khi phát hoặc xuất.")
                    : (!controller.audioAvailable
                        ? qsTr("Âm thanh đã sẵn sàng để xuất; không phát hiện thiết bị phát.")
                        : ""))
            visible: text !== "" && root.mode === "text"
            color: Theme.textMuted
            font.family: Theme.fontFamily
            font.pixelSize: Theme.fontSizeSm
        }

        Label {
            id: longParagraphNotice

            objectName: "longParagraphNotice"
            Layout.fillWidth: true
            visible: root.editorLength > 2000 && !controller.busy
                     && root.mode === "text"
            text: controller.livePreview
                ? qsTr("Lưu ý: Văn bản dài — nên tắt 'Phát trực tiếp' hoặc dùng tab Sách nói (EPUB) để tránh gián đoạn âm thanh.")
                : qsTr("Văn bản dài: Âm thanh sẽ được tạo đầy đủ ra tệp và tự động phát lại khi hoàn tất.")
            color: controller.livePreview ? Theme.warning : Theme.textMuted
            font.family: Theme.fontFamily
            font.pixelSize: Theme.fontSizeXs
            wrapMode: Text.Wrap
        }
    }
}
