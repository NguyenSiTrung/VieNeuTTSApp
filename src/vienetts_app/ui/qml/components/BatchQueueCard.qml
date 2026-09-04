// Multi-file batch queue (bead qef): file rows with live status, sequential
// auto-run footer, per-item play/reveal. Reads the `batchController` context
// property; coexists with the single-file editor card above it.
//
// objectNames are the tested contract (tests/smoke/test_ui_tabs.py):
// batchQueueCard, batchImportDialog, addFilesButton, runAllButton,
// batchCancelButton, clearFinishedButton, batchFileList, batchEmptyHint,
// batchRunSummary.
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
    subtitle: qsTr("Chọn nhiều tệp để chạy tự động theo lượt — mỗi tệp lưu thành một WAV riêng.")

    // True when a batchController context property exists at all.
    readonly property bool available: typeof batchController !== "undefined"
                                      && batchController !== null
    readonly property var model: available ? batchController.items : []

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
            onClicked: batchImportDialog.open()
        }

        AppButton {
            objectName: "clearFinishedButton"
            variant: "ghost"
            size: "sm"
            text: qsTr("Xóa đã xong")
            enabled: root.available && batchController.items.length > 0
                     && !batchController.running
            onClicked: batchController.clearFinished()
        }
    }

    ColumnLayout {
        Layout.fillWidth: true
        spacing: Theme.spacingMd

        ListView {
            id: fileList

            objectName: "batchFileList"
            Layout.fillWidth: true
            implicitHeight: Math.min(contentHeight, 280)
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

        Label {
            id: batchEmptyHint

            objectName: "batchEmptyHint"
            Layout.fillWidth: true
            visible: root.model.length === 0
            text: qsTr("Chưa có tệp nào — dùng \"Thêm tệp…\" hoặc kéo thả nhiều tệp vào khung văn bản.")
            color: Theme.textSubtle
            font.family: Theme.fontFamily
            font.pixelSize: Theme.fontSizeXs
            wrapMode: Text.Wrap
        }

        RowLayout {
            Layout.fillWidth: true
            spacing: Theme.spacingSm

            AppButton {
                id: runAllBtn

                objectName: "runAllButton"
                variant: "primary"
                size: "lg"
                iconKind: "wave"
                text: qsTr("Tạo tất cả")
                enabled: root.available && batchController.hasPending
                         && !batchController.running
                onClicked: batchController.runAll()
            }

            AppButton {
                objectName: "batchCancelButton"
                variant: "danger"
                size: "sm"
                text: qsTr("Hủy")
                visible: root.available && batchController.running
                onClicked: batchController.cancel()
            }

            Item { Layout.fillWidth: true }

            Label {
                objectName: "batchRunSummary"
                visible: root.available && batchController.runAllTotal > 0
                text: qsTr("%1/%2 tệp").arg(batchController.runAllDone)
                    .arg(batchController.runAllTotal)
                color: Theme.textMuted
                font.family: Theme.fontFamily
                font.pixelSize: Theme.fontSizeSm
            }
        }
    }
}
