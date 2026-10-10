import QtQuick
import QtQuick.Controls
import QtQuick.Dialogs
import QtQuick.Layouts
import ".."
import "."

// Shared transport dock (FR-2.2/2.3, ui_shell_redesign): the pinned card the
// Text and Paragraph tabs generate, play and export through (generalized from
// the Paragraph tab's former SynthesisBar).
//
// One row, wrapping on narrow widths:
//   voice chip · Tạo âm thanh ⇄ Dừng · [caller actions] · Phát ·
//   waveform + timecodes · Xuất WAV ▾ · Mở trong Studio
// (The live-playback toggle left the former ⋯ overflow menu for the Create
// inspector in Phase 3, FR-2.5 — one toggle per screen.)
// The waveform sits inline while it gets ≥ 200 px, otherwise it drops to its
// own full-width row under the controls (same item, re-parented). On a short
// AND narrow window (`condensed`) the secondary actions go icon-only so the
// transport stays one row above the host's editor.
//
// Ownership: the transport binds the global `controller` directly (busy,
// cancel, replay, export are app-wide, identical for every caller). Only what differs per caller is API: the content gate
// (`canGenerate`), which groups show, the objectNames of the status labels,
// and two signals — `generateRequested` (only the tab holds the text) and
// `studioRequested` (the Studio source kind/text are the tab's).
//
// Primary hierarchy: Tạo âm thanh is the dock's ONE primary; while a job runs
// it is REPLACED in place by Dừng (never a second always-visible button).
//
// Shortcuts are scoped to this dock: they fire only while the dock is
// effectively visible (`visible` is false inside a hidden StackLayout tab),
// so several docks in one window never make a window shortcut ambiguous.
//   Ctrl+Enter / Ctrl+Return  generate   (plain Enter still types a newline)
//   Esc                       stop the foreground job
//   Ctrl+E                    quick save (Lưu nhanh)
//
// Export split: the main part exports the artifact NOW in the current
// format setting (controller.exportFormat) to the output folder; the menu
// picks that format (WAV / MP3) and the location (Lưu nhanh = output folder,
// Lưu thành… = Save dialog).
//
// objectNames (tested contract): voicePicker (the chip), generateButton,
// cancelButton (Dừng), playButton, waveformIndicator, playbackWaveform,
// dockWaveformPlaceholder, exportButton, exportMenuButton, exportMenu,
// exportFormatWav, exportFormatMp3, quickExportButton, saveAsButton,
// exportDialog, studioButton, progressBar, artifactPlaybackState, plus the caller-named
// busy label / action hint / long-text notice (busyLabelObjectName, …).
Rectangle {
    id: root

    // ── Caller API ──────────────────────────────────────────────────────────
    // The caller's content gate ("the editor holds non-blank text").
    property bool canGenerate: false
    property string generateHint: qsTr("Nhập văn bản để tạo âm thanh.")
    property int editorLength: 0
    property bool showVoiceChip: true
    property bool showGenerate: true
    property bool showPlayback: true
    property bool showExport: true
    property bool showStudio: true
    property bool showHints: showGenerate
    // Short windows (the host decides, e.g. tab height < 560): the guidance
    // lines and the below-row waveform collapse and the padding tightens, so
    // a two-row dock never crushes the host's editor. Disabled controls keep
    // their own reason tooltips; the waveform stays when it fits inline.
    property bool compact: false
    // Compact AND narrow (the 640×420 minimum): Phát, Xuất and Mở trong Studio
    // drop to icon-only squares (labels stay their accessible names and
    // tooltips) so the transport is ONE row instead of two. Width-driven
    // (never implicit widths), so it cannot feed back into the layout.
    readonly property bool condensed: compact && width < 720
    // AND-ed with effective visibility; a caller can mute the dock's keys.
    property bool shortcutsEnabled: true
    property string busyLabelObjectName: "busyLabel"
    property string actionHintObjectName: "actionHint"
    property string longTextNoticeObjectName: "longTextNotice"

    // Rows appended under the transport (language picker, notices, …).
    default property alias extraContent: extras.data
    // Extra controls placed in the transport row right after Generate/Stop
    // (the Paragraph files mode puts its run-all controls here).
    property alias actions: actionsRow.data

    readonly property alias picker: voiceChip
    readonly property string selectedVoice: voiceChip.selectedVoice
    // The voice a submission carries (the chip's choice, or the active
    // profile's own fallback — never another engine's default voice).
    readonly property string effectiveVoice: voiceChip.effectiveVoice
    readonly property bool shortcutsActive: root.visible && root.shortcutsEnabled
    readonly property bool canSubmit: root.canGenerate && !controller.busy
                                      && EngineState.blockerReason === ""
    readonly property string exportFormat: controller.exportFormat === "mp3" ? "mp3" : "wav"
    readonly property bool cancelRequested: controller.foregroundJobState === "cancel_requested"

    signal generateRequested()
    signal studioRequested()

    color: Theme.surfaceCard
    radius: Theme.radiusDock
    border.width: 1
    border.color: Theme.border
    readonly property int _padV: compact ? Theme.spacingSm : Theme.spacingMd
    implicitHeight: dockLayout.implicitHeight + _padV * 2

    Accessible.role: Accessible.ToolBar
    Accessible.name: qsTr("Điều khiển phát và xuất")

    // ── Actions (also the seams tests and shortcuts drive) ─────────────────
    function quickSave() {
        if (!controller.hasArtifact || controller.exporting === true)
            return;
        controller.exportAudio("");
    }

    function setExportFormat(format) {
        if (format === "wav" || format === "mp3")
            controller.exportFormat = format;
    }

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
        defaultSuffix: root.exportFormat
        onAccepted: controller.exportAudio(root.exportPathForFilter(exportDialog.selectedFile, exportDialog.selectedNameFilter))
    }

    // ── Shortcuts (scoped to this dock's visibility) ───────────────────────
    Shortcut {
        objectName: "dockGenerateShortcut"
        sequences: ["Ctrl+Return", "Ctrl+Enter"]
        context: Qt.WindowShortcut
        enabled: root.shortcutsActive && root.showGenerate && root.canSubmit
        onActivated: root.generateRequested()
    }

    Shortcut {
        objectName: "dockStopShortcut"
        sequence: "Escape"
        context: Qt.WindowShortcut
        enabled: root.shortcutsActive && controller.busy && !root.cancelRequested
        onActivated: controller.cancel()
    }

    Shortcut {
        objectName: "dockQuickSaveShortcut"
        sequence: "Ctrl+E"
        context: Qt.WindowShortcut
        enabled: root.shortcutsActive && root.showExport && controller.hasArtifact
                 && !controller.busy && controller.exporting !== true
        onActivated: root.quickSave()
    }

    // ── Row width budget: the waveform takes what the controls leave ───────
    // Implicit widths and intent flags only (never laid-out widths or the
    // effective `visible`), so the budget cannot feed back into itself.
    readonly property real _gap: transportFlow.spacing
    readonly property real _primaryWidth: Math.max(generateBtn.implicitWidth, cancelBtn.implicitWidth)
    readonly property real _controlsWidth:
        (showVoiceChip ? voiceChip.implicitWidth + _gap : 0)
        + ((showGenerate || controller.busy) ? _primaryWidth + _gap : 0)
        + (actionsRow.implicitWidth > 0 ? actionsRow.implicitWidth + _gap : 0)
        + (showPlayback ? playBtn.implicitWidth + _gap : 0)
        + (showExport ? exportSplit.implicitWidth + _gap : 0)
        + (showStudio ? studioBtn.implicitWidth + _gap : 0)
    // 2 px slack: sub-pixel rounding must never push the slot to a new line.
    readonly property real _inlineWaveWidth: Math.floor(transportFlow.width - _controlsWidth) - 2
    readonly property bool waveInline: showPlayback && _inlineWaveWidth >= 200
    // Raw slot conditions (never the effective `visible` of the waveforms,
    // which depends on whichever slot currently parents them).
    readonly property bool _meterOn: (controller.playbackState === "prebuffering"
                                      || controller.playbackState === "generating")
                                     && !controller.replayActive
    readonly property bool _overviewOn: controller.hasArtifact
                                        && controller.waveformEnvelope.length > 0

    ColumnLayout {
        id: dockLayout

        anchors.fill: parent
        anchors.leftMargin: Theme.spacingMd + 2
        anchors.rightMargin: Theme.spacingMd + 2
        anchors.topMargin: root._padV
        anchors.bottomMargin: root._padV
        spacing: Theme.spacingSm

        Flow {
            id: transportFlow

            objectName: "dockTransportRow"
            Layout.fillWidth: true
            spacing: Theme.spacingMd

            // Voice chip → the shared catalog popup (same VoicePicker control).
            VoicePicker {
                id: voiceChip

                compact: true
                visible: root.showVoiceChip
            }

            AppButton {
                id: generateBtn

                objectName: "generateButton"
                width: root._primaryWidth
                visible: root.showGenerate && !controller.busy
                variant: "primary"
                size: "lg"
                iconKind: "wave"
                text: qsTr("Tạo âm thanh")
                enabled: root.canSubmit
                busy: controller.busy
                disabledReason: EngineState.blockerReason !== ""
                    ? EngineState.blockerReason
                    : (root.canGenerate ? "" : root.generateHint)
                tooltipText: qsTr("Tạo âm thanh (Ctrl+Enter)")
                onClicked: root.generateRequested()
            }

            // Dừng: replaces Tạo âm thanh in place while a foreground job runs
            // (queued, generating, or already cancelling).
            AppButton {
                id: cancelBtn

                objectName: "cancelButton"
                width: root._primaryWidth
                visible: controller.busy
                variant: "danger"
                size: "lg"
                iconKind: "stop"
                text: root.cancelRequested ? qsTr("Đang hủy…") : qsTr("Dừng")
                accessibleLabel: root.cancelRequested ? qsTr("Đang hủy…") : qsTr("Dừng tạo âm thanh")
                enabled: !root.cancelRequested
                busy: root.cancelRequested
                tooltipText: qsTr("Dừng tổng hợp (Esc)")
                onClicked: controller.cancel()
            }

            Row {
                id: actionsRow

                spacing: Theme.spacingSm
            }

            AppButton {
                id: playBtn

                objectName: "playButton"
                visible: root.showPlayback
                iconOnly: root.condensed
                variant: "secondary"
                checked: controller.replayActive
                size: "lg"
                text: controller.replayActive ? qsTr("Dừng") : qsTr("Phát")
                accessibleLabel: controller.replayActive ? qsTr("Dừng phát lại") : qsTr("Phát")
                iconKind: controller.replayActive ? "stop" : "play"
                enabled: controller.hasArtifact && controller.audioAvailable
                disabledReason: !controller.hasArtifact
                    ? qsTr("Tạo âm thanh trước khi phát.")
                    : qsTr("Không phát hiện thiết bị âm thanh.")
                tooltipText: controller.replayActive
                    ? qsTr("Dừng phát lại")
                    : qsTr("Phát lại âm thanh vừa tạo")
                onClicked: {
                    if (controller.replayActive)
                        controller.stopReplay();
                    else
                        controller.replay();
                }
            }

            // Inline waveform slot (wide rows); the group re-parents into
            // `belowSlot` when the row cannot spare 200 px.
            Item {
                id: inlineSlot

                visible: root.waveInline
                width: Math.max(0, root._inlineWaveWidth)
                height: 48
            }

            Row {
                id: exportSplit

                visible: root.showExport
                spacing: 2

                AppButton {
                    id: exportBtn

                    objectName: "exportButton"
                    iconOnly: root.condensed
                    variant: "secondary"
                    size: "lg"
                    iconKind: "download"
                    text: root.exportFormat === "mp3" ? qsTr("Xuất MP3") : qsTr("Xuất WAV")
                    enabled: controller.hasArtifact && controller.exporting !== true
                    busy: controller.exporting === true
                    disabledReason: qsTr("Tạo âm thanh trước khi xuất.")
                    tooltipText: qsTr("Lưu vào thư mục xuất mặc định (Ctrl+E)")
                    onClicked: root.quickSave()
                }

                AppButton {
                    id: exportMenuBtn

                    objectName: "exportMenuButton"
                    implicitWidth: Theme.controlHitTarget
                    variant: "secondary"
                    size: "lg"
                    iconKind: "chevronDown"
                    text: ""
                    checked: exportMenu.visible
                    accessibleLabel: qsTr("Chọn định dạng và vị trí xuất")
                    tooltipText: accessibleLabel
                    onClicked: exportMenu.visible ? exportMenu.close() : exportMenu.open()

                    AppMenu {
                        id: exportMenu

                        objectName: "exportMenu"
                        x: exportMenuBtn.width - width
                        y: -height - Theme.spacingXs

                        AppMenuItem {
                            objectName: "exportFormatWav"
                            text: qsTr("WAV")
                            accessibleLabel: qsTr("Định dạng WAV")
                            markable: true
                            marked: root.exportFormat === "wav"
                            onTriggered: root.setExportFormat("wav")
                        }

                        AppMenuItem {
                            objectName: "exportFormatMp3"
                            text: qsTr("MP3")
                            accessibleLabel: qsTr("Định dạng MP3")
                            markable: true
                            marked: root.exportFormat === "mp3"
                            onTriggered: root.setExportFormat("mp3")
                        }

                        MenuSeparator {
                            contentItem: Rectangle {
                                implicitHeight: 1
                                color: Theme.borderSubtle
                            }
                        }

                        AppMenuItem {
                            objectName: "quickExportButton"
                            text: qsTr("Lưu nhanh")
                            shortcutText: "Ctrl+E"
                            enabled: controller.hasArtifact && controller.exporting !== true
                            onTriggered: root.quickSave()
                        }

                        AppMenuItem {
                            objectName: "saveAsButton"
                            text: qsTr("Lưu thành…")
                            enabled: controller.hasArtifact && controller.exporting !== true
                            onTriggered: root.openExportDialog()
                        }
                    }
                }
            }

            // Studio is an artifact action, not page navigation: a quiet link
            // that never competes with the primary.
            AppButton {
                id: studioBtn

                objectName: "studioButton"
                visible: root.showStudio
                iconOnly: root.condensed
                variant: "quiet"
                size: "lg"
                iconKind: "studio"
                text: qsTr("Mở trong Studio")
                enabled: controller.hasArtifact && !controller.busy
                disabledReason: qsTr("Tạo âm thanh trước khi mở Studio.")
                tooltipText: qsTr("Chỉnh sửa âm thanh trước khi xuất")
                onClicked: root.studioRequested()
            }
        }

        // Full-width waveform row when the controls leave too little room.
        // Only while there is audio to show: the idle placeholder is an
        // inline-only nicety, never worth a whole row of a short window.
        Item {
            id: belowSlot

            Layout.fillWidth: true
            Layout.preferredHeight: 48
            visible: root.showPlayback && !root.waveInline && !root.compact
                     && (root._meterOn || root._overviewOn)
        }

        // ── Foreground progress (the Stop button lives in the row above) ──
        RowLayout {
            Layout.fillWidth: true
            spacing: Theme.spacingMd
            visible: controller.busy

            Label {
                objectName: root.busyLabelObjectName
                text: controller.foregroundJobState === "queued"
                    ? (controller.preparingEngine === true
                        ? qsTr("Đang chuẩn bị mô hình…")
                        : qsTr("Đang chờ xử lý…"))
                    : root.cancelRequested
                        ? qsTr("Đang hủy…")
                        : qsTr("Đang tổng hợp…")
                visible: controller.busy
                color: root.cancelRequested ? Theme.warning : Theme.accent
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
        }

        Label {
            objectName: "artifactPlaybackState"
            Layout.fillWidth: true
            visible: controller.playbackState !== "idle" && root.showPlayback
            text: controller.playbackState === "prebuffering"
                ? qsTr("Đệm âm thanh…")
                : controller.playbackState === "generating"
                    ? qsTr("Đang tạo và phát")
                    : qsTr("Đang phát phần còn lại…")
            color: Theme.textMuted
            font.family: Theme.fontFamily
            font.pixelSize: Theme.fontSizeSm
        }

        // One visible guidance line for the disabled transport. The disabled
        // buttons keep their own reason tooltips for hover/a11y; this line is
        // the always-visible half.
        Label {
            objectName: root.actionHintObjectName
            Layout.fillWidth: true
            text: !root.canGenerate
                ? root.generateHint
                : (!controller.hasArtifact
                    ? qsTr("Tạo âm thanh trước khi phát hoặc xuất.")
                    : (!controller.audioAvailable
                        ? qsTr("Âm thanh đã sẵn sàng để xuất; không phát hiện thiết bị phát.")
                        : ""))
            visible: text !== "" && root.showHints && !root.compact
            color: Theme.textMuted
            font.family: Theme.fontFamily
            font.pixelSize: Theme.fontSizeSm
            wrapMode: Text.Wrap
        }

        Label {
            objectName: root.longTextNoticeObjectName
            Layout.fillWidth: true
            visible: root.editorLength > 2000 && !controller.busy && root.showHints
                     && !root.compact
            text: controller.livePreview
                ? qsTr("Lưu ý: Văn bản dài — nên tắt 'Phát trực tiếp' hoặc dùng tab Sách nói (EPUB) để tránh gián đoạn âm thanh.")
                : qsTr("Văn bản dài: Âm thanh sẽ được tạo đầy đủ ra tệp và tự động phát lại khi hoàn tất.")
            color: controller.livePreview ? Theme.warning : Theme.textMuted
            font.family: Theme.fontFamily
            font.pixelSize: Theme.fontSizeXs
            wrapMode: Text.Wrap
        }

        ColumnLayout {
            id: extras

            Layout.fillWidth: true
            spacing: Theme.spacingSm
            visible: children.length > 0
        }
    }

    // ── Waveform group: live meter, finished-audio overview, idle baseline.
    // One instance, re-parented between the inline slot and the row below.
    Item {
        id: waveGroup

        objectName: "dockWaveform"
        parent: root.waveInline ? inlineSlot : belowSlot
        anchors.fill: parent

        // Finished-audio overview + replay playhead with elapsed/total time
        // labels ("Phát" feedback). Its `visible` contract is unchanged; it
        // only steps back (opacity) while the live meter owns the slot.
        PlaybackWaveform {
            id: overview

            objectName: "playbackWaveform"
            anchors.fill: parent
            visible: root._overviewOn
            opacity: root._meterOn ? 0 : 1
            envelope: controller.waveformEnvelope
            position: controller.replayPosition
            active: controller.replayActive
            durationMs: controller.replayDurationMs
        }

        // Live waveform while synthesis streams (visibility is the tested
        // contract); replay hands the slot to the overview.
        WaveformIndicator {
            id: meter

            objectName: "waveformIndicator"
            anchors.fill: parent
            visible: root._meterOn
            active: controller.streamActive
            level: controller.streamLevel
        }

        // Nothing generated yet: a quiet baseline keeps the slot's shape.
        Rectangle {
            objectName: "dockWaveformPlaceholder"
            anchors.fill: parent
            visible: !root._meterOn && !root._overviewOn
            radius: Theme.radiusMd
            color: Theme.surfaceAlt
            border.color: Theme.borderSubtle
            border.width: 1

            Rectangle {
                anchors.verticalCenter: parent.verticalCenter
                anchors.left: parent.left
                anchors.right: parent.right
                anchors.margins: Theme.spacingMd
                height: 1
                color: Theme.border
            }
        }
    }
}
