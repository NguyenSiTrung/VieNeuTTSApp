import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import ".."
import "."

// Paragraph tab's docked bar — now a THIN WRAPPER around TransportDock
// (ui_shell_redesign Task 2.2) until Phase 3 removes its last caller. It keeps
// the public surface ParagraphTab binds (mode, editorReady, editorLength,
// selectedVoice, effectiveVoice, batchAvailable, generateRequested,
// studioRequested) and maps it onto the dock, plus the two Paragraph-only
// pieces: the files-mode run controls and the language picker.
//
// objectNames are the tested contract (tests/smoke/test_ui_tabs.py):
// everything TransportDock names (generateButton, cancelButton, playButton,
// exportButton, studioButton, livePreviewToggle, progressBar,
// waveformIndicator, playbackWaveform, artifactPlaybackState, exportDialog,
// voicePicker, …) plus paraBusyLabel, paragraphActionHint,
// longParagraphNotice, runAllButton, batchCancelButton, batchRunSummary,
// paraLanguagePicker.
// Pinned copy: "Tạo âm thanh", "Nhập văn bản để tạo âm thanh.",
// "Tạo âm thanh trước khi phát hoặc xuất.", "%1/%2 tệp".
TransportDock {
    id: root

    property string mode: "text"      // "text" | "files"
    property bool editorReady: false  // editor holds non-blank text

    readonly property bool batchAvailable: typeof batchController !== "undefined"
                                           && batchController !== null

    canGenerate: editorReady
    showGenerate: mode === "text"
    showPlayback: mode === "text"
    showExport: mode === "text"
    showLivePreview: mode === "text"
    busyLabelObjectName: "paraBusyLabel"
    actionHintObjectName: "paragraphActionHint"
    longTextNoticeObjectName: "longParagraphNotice"

    // Files mode: the queue's run controls sit in the transport row, beside
    // the same voice chip the run uses, instead of a second footer inside the
    // queue card.
    actions: [
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
                     && EngineState.blockerReason === ""
            disabledReason: EngineState.blockerReason !== ""
                ? EngineState.blockerReason
                : qsTr("Thêm tệp vào hàng đợi để tạo âm thanh.")
            tooltipText: qsTr("Tổng hợp lần lượt mọi tệp đang chờ")
            onClicked: batchController.runAll()
        },
        AppButton {
            id: batchCancelBtn

            objectName: "batchCancelButton"
            visible: root.mode === "files" && root.batchAvailable
                     && batchController.running
            variant: "danger"
            size: "sm"
            text: qsTr("Hủy")
            onClicked: batchController.cancel()
        },
        Label {
            objectName: "batchRunSummary"
            height: Theme.controlHitTarget
            visible: root.mode === "files" && root.batchAvailable
                     && batchController.runAllTotal > 0
            text: qsTr("%1/%2 tệp").arg(root.batchAvailable ? batchController.runAllDone : 0)
                .arg(root.batchAvailable ? batchController.runAllTotal : 0)
            color: Theme.textMuted
            font.family: Theme.fontFamily
            font.pixelSize: Theme.fontSizeSm
            verticalAlignment: Text.AlignVCenter
        }
    ]

    // Language row: the same capability-driven control the Text tab uses,
    // so the paragraph/file run cannot be submitted with a language the
    // active engine does not accept.
    LanguagePicker {
        objectName: "paraLanguagePicker"
        Layout.fillWidth: true
    }

    // The batch run speaks with the tab's chip voice (one shared voice for the
    // whole run — per-file voices are a non-goal).
    Connections {
        target: root.picker

        function onEffectiveVoiceChanged() {
            if (root.batchAvailable)
                batchController.renderVoice = root.picker.effectiveVoice;
        }
    }

    // Seed the run's voice as soon as the catalog resolves: the batch
    // controller's own fallback is the VieNeu-scoped default voice, which a
    // Qwen profile could not serve.
    Component.onCompleted: {
        if (root.batchAvailable)
            batchController.renderVoice = root.picker.effectiveVoice;
    }
}
