// Audiobook studio tab (FR-A7): EPUB shelf, chapter render/cache, continuous
// listening, resume, export. Signal design system; context property
// `audiobook` (AudiobookController) + shared `controller` for the voice
// catalog.
//
// Layout (FR-4.1 master–detail, Proposed-Audiobook): a fixed page — header,
// then the library column (book tiles + drop target) beside the detail card
// whose chapter list fills the height — with a PINNED player dock at the tab
// bottom (always visible while a book is open). There is no page scroll: the
// shelf, the chapter list and the reader are sibling scrollers. The reader
// (FR-A9 transcript) docks as a third column at ≥1200 px windows; below that
// it is an overlay above the dock behind the "Văn bản" toggle / dock title.
//
// objectNames are the tested contract (tests/smoke/test_ui_tabs.py):
// audiobookTab, addEpubButton, epubDialog, shelfEmptyLabel, bookShelfList,
// audiobookBody, audiobookLibrary, shelfRow, shelfRowSubtitle,
// shelfRowProgress, shelfRemoveButton, shelfDropHint, audiobookBookCount,
// audiobookBookTitle, audiobookBookMeta, audiobookBookActions,
// audiobookLibraryButton, readerSlot,
// audiobookBookCard, renderAllButton, exportAllButton, autoAdvanceToggle,
// voicePicker, chapterList, renderBusyLabel, renderProgressBar,
// renderPercentLabel, renderEtaLabel, renderAllProgressBar,
// renderAllProgressLabel, renderDoneLabel, cancelRenderButton,
// chapterProgressBar, chapterProgressLabel, chapterStopButton, playerDock,
// readerCard (the overlay), readerView, readerParagraph, readerText,
// readerCloseButton, prevChapterButton, playPauseButton, nextChapterButton,
// readerToggleButton, positionLabel, durationLabel, seekSlider,
// audiobookErrorBanner, audiobookErrorLabel, audiobookLanguagePicker.
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

    // Master–detail budget (FR-4.1): the library column sits beside the
    // detail card once the page is wide enough; the reader docks as a third
    // column at ≥1200 px windows and stays behind the "Văn bản" toggle below.
    readonly property int libraryWidth: 248
    readonly property bool splitLayout: availableWidth >= 720
    readonly property bool readerDocked: splitLayout && bookOpen && Window.width >= 1200
    // Narrow page with a book open: the "Thư viện" chip swaps the detail for
    // the shelf; opening (or switching) a book swaps back.
    property bool showLibrary: false

    Connections {
        target: audiobook
        function onCurrentBookIdChanged() {
            root.showLibrary = false;
        }
    }

    // "ST" for "Sách thử nghiệm": the shelf tile's cover initials.
    function initials(title) {
        const words = String(title).trim().split(/\s+/).filter(w => w.length > 0);
        if (words.length === 0)
            return "?";
        const first = words[0].charAt(0);
        const second = words.length > 1 ? words[1].charAt(0) : "";
        return (first + second).toUpperCase();
    }

    // "Tác giả · 3 chương · 1 đã tạo" — the detail header's meta line.
    function bookMeta() {
        controller.language;
        const parts = [];
        if (audiobook.currentBookAuthor !== "")
            parts.push(audiobook.currentBookAuthor);
        parts.push(qsTr("%1 chương").arg(audiobook.chapterCount));
        if (audiobook.readyChapterCount > 0)
            parts.push(qsTr("%1 đã tạo").arg(audiobook.readyChapterCount));
        return parts.join(" · ");
    }

    // QUrl → local path string (same helper shape as CreateTab)
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

    // ── Page: header over the master–detail body (FR-4.1) ────────────────
    // No page scroll: the library column, the chapter list and the docked
    // reader are sibling scrollers, so a wheel never has to hand off from a
    // nested list to the page (the old 360 px chapterList inside PageShell).
    ColumnLayout {
        id: page

        anchors {
            top: parent.top
            left: parent.left
            right: parent.right
            bottom: parent.bottom
            // Reserve the pinned dock strip while a book is open.
            bottomMargin: playerDock.visible ? playerDock.height + Theme.spacingLg : 0
        }
        spacing: Theme.spacingLg

        PageHeader {
            Layout.fillWidth: true
            iconKind: "audiobook"
            title: qsTr("Sách nói")
            trailing: RowLayout {
                spacing: Theme.spacingMd

                Label {
                    objectName: "audiobookBookCount"
                    visible: audiobook.books.length > 0
                    text: qsTr("%1 sách").arg(audiobook.books.length)
                    color: Theme.textMuted
                    font.family: Theme.fontFamily
                    font.pixelSize: Theme.fontSizeSm
                }

                AppButton {
                    id: addEpubButton

                    objectName: "addEpubButton"
                    // The empty shelf's one action is the screen's primary; once a
                    // book exists, rendering it (renderAllButton) takes over.
                    variant: audiobook.books.length === 0 ? "primary" : "secondary"
                    size: "sm"
                    iconKind: "upload"
                    text: qsTr("Thêm EPUB…")
                    onClicked: epubDialog.open()
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

        RowLayout {
            id: body

            objectName: "audiobookBody"
            Layout.fillWidth: true
            Layout.fillHeight: true
            spacing: Theme.spacingLg

            // ── Library column: book tiles + drop target ────────────────
            // Beside the detail card when there is room (splitLayout); on a
            // narrow page an open book shows the detail alone and the
            // "Thư viện" chip brings the shelf back.
            Item {
                id: library

                objectName: "audiobookLibrary"
                readonly property bool asColumn: root.bookOpen && root.splitLayout
                visible: !root.bookOpen || root.splitLayout || root.showLibrary
                Layout.fillHeight: true
                Layout.fillWidth: !library.asColumn
                Layout.preferredWidth: library.asColumn ? root.libraryWidth : -1
                Layout.maximumWidth: library.asColumn ? root.libraryWidth : 640
                Layout.alignment: Qt.AlignTop | Qt.AlignLeft

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
                    anchors.fill: parent
                    spacing: Theme.spacingSm

                    Label {
                        id: shelfEmptyLabel

                        objectName: "shelfEmptyLabel"
                        Layout.fillWidth: true
                        visible: audiobook.books.length === 0
                        text: qsTr("Chưa có sách nào. Thêm một tệp .epub để bắt đầu.")
                        color: Theme.textMuted
                        font.family: Theme.fontFamily
                        font.pixelSize: Theme.fontSizeBase
                        wrapMode: Text.Wrap
                    }

                    // Book tiles. Grows to its content and scrolls only when the
                    // shelf outgrows the column (never nested in another scroller).
                    ListView {
                        id: bookShelfList

                        objectName: "bookShelfList"
                        Layout.fillWidth: true
                        Layout.fillHeight: true
                        Layout.maximumHeight: contentHeight
                        visible: count > 0
                        clip: true
                        spacing: Theme.spacingSm
                        boundsBehavior: Flickable.StopAtBounds
                        model: audiobook.books

                        ScrollBar.vertical: ScrollBar {
                            implicitWidth: 8
                            // Only when the list overflows: a full-height thumb on a list
                            // that fits reads as a stray rule.
                            policy: bookShelfList.contentHeight > bookShelfList.height + 1
                                    ? ScrollBar.AlwaysOn : ScrollBar.AlwaysOff
                            contentItem: Rectangle {
                                radius: 4
                                color: Theme.border
                                opacity: 0.7
                            }
                        }

                        delegate: Rectangle {
                            id: shelfRow

                            objectName: "shelfRow"
                            required property var modelData
                            readonly property bool isActive:
                                audiobook.currentBookId === shelfRow.modelData.id
                            // The book is read off the GUI thread; this row
                            // shows progress until it lands.
                            readonly property bool isLoading:
                                audiobook.loadingBookId === shelfRow.modelData.id

                            width: bookShelfList.width
                            implicitHeight: shelfTileRow.implicitHeight + Theme.spacingMd * 2
                            radius: Theme.radiusMd
                            color: shelfRow.isActive ? Theme.accentSubtle
                                : (shelfMa.containsMouse ? Theme.surfaceHover : Theme.surfaceCard)
                            border.width: 1
                            border.color: shelfRow.isActive ? Theme.borderFocus : Theme.border
                            Accessible.role: Accessible.Button
                            Accessible.name: shelfRow.modelData.title

                            MouseArea {
                                id: shelfMa

                                anchors.fill: parent
                                hoverEnabled: true
                                cursorShape: Qt.PointingHandCursor
                                onClicked: audiobook.openBook(shelfRow.modelData.id)
                            }

                            RowLayout {
                                id: shelfTileRow

                                anchors.left: parent.left
                                anchors.right: parent.right
                                anchors.verticalCenter: parent.verticalCenter
                                anchors.leftMargin: Theme.spacingMd
                                anchors.rightMargin: Theme.spacingXs
                                spacing: Theme.spacingMd

                                // Cover tile: the title's initials.
                                Rectangle {
                                    Layout.preferredWidth: 40
                                    Layout.preferredHeight: 52
                                    Layout.alignment: Qt.AlignTop
                                    radius: Theme.radiusSm
                                    color: shelfRow.isActive ? Theme.accent : Theme.surfaceAlt

                                    Label {
                                        anchors.centerIn: parent
                                        text: root.initials(shelfRow.modelData.title)
                                        color: shelfRow.isActive ? Theme.accentText : Theme.textMuted
                                        font.family: Theme.fontFamily
                                        font.pixelSize: Theme.fontSizeBase
                                        font.weight: Theme.fontWeightHeading
                                    }
                                }

                                ColumnLayout {
                                    Layout.fillWidth: true
                                    spacing: Theme.spacingXxs

                                    Label {
                                        Layout.fillWidth: true
                                        text: shelfRow.modelData.title
                                        color: Theme.text
                                        font.family: Theme.fontFamily
                                        font.pixelSize: Theme.fontSizeBase
                                        font.weight: Theme.fontWeightHeading
                                        elide: Text.ElideRight
                                        maximumLineCount: 2
                                        wrapMode: Text.Wrap
                                    }

                                    Label {
                                        objectName: "shelfRowSubtitle"
                                        Layout.fillWidth: true
                                        text: shelfRow.isLoading
                                            ? qsTr("Đang mở…")
                                            : (shelfRow.modelData.author !== ""
                                                ? shelfRow.modelData.author + " · " : "")
                                                + qsTr("%1 chương").arg(shelfRow.modelData.chapterCount)
                                        color: shelfRow.isLoading ? Theme.accent : Theme.textMuted
                                        font.family: Theme.fontFamily
                                        font.pixelSize: Theme.fontSizeXs
                                        elide: Text.ElideRight
                                    }

                                    // Rendered-chapter progress of the open book.
                                    Rectangle {
                                        objectName: "shelfRowProgress"
                                        Layout.fillWidth: true
                                        Layout.topMargin: Theme.spacingXs
                                        visible: shelfRow.isActive && audiobook.chapterCount > 0
                                        implicitHeight: 4
                                        radius: 2
                                        color: Theme.surfaceAlt

                                        Rectangle {
                                            width: parent.width * (audiobook.chapterCount > 0
                                                ? audiobook.readyChapterCount / audiobook.chapterCount : 0)
                                            height: parent.height
                                            radius: 2
                                            color: Theme.accent
                                        }
                                    }
                                }

                                AppIconButton {
                                    objectName: "shelfRemoveButton"
                                    iconKind: "close"
                                    accessibleLabel: qsTr("Xóa sách khỏi thư viện")
                                    tooltipText: qsTr("Xóa sách khỏi thư viện")
                                    visible: shelfMa.containsMouse || hovered || shelfRow.isActive
                                    onClicked: audiobook.removeBook(shelfRow.modelData.id)
                                }
                            }
                        }
                    }

                    // Drop target hint (always offered; lights up while a file
                    // is dragged over the column). On an empty shelf it is the
                    // page's big landing zone: taller, with an upload glyph.
                    Rectangle {
                        id: dropZone

                        readonly property bool empty: audiobook.books.length === 0
                        Layout.fillWidth: true
                        implicitHeight: dropZoneContent.implicitHeight
                                        + (dropZone.empty ? Theme.spacingXl * 3 : Theme.spacingLg * 2)
                        radius: Theme.radiusMd
                        color: root.dragOver ? Theme.accentSubtle
                                             : (dropZone.empty ? Theme.surfaceAlt : "transparent")
                        border.width: 1
                        border.color: root.dragOver ? Theme.borderFocus : Theme.border

                        ColumnLayout {
                            id: dropZoneContent

                            anchors.left: parent.left
                            anchors.right: parent.right
                            anchors.verticalCenter: parent.verticalCenter
                            anchors.margins: Theme.spacingMd
                            spacing: Theme.spacingSm

                            AppIcon {
                                Layout.alignment: Qt.AlignHCenter
                                visible: dropZone.empty
                                width: 32
                                height: 32
                                kind: "upload"
                                iconColor: root.dragOver ? Theme.accent : Theme.textMuted
                            }

                            Label {
                                id: shelfDropHint

                                objectName: "shelfDropHint"
                                Layout.fillWidth: true
                                text: qsTr("Kéo thả tệp .epub vào đây hoặc nhấn “Thêm EPUB…”")
                                color: root.dragOver ? Theme.accent : Theme.textMuted
                                font.family: Theme.fontFamily
                                font.pixelSize: dropZone.empty ? Theme.fontSizeBase : Theme.fontSizeSm
                                wrapMode: Text.Wrap
                                horizontalAlignment: Text.AlignHCenter
                            }
                        }
                    }

                    Item {
                        Layout.fillHeight: true
                    }
                }
            }

            // ── Detail: the open book's chapters ────────────────────────
            AppCard {
                id: bookCard

                objectName: "audiobookBookCard"
                visible: root.bookOpen && (root.splitLayout || !root.showLibrary)
                Layout.fillWidth: true
                Layout.fillHeight: true
                Layout.minimumWidth: 280


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
                    Layout.fillHeight: true
                    spacing: Theme.spacingMd

                    // Book identity: title + "author · N chương · k đã tạo".
                    RowLayout {
                        Layout.fillWidth: true
                        spacing: Theme.spacingSm

                        AppButton {
                            objectName: "audiobookLibraryButton"
                            visible: !root.splitLayout
                            variant: "quiet"
                            size: "sm"
                            iconKind: "chevronLeft"
                            text: qsTr("Thư viện")
                            onClicked: root.showLibrary = true
                        }

                        ColumnLayout {
                            Layout.fillWidth: true
                            spacing: Theme.spacingXxs

                            Label {
                                objectName: "audiobookBookTitle"
                                Layout.fillWidth: true
                                text: audiobook.currentBookTitle
                                color: Theme.text
                                font.family: Theme.fontFamily
                                font.pixelSize: Theme.fontSizeLg
                                font.weight: Theme.fontWeightHeading
                                elide: Text.ElideRight
                            }

                            Label {
                                objectName: "audiobookBookMeta"
                                Layout.fillWidth: true
                                text: root.bookMeta()
                                color: Theme.textMuted
                                font.family: Theme.fontFamily
                                font.pixelSize: Theme.fontSizeXs
                                elide: Text.ElideRight
                            }
                        }
                    }

                    // Voice + book actions; wraps on a narrow detail column.
                    Flow {
                        objectName: "audiobookBookActions"
                        Layout.fillWidth: true
                        spacing: Theme.spacingSm

                        VoicePicker {
                            id: voicePicker

                            objectName: "voicePicker"
                            compact: true
                            onEffectiveVoiceChanged: {
                                if (effectiveVoice !== "")
                                    audiobook.renderVoice = effectiveVoice;
                            }
                            Component.onCompleted: {
                                if (effectiveVoice !== "")
                                    audiobook.renderVoice = effectiveVoice;
                            }
                        }


                        AppButton {
                            id: exportAllButton

                            // Export runs off the GUI thread: progress arrives as
                            // exportProgress(done, total) while `exporting` is true.
                            property int exportDone: 0
                            property int exportTotal: 0

                            objectName: "exportAllButton"
                            variant: "secondary"
                            size: "sm"
                            iconKind: "download"
                            text: audiobook.exporting
                                ? qsTr("Đang xuất %1/%2").arg(exportDone).arg(exportTotal)
                                : qsTr("Xuất âm thanh")
                            enabled: audiobook.chapterCount > 0 && !audiobook.exporting
                            disabledReason: audiobook.exporting ? qsTr("Đang xuất âm thanh — vui lòng đợi.") : ""
                            onClicked: exportAllDialog.open()

                            Connections {
                                target: audiobook

                                function onExportProgress(done, total) {
                                    exportAllButton.exportDone = done;
                                    exportAllButton.exportTotal = total;
                                }
                            }
                        }

                        AppButton {
                            id: studioButton

                            objectName: "studioButton"
                            variant: "secondary"
                            size: "sm"
                            text: qsTr("Mở trong Studio")
                            enabled: root.bookOpen && audiobook.currentChapterReady
                                && !controller.busy
                            disabledReason: qsTr("Cần tạo âm thanh chương trước khi mở Studio.")
                            ToolTip.text: qsTr("Chỉnh sửa âm thanh trước khi xuất")
                            ToolTip.visible: hovered
                            onClicked: {
                                if (controller.openChapterInStudio(audiobook.currentBookId, audiobook.currentChapterIndex))
                                    bridge.setCurrentTab("studio");
                            }
                        }

                        AppButton {
                            id: renderAllButton

                            objectName: "renderAllButton"
                            variant: "primary"
                            size: "sm"
                            iconKind: "wave"
                            text: qsTr("Tạo tất cả")
                            enabled: audiobook.renderingIndex < 0 && !controller.busy
                                     && EngineState.blockerReason === ""
                            disabledReason: EngineState.blockerReason
                            onClicked: audiobook.renderAllPending()
                        }
                    }

                    // Language row: capability-driven, so a render can never be
                    // queued with a language the active engine does not accept.
                    LanguagePicker {
                        objectName: "audiobookLanguagePicker"
                        Layout.fillWidth: true
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
                                audiobook.readyChapterCount).arg(audiobook.chapterCount)
                            color: Theme.textMuted
                            font.family: Theme.fontFamily
                            font.pixelSize: Theme.fontSizeSm
                            visible: audiobook.chapterCount > 1
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
                        // Fills the detail card down to the dock (FR-4.1):
                        // the only scroller in the detail column.
                        Layout.fillHeight: true
                        Layout.minimumHeight: 88
                        boundsBehavior: Flickable.StopAtBounds
                        clip: true
                        spacing: 4
                        // Row-level model: a status update changes one row in
                        // place, so delegates and the scroll position survive.
                        model: audiobook.chapterModel

                        ScrollBar.vertical: ScrollBar {
                            implicitWidth: 8
                            // Only when the list overflows: a full-height thumb on a list
                            // that fits reads as a stray rule.
                            policy: chapterList.contentHeight > chapterList.height + 1
                                    ? ScrollBar.AlwaysOn : ScrollBar.AlwaysOff
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
                                        text: (chapterRow.modelData.segmentsTotal || 1) > 1
                                            ? qsTr("%1 ký tự · %2/%3 đoạn").arg(chapterRow.modelData.chars)
                                                .arg(chapterRow.modelData.segmentsReady || 0)
                                                .arg(chapterRow.modelData.segmentsTotal)
                                            : qsTr("%1 ký tự").arg(chapterRow.modelData.chars)
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
                                        text: qsTr("Tạo")
                                        accessibleLabel: qsTr("Tạo âm thanh cho %1").arg(chapterRow.modelData.title)
                                        visible: (chapterRow.modelData.status === "pending"
                                            || chapterRow.modelData.status === "failed")
                                            && !chapterRow.isRendering
                                        enabled: audiobook.renderingIndex < 0 && !controller.busy
                                            && EngineState.blockerReason === ""
                                        disabledReason: EngineState.blockerReason
                                        onClicked: audiobook.renderChapter(chapterRow.modelData.index)
                                        tooltipText: accessibleLabel
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
                                        accessibleLabel: qsTr("Dừng tạo %1").arg(chapterRow.modelData.title)
                                        tooltipText: accessibleLabel
                                        visible: chapterRow.isRendering
                                        onClicked: audiobook.cancelRender()
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

            // ≥1200 px: the reader panel docks here, beside the chapter list.
            Item {
                id: readerSlot

                objectName: "readerSlot"
                visible: root.readerDocked
                Layout.fillHeight: true
                Layout.preferredWidth: Math.round(Math.max(300, Math.min(440, body.width * 0.32)))
            }
        }
    }

    // ── Reader (FR-A9): docked beside the chapters at ≥1200 px, otherwise an
    // overlay over the page (above the dock) behind the "Văn bản" toggle. ──
    Item {
        id: overlayHost

        anchors {
            top: parent.top
            left: parent.left
            right: parent.right
            bottom: parent.bottom
            bottomMargin: playerDock.visible ? playerDock.height + Theme.spacingMd : 0
        }

        Rectangle {
            id: readerCard

            objectName: "readerCard"
            readonly property bool shown: root.bookOpen && audiobook.currentChapterIndex >= 0
                && (root.readerDocked || audiobook.readerOpen)

            parent: root.readerDocked ? readerSlot : overlayHost
            anchors.fill: parent
            // Opens with a quick fade; retreats instantly (no lingering scrim).
            visible: shown
            opacity: shown ? 1 : 0
            Behavior on opacity { NumberAnimation { duration: Theme.durationFast } }

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
                            text: audiobook.currentChapterTitle
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

                    // Whole-chapter export: a selection can never span the
                    // per-paragraph editors, so the transcript-level copy is a
                    // button, not a drag.
                    AppButton {
                        id: readerCopyButton

                        objectName: "readerCopyButton"
                        variant: "quiet"
                        size: "sm"
                        iconKind: "copy"
                        text: qsTr("Sao chép chương")
                        enabled: audiobook.paragraphs.length > 0
                        onClicked: audiobook.copyChapter()
                        ToolTip.text: qsTr("Sao chép toàn bộ văn bản chương")
                        ToolTip.visible: hovered
                    }

                    AppButton {
                        id: readerCloseButton

                        objectName: "readerCloseButton"
                        variant: "quiet"
                        size: "sm"
                        iconKind: "close"
                        accessibleLabel: qsTr("Đóng vùng đọc văn bản")
                        // Docked beside the chapters there is nothing to close.
                        visible: !root.readerDocked
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
                            policy: readerView.contentHeight > readerView.height + 1
                                    ? ScrollBar.AlwaysOn : ScrollBar.AlwaysOff
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
                            // Tested seam: seekHere stays the funnel for tests.
                            function seekHere() {
                                audiobook.seekToParagraph(readerParagraph.modelData.index);
                            }

                            // Selectable + copyable, never editable: a read-only
                            // TextEdit instead of a Text. TapHandler (inside the
                            // editor — a MouseArea on top would eat selection
                            // drags) keeps click-to-seek alive: handlers don't
                            // steal the press, so clean taps seek while drags
                            // select.
                            TextEdit {
                                id: readerText

                                objectName: "readerText"
                                anchors {
                                    fill: parent
                                    margins: Theme.spacingSm
                                }
                                textFormat: TextEdit.RichText
                                wrapMode: TextEdit.Wrap
                                // Karaoke split: inactive rows bind the base text
                                // (no span dependency), so word ticks re-parse
                                // only the active delegate.
                                text: readerParagraph.isActive
                                      ? root.paragraphHtml(readerParagraph.modelData)
                                      : root.paragraphBaseHtml(readerParagraph.modelData)
                                color: Theme.text
                                font.family: Theme.fontFamily
                                font.pixelSize: Theme.fontSizeBase

                                readOnly: true
                                selectByMouse: true
                                // Keyboard selection would let the focused editor
                                // claim ←/→/Space via shortcut overrides and
                                // shadow the tab's transport shortcuts.
                                selectByKeyboard: false

                                TapHandler {
                                    enabled: audiobook.syncAvailable
                                    onTapped: readerParagraph.seekHere()
                                }
                                HoverHandler {
                                    enabled: audiobook.syncAvailable
                                    cursorShape: Qt.PointingHandCursor
                                }
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
    }


    // ── Player Dock: pinned transport, visible while a book is open ────
    // TransportDock skin (FR-2.4): the same pinned card (radiusDock,
    // surfaceCard, hairline border) but its own content. TransportDock is a
    // synthesis dock bound to `controller` (voice chip, generate/stop,
    // export); this one is chapter playback through `audiobook`, so it is
    // built from the same pieces instead of switching TransportDock's groups
    // off. Layout follows Proposed-Audiobook:
    //   wide    [waveform ...........................................]
    //           identity · ⏮ ▶ ⏭ · 0:01 ━━━━ 0:04 · Tự chuyển chương · Văn bản
    //   narrow  identity ................ Tự chuyển chương · Văn bản
    //           [waveform ...........................................]
    //           ⏮ ▶ ⏭ · 0:01 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ 0:04
    // Short windows (compact) drop the waveform row; the slider still seeks.
    Rectangle {
        id: playerDock

        objectName: "playerDock"
        visible: root.bookOpen
        Accessible.role: Accessible.ToolBar
        Accessible.name: qsTr("Trình phát")

        // One row once the transport + options fit: identity 160 + transport
        // (~350 with the slider at its minimum) + options (~290) + gaps — the
        // 1120×740 window (dock ~856 px) qualifies.
        readonly property bool wide: width >= 820
        readonly property bool compact: root.height < 560

        anchors {
            left: parent.left
            right: parent.right
            bottom: parent.bottom
        }
        height: dockGrid.implicitHeight + Theme.spacingMd * 2

        radius: Theme.radiusDock
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
                * (parent.width - Theme.radiusDock * 2)
            height: 3
            radius: 1.5
            color: Theme.accent
            visible: audiobook.renderingIndex >= 0
        }

        // One grid whose cells move with `wide` (bindings, not re-parenting,
        // so every control stays a single instance).
        GridLayout {
            id: dockGrid

            anchors.fill: parent
            anchors.leftMargin: Theme.spacingLg
            anchors.rightMargin: Theme.spacingLg
            anchors.topMargin: Theme.spacingMd
            anchors.bottomMargin: Theme.spacingMd
            columns: 3
            rowSpacing: Theme.spacingSm
            columnSpacing: Theme.spacingLg

            // Chapter/book identity — click to toggle the reader overlay.
            // Plain Item wrapper: the MouseArea anchors to IT, not to a
            // layout-managed child (anchors inside layouts are undefined).
            Item {
                id: dockTitle

                Layout.row: playerDock.wide ? 1 : 0
                Layout.column: 0
                Layout.fillWidth: !playerDock.wide
                Layout.preferredWidth: playerDock.wide ? 160 : -1
                Layout.minimumWidth: 120
                Layout.alignment: Qt.AlignVCenter
                implicitHeight: dockTitleCol.implicitHeight

                ColumnLayout {
                    id: dockTitleCol

                    anchors {
                        left: parent.left
                        right: parent.right
                        verticalCenter: parent.verticalCenter
                    }
                    spacing: 0

                    Label {
                        Layout.fillWidth: true
                        text: audiobook.currentChapterIndex >= 0
                            && audiobook.chapterCount > 0
                            ? audiobook.currentChapterTitle
                            : qsTr("Chọn một chương để bắt đầu")
                        color: Theme.text
                        font.family: Theme.fontFamily
                        font.pixelSize: Theme.fontSizeBase
                        font.weight: Theme.fontWeightHeading
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

            // Chapter waveform overview with a live playhead (click/drag to
            // seek — mirrors the app tabs' PlaybackWaveform).
            PlaybackWaveform {
                objectName: "chapterWaveform"
                Layout.row: playerDock.wide ? 0 : 1
                Layout.column: 0
                Layout.columnSpan: 3
                Layout.fillWidth: true
                Layout.preferredHeight: 44
                visible: !playerDock.compact
                    && audiobook.currentChapterIndex >= 0
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

            // Transport: 44 px icon buttons around a larger round play, then
            // the timecodes around the seek slider.
            RowLayout {
                id: dockRow

                objectName: "playerTransportRow"
                Layout.row: 2 - (playerDock.wide ? 1 : 0)
                Layout.column: playerDock.wide ? 1 : 0
                Layout.columnSpan: playerDock.wide ? 1 : 3
                Layout.fillWidth: true
                spacing: Theme.spacingMd

                RowLayout {
                    spacing: Theme.spacingXs

                    AppIconButton {
                        id: prevChapterButton

                        objectName: "prevChapterButton"
                        iconKind: "previous"
                        accessibleLabel: qsTr("Chương trước")
                        tooltipText: qsTr("Chương trước")
                        enabled: audiobook.currentChapterIndex > 0
                        onClicked: audiobook.prevChapter()
                    }

                    // Larger than its neighbours but NOT primary (Tạo tất cả
                    // is this screen's one primary): a 52 px round control
                    // that turns accent-tinted while a chapter plays.
                    AppButton {
                        id: playPauseButton

                        objectName: "playPauseButton"
                        variant: "chip"
                        text: ""
                        implicitWidth: 52
                        implicitHeight: 52
                        checked: audiobook.playerState === "playing"
                        iconKind: audiobook.playerState === "playing" ? "pause" : "play"
                        accessibleLabel: audiobook.playerState === "playing"
                            ? qsTr("Tạm dừng") : qsTr("Phát")
                        tooltipText: accessibleLabel
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

                    AppIconButton {
                        id: nextChapterButton

                        objectName: "nextChapterButton"
                        iconKind: "next"
                        accessibleLabel: qsTr("Chương tiếp theo")
                        tooltipText: qsTr("Chương tiếp theo")
                        enabled: audiobook.currentChapterIndex >= 0
                            && audiobook.currentChapterIndex < audiobook.chapterCount - 1
                        onClicked: audiobook.nextChapter()
                    }
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
                    Layout.minimumWidth: 80
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

            // Chapter options. "Tự chuyển chương" is a playback option, so it
            // lives in the player (it used to sit in the book card header).
            // Strict binding + write-back: the controller flag is the truth.
            RowLayout {
                id: dockOptions

                Layout.row: playerDock.wide ? 1 : 0
                Layout.column: playerDock.wide ? 2 : 1
                Layout.columnSpan: playerDock.wide ? 1 : 2
                Layout.alignment: Qt.AlignRight | Qt.AlignVCenter
                spacing: Theme.spacingMd

                AppToggle {
                    id: autoAdvanceToggle

                    objectName: "autoAdvanceToggle"
                    text: qsTr("Tự chuyển chương")
                    checked: audiobook.autoAdvance
                    onToggled: audiobook.autoAdvance = checked
                    accessibleLabel: qsTr("Tự chuyển chương")
                }

                AppButton {
                    id: readerToggleButton

                    objectName: "readerToggleButton"
                    variant: "secondary"
                    checked: audiobook.readerOpen
                    size: "sm"
                    iconKind: "paragraph"
                    text: qsTr("Văn bản")
                    // ≥1200 px the reader is always beside the chapter list.
                    visible: !root.readerDocked
                    enabled: audiobook.currentChapterIndex >= 0
                    onClicked: audiobook.readerOpen = !audiobook.readerOpen
                    accessibleLabel: qsTr("Xem văn bản chương khi nghe")
                    tooltipText: qsTr("Xem văn bản chương khi nghe")
                }
            }
        }
    }
}
