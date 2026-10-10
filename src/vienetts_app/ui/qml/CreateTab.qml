// Tạo giọng đọc (ui_shell_redesign FR-3.2): the one synthesis destination.
//
//   header     title · mode switch (Soạn thảo | Tài liệu | Nhiều tệp | Phụ đề)
//              · Nhập tệp…   — one row, wrapping to two on narrow windows
//   workspace  the active mode's surface, scrolling in a PageShell:
//                compose   → ComposeEditorCard (editor + emotion-chip toolbar)
//                document  → DocumentEditorCard
//                files     → BatchQueueCard
//                subtitles → SubtitleCard
//              + a right-hand inspector slot (Task 3.4 fills it)
//   dock       ONE shared TransportDock pinned under the workspace, outside
//              the scroll area, so Tạo âm thanh never scrolls away (AC-3).
//              Subtitles hide it (SubtitleCard owns its own transport).
//
// The mode is bridge.createMode, strictly: the switch binds to it and writes
// back through bridge.setCreateMode, so the shell, the legacy tab aliases
// ("text", "paragraph") and this page can never disagree.
//
// First paint (NFR-3): this is the landing page, so it is built eagerly with
// the compose workspace. The document/files/subtitles workspaces live in an
// ASYNCHRONOUS Loader (createModesLoader) that incubates on the first visit
// to one of those modes or in the shell's idle prebuild (`prebuildModes`,
// bound to Main.qml's prebuildTabs) — the cost the former ParagraphTab
// Loader kept off the first frame.
//
// objectNames are the tested contract (tests/smoke/test_ui_tabs.py,
// test_ui_shell.py): createTab, createHeader, createPageHeader,
// createModeSwitch (+ createModeSwitch_<mode> segments), importButton,
// importDialog, errorBanner (+ errorLabel), toastLabel, createWorkspace,
// createModesLoader, createInspectorSlot, createDock, createLanguagePicker,
// createActionHint, longTextNotice, busyLabel, runAllButton,
// batchCancelButton, batchRunSummary, paragraphEscapeShortcut, plus the
// mode cards' own (ComposeEditorCard, DocumentEditorCard, BatchQueueCard,
// SubtitleCard) and the dock's (voicePicker, generateButton, …).
// QML seams tests drive through QMetaObject: setMode(mode), importPath(path),
// handleDroppedUrls(urls), submitForSynthesis().
import QtQuick
import QtQuick.Controls
import QtQuick.Dialogs
import QtQuick.Layouts
import "."
import "components"

Pane {
    id: root

    objectName: "createTab"
    padding: Theme.spacingLg

    background: Rectangle {
        color: Theme.bg
    }

    // Main.qml binds the shell's idle-prebuild flag here so the mode
    // workspaces incubate after the first frame, like the other tabs.
    property bool prebuildModes: false
    // The mode workspaces finished incubating (part of the shell's tabsReady).
    readonly property bool modesReady: modesLoader.ready

    // compose | document | files | subtitles — the shell's state, read-only.
    readonly property string mode: bridge.createMode
    readonly property bool shown: bridge.currentTab === "create"
    readonly property int contentMaxWidth: 960

    property string importError: ""
    // Imported document text that arrived before the document workspace
    // finished incubating; applied on load.
    property string pendingDocumentText: ""

    readonly property var modeModel: [
        { "value": "compose", "label": qsTr("Soạn thảo") },
        { "value": "document", "label": qsTr("Tài liệu") },
        { "value": "files", "label": qsTr("Nhiều tệp") },
        { "value": "subtitles", "label": qsTr("Phụ đề") }
    ]

    // Batch failures (unsupported extension, parse errors) and subtitle
    // failures surface in the same banner as import failures.
    readonly property string batchErrorText: (typeof batchController !== "undefined"
        && batchController !== null) ? (batchController.errorText || "") : ""
    readonly property string subtitleErrorText: (typeof subtitleController !== "undefined"
        && subtitleController !== null) ? (subtitleController.errorText || "") : ""
    readonly property bool batchHasItems: (typeof batchController !== "undefined"
        && batchController !== null) ? batchController.itemCount > 0 : false
    readonly property bool batchAvailable: typeof batchController !== "undefined"
        && batchController !== null

    // The document workspace's editor (null until the Loader is ready).
    readonly property var documentCard: modesLoader.item ? modesLoader.item.documentCard : null
    readonly property string documentText: documentCard ? documentCard.text : ""
    // The text the dock would submit in the current mode.
    readonly property string activeText: mode === "compose" ? composeCard.text
        : (mode === "document" ? documentText : "")

    function setMode(id) {
        bridge.setCreateMode(id);
    }

    // Each mode is a different page: restart at the top so the header and
    // the mode switch stay where the user left them.
    onModeChanged: page.scrollToTop()

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

    // Imports always land in the document editor: a file is a document, and
    // compose is for typed text. From compose, the page follows the import.
    function importPath(path) {
        // Fire-and-forget: the parse runs off the UI thread (multi-second
        // PDFs); text/error arrive on controller.documentImported below.
        importError = "";
        if (root.mode === "compose")
            root.setMode("document");
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
        if (root.batchAvailable && typeof batchController.addFiles === "function") {
            batchController.addFiles(paths);
            root.setMode("files");
        }
    }

    // ONE generate path: the dock's request submits the active editor.
    function submitForSynthesis() {
        if (root.mode !== "compose" && root.mode !== "document")
            return;
        const text = root.activeText;
        if (text.trim() === "" || controller.busy || EngineState.blockerReason !== "")
            return;
        controller.generateStream(text, dock.effectiveVoice);
    }

    function applyDocumentText(text) {
        if (root.documentCard) {
            root.documentCard.text = text;
            root.pendingDocumentText = "";
        } else {
            root.pendingDocumentText = text;
        }
    }

    Connections {
        target: controller

        function onDocumentImported(path, text) {
            if (typeof text === "string" && text !== "") {
                root.applyDocumentText(text);
                return;
            }
            const reason = typeof controller.errorText === "string"
                && controller.errorText !== ""
                ? controller.errorText : qsTr("Không thể nhập tệp");
            root.importError = reason;
        }
    }

    FileDialog {
        id: importDialog

        objectName: "importDialog"
        fileMode: FileDialog.OpenFile
        title: qsTr("Chọn tệp văn bản")
        nameFilters: ["Văn bản (*.txt *.md *.docx *.pdf *.srt)"]
        onAccepted: root.importPath(root.toLocalPath(selectedFile))
    }

    // --- Keyboard shortcuts ----------------------------------------------------
    // Ctrl+Enter (generate), Esc (stop) and Ctrl+E (quick save) belong to the
    // TransportDock while it is shown. The subtitles mode hides the dock, so
    // this page-level Escape covers that mode only: a running cue render is
    // cancelled via the subtitle controller, a regular foreground job still
    // goes to controller.cancel. Mode-gated so it can never overlap the dock's
    // own Escape (an ambiguous window shortcut fires neither).
    Shortcut {
        objectName: "paragraphEscapeShortcut"
        sequence: "Escape"
        enabled: root.shown && root.mode === "subtitles"
            && ((controller.busy && controller.foregroundJobState !== "cancel_requested")
                || (typeof subtitleController !== "undefined"
                    && subtitleController !== null && subtitleController.rendering))
        onActivated: {
            if (typeof subtitleController !== "undefined" && subtitleController !== null
                    && subtitleController.rendering) {
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

        // ── Header: title · mode switch · Nhập tệp… ──────────────────────────
        // One row while everything fits; otherwise the mode switch drops to
        // its own full-width row under the title (never a horizontal
        // overflow). Implicit widths only, so the decision cannot feed back.
        GridLayout {
            id: header

            objectName: "createHeader"
            // Very short windows (≈640×420): the title repeats the nav rail's
            // selection, so it yields its row to the editor, which would
            // otherwise start under the pinned dock.
            readonly property bool showTitle: root.height >= 480
            readonly property bool oneRow: width >= (showTitle ? titleHeader.implicitWidth : 0)
                + modeSwitch.implicitWidth + importButton.implicitWidth
                + columnSpacing * 3 + Theme.spacingXl

            Layout.fillWidth: true
            Layout.maximumWidth: root.contentMaxWidth
            Layout.alignment: Qt.AlignHCenter
            columns: oneRow ? 4 : 2
            columnSpacing: Theme.spacingMd
            rowSpacing: Theme.spacingSm

            PageHeader {
                id: titleHeader

                objectName: "createPageHeader"
                Layout.row: 0
                Layout.column: 0
                Layout.fillWidth: !header.oneRow
                visible: header.showTitle
                title: qsTr("Tạo giọng đọc")
            }

            AppSegmented {
                id: modeSwitch

                objectName: "createModeSwitch"
                Layout.row: header.oneRow ? 0 : 1
                Layout.column: header.oneRow ? 1 : 0
                Layout.columnSpan: header.oneRow ? 1 : 2
                Layout.maximumWidth: header.width
                Layout.fillWidth: !header.oneRow && implicitWidth > header.width
                model: root.modeModel
                currentValue: bridge.createMode
                accessibleLabel: qsTr("Kiểu nội dung")
                onActivated: function (value) {
                    bridge.setCreateMode(value);
                }
            }

            Item {
                Layout.row: 0
                Layout.column: 2
                Layout.fillWidth: true
                visible: header.oneRow
            }

            // One import entry for typed and document text (the file lands in
            // the document editor). The queue and the subtitle card own their
            // own add/import actions, so the header has none in those modes.
            AppButton {
                id: importButton

                objectName: "importButton"
                Layout.row: 0
                Layout.column: header.oneRow ? 3 : 1
                Layout.alignment: Qt.AlignRight | Qt.AlignVCenter
                visible: root.mode === "compose" || root.mode === "document"
                variant: "secondary"
                size: "sm"
                iconKind: "upload"
                text: qsTr("Nhập tệp…")
                enabled: !controller.busy && !controller.importing
                busy: controller.importing === true
                tooltipText: qsTr("Nhập .txt, .md, .docx, .pdf hoặc .srt")
                onClicked: importDialog.open()
            }
        }

        // ── Workspace + inspector slot ───────────────────────────────────────
        RowLayout {
            objectName: "createWorkspaceRow"
            Layout.fillWidth: true
            Layout.fillHeight: true
            Layout.maximumWidth: root.contentMaxWidth
            Layout.alignment: Qt.AlignHCenter
            spacing: Theme.spacingLg

            PageShell {
                id: page

                objectName: "createWorkspace"
                Layout.fillWidth: true
                Layout.fillHeight: true
                maxWidth: root.contentMaxWidth
                stretch: true
                reserveVerticalScrollBar: true
                pageSpacing: Theme.spacingMd

                // Errors first, above the workspace: a refusal must not sit
                // below the fold on smaller windows.
                AppNotice {
                    id: errorBanner

                    objectName: "errorBanner"
                    readonly property bool synthesisError: controller.errorText !== ""
                    readonly property bool exportError: synthesisError
                        && (controller.errorText.indexOf(qsTr("Xuất")) !== -1
                            || controller.errorText.indexOf(qsTr("xuất")) !== -1
                            || controller.errorText.indexOf("export") !== -1
                            || controller.errorText.indexOf("Export") !== -1)

                    Layout.fillWidth: true
                    // A failed synthesis/export is an error; an import, queue
                    // or subtitle refusal asks for attention.
                    tone: !synthesisError && message !== "" ? "warning" : "error"
                    title: !synthesisError ? qsTr("Cần chú ý")
                        : (exportError ? qsTr("Không thể xuất tệp âm thanh")
                                       : qsTr("Không thể tạo âm thanh"))
                    message: controller.errorText || root.batchErrorText
                        || root.subtitleErrorText || root.importError
                    messageObjectName: "errorLabel"
                    visible: message !== ""
                }

                ComposeEditorCard {
                    id: composeCard

                    Layout.fillWidth: true
                    Layout.fillHeight: root.mode === "compose"
                    visible: root.mode === "compose"
                    compact: dock.compact
                }

                Loader {
                    id: modesLoader

                    objectName: "createModesLoader"
                    property bool visited: false
                    readonly property bool ready: status === Loader.Ready

                    Layout.fillWidth: true
                    Layout.fillHeight: root.mode !== "compose"
                    visible: root.mode !== "compose"
                    asynchronous: true
                    active: root.prebuildModes || visited || root.mode !== "compose"
                    onActiveChanged: if (active) visited = true
                    onLoaded: {
                        if (root.pendingDocumentText !== "")
                            root.applyDocumentText(root.pendingDocumentText);
                    }

                    sourceComponent: Component {
                        ColumnLayout {
                            objectName: "createModeWorkspaces"
                            property alias documentCard: documentCard

                            spacing: Theme.spacingMd

                            DocumentEditorCard {
                                id: documentCard

                                Layout.fillWidth: true
                                // The document owns the height the dock leaves.
                                Layout.fillHeight: root.mode === "document"
                                compact: dock.compact
                                visible: root.mode === "document"
                                onFilesDropped: function (urls) {
                                    root.handleDroppedUrls(urls);
                                }
                            }

                            BatchQueueCard {
                                Layout.fillWidth: true
                                // A populated queue owns the page (list + drop
                                // strip pinned under the header); an empty one
                                // stays a compact drop prompt.
                                Layout.fillHeight: root.mode === "files" && root.batchHasItems
                                visible: root.mode === "files"
                            }

                            SubtitleCard {
                                Layout.fillWidth: true
                                visible: root.mode === "subtitles"
                            }

                            Item {
                                Layout.fillHeight: root.mode === "subtitles"
                                    || (root.mode === "files" && !root.batchHasItems)
                            }
                        }
                    }
                }

                // ── Toast: cancel / export confirmations ─────────────────────
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
                                toastLabel.text = controller.lastExportPath.toLowerCase().endsWith(".mp3")
                                    ? qsTr("Đã xuất MP3")
                                    : qsTr("Đã xuất WAV")
                                toastLabel.visible = true
                                toastTimer.restart()
                            }
                        }
                    }
                }
            }

            // Inspector (voice card, recents, per-run settings): Task 3.4.
            // Reserved now so the workspace already lays out beside it.
            Item {
                objectName: "createInspectorSlot"
                Layout.fillHeight: true
                Layout.preferredWidth: 0
                visible: false
            }
        }

        // ── Pinned transport dock (FR-2.3): voice, transport, mode-aware
        // primary action. Subtitles own their controls inside SubtitleCard, so
        // the dock is hidden there rather than showing an empty shell. In
        // files mode the transport groups hide and the queue's run controls
        // take the primary slot (`actions`).
        TransportDock {
            id: dock

            objectName: "createDock"
            readonly property bool editsText: root.mode === "compose" || root.mode === "document"

            Layout.fillWidth: true
            // Aligned with the page's reading column on wide windows.
            Layout.maximumWidth: root.contentMaxWidth
            Layout.alignment: Qt.AlignHCenter
            visible: root.mode !== "subtitles"
            // Short windows: the dock sheds its hint lines so the editor keeps
            // a usable height (640×420 leaves ~330 px for the page).
            compact: root.height < 560
            canGenerate: editsText && root.activeText.trim() !== ""
            editorLength: root.activeText.length
            showGenerate: editsText
            showPlayback: editsText
            showExport: editsText
            showLivePreview: editsText
            actionHintObjectName: "createActionHint"
            onGenerateRequested: root.submitForSynthesis()
            onStudioRequested: {
                const kind = root.mode === "compose" ? "text" : "paragraph";
                if (controller.openInStudio(kind, root.activeText))
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

            // Language row: capability-driven (hidden with its reason when the
            // active engine takes no language argument), so no mode can submit
            // a language the active engine does not accept.
            LanguagePicker {
                objectName: "createLanguagePicker"
                Layout.fillWidth: true
                // Compact: a note-only row (the engine takes no language)
                // yields its line to the editor; a real choice stays.
                visible: !dock.compact || takesLanguage
            }

            // The batch run speaks with the dock's chip voice (one shared
            // voice for the whole run — per-file voices are a non-goal).
            Connections {
                target: dock.picker

                function onEffectiveVoiceChanged() {
                    if (root.batchAvailable)
                        batchController.renderVoice = dock.picker.effectiveVoice;
                }
            }

            // Seed the run's voice as soon as the catalog resolves: the batch
            // controller's own fallback is the VieNeu-scoped default voice,
            // which a Qwen profile could not serve.
            Component.onCompleted: {
                if (root.batchAvailable)
                    batchController.renderVoice = dock.picker.effectiveVoice;
            }
        }
    }
}
