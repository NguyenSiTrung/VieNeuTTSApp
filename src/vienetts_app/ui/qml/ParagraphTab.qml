// Paragraph/File tab (FR-3.3, FR-4.4, FR-UX-5): long-text & document synthesis studio.
//
// Two explicit modes share one docked action bar instead of stacking every
// control in a single scrolling column (the audit measured the primary CTA
// 208 px below the fold at the default 1120x740 window):
//   "text"  — one document: paste text or import a single file, then synthesize
//   "files" — many documents: drop/select a batch, run it in sequence
// The editor card and the queue card are therefore mutually exclusive, and the
// voice/transport/run controls never scroll away.
//
// objectNames are the tested contract (tests/smoke/test_ui_tabs.py):
// paragraphTab, paragraphEditor, importButton, importDialog, charCountLabel,
// voicePicker, generateButton, playButton, exportButton, waveformIndicator,
// paraBusyLabel, progressBar, cancelButton, errorBanner, errorLabel,
// srtKeepCheckbox, studioButton, livePreviewToggle, paragraphActionHint,
// longParagraphNotice, artifactPlaybackState, playbackWaveform,
// batchQueueCard, batchImportDialog, addFilesButton, runAllButton,
// batchCancelButton, clearFinishedButton, batchFileList, batchEmptyHint,
// batchRunSummary.
// Pinned copy: header "Đoạn văn / Tệp", a ".pdf" mention, "Nhập tệp…",
// "%1 ký tự", "Không thể nhập tệp", "Giữ timecode SRT".
import QtQuick
import QtQuick.Controls
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
    // "text" = single document editor · "files" = multi-file queue
    property string mode: "text"
    // Batch failures (unsupported extension, parse errors) surface in the same
    // banner as import failures — the queue card has no notice row of its own.
    readonly property string batchErrorText: (typeof batchController !== "undefined"
        && batchController !== null) ? (batchController.errorText || "") : ""
    readonly property bool batchHasItems: (typeof batchController !== "undefined"
        && batchController !== null) ? batchController.items.length > 0 : false

    readonly property var modeModel: [
        { id: "text", label: qsTr("Một tài liệu"), icon: "paragraph" },
        { id: "files", label: qsTr("Nhiều tệp"), icon: "file" }
    ]

    function setMode(id) {
        if (id !== "text" && id !== "files")
            return;
        if (mode === id)
            return;
        root.mode = id;
    }

    // QUrl → local path string for controller.importDocument
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

    function importPath(path) {
        // Fire-and-forget: the parse runs off the UI thread (multi-second
        // PDFs); text/error arrive on controller.documentImported below.
        importError = "";
        if (typeof controller.importDocument !== "function") {
            importError = qsTr("Không thể nhập tệp");
            return;
        }
        if (!controller.importDocument(path)) {
            const reason = typeof controller.errorText === "string"
                ? controller.errorText : "";
            importError = reason !== "" ? reason : qsTr("Không thể nhập tệp");
        }
    }

    // Editor drop router: ONE url keeps the editor-import behavior; several
    // urls feed the batch queue (and switch to its mode so the user sees them).
    function handleDroppedUrls(urls) {
        const paths = [];
        for (let i = 0; i < urls.length; i++)
            paths.push(root.toLocalPath(urls[i]));
        if (paths.length === 1) {
            root.importPath(paths[0]);
            return;
        }
        if (typeof batchController !== "undefined" && batchController
            && typeof batchController.addFiles === "function") {
            batchController.addFiles(paths);
            root.setMode("files");
        }
    }

    function submitForSynthesis() {
        if (editorCard.text.trim() === "" || controller.busy)
            return;
        const voice = bar.selectedVoice !== ""
            ? bar.selectedVoice
            : controller.defaultVoice;
        controller.generateStream(editorCard.text, voice);
    }

    Connections {
        target: controller

        function onDocumentImported(path, text) {
            if (typeof text === "string" && text !== "") {
                editorCard.text = text;
                return;
            }
            const reason = typeof controller.errorText === "string"
                && controller.errorText !== ""
                ? controller.errorText : qsTr("Không thể nhập tệp");
            root.importError = reason;
        }
    }

    // --- Keyboard shortcuts (additive) ----------------------------------------
    Shortcut {
        sequence: "Ctrl+Return"
        enabled: editorCard.text.trim() !== "" && !controller.busy
            && root.mode === "text"
        onActivated: root.submitForSynthesis()
        context: Qt.WindowShortcut
    }
    Shortcut {
        sequence: "Escape"
        // Tab-gated: with three window-scoped Escape shortcuts registered
        // (text/paragraph/audiobook), an ungated overlap would make Qt
        // resolve the ambiguity arbitrarily. Only the visible tab's fires.
        enabled: bridge.currentTab === "paragraph" && controller.busy && controller.foregroundJobState !== "cancel_requested"
        onActivated: controller.cancel()
        context: Qt.WindowShortcut
    }

    ColumnLayout {
        anchors.fill: parent
        spacing: Theme.spacingMd

        // ── Scrollable content: header, mode switch, active mode's surface ──
        PageShell {
            id: page

            Layout.fillWidth: true
            Layout.fillHeight: true
            maxWidth: 960
            stretch: true

            PageHeader {
                Layout.fillWidth: true
                iconKind: "paragraph"
                title: qsTr("Đoạn văn / Tệp")
                subtitle: qsTr("Dán văn bản dài hoặc nhập cả một nhóm tài liệu — hệ thống tự phân đoạn và tổng hợp thành tệp âm thanh.")
            }

            // Errors first: an import refusal used to sit at the very bottom of
            // the page, below the fold on smaller windows.
            AppNotice {
                id: errorBanner

                objectName: "errorBanner"
                Layout.fillWidth: true
                tone: "warning"
                title: qsTr("Cần chú ý")
                message: controller.errorText || root.batchErrorText || root.importError
                messageObjectName: "errorLabel"
                visible: message !== ""
            }

            // ── Mode switch ────────────────────────────────────────────────
            // Self-describing labels; no second hint line (the active mode's
            // card subtitle already states what to do).
            ModeTabs {
                id: modeTabs

                objectName: "modeTabs"
                Layout.alignment: Qt.AlignLeft
                model: root.modeModel
                currentId: root.mode
                accessibleLabel: qsTr("Chế độ làm việc")
                onActivated: function (id) {
                    root.setMode(id);
                }
            }

            // ── Mode surfaces (mutually exclusive) ─────────────────────────
            DocumentEditorCard {
                id: editorCard

                Layout.fillWidth: true
                visible: root.mode === "text"
                onFilePicked: function (url) {
                    root.importPath(root.toLocalPath(url));
                }
                onFilesDropped: function (urls) {
                    root.handleDroppedUrls(urls);
                }
            }

            BatchQueueCard {
                id: batchCard

                Layout.fillWidth: true
                // A populated queue owns the page (list + drop strip pinned
                // under the header); an empty one stays a compact drop prompt.
                Layout.fillHeight: root.mode === "files" && root.batchHasItems
                visible: root.mode === "files"
            }
        }

        // ── Docked action bar: voice, transport, mode-aware primary action ──
        SynthesisBar {
            id: bar

            Layout.fillWidth: true
            mode: root.mode
            editorReady: editorCard.text.trim() !== ""
            editorLength: editorCard.text.length
            onGenerateRequested: root.submitForSynthesis()
            onStudioRequested: {
                if (controller.openInStudio("paragraph", editorCard.text))
                    bridge.setCurrentTab("studio");
            }
        }
    }
}
