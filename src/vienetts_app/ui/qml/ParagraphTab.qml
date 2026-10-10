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
// batchRunSummary, paragraphEscapeShortcut, subtitleCard, paraLanguagePicker.
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
    // "srt"  = subtitle timeline studio (dub/transcript)
    property string mode: "text"
    // Batch failures (unsupported extension, parse errors) surface in the same
    // banner as import failures — the queue card has no notice row of its own.
    readonly property string batchErrorText: (typeof batchController !== "undefined"
        && batchController !== null) ? (batchController.errorText || "") : ""
    readonly property string subtitleErrorText: (typeof subtitleController !== "undefined"
        && subtitleController !== null) ? (subtitleController.errorText || "") : ""
    readonly property bool batchHasItems: (typeof batchController !== "undefined"
        && batchController !== null) ? batchController.itemCount > 0 : false

    // "compose" is not a mode of this page: it routes back to the Text page
    // (Tạo giọng đọc's compose mode, FR-3.1) — interim until CreateTab.
    readonly property var modeModel: [
        { id: "compose", label: qsTr("Soạn thảo"), icon: "text" },
        { id: "text", label: qsTr("Một tài liệu"), icon: "paragraph" },
        { id: "files", label: qsTr("Nhiều tệp"), icon: "file" },
        { id: "srt", label: qsTr("Phụ đề (SRT)"), icon: "wave" }
    ]

    function setMode(id) {
        if (id !== "text" && id !== "files" && id !== "srt")
            return;
        if (mode === id)
            return;
        root.mode = id;
        // Each mode is a different page: restart at the top so the header
        // and the mode switch stay where the user left them.
        page.scrollToTop();
    }

    // ── Shell navigation (FR-3.1, interim until CreateTab) ─────────────────
    // This page is Tạo giọng đọc's document/files/subtitles modes. The two
    // mode states stay in sync both ways: bridge.createMode drives `mode`,
    // and a switch made here (mode tabs, multi-file drop) is written back
    // while this page is the one on screen.
    readonly property var modeForCreateMode: ({ "document": "text", "files": "files", "subtitles": "srt" })
    readonly property var createModeForMode: ({ "text": "document", "files": "files", "srt": "subtitles" })
    readonly property bool shownInShell: bridge.currentTab === "create"
        && bridge.createMode !== "compose"

    function followCreateMode() {
        const target = root.modeForCreateMode[bridge.createMode];
        if (target !== undefined)
            root.setMode(target);
    }

    onModeChanged: if (root.shownInShell) bridge.setCreateMode(root.createModeForMode[root.mode])
    Component.onCompleted: root.followCreateMode()

    Connections {
        target: bridge

        function onCreateModeChanged() {
            root.followCreateMode();
        }
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
        if (editorCard.text.trim() === "" || controller.busy
                || EngineState.blockerReason !== "")
            return;
        controller.generateStream(editorCard.text, bar.effectiveVoice);
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

    // --- Keyboard shortcuts ----------------------------------------------------
    // Ctrl+Enter (generate), Esc (stop) and Ctrl+E (quick save) belong to the
    // docked TransportDock while it is shown (text/files modes). The SRT mode
    // hides the dock, so this tab-level Escape covers that mode only: a
    // running cue render is cancelled via the subtitle controller, a regular
    // foreground job still goes to controller.cancel. Mode-gated so it can
    // never overlap the dock's own Escape (an ambiguous window shortcut
    // fires neither).
    Shortcut {
        objectName: "paragraphEscapeShortcut"
        sequence: "Escape"
        // Tab-gated: with several window-scoped Escape shortcuts registered
        // (text/paragraph/audiobook), an ungated overlap would make Qt
        // resolve the ambiguity arbitrarily. Only the visible tab's fires.
        enabled: root.shownInShell && root.mode === "srt"
            && ((controller.busy && controller.foregroundJobState !== "cancel_requested")
                || (typeof subtitleController !== "undefined"
                    && subtitleController !== null && subtitleController.rendering))
        onActivated: {
            if (root.mode === "srt" && typeof subtitleController !== "undefined"
                && subtitleController !== null && subtitleController.rendering) {
                subtitleController.cancelRender();
            } else {
                controller.cancel();
            }
        }
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
            reserveVerticalScrollBar: true

            PageHeader {
                objectName: "paragraphPageHeader"
                Layout.fillWidth: true
                // Very short windows (≈640×420): the title repeats the nav
                // rail's selection, so it yields its row to the document
                // editor, which would otherwise start under the pinned dock.
                visible: root.height >= 420
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
                message: controller.errorText || root.batchErrorText
                    || root.subtitleErrorText || root.importError
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
                    if (id === "compose")
                        bridge.setCreateMode("compose");
                    else
                        root.setMode(id);
                }
            }

            // ── Mode surfaces (mutually exclusive) ─────────────────────────
            DocumentEditorCard {
                id: editorCard

                Layout.fillWidth: true
                // The document owns the page height the dock leaves.
                Layout.fillHeight: root.mode === "text"
                compact: bar.compact
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

            SubtitleCard {
                id: subtitleCard

                Layout.fillWidth: true
                visible: root.mode === "srt"
            }

            Item {
                Layout.fillHeight: root.mode === "srt"
                                   || (root.mode === "files" && !root.batchHasItems)
            }
        }

        // ── Pinned transport dock (FR-2.3): voice, transport, mode-aware
        // primary action. The SRT mode owns its own controls inside
        // SubtitleCard, so the dock is hidden there rather than showing an
        // empty shell. In files mode the transport groups hide and the
        // queue's run controls take the primary slot (`actions`).
        TransportDock {
            id: bar

            objectName: "paragraphDock"
            readonly property bool batchAvailable: typeof batchController !== "undefined"
                                                   && batchController !== null

            Layout.fillWidth: true
            // Aligned with the page's reading column on wide windows.
            Layout.maximumWidth: 960
            Layout.alignment: Qt.AlignHCenter
            visible: root.mode !== "srt"
            // Short windows: the dock sheds its hint lines so the document
            // editor keeps a usable height (640×420 leaves ~330 px).
            compact: root.height < 560
            canGenerate: editorCard.text.trim() !== ""
            editorLength: editorCard.text.length
            showGenerate: root.mode === "text"
            showPlayback: root.mode === "text"
            showExport: root.mode === "text"
            showLivePreview: root.mode === "text"
            busyLabelObjectName: "paraBusyLabel"
            actionHintObjectName: "paragraphActionHint"
            longTextNoticeObjectName: "longParagraphNotice"
            onGenerateRequested: root.submitForSynthesis()
            onStudioRequested: {
                if (controller.openInStudio("paragraph", editorCard.text))
                    bridge.setCurrentTab("studio");
            }

            // Files mode: the queue's run controls sit in the transport row,
            // beside the same voice chip the run uses, instead of a second
            // footer inside the queue card.
            actions: [
                AppButton {
                    objectName: "runAllButton"
                    visible: root.mode === "files"
                    variant: "primary"
                    size: "lg"
                    iconKind: "wave"
                    text: qsTr("Tạo tất cả")
                    enabled: bar.batchAvailable && batchController.hasPending
                             && !batchController.running
                             && EngineState.blockerReason === ""
                    disabledReason: EngineState.blockerReason !== ""
                        ? EngineState.blockerReason
                        : qsTr("Thêm tệp vào hàng đợi để tạo âm thanh.")
                    tooltipText: qsTr("Tổng hợp lần lượt mọi tệp đang chờ")
                    onClicked: batchController.runAll()
                },
                AppButton {
                    objectName: "batchCancelButton"
                    visible: root.mode === "files" && bar.batchAvailable
                             && batchController.running
                    variant: "danger"
                    size: "sm"
                    text: qsTr("Hủy")
                    onClicked: batchController.cancel()
                },
                Label {
                    objectName: "batchRunSummary"
                    height: Theme.controlHitTarget
                    visible: root.mode === "files" && bar.batchAvailable
                             && batchController.runAllTotal > 0
                    text: qsTr("%1/%2 tệp").arg(bar.batchAvailable ? batchController.runAllDone : 0)
                        .arg(bar.batchAvailable ? batchController.runAllTotal : 0)
                    color: Theme.textMuted
                    font.family: Theme.fontFamily
                    font.pixelSize: Theme.fontSizeSm
                    verticalAlignment: Text.AlignVCenter
                }
            ]

            // Language row: the same capability-driven control the Text tab
            // uses, so the paragraph/file run cannot be submitted with a
            // language the active engine does not accept.
            LanguagePicker {
                objectName: "paraLanguagePicker"
                Layout.fillWidth: true
                // Compact: a note-only row (the engine takes no language)
                // yields its line to the editor; a real choice stays.
                visible: !bar.compact || takesLanguage
            }

            // The batch run speaks with the tab's chip voice (one shared
            // voice for the whole run — per-file voices are a non-goal).
            Connections {
                target: bar.picker

                function onEffectiveVoiceChanged() {
                    if (bar.batchAvailable)
                        batchController.renderVoice = bar.picker.effectiveVoice;
                }
            }

            // Seed the run's voice as soon as the catalog resolves: the batch
            // controller's own fallback is the VieNeu-scoped default voice,
            // which a Qwen profile could not serve.
            Component.onCompleted: {
                if (bar.batchAvailable)
                    batchController.renderVoice = bar.picker.effectiveVoice;
            }
        }
    }
}
