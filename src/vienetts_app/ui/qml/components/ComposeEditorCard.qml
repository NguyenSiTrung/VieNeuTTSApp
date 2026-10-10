import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import ".."
import "."

// Compose editor card (Tạo giọng đọc › Soạn thảo, ui_shell_redesign FR-3.2):
// ONE toolbar row on top of the editor (design: Proposed-Create) —
//   Biểu cảm  + cười  + thở dài  + hắng giọng · · · 27 từ · 140 ký tự · ~11s  Xóa
// then the free-text editor, which takes whatever height the host's pinned
// dock leaves. The emotion chips are VieNeu's own SDK vocabulary: a profile
// whose engine would speak the brackets literally gets the reason sentence
// (`emotionNote`) in the chips' place instead of a dead control.
//
// Split out of the former TextTab.qml so CreateTab stays a composition of
// mode workspaces. The host owns submission (it holds the dock and the
// voice); this card owns the text, its metrics and the clear/undo path.
//
// objectNames are the tested contract (tests/smoke/test_ui_tabs.py):
// composeEditorCard, composeToolbar, emotionToolbar, emotionNote,
// textEditor, textMetricsLabel, textMetricsDebounce, textClearButton,
// composeHintLabel.
AppCard {
    id: root

    objectName: "composeEditorCard"

    property alias text: textEditor.text
    readonly property int length: textEditor.length
    readonly property alias editor: textEditor
    // Short windows (host-set): the footer hint line yields to the editor.
    property bool compact: false

    // Script-aware metrics come from the controller (core.text_metrics):
    // Chinese/Japanese write without inter-word spaces, so a whitespace split
    // collapses a whole paragraph to one "word" and the duration reads ~1s;
    // Korean eojeol need their own speech rate. Controllers without the slot
    // (smoke-test guard scenario) show 0 rather than a wrong number.
    // DEBOUNCED: one textMetrics() call ~250 ms after typing pauses, never a
    // Python round trip per keystroke (78 ms at 200k chars).
    property int metricWords: 0
    property int metricSeconds: 0

    function refreshMetrics() {
        if (typeof controller === "undefined" || !controller
                || typeof controller.textMetrics !== "function") {
            metricWords = 0;
            metricSeconds = 0;
            return;
        }
        const metrics = controller.textMetrics(String(textEditor.text || ""));
        metricWords = metrics.words || 0;
        // Estimated spoken seconds (per-script rates; space languages ~150 wpm)
        metricSeconds = metricWords === 0 ? 0 : Math.max(1, metrics.seconds || 0);
    }

    function insertTag(tag) {
        textEditor.insert(textEditor.cursorPosition, tag + " ");
        textEditor.forceActiveFocus();
    }

    Timer {
        id: metricsDebounce

        objectName: "textMetricsDebounce"
        interval: 250
        onTriggered: root.refreshMetrics()
    }

    ColumnLayout {
        Layout.fillWidth: true
        Layout.fillHeight: true
        spacing: Theme.spacingMd

        // ── Toolbar: emotion chips (or the reason they are absent), then
        // the passive counters and the clear action on the right. ─────────
        RowLayout {
            objectName: "composeToolbar"
            Layout.fillWidth: true
            spacing: Theme.spacingSm

            RowLayout {
                objectName: "emotionToolbar"
                Layout.fillWidth: true
                // Let the chips wrap instead of pushing the counters out.
                Layout.minimumWidth: 0
                spacing: Theme.spacingSm
                visible: EngineState.supportsEmotionTags

                SectionLabel {
                    Layout.alignment: Qt.AlignVCenter
                    text: qsTr("Biểu cảm")
                }

                Flow {
                    Layout.fillWidth: true
                    Layout.minimumWidth: 0
                    spacing: Theme.spacingSm

                    EmotionChip {
                        tag: "[cười]"
                        label: qsTr("Cười")
                        onClicked: root.insertTag(tag)
                    }

                    EmotionChip {
                        tag: "[thở dài]"
                        label: qsTr("Thở dài")
                        onClicked: root.insertTag(tag)
                    }

                    EmotionChip {
                        tag: "[hắng giọng]"
                        label: qsTr("Hắng giọng")
                        onClicked: root.insertTag(tag)
                    }
                }
            }

            // Where the chips went, and where expression comes from on this
            // engine instead ("" for VieNeu, which has the chips).
            Label {
                objectName: "emotionNote"
                Layout.fillWidth: true
                Layout.minimumWidth: 0
                visible: text !== ""
                text: EngineState.expressivenessNote
                color: Theme.textMuted
                font.family: Theme.fontFamily
                font.pixelSize: Theme.fontSizeXs
                wrapMode: Text.Wrap
                lineHeight: 1.25
            }

            Label {
                objectName: "textMetricsLabel"
                Layout.alignment: Qt.AlignVCenter
                text: qsTr("%1 từ · %2 ký tự · ~%3s").arg(root.metricWords).arg(textEditor.length).arg(root.metricSeconds)
                color: Theme.textMuted
                font.family: Theme.fontFamily
                font.pixelSize: Theme.fontSizeXs
                font.weight: Theme.fontWeightMedium
            }

            // Clears through the editor's own edit op (not `text = ""`), so
            // the removal lands on the TextArea undo stack and Ctrl+Z
            // restores the text (FR-1.5).
            AppButton {
                objectName: "textClearButton"
                Layout.alignment: Qt.AlignVCenter
                variant: "ghost"
                size: "sm"
                iconKind: "close"
                text: qsTr("Xóa")
                tooltipText: qsTr("Xóa văn bản (Ctrl+Z để hoàn tác)")
                visible: textEditor.text.length > 0
                onClicked: {
                    textEditor.remove(0, textEditor.length);
                    textEditor.forceActiveFocus();
                }
            }
        }

        // ── Editor: takes whatever height the pinned dock leaves; the page
        // scrolls only below its minimum. ───────────────────────────────────
        ScrollView {
            Layout.fillWidth: true
            Layout.fillHeight: true
            Layout.minimumHeight: 120
            Layout.preferredHeight: 200

            ScrollBar.vertical: ScrollBar {
                implicitWidth: 8
                contentItem: Rectangle { radius: 4; color: Theme.border; opacity: 0.7 }
            }

            TextArea {
                id: textEditor

                objectName: "textEditor"
                onTextChanged: metricsDebounce.restart()
                placeholderText: qsTr("Nhập hoặc dán văn bản tiếng Việt / English…")
                placeholderTextColor: Theme.textSubtle
                wrapMode: TextArea.Wrap
                color: Theme.text
                selectedTextColor: Theme.accentText
                selectionColor: Theme.accent
                font.family: Theme.fontFamily
                font.pixelSize: Theme.fontSizeMd
                selectByMouse: true
                leftPadding: Theme.spacingMd
                rightPadding: Theme.spacingMd
                topPadding: Theme.spacingMd
                bottomPadding: Theme.spacingMd
                background: Rectangle {
                    radius: Theme.radiusMd
                    color: Theme.surface
                    border.width: textEditor.activeFocus ? Theme.focusRingWidth : 1
                    border.color: textEditor.activeFocus ? Theme.accent : Theme.borderSubtle
                    Behavior on border.color { ColorAnimation { duration: Theme.durationFast } }
                }
            }
        }

        // Footer hint (design): what the editor accepts. VieNeu reads mixed
        // Vietnamese/English; other profiles state their languages in the
        // dock's language row instead.
        Label {
            objectName: "composeHintLabel"
            Layout.fillWidth: true
            visible: !root.compact && EngineState.supportsEmotionTags
            text: qsTr("Tiếng Việt và tiếng Anh có thể xen kẽ")
            color: Theme.textSubtle
            font.family: Theme.fontFamily
            font.pixelSize: Theme.fontSizeXs
            elide: Text.ElideRight
        }
    }
}
