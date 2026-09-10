import QtQuick
import QtQuick.Controls
import QtQuick.Dialogs
import QtQuick.Layouts
import ".."
import "."

// Single-document editor card (Paragraph studio, "Đoạn văn" mode): import a
// file or paste text, see live length/word/duration metrics, and drop documents
// straight onto the editor.
//
// Split out of ParagraphTab.qml so the tab file stays a composition of modes
// (editor | queue) instead of holding every control inline. The card owns the
// import dialog and the drop target; BOTH hand their raw QUrl to the tab
// (`filePicked` / `filesDropped`) so path normalisation and the one-file →
// editor / many-files → queue routing stay in a single place.
//
// objectNames are the tested contract (tests/smoke/test_ui_tabs.py):
// importButton, importDialog, charCountLabel, srtKeepCheckbox, paragraphEditor.
// Pinned copy: "Nhập tệp…", "%1 ký tự".
AppCard {
    id: root

    objectName: "documentEditorCard"
    title: qsTr("Nội dung tài liệu")
    subtitle: qsTr("Dán văn bản trực tiếp, hoặc kéo thả tệp tài liệu vào khung bên dưới")

    property alias text: paragraphEditor.text
    property bool dragOver: false

    signal filePicked(url url)
    signal filesDropped(var urls)

    // Helper to calculate word count
    function countWords(str) {
        if (!str || str.trim() === "")
            return 0;
        const matches = str.trim().match(/\S+/g);
        return matches ? matches.length : 0;
    }

    // Helper to estimate duration (~150 wpm -> ~2.5 words/sec)
    function estimateDurationMinutes(str) {
        const words = countWords(str);
        if (words === 0)
            return 0;
        return (words / 150).toFixed(1);
    }

    FileDialog {
        id: importDialog

        objectName: "importDialog"
        fileMode: FileDialog.OpenFile
        title: qsTr("Chọn tệp văn bản")
        nameFilters: ["Văn bản (*.txt *.md *.docx *.pdf *.srt)"]
        onAccepted: root.filePicked(selectedFile)
    }

    headerAction: RowLayout {
        spacing: Theme.spacingSm

        AppButton {
            id: importBtn

            objectName: "importButton"
            variant: "secondary"
            size: "sm"
            iconKind: "upload"
            text: qsTr("Nhập tệp…")
            enabled: !controller.busy && !controller.importing
            busy: controller.importing === true
            ToolTip.text: qsTr("Nhập .txt, .md, .docx, .pdf hoặc .srt")
            ToolTip.visible: hovered
            onClicked: importDialog.open()
        }

        // Passive metrics separated from the buttons: the old pill re-used the
        // button surface, so read-only counters looked clickable.
        Rectangle {
            Layout.preferredWidth: 1
            Layout.preferredHeight: 16
            Layout.leftMargin: Theme.spacingXs
            Layout.rightMargin: Theme.spacingXs
            color: Theme.borderSubtle
            visible: paragraphEditor.length > 0
        }

        RowLayout {
            spacing: Theme.spacingXs
            visible: paragraphEditor.length > 0

            Label {
                id: charCountLabel

                objectName: "charCountLabel"
                text: qsTr("%1 ký tự").arg(paragraphEditor.length)
                color: Theme.textMuted
                font.family: Theme.fontFamily
                font.pixelSize: Theme.fontSizeXs
                font.weight: Theme.fontWeightMedium
            }

            Label {
                text: "·"
                color: Theme.textSubtle
                font.family: Theme.fontFamily
                font.pixelSize: Theme.fontSizeXs
            }

            Label {
                text: qsTr("%1 từ (~%2 phút)").arg(root.countWords(paragraphEditor.text)).arg(root.estimateDurationMinutes(paragraphEditor.text))
                color: Theme.textMuted
                font.family: Theme.fontFamily
                font.pixelSize: Theme.fontSizeXs
                font.weight: Theme.fontWeightMedium
            }
        }

        AppButton {
            variant: "ghost"
            size: "sm"
            iconKind: "close"
            text: qsTr("Xóa")
            visible: paragraphEditor.text.length > 0
            ToolTip.text: qsTr("Xóa toàn bộ nội dung")
            ToolTip.visible: hovered
            onClicked: paragraphEditor.text = ""
        }
    }

    ColumnLayout {
        Layout.fillWidth: true
        spacing: Theme.spacingMd

        // Supported formats + the one format-specific option, kept together:
        // "Giữ timecode SRT" only means anything for the .srt chip it follows.
        RowLayout {
            Layout.fillWidth: true
            spacing: Theme.spacingXs

            Label {
                text: qsTr("Hỗ trợ:")
                color: Theme.textSubtle
                font.family: Theme.fontFamily
                font.pixelSize: Theme.fontSizeXs
            }

            StatusBadge { text: ".txt"; status: "neutral" }
            StatusBadge { text: ".md"; status: "neutral" }
            StatusBadge { text: ".docx"; status: "neutral" }
            StatusBadge { text: ".pdf"; status: "neutral" }
            StatusBadge { text: ".srt"; status: "neutral" }

            Rectangle {
                Layout.preferredWidth: 1
                Layout.preferredHeight: 16
                Layout.leftMargin: Theme.spacingXs
                Layout.rightMargin: Theme.spacingXs
                color: Theme.borderSubtle
            }

            AppToggle {
                id: srtKeepCheckbox

                objectName: "srtKeepCheckbox"
                text: qsTr("Giữ timecode SRT")
                checked: controller.srtKeepTimestamps === true
                onToggled: controller.srtKeepTimestamps = checked
                accessibleLabel: qsTr("Giữ timecode SRT")
                ToolTip.text: qsTr("Giữ mốc thời gian khi nhập tệp .srt")
                ToolTip.visible: hovered
            }

            Item { Layout.fillWidth: true }
        }

        // Editor Area (wrapped so the DropArea is not layout-managed)
        Item {
            Layout.fillWidth: true
            Layout.minimumHeight: 150
            Layout.preferredHeight: 180

            DropArea {
                anchors.fill: parent
                onEntered: if (drag.hasUrls) root.dragOver = true
                onExited: root.dragOver = false
                onDropped: if (drop.hasUrls && drop.urls.length > 0) {
                    root.dragOver = false;
                    root.filesDropped(drop.urls);
                }
            }

            ScrollView {
                id: editorScroll

                anchors.fill: parent
                contentWidth: availableWidth

                ScrollBar.vertical: ScrollBar {
                    implicitWidth: 8
                    contentItem: Rectangle { radius: 4; color: Theme.border; opacity: 0.7 }
                }

                TextArea {
                    id: paragraphEditor

                    objectName: "paragraphEditor"
                    placeholderText: qsTr("Dán văn bản dài / nhiều đoạn văn vào đây…")
                    placeholderTextColor: Theme.textSubtle
                    wrapMode: TextArea.Wrap
                    color: Theme.text
                    selectedTextColor: Theme.accentText
                    selectionColor: Theme.accent
                    font.family: Theme.fontFamily
                    font.pixelSize: Theme.fontSizeBase
                    selectByMouse: true
                    leftPadding: Theme.spacingMd
                    rightPadding: Theme.spacingMd
                    topPadding: Theme.spacingMd
                    bottomPadding: Theme.spacingMd
                    background: Rectangle {
                        radius: Theme.radiusMd
                        color: Theme.surface
                        border.width: paragraphEditor.activeFocus ? Theme.focusRingWidth : 1
                        border.color: paragraphEditor.activeFocus ? Theme.accent : Theme.borderSubtle
                        Behavior on border.color { ColorAnimation { duration: Theme.durationFast } }
                    }
                }
            }

            // Explicit drop affordance: the border tint alone was easy to miss.
            Rectangle {
                anchors.fill: parent
                radius: Theme.radiusMd
                visible: root.dragOver
                color: Theme.accentSubtle
                border.width: Theme.focusRingWidth
                border.color: Theme.accent

                Label {
                    anchors.centerIn: parent
                    width: parent.width - Theme.spacingXl
                    horizontalAlignment: Text.AlignHCenter
                    wrapMode: Text.Wrap
                    text: qsTr("Thả tệp để nhập — một tệp mở trong khung soạn thảo, nhiều tệp vào hàng đợi")
                    color: Theme.accent
                    font.family: Theme.fontFamily
                    font.pixelSize: Theme.fontSizeSm
                    font.weight: Theme.fontWeightMedium
                }
            }
        }
    }
}
