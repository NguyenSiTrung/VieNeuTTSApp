import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import ".."
import "."

// Single-document editor card (Tạo giọng đọc › Tài liệu): paste text or drop
// a document onto the editor, see live length/word/duration metrics.
//
// Split out of the former ParagraphTab.qml so CreateTab stays a composition
// of mode workspaces. The import action ("Nhập tệp…") and its dialog live in
// CreateTab's header (one import entry for compose and document modes); the
// drop target here hands its raw QUrls to the host (`filesDropped`) so path
// normalisation and the one-file → editor / many-files → queue routing stay
// in a single place.
//
// objectNames are the tested contract (tests/smoke/test_ui_tabs.py):
// documentEditorCard, charCountLabel, srtKeepCheckbox, paragraphEditor,
// paragraphClearButton.
// Pinned copy: "%1 ký tự".
AppCard {
    id: root

    objectName: "documentEditorCard"
    title: qsTr("Nội dung tài liệu")
    subtitle: qsTr("Dán văn bản trực tiếp, hoặc kéo thả tệp tài liệu vào khung bên dưới")

    property alias text: paragraphEditor.text
    property bool dragOver: false
    // Short windows (host-set): the editor moves above the formats/SRT row so
    // it starts right under the card title instead of below the fold.
    property bool compact: false

    signal filesDropped(var urls)

    // Script-aware metrics come from the controller (core.text_metrics):
    // a whitespace split is only a word count for space-delimited scripts —
    // Chinese/Japanese would collapse a whole paragraph to one "word".
    // Controllers without the slot (smoke-test guard scenario) show 0
    // rather than a wrong number. Debounced like the Text tab chip: one
    // textMetrics() call ~250 ms after typing pauses.
    property int metricWords: 0
    // Estimated spoken duration in minutes (per-script rates; ~150 wpm)
    property string metricMinutes: "0"

    function refreshMetrics() {
        if (typeof controller === "undefined" || !controller
                || typeof controller.textMetrics !== "function") {
            metricWords = 0;
            metricMinutes = "0";
            return;
        }
        const metrics = controller.textMetrics(String(paragraphEditor.text || ""));
        metricWords = metrics.words || 0;
        metricMinutes = metricWords === 0 ? "0"
                : (Math.max(1, metrics.seconds || 0) / 60).toFixed(1);
    }

    Timer {
        id: metricsDebounce

        objectName: "paragraphMetricsDebounce"
        interval: 250
        onTriggered: root.refreshMetrics()
    }

    headerAction: RowLayout {
        spacing: Theme.spacingSm

        // Passive counters (never styled like a button, so they do not read
        // as clickable), then the clear action.
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
                text: qsTr("%1 từ (~%2 phút)").arg(root.metricWords).arg(root.metricMinutes)
                color: Theme.textMuted
                font.family: Theme.fontFamily
                font.pixelSize: Theme.fontSizeXs
                font.weight: Theme.fontWeightMedium
            }
        }

        AppButton {
            objectName: "paragraphClearButton"
            variant: "ghost"
            size: "sm"
            iconKind: "close"
            text: qsTr("Xóa")
            visible: paragraphEditor.text.length > 0
            tooltipText: qsTr("Xóa văn bản (Ctrl+Z để hoàn tác)")
            // Clear through the TextArea edit stack (not `text = ""`, which
            // resets the undo history) so Ctrl+Z restores the document.
            onClicked: {
                paragraphEditor.remove(0, paragraphEditor.length);
                paragraphEditor.forceActiveFocus();
            }
        }
    }

    // One column whose two rows swap order in compact mode (GridLayout so
    // the order is a binding, not a re-parent).
    GridLayout {
        Layout.fillWidth: true
        Layout.fillHeight: true
        columns: 1
        rowSpacing: Theme.spacingMd

        // Supported formats + the one format-specific option, kept together:
        // "Giữ timecode SRT" only means anything for the .srt chip it follows.
        RowLayout {
            Layout.fillWidth: true
            Layout.row: root.compact ? 1 : 0
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

        // Editor Area (wrapped so the DropArea is not layout-managed). It
        // takes the height the host's pinned dock leaves.
        Item {
            Layout.fillWidth: true
            Layout.fillHeight: true
            Layout.row: root.compact ? 0 : 1
            Layout.minimumHeight: 120
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
                    onTextChanged: metricsDebounce.restart()
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
