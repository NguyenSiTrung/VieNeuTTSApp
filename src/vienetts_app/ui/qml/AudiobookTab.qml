// Audiobook studio tab (FR-A7): EPUB shelf, chapter render/cache, continuous
// listening, resume, export. Signal design system; context property
// `audiobook` (AudiobookController) + shared `controller` for the voice
// catalog.
//
// objectNames are the tested contract (tests/smoke/test_ui_tabs.py):
// audiobookTab, addEpubButton, epubDialog, shelfEmptyLabel, bookShelfList,
// audiobookBookCard, renderAllButton, exportAllButton, autoAdvanceToggle,
// voicePicker, chapterList, renderBusyLabel, renderProgressBar,
// cancelRenderButton, prevChapterButton, playPauseButton, nextChapterButton,
// positionLabel, durationLabel, seekSlider, audiobookErrorBanner,
// audiobookErrorLabel.
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

    // QUrl → local path string (same helper shape as ParagraphTab)
    function toLocalPath(url) {
        const s = url.toString();
        return s.startsWith("file://") ? decodeURIComponent(s.substring(7)) : s;
    }

    function openEpub(path) {
        if (typeof audiobook.openEpub !== "function")
            return;
        audiobook.openEpub(path);
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

    Shortcut {
        sequence: "Escape"
        enabled: audiobook.renderingIndex >= 0
        onActivated: audiobook.cancelRender()
        context: Qt.WindowShortcut
    }

    PageShell {
        anchors.fill: parent
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
                glyph: "↑"
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
                                    variant: "ghost"
                                    size: "sm"
                                    glyph: "✕"
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

                CheckBox {
                    id: autoAdvanceToggle

                    objectName: "autoAdvanceToggle"
                    text: qsTr("Tự chuyển chương")
                    checked: audiobook.autoAdvance
                    onToggled: audiobook.autoAdvance = checked
                    font.family: Theme.fontFamily
                    font.pixelSize: Theme.fontSizeXs
                }

                AppButton {
                    id: exportAllButton

                    objectName: "exportAllButton"
                    variant: "secondary"
                    size: "sm"
                    glyph: "↓"
                    text: qsTr("Xuất WAV")
                    enabled: audiobook.chapters.length > 0
                    onClicked: exportAllDialog.open()
                }

                AppButton {
                    id: renderAllButton

                    objectName: "renderAllButton"
                    variant: "primary"
                    size: "sm"
                    text: qsTr("Tạo tất cả")
                    enabled: audiobook.renderingIndex < 0 && !controller.busy
                    onClicked: audiobook.renderAllPending()
                }
            }

            FolderDialog {
                id: exportAllDialog

                objectName: "exportAllDialog"
                title: qsTr("Chọn thư mục xuất các chương")
                onAccepted: audiobook.exportAllReady(exportAllDialog.selectedFolder.toString().substring(7))
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
                                    glyph: "⟳"
                                    visible: chapterRow.modelData.status === "pending"
                                        || chapterRow.modelData.status === "failed"
                                    enabled: audiobook.renderingIndex < 0 && !controller.busy
                                    onClicked: audiobook.renderChapter(chapterRow.modelData.index)
                                    ToolTip.text: qsTr("Tạo âm thanh cho chương này")
                                    ToolTip.visible: hovered
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

                // Render progress + cancel
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

                    ProgressBar {
                        id: renderProgressBar

                        objectName: "renderProgressBar"
                        Layout.fillWidth: true
                        from: 0
                        to: 1
                        value: audiobook.renderProgress

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

                    AppButton {
                        id: cancelRenderButton

                        objectName: "cancelRenderButton"
                        variant: "danger"
                        size: "sm"
                        text: qsTr("Hủy")
                        onClicked: audiobook.cancelRender()
                    }
                }
            }
        }

        // ── Player Bar Card ─────────────────────────────────────────────
        AppCard {
            Layout.fillWidth: true
            visible: audiobook.currentBookId !== ""

            ColumnLayout {
                Layout.fillWidth: true
                spacing: Theme.spacingMd

                // Chapter title row
                Label {
                    Layout.fillWidth: true
                    text: audiobook.currentChapterIndex >= 0 && audiobook.chapters.length > 0
                        ? audiobook.chapters[Math.min(audiobook.currentChapterIndex,
                                                      audiobook.chapters.length - 1)].title
                        : qsTr("Chọn một chương để bắt đầu")
                    color: Theme.text
                    font.family: Theme.fontFamily
                    font.pixelSize: Theme.fontSizeLg
                    font.weight: Theme.fontWeightHeading
                    elide: Text.ElideRight
                }

                // Transport controls
                RowLayout {
                    Layout.fillWidth: true
                    spacing: Theme.spacingSm

                    AppButton {
                        id: prevChapterButton

                        objectName: "prevChapterButton"
                        variant: "secondary"
                        glyph: "◀◀"
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
                        glyph: audiobook.playerState === "playing" ? "❚❚" : "▶"
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
                        glyph: "▶▶"
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

                    Slider {
                        id: seekSlider

                        objectName: "seekSlider"
                        Layout.fillWidth: true
                        from: 0
                        to: Math.max(1, audiobook.durationMs)
                        value: audiobook.positionMs
                        enabled: audiobook.playerState !== "stopped"
                            && audiobook.durationMs > 0
                        onMoved: audiobook.seek(value)

                        background: Rectangle {
                            x: seekSlider.leftPadding
                            y: seekSlider.topPadding + seekSlider.availableHeight / 2 - height / 2
                            width: seekSlider.availableWidth
                            height: 6
                            radius: 3
                            color: Theme.surfaceAlt

                            Rectangle {
                                width: seekSlider.visualPosition * parent.width
                                height: parent.height
                                radius: 3
                                color: Theme.accent
                            }
                        }

                        handle: Rectangle {
                            x: seekSlider.leftPadding + seekSlider.visualPosition
                                * (seekSlider.availableWidth - width)
                            y: seekSlider.topPadding + seekSlider.availableHeight / 2 - height / 2
                            width: 16
                            height: 16
                            radius: 8
                            color: Theme.accent
                            border.width: 2
                            border.color: Theme.bg
                        }
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

        // ── Error Banner ────────────────────────────────────────────────
        Rectangle {
            id: audiobookErrorBanner

            objectName: "audiobookErrorBanner"
            Layout.fillWidth: true
            radius: Theme.radiusMd
            color: Theme.warningSubtle
            border.width: 1
            border.color: Theme.warning
            implicitHeight: audiobookErrorLabel.implicitHeight + Theme.spacingMd * 2
            visible: audiobook.errorText !== ""

            Rectangle {
                anchors {
                    left: parent.left
                    top: parent.top
                    bottom: parent.bottom
                    leftMargin: Theme.spacingSm
                    topMargin: Theme.spacingSm
                    bottomMargin: Theme.spacingSm
                }
                width: 3
                radius: 1.5
                color: Theme.warning
            }

            Label {
                id: audiobookErrorLabel

                objectName: "audiobookErrorLabel"
                visible: audiobookErrorBanner.visible
                text: audiobook.errorText
                color: Theme.warningText
                font.family: Theme.fontFamily
                font.pixelSize: Theme.fontSizeBase
                wrapMode: Text.Wrap
                anchors {
                    left: parent.left
                    right: parent.right
                    verticalCenter: parent.verticalCenter
                    leftMargin: Theme.spacingLg
                    rightMargin: Theme.spacingMd
                }
            }
        }
    }
}
