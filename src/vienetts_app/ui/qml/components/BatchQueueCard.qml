// Multi-file batch queue (bead qef): file rows with live status, per-item
// play/reveal. Reads the `batchController` context property; lives in the
// Paragraph tab's "Tệp" mode, where it is the whole page.
//
// The run controls ("Tạo tất cả" / "Hủy" / x-of-y) live in the tab's docked
// SynthesisBar so the queue card stays a list and the primary action never
// scrolls; the card's own drop strip is the file-input affordance.
//
// objectNames are the tested contract (tests/smoke/test_ui_tabs.py):
// batchQueueCard, batchImportDialog, addFilesButton, clearFinishedButton,
// batchFileList, batchEmptyHint.
import QtQuick
import QtQuick.Controls
import QtQuick.Dialogs
import QtQuick.Layouts
import ".."
import "."

AppCard {
    id: root

    objectName: "batchQueueCard"
    title: qsTr("Hàng đợi tệp")
    subtitle: qsTr("Chọn nhiều tệp để chạy tự động theo lượt — mỗi tệp lưu thành một tệp âm thanh riêng theo định dạng xuất đã chọn.")

    // True when a batchController context property exists at all.
    readonly property bool available: typeof batchController !== "undefined"
                                      && batchController !== null
    readonly property var model: available ? batchController.items : []
    property bool dragOver: false


    // QUrl → local path (same normalization as ParagraphTab.toLocalPath,
    // untyped return so tests can invoke it through the QVariant seam).
    function toLocalPath(url) {
        const s = url.toString();
        if (!s.startsWith("file://"))
            return s;
        let path = decodeURIComponent(s.substring(7));
        if (/^\/[A-Za-z]:\//.test(path))
            path = path.substring(1);
        return path;
    }

    FileDialog {
        id: batchImportDialog

        objectName: "batchImportDialog"
        fileMode: FileDialog.OpenFiles
        title: qsTr("Chọn một hoặc nhiều tệp văn bản")
        nameFilters: ["Văn bản (*.txt *.md *.docx *.pdf *.srt)"]
        onAccepted: if (root.available)
            batchController.addFiles(selectedFiles.map(u => root.toLocalPath(u)))
    }

    function statusInfo(status) {
        switch (status) {
        case "importing": return { label: qsTr("Đang nhập"), tone: "info" };
        case "pending": return { label: qsTr("Chờ"), tone: "neutral" };
        case "rendering": return { label: qsTr("Đang tạo"), tone: "info" };
        case "saving": return { label: qsTr("Đang lưu"), tone: "info" };
        case "ready": return { label: qsTr("Sẵn sàng"), tone: "success" };
        case "failed": return { label: qsTr("Lỗi"), tone: "error" };
        }
        return { label: status, tone: "neutral" };
    }

    headerAction: RowLayout {
        spacing: Theme.spacingSm

        AppButton {
            id: addFilesBtn

            objectName: "addFilesButton"
            variant: "secondary"
            size: "sm"
            iconKind: "upload"
            text: qsTr("Thêm tệp…")
            enabled: root.available && !batchController.running
            disabledReason: qsTr("Đang chạy — hãy chờ hoặc hủy trước khi thêm tệp.")
            ToolTip.text: qsTr("Chọn một hoặc nhiều tệp để xếp vào hàng đợi")
            ToolTip.visible: hovered
            onClicked: batchImportDialog.open()
        }

        AppButton {
            objectName: "clearFinishedButton"
            variant: "ghost"
            size: "sm"
            text: qsTr("Xóa đã xong")
            enabled: root.available && root.model.length > 0
                     && !batchController.running
            disabledReason: root.model.length === 0
                ? qsTr("Chưa có tệp nào để xóa.")
                : qsTr("Đang chạy — hãy chờ hoặc hủy trước khi xóa.")
            onClicked: batchController.clearFinished()
        }
    }

    ColumnLayout {
        Layout.fillWidth: true
        spacing: Theme.spacingMd

        // Drop strip: the files-mode counterpart of the editor's drop target —
        // one obvious place to put documents, directly under the same header
        // that owns "Thêm tệp…" (the old empty-state hint vanished with the
        // first file, so the drop target did too).
        Rectangle {
            Layout.fillWidth: true
            Layout.preferredHeight: 56
            radius: Theme.radiusMd
            color: root.dragOver ? Theme.accentSubtle : Theme.surfaceAlt
            border.width: root.dragOver ? Theme.focusRingWidth : 1
            border.color: root.dragOver ? Theme.accent : Theme.borderSubtle

            Behavior on color { ColorAnimation { duration: Theme.durationFast } }
            Behavior on border.color { ColorAnimation { duration: Theme.durationFast } }

            DropArea {
                anchors.fill: parent
                onEntered: if (drag.hasUrls) root.dragOver = true
                onExited: root.dragOver = false
                onDropped: if (drop.hasUrls && drop.urls.length > 0) {
                    root.dragOver = false;
                    if (root.available)
                        batchController.addFiles(drop.urls.map(u => root.toLocalPath(u)));
                }
            }

            RowLayout {
                anchors.fill: parent
                anchors.leftMargin: Theme.spacingMd
                anchors.rightMargin: Theme.spacingMd
                spacing: Theme.spacingSm

                AppIcon {
                    Layout.preferredWidth: 16
                    Layout.preferredHeight: 16
                    kind: "upload"
                    iconColor: root.dragOver ? Theme.accent : Theme.textMuted
                }

                Label {
                    id: batchEmptyHint

                    objectName: "batchEmptyHint"
                    Layout.fillWidth: true
                    visible: root.model.length === 0
                    text: qsTr("Chưa có tệp nào — kéo thả tệp vào đây hoặc bấm \"Thêm tệp…\".")
                    color: Theme.textSubtle
                    font.family: Theme.fontFamily
                    font.pixelSize: Theme.fontSizeXs
                    wrapMode: Text.Wrap
                }

                Label {
                    Layout.fillWidth: true
                    visible: root.model.length > 0
                    text: qsTr("Kéo thả thêm tệp vào đây, hoặc bấm \"Thêm tệp…\".")
                    color: Theme.textSubtle
                    font.family: Theme.fontFamily
                    font.pixelSize: Theme.fontSizeXs
                    elide: Text.ElideRight
                }
            }
        }

        ListView {
            id: fileList

            objectName: "batchFileList"
            Layout.fillWidth: true
            implicitHeight: Math.min(contentHeight, 360)
            visible: root.model.length > 0
            spacing: Theme.spacingXs
            clip: true

            model: root.model

            ScrollBar.vertical: ScrollBar {
                policy: ScrollBar.AsNeeded
            }

            delegate: Rectangle {
                width: fileList.width
                height: rowLayout.implicitHeight + Theme.spacingSm * 2
                radius: Theme.radiusMd
                color: Theme.surface
                border.color: Theme.borderSubtle
                border.width: 1

                readonly property var info: root.statusInfo(modelData.status)
                readonly property bool isPlaying: root.available
                    && batchController.playingIndex === index

                ColumnLayout {
                    id: rowLayout
                    anchors.fill: parent
                    anchors.margins: Theme.spacingSm
                    spacing: Theme.spacingXs

                    RowLayout {
                        Layout.fillWidth: true
                        spacing: Theme.spacingSm

                        Label {
                            text: modelData.fileName
                            color: Theme.text
                            font.family: Theme.fontFamily
                            font.pixelSize: Theme.fontSizeSm
                            font.weight: Theme.fontWeightMedium
                            elide: Text.ElideMiddle
                            Layout.fillWidth: true
                        }

                        StatusBadge {
                            text: info.label
                            status: info.tone
                        }

                        AppIconButton {
                            iconKind: isPlaying ? "stop" : "play"
                            size: "sm"
                            accessibleLabel: isPlaying ? qsTr("Dừng") : qsTr("Phát")
                            tooltipText: accessibleLabel
                            enabled: modelData.status === "ready"
                            visible: enabled
                            onClicked: batchController.playItem(index)
                        }

                        AppIconButton {
                            iconKind: "folder"
                            size: "sm"
                            accessibleLabel: qsTr("Mở thư mục")
                            tooltipText: accessibleLabel
                            enabled: modelData.status === "ready" && modelData.wavPath !== ""
                            visible: enabled
                            onClicked: batchController.showInFolder(index)
                        }

                        AppIconButton {
                            iconKind: "close"
                            size: "sm"
                            accessibleLabel: qsTr("Xóa khỏi hàng đợi")
                            tooltipText: accessibleLabel
                            enabled: modelData.status !== "rendering"
                                      && modelData.status !== "saving"
                            onClicked: batchController.removeItem(index)
                        }
                    }

                    ProgressBar {
                        id: rowProgress

                        Layout.fillWidth: true
                        from: 0
                        to: 1
                        visible: modelData.status === "rendering"
                        value: root.available && batchController.currentIndex === index
                            ? batchController.progress : 0
                        background: Rectangle {
                            implicitHeight: 4
                            radius: 2
                            color: Theme.surfaceAlt
                        }
                        contentItem: Rectangle {
                            visible: rowProgress.value > 0
                            width: rowProgress.position * parent.width
                            height: parent.height
                            radius: 2
                            color: Theme.accent
                        }
                    }

                    Label {
                        Layout.fillWidth: true
                        visible: modelData.error !== ""
                        text: modelData.error
                        color: Theme.error
                        font.family: Theme.fontFamily
                        font.pixelSize: Theme.fontSizeXs
                        wrapMode: Text.Wrap
                    }
                }
            }
        }

        // A stretched queue card (populated "Nhiều tệp" mode) absorbs its extra
        // height here, so the rows stay directly under the drop strip.
        Item { Layout.fillHeight: true }
    }
}
