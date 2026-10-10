// Text tab (FR-3.2, FR-4.3, FR-UX-4, FR-2.3): free-text synthesis studio.
// Page header and the editor card (focus glow, live metrics, emotion chips)
// scroll in a PageShell; the shared TransportDock is pinned BELOW it, outside
// the scroll area, so Tạo âm thanh never scrolls away (AC-3). The editor card
// fills the height the dock leaves. The dock owns voice, generate/stop,
// playback, export, Studio, live playback and the Ctrl+Enter / Esc / Ctrl+E
// shortcuts (scoped to its visibility).
//
// objectNames are the tested contract (tests/smoke/test_ui_tabs.py):
// textEditor, textDock, textLanguagePicker, textActionHint, longTextNotice,
// errorLabel, toastLabel, textMetricsLabel, textClearButton, emotionToolbar,
// emotionNote, plus the dock's own (voicePicker, generateButton,
// cancelButton, playButton, exportButton, quickExportButton, saveAsButton,
// studioButton, livePreviewToggle, progressBar, busyLabel, …).
// Pinned copy: "Tạo âm thanh", "Đã hủy", the editor placeholder, and a
// visible "[cười]" hint.
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "."
import "components"

Pane {
    id: root

    objectName: "textTab"
    padding: Theme.spacingLg

    background: Rectangle {
        color: Theme.bg
    }

    // Script-aware metrics come from the controller (core.text_metrics):
    // Chinese/Japanese write without inter-word spaces, so a whitespace split
    // collapses a whole paragraph to one "word" and the duration chip reads
    // ~1s; Korean eojeol need their own speech rate. Controllers without the
    // slot (smoke-test guard scenario) show 0 rather than a wrong number.
    // The chip is DEBOUNCED: one textMetrics() call ~250 ms after typing
    // pauses, never a Python round trip per keystroke (78 ms at 200k chars).
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

    Timer {
        id: metricsDebounce

        objectName: "textMetricsDebounce"
        interval: 250
        onTriggered: root.refreshMetrics()
    }

    function submitForSynthesis() {
        if (textEditor.text.trim() === "" || controller.busy
                || EngineState.blockerReason !== "")
            return;
        controller.generateStream(textEditor.text, dock.effectiveVoice);
    }

    ColumnLayout {
        anchors.fill: parent
        spacing: Theme.spacingMd

        PageShell {
            Layout.fillWidth: true
            Layout.fillHeight: true
            maxWidth: 960
            stretch: true

            // ── Studio Header ───────────────────────────────────────────────
            PageHeader {
                Layout.fillWidth: true
                iconKind: "text"
                title: qsTr("Studio Tổng hợp Văn bản")
                subtitle: EngineState.supportsEmotionTags
                    ? qsTr("Nhập văn bản tiếng Việt hoặc Anh, gắn thẻ biểu cảm và trải nghiệm giọng đọc AI chất lượng cao.")
                    : qsTr("Nhập văn bản rồi tạo âm thanh bằng hồ sơ engine đã chọn.")
            }

            // ── Editor Card ─────────────────────────────────────────────────
            AppCard {
                Layout.fillWidth: true
                Layout.fillHeight: true
                title: qsTr("Nội dung văn bản")
                subtitle: qsTr("Hỗ trợ tiếng Việt đa vùng miền và tiếng Anh xen kẽ")

                headerAction: RowLayout {
                    spacing: Theme.spacingSm

                    // Metric chips
                    Rectangle {
                        radius: Theme.radiusSm
                        color: Theme.surface
                        border.color: Theme.borderSubtle
                        border.width: 1
                        implicitHeight: 24
                        implicitWidth: metricsText.implicitWidth + Theme.spacingMd

                        Label {
                            id: metricsText
                            objectName: "textMetricsLabel"
                            anchors.centerIn: parent
                            text: qsTr("%1 từ · %2 ký tự · ~%3s").arg(root.metricWords).arg(textEditor.length).arg(root.metricSeconds)
                            color: Theme.textMuted
                            font.family: Theme.fontFamily
                            font.pixelSize: Theme.fontSizeXs
                            font.weight: Theme.fontWeightMedium
                        }
                    }

                    // Clear button. Clears through the editor's own edit op (not
                    // `text = ""`), so the removal lands on the TextArea undo
                    // stack and Ctrl+Z restores the text (FR-1.5).
                    AppButton {
                        objectName: "textClearButton"
                        variant: "ghost"
                        size: "sm"
                        text: qsTr("Xóa")
                        tooltipText: qsTr("Xóa văn bản (Ctrl+Z để hoàn tác)")
                        visible: textEditor.text.length > 0
                        onClicked: {
                            textEditor.remove(0, textEditor.length);
                            textEditor.forceActiveFocus();
                        }
                    }
                }

                ColumnLayout {
                    Layout.fillWidth: true
                    Layout.fillHeight: true
                    spacing: Theme.spacingMd

                    // Main Text Editor: takes whatever height the pinned dock
                    // leaves; the page scrolls only below its minimum.
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

                    // Emotion Tag Chips Toolbar — the inline tag vocabulary is
                    // VieNeu's own SDK feature, so the chips appear only for a
                    // profile whose engine reads them (EngineState). A profile
                    // that would speak the brackets literally gets the reason
                    // sentence below instead, never a dead control.
                    ColumnLayout {
                        objectName: "emotionToolbar"
                        Layout.fillWidth: true
                        spacing: Theme.spacingSm
                        visible: EngineState.supportsEmotionTags

                        RowLayout {
                            Layout.fillWidth: true
                            spacing: Theme.spacingSm

                            SectionLabel {
                                text: qsTr("Biểu cảm")
                            }

                            Label {
                                Layout.fillWidth: true
                                text: qsTr("nhấn để chèn tại con trỏ: [cười] [thở dài] [hắng giọng]")
                                color: Theme.textSubtle
                                font.family: Theme.fontFamily
                                font.pixelSize: Theme.fontSizeXs
                                elide: Text.ElideRight
                            }
                        }

                        Flow {
                            Layout.fillWidth: true
                            spacing: Theme.spacingSm

                            EmotionChip {
                                tag: "[cười]"
                                label: qsTr("Cười")
                                onClicked: textEditor.insert(textEditor.cursorPosition, tag + " ")
                            }

                            EmotionChip {
                                tag: "[thở dài]"
                                label: qsTr("Thở dài")
                                onClicked: textEditor.insert(textEditor.cursorPosition, tag + " ")
                            }

                            EmotionChip {
                                tag: "[hắng giọng]"
                                label: qsTr("Hắng giọng")
                                onClicked: textEditor.insert(textEditor.cursorPosition, tag + " ")
                            }
                        }
                    }

                    // Where the chips went, and where expression comes from on
                    // this engine instead ("" for VieNeu, which has the chips).
                    Label {
                        objectName: "emotionNote"
                        Layout.fillWidth: true
                        visible: text !== ""
                        text: EngineState.expressivenessNote
                        color: Theme.textMuted
                        font.family: Theme.fontFamily
                        font.pixelSize: Theme.fontSizeXs
                        wrapMode: Text.Wrap
                        lineHeight: 1.25
                    }
                }
            }

            // ── Error Notice ────────────────────────────────────────────────
            AppNotice {
                objectName: "textErrorNotice"
                Layout.fillWidth: true
                tone: "error"
                title: (controller.errorText.indexOf(qsTr("Xuất")) !== -1
                        || controller.errorText.indexOf(qsTr("xuất")) !== -1
                        || controller.errorText.indexOf("export") !== -1
                        || controller.errorText.indexOf("Export") !== -1)
                    ? qsTr("Không thể xuất tệp âm thanh")
                    : qsTr("Không thể tạo âm thanh")
                message: controller.errorText
                messageObjectName: "errorLabel"
                visible: controller.errorText !== ""
            }

            // ── Toast Notice ────────────────────────────────────────────────
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

        // ── Pinned transport dock (FR-2.3): outside the scroll area ──────
        TransportDock {
            id: dock

            objectName: "textDock"
            Layout.fillWidth: true
            // Aligned with the page's reading column on wide windows.
            Layout.maximumWidth: 960
            Layout.alignment: Qt.AlignHCenter
            // Short windows: the dock sheds its hint lines so the editor
            // keeps a usable height (640×420 leaves ~330 px for the page).
            compact: root.height < 560
            canGenerate: textEditor.text.trim() !== ""
            editorLength: textEditor.length
            actionHintObjectName: "textActionHint"
            onGenerateRequested: root.submitForSynthesis()
            onStudioRequested: {
                if (controller.openInStudio("text", textEditor.text))
                    bridge.setCurrentTab("studio");
            }

            // Language row: capability-driven (hidden with its reason when
            // the active engine takes no language argument).
            LanguagePicker {
                objectName: "textLanguagePicker"
                Layout.fillWidth: true
            }
        }
    }
}
