// Audiobook studio tab (FR-A7): EPUB shelf, chapter render/cache, continuous
// listening, resume, export. Signal design system; context property
// `audiobook` (AudiobookController) + shared `controller` for the voice
// catalog.
//
// Layout: a scrolling page (shelf + book card) with a PINNED player dock at
// the tab bottom (always visible while a book is open, so transport and
// audio position never scroll away) and a full-tab reader overlay above it
// (FR-A9 transcript; opened via the "Văn bản" toggle or the dock title).
//
// objectNames are the tested contract (tests/smoke/test_ui_tabs.py):
// audiobookTab, addEpubButton, epubDialog, shelfEmptyLabel, bookShelfList,
// audiobookBookCard, renderAllButton, exportAllButton, autoAdvanceToggle,
// voicePicker, chapterList, renderBusyLabel, renderProgressBar,
// renderPercentLabel, renderEtaLabel, renderAllProgressBar,
// renderAllProgressLabel, renderDoneLabel, cancelRenderButton,
// chapterProgressBar, chapterProgressLabel, chapterStopButton, playerDock,
// readerCard (the overlay), readerView, readerParagraph, readerText,
// readerCloseButton, prevChapterButton, playPauseButton, nextChapterButton,
// readerToggleButton, positionLabel, durationLabel, seekSlider,
// audiobookErrorBanner, audiobookErrorLabel.
// Pinned copy: header "Sách nói", a ".epub" mention, "Thêm EPUB…".
import QtQuick
import QtQuick.Controls
import QtQuick.Dialogs
import QtQuick.Layouts
import "."
import "components"

Pane {
    id: root

    objectName: "audiobookTab"
    padding: Theme.spacingLg

    background: Rectangle {
        color: Theme.bg
    }

    property bool dragOver: false
    readonly property bool bookOpen: audiobook.currentBookId !== ""

    // QUrl → local path string (same helper shape as ParagraphTab)
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

    function openEpub(path) {
        if (typeof audiobook.openEpub !== "function")
            return;
        audiobook.openEpub(path);
    }

    // Export-all entry point for exportAllDialog.onAccepted AND the offscreen
    // tests — the URL must go through toLocalPath, never toString()-slicing
    // (Windows drive letters, percent-encoded diacritics).
    function exportAllTo(url) {
        audiobook.exportAllReady(toLocalPath(url));
    }

    // ms → "m:ss" / "h:mm:ss"
    function fmtTime(ms) {
        const total = Math.max(0, Math.floor(ms / 1000));
        const h = Math.floor(total / 3600);
        const m = Math.floor((total % 3600) / 60);
        const s = total % 60;
        const mm = h > 0 ? String(m).padStart(2, "0") : String(m);
        const ss = String(s).padStart(2, "0");
        return h > 0 ? `${h}:${mm}:${ss}` : `${mm}:${ss}`;
    }

    function statusText(s) {
        // Reading controller.language registers every CALLING binding as a
        // dependency on it, so live language switches (retranslate) refresh
        // these function-mediated qsTr strings too — without this read,
        // retranslate() cannot see them.
        controller.language;
        switch (s) {
        case "ready": return qsTr("Sẵn sàng");
        case "rendering": return qsTr("Đang tạo…");
        case "failed": return qsTr("Lỗi");
        default: return qsTr("Chờ");
        }
    }

    function statusKind(s) {
        switch (s) {
        case "ready": return "success";
        case "rendering": return "info";
        case "failed": return "error";
        default: return "neutral";
        }
    }

    // ── Reader (FR-A9) helpers ──────────────────────────────────────────

    function escapeHtml(s) {
        return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
    }

    function accentHex() {
        // Opaque #rrggbb — the rich-text subset rejects #aarrggbb.
        return Qt.rgba(Theme.accent.r, Theme.accent.g, Theme.accent.b, 1).toString();
    }

    // Base paragraph text: escaped once per paragraph. Must not read the
    // active char span — word ticks then only re-parse the active row.
    // Reading controller.language registers this binding for live
    // retranslate (same pattern as statusText).
    function paragraphBaseHtml(p) {
        controller.language;
        return escapeHtml(p.text);
    }

    // Paragraph text with the spoken word bolded/colored. Only the active
    // row's binding calls this, so its span reads don't invalidate the rest.
    function paragraphHtml(p) {
        const a = audiobook.activeCharStart;
        const b = audiobook.activeCharEnd;
        if (!audiobook.syncAvailable || a < 0 || b <= a
                || b <= p.charStart || a >= p.charEnd)
            return escapeHtml(p.text);
        const la = Math.max(a, p.charStart) - p.charStart;
        const lb = Math.min(b, p.charEnd) - p.charStart;
        return escapeHtml(p.text.slice(0, la))
            + "<b><font color=\"" + accentHex() + "\">"
            + escapeHtml(p.text.slice(la, lb)) + "</font></b>"
            + escapeHtml(p.text.slice(lb));
    }

    // Flat voice model for the picker (same format as TextTab/ParagraphTab).
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
        id: epubDialog

        objectName: "epubDialog"
        fileMode: FileDialog.OpenFile
        title: qsTr("Chọn sách EPUB")
        nameFilters: ["Sách EPUB (*.epub)"]
        onAccepted: root.openEpub(root.toLocalPath(epubDialog.selectedFile))
    }

    // Escape cancels an in-flight render first (urgent); otherwise it
    // retreats the reader overlay. Tab-gated so the text/paragraph Escape
    // cancel shortcuts can never be ambiguous with this one.
    Shortcut {
        sequence: "Escape"
        enabled: bridge.currentTab === "audiobook"
            && (audiobook.renderingIndex >= 0 || audiobook.readerOpen)
        context: Qt.WindowShortcut
        onActivated: {
            if (audiobook.renderingIndex >= 0)
                audiobook.cancelRender();
            else
                audiobook.readerOpen = false;
        }
    }

    // Transport keys (this tab only): Space toggles play/pause, ←/→ seek
    // 5 s — mirrors the dock's play button and slider.
    Shortcut {
        sequence: "Space"
        enabled: bridge.currentTab === "audiobook" && audiobook.currentChapterIndex >= 0
        context: Qt.WindowShortcut
        onActivated: {
            if (audiobook.playerState === "playing")
                audiobook.pause();
            else if (audiobook.playerState === "paused")
                audiobook.resume();
            else
                audiobook.playChapter(audiobook.currentChapterIndex);
        }
    }
    Shortcut {
        sequence: "Left"
        enabled: bridge.currentTab === "audiobook" && audiobook.playerState !== "stopped"
        context: Qt.WindowShortcut
        onActivated: audiobook.seek(Math.max(0, audiobook.positionMs - 5000))
    }
    Shortcut {
        sequence: "Right"
        enabled: bridge.currentTab === "audiobook" && audiobook.playerState !== "stopped"
        context: Qt.WindowShortcut
        onActivated: audiobook.seek(audiobook.positionMs + 5000)
    }

    PageShell {
        id: pageShell

        anchors.fill: parent
        // Reserve the pinned dock strip while a book is open so the page's
        // last content never scrolls under the dock unseen.
        anchors.bottomMargin: playerDock.visible
            ? playerDock.height + Theme.spacingLg : 0
        maxWidth: 960

        // ── Studio Header ───────────────────────────────────────────────
        PageHeader {
            Layout.fillWidth: true
            iconKind: "audiobook"
            title: qsTr("Sách nói")
            subtitle: qsTr("Nhập sách EPUB, tạo âm thanh từng chương một lần và nghe liền mạch — ứng dụng ghi nhớ vị trí bạn đang nghe.")
        }

        // ── Shelf Card ──────────────────────────────────────────────────
        AppCard {
            id: shelfCard

            Layout.fillWidth: true
            title: qsTr("Thư viện")
            subtitle: audiobook.books.length > 0
                ? qsTr("%1 sách").arg(audiobook.books.length)
                : qsTr("Kéo thả tệp .epub vào đây, hoặc bấm “Thêm EPUB…”")

            headerAction: AppButton {
                id: addEpubButton

                objectName: "addEpubButton"
                variant: "secondary"
                size: "sm"
                iconKind: "upload"
                text: qsTr("Thêm EPUB…")
                onClicked: epubDialog.open()
            }

            // Wrap the shelf in a plain Item so the DropArea is not a direct
            // layout-managed card child (pattern: DropArea anchoring).
            Item {
                Layout.fillWidth: true
                implicitHeight: shelfColumn.implicitHeight

                DropArea {
                    anchors.fill: parent
                    onEntered: if (drag.hasUrls) root.dragOver = true
                    onExited: root.dragOver = false
                    onDropped: if (drop.hasUrls && drop.urls.length > 0) {
                        root.dragOver = false;
                        root.openEpub(root.toLocalPath(drop.urls[0]));
                    }
                }

                ColumnLayout {
                    id: shelfColumn

                    anchors.fill: parent
                    spacing: Theme.spacingXs

                    Label {
                        id: shelfEmptyLabel

                        objectName: "shelfEmptyLabel"
                        Layout.fillWidth: true
                        visible: audiobook.books.length === 0
                        text: qsTr("Chưa có sách nào. Thêm một tệp .epub để bắt đầu.")
                        color: root.dragOver ? Theme.accent : Theme.textSubtle
                        font.family: Theme.fontFamily
                        font.pixelSize: Theme.fontSizeSm
                        wrapMode: Text.Wrap
                        horizontalAlignment: Text.AlignHCenter
                        topPadding: Theme.spacingLg
                        bottomPadding: Theme.spacingLg
                    }

                    Repeater {
                        id: bookShelfList

                        objectName: "bookShelfList"
                        model: audiobook.books

                        delegate: Rectangle {
                            id: shelfRow

                            objectName: "shelfRow"
                            required property var modelData
                            readonly property bool isActive:
                                audiobook.currentBookId === shelfRow.modelData.id

                            Layout.fillWidth: true
                            implicitHeight: 52
                            radius: Theme.radiusMd
                            color: shelfRow.isActive ? Theme.accentSubtle
                                : (shelfMa.containsMouse ? Theme.surfaceHover : Theme.surface)
                            border.width: shelfRow.isActive ? 1 : 0
                            border.color: Theme.borderFocus

                            MouseArea {
                                id: shelfMa

                                anchors.fill: parent
                                hoverEnabled: true
                                cursorShape: Qt.PointingHandCursor
                                onClicked: audiobook.openBook(shelfRow.modelData.id)
                            }

                            RowLayout {
                                anchors.fill: parent
                                anchors.leftMargin: Theme.spacingMd
                                anchors.rightMargin: Theme.spacingSm
                                spacing: Theme.spacingSm

                                AppIcon {
                                    kind: "audiobook"
                                    iconColor: shelfRow.isActive ? Theme.accent : Theme.textMuted
                                }

                                ColumnLayout {
                                    Layout.fillWidth: true
                                    spacing: 0

                                    Label {
                                        Layout.fillWidth: true
                                        text: shelfRow.modelData.title
                                        color: shelfRow.isActive ? Theme.accent : Theme.text
                                        font.family: Theme.fontFamily
                                        font.pixelSize: Theme.fontSizeBase
                                        font.weight: Theme.fontWeightMedium
                                        elide: Text.ElideRight
                                    }

                                    Label {
                                        Layout.fillWidth: true
                                        text: (shelfRow.modelData.author !== ""
                                            ? shelfRow.modelData.author + " · " : "")
                                            + qsTr("%1 chương").arg(shelfRow.modelData.chapterCount)
                                        color: Theme.textMuted
                                        font.family: Theme.fontFamily
                                        font.pixelSize: Theme.fontSizeXs
                                        elide: Text.ElideRight
                                    }
                                }

                                AppButton {
                                    variant: "quiet"
                                    size: "sm"
                                    iconKind: "close"
                                    accessibleLabel: qsTr("Xóa sách khỏi thư viện")
                                    visible: shelfMa.containsMouse || shelfRow.isActive
                                    onClicked: audiobook.removeBook(shelfRow.modelData.id)
                                    ToolTip.text: qsTr("Xóa sách khỏi thư viện")
                                    ToolTip.visible: hovered
                                }
                            }
                        }
                    }
                }
            }
        }

        // ── Book Card (chapters + actions) ──────────────────────────────
        AppCard {
            id: bookCard

            objectName: "audiobookBookCard"
            Layout.fillWidth: true
            visible: audiobook.currentBookId !== ""
            title: audiobook.currentBookTitle
            subtitle: (audiobook.currentBookAuthor !== ""
                ? audiobook.currentBookAuthor + " · " : "")
                + qsTr("%1 chương").arg(audiobook.chapters.length)

            headerAction: RowLayout {
                spacing: Theme.spacingSm

                AppToggle {
                    id: autoAdvanceToggle

                    objectName: "autoAdvanceToggle"
                    text: qsTr("Tự chuyển chương")
                    checked: audiobook.autoAdvance
                    onToggled: audiobook.autoAdvance = checked
                    accessibleLabel: qsTr("Tự chuyển chương")
                }

                AppButton {
                    id: exportAllButton

                    objectName: "exportAllButton"
                    variant: "secondary"
                    size: "sm"
                    iconKind: "download"
                    text: qsTr("Xuất WAV")
                    enabled: audiobook.chapters.length > 0
                    onClicked: exportAllDialog.open()
                }

                AppButton {
                    id: renderAllButton

                    objectName: "renderAllButton"
                    variant: "primary"
                    size: "sm"
                    iconKind: "wave"
                    text: qsTr("Tạo tất cả")
                    enabled: audiobook.renderingIndex < 0 && !controller.busy
                    onClicked: audiobook.renderAllPending()
                }
            }

            FolderDialog {
                id: exportAllDialog

                objectName: "exportAllDialog"
                title: qsTr("Chọn thư mục xuất các chương")
                // toLocalPath (not toString().substring(7)): strips the
                // Windows drive-letter slash and percent-decodes diacritics.
                onAccepted: exportAllTo(exportAllDialog.selectedFolder)
            }

            ColumnLayout {
                Layout.fillWidth: true
                spacing: Theme.spacingMd

                // Voice picker for render jobs
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

                        objectName: "voicePicker"
                        Layout.fillWidth: true
                        flatModel: root.buildFlatModel(controller.voices)
                        onSelectedVoiceChanged: {
                            if (selectedVoice !== "")
                                audiobook.renderVoice = selectedVoice;
                        }
                        Component.onCompleted: {
                            if (selectedVoice === "")
                                selectedVoice = controller.defaultVoice;
                        }
                    }
                }

                // Render progress + cancel — ABOVE the chapter list so it
                // is visible without scrolling the page (the list itself
                // can push content well past the fold on real books).
                RowLayout {
                    Layout.fillWidth: true
                    spacing: Theme.spacingMd
                    visible: audiobook.renderingIndex >= 0

                    Label {
                        id: renderBusyLabel

                        objectName: "renderBusyLabel"
                        text: qsTr("Đang tạo chương %1…").arg(audiobook.renderingIndex + 1)
                        color: Theme.accent
                        font.family: Theme.fontFamily
                        font.pixelSize: Theme.fontSizeBase
                        font.weight: Theme.fontWeightMedium
                    }

                    Label {
                        id: renderDoneLabel

                        objectName: "renderDoneLabel"
                        // Ready-count overview (render-all friendly); `ready`
                        // mirrors cached-on-disk audio, so replays count too.
                        text: qsTr("%1/%2 đã xong").arg(
                            audiobook.chapters.filter(c => c.ready).length).arg(
                            audiobook.chapters.length)
                        color: Theme.textMuted
                        font.family: Theme.fontFamily
                        font.pixelSize: Theme.fontSizeSm
                        visible: audiobook.chapters.length > 1
                    }

                    ProgressBar {
                        id: renderProgressBar

                        objectName: "renderProgressBar"
                        Layout.fillWidth: true
                        from: 0
                        to: 1
                        value: audiobook.renderProgress
                        // 0% while the model loads / before the first
                        // segment lands — animate so it never looks frozen.
                        indeterminate: audiobook.renderProgress <= 0

                        background: Rectangle {
                            implicitHeight: 6
                            radius: 3
                            color: Theme.surfaceAlt
                        }
                        contentItem: Item {
                            clip: true
                            Rectangle {
                                width: renderProgressBar.visualPosition * parent.width
                                height: parent.height
                                radius: 3
                                color: Theme.accent
                            }
                        }
                    }

                    Label {
                        id: renderPercentLabel

                        objectName: "renderPercentLabel"
                        text: Math.round(audiobook.renderProgress * 100) + "%"
                        color: Theme.textMuted
                        font.family: Theme.fontFamilyMono
                        font.pixelSize: Theme.fontSizeSm
                        Layout.preferredWidth: 44
                        horizontalAlignment: Text.AlignRight
                    }

                    // ETA for the in-flight chapter (FR-A10), from the mean
                    // per-segment render time so far.
                    Label {
                        id: renderEtaLabel

                        objectName: "renderEtaLabel"
                        visible: audiobook.renderEtaMs >= 0
                        text: qsTr("còn ~%1").arg(root.fmtTime(audiobook.renderEtaMs))
                        color: Theme.textMuted
                        font.family: Theme.fontFamily
                        font.pixelSize: Theme.fontSizeSm
                    }

                    AppButton {
                        id: cancelRenderButton

                        objectName: "cancelRenderButton"
                        variant: "danger"
                        size: "sm"
                        iconKind: "close"
                        text: qsTr("Hủy")
                        onClicked: audiobook.cancelRender()
                    }
                }

                // Overall progress of a "Tạo tất cả" run (FR-A10): chapters
                // landed / chapters the run set out to synthesize.
                RowLayout {
                    objectName: "renderAllRow"

                    Layout.fillWidth: true
                    spacing: Theme.spacingMd
                    visible: audiobook.renderAllTotal > 0 && audiobook.renderingIndex >= 0

                    Label {
                        id: renderAllProgressLabel

                        objectName: "renderAllProgressLabel"
                        text: qsTr("Tổng: %1/%2 chương").arg(audiobook.renderAllDone).arg(
                            audiobook.renderAllTotal)
                        color: Theme.textMuted
                        font.family: Theme.fontFamily
                        font.pixelSize: Theme.fontSizeSm
                    }

                    ProgressBar {
                        id: renderAllProgressBar

                        objectName: "renderAllProgressBar"
                        Layout.fillWidth: true
                        from: 0
                        to: 1
                        value: audiobook.renderAllTotal > 0
                            ? audiobook.renderAllDone / audiobook.renderAllTotal : 0

                        background: Rectangle {
                            implicitHeight: 6
                            radius: 3
                            color: Theme.surfaceAlt
                        }
                        contentItem: Item {
                            clip: true
                            Rectangle {
                                width: renderAllProgressBar.visualPosition * parent.width
                                height: parent.height
                                radius: 3
                                color: Theme.accent
                            }
                        }
                    }
                }

                // Chapter list
                ListView {
                    id: chapterList

                    objectName: "chapterList"
                    Layout.fillWidth: true
                    Layout.preferredHeight: Math.min(360, contentHeight)
                    clip: true
                    spacing: 4
                    model: audiobook.chapters

                    ScrollBar.vertical: ScrollBar {
                        implicitWidth: 8
                        contentItem: Rectangle {
                            radius: 4
                            color: Theme.border
                            opacity: 0.7
                        }
                    }

                        delegate: Rectangle {
                            id: chapterRow

                            objectName: "chapterRow"
                            required property var modelData
                            readonly property bool isCurrent: chapterRow.modelData.current
                            readonly property bool isRendering:
                                audiobook.renderingIndex === chapterRow.modelData.index

                            // Tested interaction seam: the row MouseArea and
                            // drivers both funnel through here.
                            function playRow() {
                                audiobook.playChapter(chapterRow.modelData.index);
                            }

                            width: chapterList.width
                        height: chapterCol.implicitHeight + Theme.spacingSm * 2
                        radius: Theme.radiusMd
                        color: chapterRow.isCurrent ? Theme.accentSubtle
                            : (chapterMa.containsMouse ? Theme.surfaceHover : Theme.surface)
                        border.width: chapterRow.isCurrent ? 1 : 0
                        border.color: Theme.borderFocus

                        MouseArea {
                            id: chapterMa

                            anchors.fill: parent
                            hoverEnabled: true
                            cursorShape: Qt.PointingHandCursor
                            onClicked: chapterRow.playRow()
                        }

                        ColumnLayout {
                            id: chapterCol

                            anchors {
                                left: parent.left
                                right: parent.right
                                top: parent.top
                                leftMargin: Theme.spacingMd
                                rightMargin: Theme.spacingSm
                                topMargin: Theme.spacingSm
                            }
                            spacing: 2

                            RowLayout {
                                Layout.fillWidth: true
                                spacing: Theme.spacingSm

                                Label {
                                    text: String(chapterRow.modelData.index + 1).padStart(2, "0")
                                    color: chapterRow.isCurrent ? Theme.accent : Theme.textMuted
                                    font.family: Theme.fontFamilyMono
                                    font.pixelSize: Theme.fontSizeSm
                                    font.weight: Theme.fontWeightHeading
                                }

                                Label {
                                    Layout.fillWidth: true
                                    text: chapterRow.modelData.title
                                    color: chapterRow.isCurrent ? Theme.accent : Theme.text
                                    font.family: Theme.fontFamily
                                    font.pixelSize: Theme.fontSizeBase
                                    font.weight: chapterRow.isCurrent
                                        ? Theme.fontWeightHeading : Theme.fontWeightNormal
                                    elide: Text.ElideRight
                                }

                                Label {
                                    text: qsTr("%1 ký tự").arg(chapterRow.modelData.chars)
                                    color: Theme.textSubtle
                                    font.family: Theme.fontFamily
                                    font.pixelSize: Theme.fontSizeXs
                                    visible: !chapterRow.isCurrent
                                }

                                StatusBadge {
                                    id: chapterStatusBadge

                                    objectName: "chapterStatusBadge"
                                    text: root.statusText(chapterRow.modelData.status)
                                    status: root.statusKind(chapterRow.modelData.status)
                                    dotVisible: false
                                }

                                // Inline per-chapter render button (hidden
                                // once cached — replays never resynthesize).
                                AppButton {
                                    id: chapterRenderButton

                                    objectName: "chapterRenderButton"
                                    variant: "secondary"
                                    size: "sm"
                                    iconKind: "refresh"
                                    accessibleLabel: qsTr("Tạo âm thanh cho chương này")
                                    visible: (chapterRow.modelData.status === "pending"
                                        || chapterRow.modelData.status === "failed")
                                        && !chapterRow.isRendering
                                    enabled: audiobook.renderingIndex < 0 && !controller.busy
                                    onClicked: audiobook.renderChapter(chapterRow.modelData.index)
                                    ToolTip.text: qsTr("Tạo âm thanh cho chương này")
                                    ToolTip.visible: hovered
                                }

                                // While THIS chapter renders, its own button
                                // slot becomes the stop affordance (the row
                                // the user clicked is where they look first).
                                AppButton {
                                    id: chapterStopButton

                                    objectName: "chapterStopButton"
                                    variant: "danger"
                                    size: "sm"
                                    iconKind: "close"
                                    accessibleLabel: qsTr("Dừng tạo âm thanh")
                                    visible: chapterRow.isRendering
                                    onClicked: audiobook.cancelRender()
                                    ToolTip.text: qsTr("Dừng tạo âm thanh")
                                    ToolTip.visible: hovered
                                }
                            }

                            // Live progress inside the rendering chapter's
                            // row — no scrolling needed to find it.
                            RowLayout {
                                Layout.fillWidth: true
                                spacing: Theme.spacingSm
                                visible: chapterRow.isRendering

                                ProgressBar {
                                    id: chapterProgressBar

                                    objectName: "chapterProgressBar"
                                    Layout.fillWidth: true
                                    from: 0
                                    to: 1
                                    value: audiobook.renderProgress
                                    indeterminate: audiobook.renderProgress <= 0

                                    background: Rectangle {
                                        implicitHeight: 4
                                        radius: 2
                                        color: Theme.surfaceAlt
                                    }
                                    contentItem: Item {
                                        clip: true
                                        Rectangle {
                                            width: chapterProgressBar.visualPosition * parent.width
                                            height: parent.height
                                            radius: 2
                                            color: Theme.accent
                                        }
                                    }
                                }

                                Label {
                                    id: chapterProgressLabel

                                    objectName: "chapterProgressLabel"
                                    text: Math.round(audiobook.renderProgress * 100) + "%"
                                    color: Theme.accent
                                    font.family: Theme.fontFamilyMono
                                    font.pixelSize: Theme.fontSizeXs
                                    Layout.preferredWidth: 36
                                    horizontalAlignment: Text.AlignRight
                                }
                            }

                            Label {
                                id: chapterErrorLabel

                                objectName: "chapterErrorLabel"
                                Layout.fillWidth: true
                                visible: chapterRow.modelData.status === "failed"
                                    && chapterRow.modelData.error !== ""
                                text: chapterRow.modelData.error
                                color: Theme.error
                                font.family: Theme.fontFamily
                                font.pixelSize: Theme.fontSizeXs
                                wrapMode: Text.Wrap
                            }
                        }
                    }
                }

                // Keep the rendering chapter in view (also follows a
                // render-all run chapter by chapter). callLater lets the
                // row's inline bar settle its height first.
                Connections {
                    target: audiobook
                    function onRenderingIndexChanged() {
                        if (audiobook.renderingIndex >= 0) {
                            Qt.callLater(chapterList.positionViewAtIndex,
                                         audiobook.renderingIndex, ListView.Contain);
                        }
                    }
                }
            }
        }

        // ── Error Banner ────────────────────────────────────────────────
        AppNotice {
            id: audiobookErrorBanner

            objectName: "audiobookErrorBanner"
            Layout.fillWidth: true
            tone: "warning"
            title: qsTr("Không thể xử lý sách nói")
            message: audiobook.errorText
            messageObjectName: "audiobookErrorLabel"
            visible: audiobook.errorText !== ""
        }
    }

    // ── Reader Overlay (FR-A9): transcript fills the tab above the dock ─
    Rectangle {
        id: readerCard

        objectName: "readerCard"
        readonly property bool shown: audiobook.readerOpen && root.bookOpen
            && audiobook.currentChapterIndex >= 0

        // Opens with a quick fade; retreats instantly (no lingering scrim).
        visible: shown
        opacity: shown ? 1 : 0
        Behavior on opacity { NumberAnimation { duration: Theme.durationFast } }

        anchors {
            top: parent.top
            left: parent.left
            right: parent.right
            bottom: parent.bottom
            bottomMargin: playerDock.visible
                ? playerDock.height + Theme.spacingMd : 0
        }

        radius: Theme.radiusLg
        color: Theme.surfaceCard
        border.width: 1
        border.color: Theme.border

        ColumnLayout {
            anchors.fill: parent
            anchors.margins: Theme.spacingLg
            spacing: Theme.spacingMd

            // Overlay header: what am I reading + retreat affordance.
            RowLayout {
                Layout.fillWidth: true
                spacing: Theme.spacingSm

                Rectangle {
                    width: 32
                    height: 32
                    radius: Theme.radiusMd
                    color: Theme.accentSubtle
                    border.color: Theme.borderFocus
                    border.width: 1

                    AppIcon {
                        anchors.centerIn: parent
                        kind: "paragraph"
                        iconColor: Theme.accent
                    }
                }

                ColumnLayout {
                    Layout.fillWidth: true
                    spacing: 0

                    Label {
                        Layout.fillWidth: true
                        text: audiobook.currentChapterIndex >= 0
                            && audiobook.chapters.length > 0
                            ? audiobook.chapters[Math.min(audiobook.currentChapterIndex,
                                                          audiobook.chapters.length - 1)].title
                            : ""
                        color: Theme.text
                        font.family: Theme.fontFamily
                        font.pixelSize: Theme.fontSizeLg
                        font.weight: Theme.fontWeightHeading
                        elide: Text.ElideRight
                    }

                    Label {
                        Layout.fillWidth: true
                        visible: audiobook.currentBookTitle !== ""
                        text: (audiobook.currentBookAuthor !== ""
                            ? audiobook.currentBookAuthor + " · " : "")
                            + audiobook.currentBookTitle
                        color: Theme.textMuted
                        font.family: Theme.fontFamily
                        font.pixelSize: Theme.fontSizeXs
                        elide: Text.ElideRight
                    }
                }

                AppButton {
                    id: readerCloseButton

                    objectName: "readerCloseButton"
                    variant: "quiet"
                    size: "sm"
                    iconKind: "close"
                    accessibleLabel: qsTr("Đóng vùng đọc văn bản")
                    onClicked: audiobook.readerOpen = false
                    ToolTip.text: qsTr("Đóng vùng đọc văn bản")
                    ToolTip.visible: hovered
                }
            }

            Rectangle {
                Layout.fillWidth: true
                height: 1
                color: Theme.borderSubtle
            }

            // Transcript on a centered reading measure (PageShell-like).
            Item {
                Layout.fillWidth: true
                Layout.fillHeight: true

                ListView {
                    id: readerView

                    objectName: "readerView"
                    anchors {
                        top: parent.top
                        bottom: parent.bottom
                        horizontalCenter: parent.horizontalCenter
                    }
                    width: Math.min(parent.width, 720)
                    clip: true
                    spacing: Theme.spacingXs
                    model: audiobook.paragraphs

                    ScrollBar.vertical: ScrollBar {
                        implicitWidth: 8
                        contentItem: Rectangle {
                            radius: 4
                            color: Theme.border
                            opacity: 0.7
                        }
                    }

                    delegate: Rectangle {
                        id: readerParagraph

                        objectName: "readerParagraph"
                        required property var modelData
                        readonly property bool isActive:
                            audiobook.activeParagraph === readerParagraph.modelData.index

                        width: readerView.width
                        height: readerText.implicitHeight + Theme.spacingSm * 2
                        radius: Theme.radiusMd
                        color: readerParagraph.isActive ? Theme.accentSubtle : "transparent"

                        // Click a paragraph to jump the audio to it (FR-A9).
                        // Tested seam: the MouseArea funnels through here.
                        function seekHere() {
                            audiobook.seekToParagraph(readerParagraph.modelData.index);
                        }

                        MouseArea {
                            anchors.fill: parent
                            cursorShape: Qt.PointingHandCursor
                            enabled: audiobook.syncAvailable
                            onClicked: readerParagraph.seekHere()
                        }

                        Text {
                            id: readerText

                            objectName: "readerText"
                            anchors {
                                fill: parent
                                margins: Theme.spacingSm
                            }
                            textFormat: Text.RichText
                            wrapMode: Text.Wrap
                            // Karaoke split: inactive rows bind the base text
                            // (no span dependency), so word ticks re-parse
                            // only the active delegate.
                            text: readerParagraph.isActive
                                  ? root.paragraphHtml(readerParagraph.modelData)
                                  : root.paragraphBaseHtml(readerParagraph.modelData)
                            color: Theme.text
                            font.family: Theme.fontFamily
                            font.pixelSize: Theme.fontSizeBase
                        }
                    }

                    // Follow playback paragraph by paragraph (never word by
                    // word — the reader keeps the user's scroll position within
                    // a paragraph).
                    Connections {
                        target: audiobook
                        function onActiveParagraphChanged() {
                            if (audiobook.playerState === "playing" && audiobook.activeParagraph >= 0)
                                Qt.callLater(readerView.positionViewAtIndex,
                                             audiobook.activeParagraph, ListView.Contain);
                        }
                    }
                }
            }
        }
    }

    // ── Player Dock: pinned transport, visible while a book is open ────
    Rectangle {
        id: playerDock

        objectName: "playerDock"
        visible: root.bookOpen

        anchors {
            left: parent.left
            right: parent.right
            bottom: parent.bottom
        }
        height: dockCol.implicitHeight + Theme.spacingMd * 2

        radius: Theme.radiusLg
        color: Theme.surfaceCard
        border.width: 1
        border.color: Theme.border

        // Slim render-progress line along the top edge so a running render
        // stays glanceable even with the reader overlay open (FR-A10).
        Rectangle {
            anchors {
                top: parent.top
                topMargin: 3
                horizontalCenter: parent.horizontalCenter
            }
            width: Math.max(0, Math.min(1, audiobook.renderProgress))
                * (parent.width - Theme.radiusLg * 2)
            height: 3
            radius: 1.5
            color: Theme.accent
            visible: audiobook.renderingIndex >= 0
        }

        ColumnLayout {
            id: dockCol

            anchors.fill: parent
            anchors.margins: Theme.spacingMd
            spacing: Theme.spacingSm

            // Chapter waveform overview with a live playhead (click/drag to
            // seek — mirrors the app tabs' PlaybackWaveform on the transport).
            PlaybackWaveform {
                objectName: "chapterWaveform"
                Layout.fillWidth: true
                Layout.preferredHeight: 52
                visible: audiobook.currentChapterIndex >= 0
                    && audiobook.chapterEnvelope.length > 0
                envelope: audiobook.chapterEnvelope
                position: audiobook.durationMs > 0
                    ? audiobook.positionMs / audiobook.durationMs : 0
                active: audiobook.playerState !== "stopped"
                durationMs: audiobook.durationMs
                seekable: audiobook.playerState !== "stopped"
                    && audiobook.durationMs > 0
                onSeekRequested: (fraction) =>
                    audiobook.seek(Math.round(fraction * audiobook.durationMs))
            }

            RowLayout {
                id: dockRow

                Layout.fillWidth: true
                Layout.fillHeight: true
                spacing: Theme.spacingMd

                // Chapter/book identity — click to toggle the reader overlay.
                // Plain Item wrapper: the MouseArea anchors to IT, not to a
                // layout-managed child (anchors inside layouts are undefined).
                Item {
                    id: dockTitle

                    visible: root.width >= 780
                    Layout.preferredWidth: 210
                    implicitHeight: dockTitleCol.implicitHeight

                    ColumnLayout {
                        id: dockTitleCol

                        anchors {
                            left: parent.left
                            right: parent.right
                            top: parent.top
                        }
                        spacing: 0

                        Label {
                            Layout.fillWidth: true
                            text: audiobook.currentChapterIndex >= 0
                                && audiobook.chapters.length > 0
                                ? audiobook.chapters[Math.min(audiobook.currentChapterIndex,
                                                              audiobook.chapters.length - 1)].title
                                : qsTr("Chọn một chương để bắt đầu")
                            color: Theme.text
                            font.family: Theme.fontFamily
                            font.pixelSize: Theme.fontSizeBase
                            font.weight: Theme.fontWeightMedium
                            elide: Text.ElideRight
                        }

                        Label {
                            Layout.fillWidth: true
                            text: audiobook.currentBookTitle
                            color: Theme.textMuted
                            font.family: Theme.fontFamily
                            font.pixelSize: Theme.fontSizeXs
                            elide: Text.ElideRight
                        }
                    }

                    MouseArea {
                        anchors.fill: parent
                        enabled: audiobook.currentChapterIndex >= 0
                        cursorShape: enabled ? Qt.PointingHandCursor : Qt.ArrowCursor
                        onClicked: audiobook.readerOpen = !audiobook.readerOpen
                    }
                }

                AppButton {
                    id: readerToggleButton

                    objectName: "readerToggleButton"
                    variant: audiobook.readerOpen ? "primary" : "secondary"
                    size: "sm"
                    iconKind: "paragraph"
                    text: qsTr("Văn bản")
                    enabled: audiobook.currentChapterIndex >= 0
                    onClicked: audiobook.readerOpen = !audiobook.readerOpen
                    accessibleLabel: qsTr("Xem văn bản chương khi nghe")
                    ToolTip.text: qsTr("Xem văn bản chương khi nghe")
                    ToolTip.visible: hovered
                }

                Item {
                    Layout.fillWidth: true
                }

                AppButton {
                    id: prevChapterButton

                    objectName: "prevChapterButton"
                    variant: "secondary"
                    iconKind: "previous"
                    accessibleLabel: qsTr("Chương trước")
                    enabled: audiobook.currentChapterIndex > 0
                    onClicked: audiobook.prevChapter()
                    ToolTip.text: qsTr("Chương trước")
                    ToolTip.visible: hovered
                }

                AppButton {
                    id: playPauseButton

                    objectName: "playPauseButton"
                    variant: "primary"
                    size: "lg"
                    iconKind: audiobook.playerState === "playing" ? "pause" : "play"
                    accessibleLabel: audiobook.playerState === "playing"
                        ? qsTr("Tạm dừng") : qsTr("Phát")
                    enabled: audiobook.currentChapterIndex >= 0
                    onClicked: {
                        if (audiobook.playerState === "playing")
                            audiobook.pause();
                        else if (audiobook.playerState === "paused")
                            audiobook.resume();
                        else if (audiobook.currentChapterIndex >= 0)
                            audiobook.playChapter(audiobook.currentChapterIndex);
                    }
                }

                AppButton {
                    id: nextChapterButton

                    objectName: "nextChapterButton"
                    variant: "secondary"
                    iconKind: "next"
                    accessibleLabel: qsTr("Chương tiếp theo")
                    enabled: audiobook.currentChapterIndex >= 0
                        && audiobook.currentChapterIndex < audiobook.chapters.length - 1
                    onClicked: audiobook.nextChapter()
                    ToolTip.text: qsTr("Chương tiếp theo")
                    ToolTip.visible: hovered
                }

                Label {
                    id: positionLabel

                    objectName: "positionLabel"
                    text: root.fmtTime(audiobook.positionMs)
                    color: Theme.textMuted
                    font.family: Theme.fontFamilyMono
                    font.pixelSize: Theme.fontSizeSm
                }

                AppSlider {
                    id: seekSlider

                    objectName: "seekSlider"
                    Layout.fillWidth: true
                    Layout.preferredWidth: 220
                    from: 0
                    to: Math.max(1, audiobook.durationMs)
                    value: audiobook.positionMs
                    enabled: audiobook.playerState !== "stopped"
                        && audiobook.durationMs > 0
                    onMoved: audiobook.seek(value)
                    accessibleLabel: qsTr("Vị trí phát")
                }

                Label {
                    id: durationLabel

                    objectName: "durationLabel"
                    text: root.fmtTime(audiobook.durationMs)
                    color: Theme.textMuted
                    font.family: Theme.fontFamilyMono
                    font.pixelSize: Theme.fontSizeSm
                }
                }
            }
    }
}
