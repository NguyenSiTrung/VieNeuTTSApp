// Paragraph/File tab (FR-3.3, FR-4.4): long-text synthesis — paste multi-
// paragraph text directly OR import a .txt/.md/.docx/.pdf document, then run
// the same generate → progress/cancel → play/export flow as the Text tab.
//
// Streaming (FR-4.4): the Generate button submits through
// controller.generateStream so long documents play as chunks arrive; the
// segment-counted progress keeps the bar live, the shared WaveformIndicator
// rolls while controller.streamActive is up, and cancel stops both synthesis
// and playback. On done the retained audio keeps replay/export working
// exactly as before.
//
// objectNames are the tested contract (tests/smoke/test_ui_tabs.py). The
// shared names (voicePicker, generateButton, waveformIndicator, progressBar,
// cancelButton, errorLabel inside errorBanner, playButton, exportButton)
// intentionally MATCH TextTab's — the driver scopes lookups to this tab's
// subtree (root objectName "paragraphTab"), since StackLayout instantiates
// both tabs at once.
//
// Import seam: importDialog.onAccepted funnels into root.importPath(path),
// which delegates to controller.importDocument(path) and expects the
// extracted text back; failures surface in the visible errorBanner notice —
// an oversized document shows the controller's limit message verbatim
// (FR-4.6b: refuse with an actionable warning, never truncate).
import QtQuick
import QtQuick.Controls
import QtQuick.Dialogs
import QtQuick.Layouts
import "."

Pane {
    id: root

    objectName: "paragraphTab"
    padding: Theme.spacingLg

    background: Rectangle {
        color: Theme.surface
    }

    // Import failure message (missing controller slot, unreadable/empty or
    // oversized document). Kept locally — QML cannot write
    // controller.errorText — and errorBanner/errorLabel show whichever of the
    // two is set, controller-provided reason first.
    property string importError: ""

    // QUrl → local path string for controller.importDocument (FileDialog
    // gives a url; the importer takes a filesystem path). %XX escapes
    // decoded; non-file schemes pass through untouched.
    function toLocalPath(url) {
        const s = url.toString();
        return s.startsWith("file://") ? decodeURIComponent(s.substring(7)) : s;
    }

    // Shared entry point for importDialog.onAccepted AND the offscreen
    // tests, which invoke it via QMetaObject.invokeMethod on the
    // "paragraphTab" item (native open dialogs are unreliable headless —
    // same policy as TextTab's export dialog). Never throws: failures land
    // in importError. On refusal the CONTROLLER's specific reason wins over
    // the generic fallback — e.g. an oversized import shows the exact
    // IMPORT_CHAR_LIMIT message (FR-4.6b).
    function importPath(path) {
        importError = "";
        if (typeof controller.importDocument !== "function") {
            importError = qsTr("Không thể nhập tệp");
            return;
        }
        const text = controller.importDocument(path);
        if (typeof text !== "string" || text === "") {
            const reason = typeof controller.errorText === "string"
                ? controller.errorText : "";
            importError = reason !== "" ? reason : qsTr("Không thể nhập tệp");
            return;
        }
        paragraphEditor.text = text;
    }

    // Flat picker model from controller.voices — identical idiom to TextTab:
    // group headers carry id "" (non-selectable, guarded in the generate
    // handler), inner voices are prefixed "— ".
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
        id: importDialog

        objectName: "importDialog"
        fileMode: FileDialog.OpenFile
        title: qsTr("Chọn tệp văn bản")
        // Hardcoded mirror of SUPPORTED_EXTENSIONS in
        // core/importers.py (.txt .md .docx .pdf) — QML cannot import
        // Python constants; keep the two lists in sync.
        nameFilters: ["Văn bản (*.txt *.md *.docx *.pdf)"]
        onAccepted: root.importPath(root.toLocalPath(importDialog.selectedFile))
    }

    ColumnLayout {
        anchors.fill: parent
        spacing: Theme.spacingMd

        Label {
            text: qsTr("Đoạn văn / Tệp")
            color: Theme.text
            font.family: Theme.fontFamily
            font.pixelSize: Theme.fontSizeXl
            font.weight: Theme.fontWeightHeading
        }

        Label {
            Layout.fillWidth: true
            text: qsTr(
                "Dán văn bản dài vào bên dưới hoặc nhập tệp .txt / .md / .docx / .pdf. "
                + "Tiến độ và nút Hủy hiển thị trong lúc tổng hợp.")
            color: Theme.textMuted
            font.family: Theme.fontFamily
            font.pixelSize: Theme.fontSizeBase
            wrapMode: Text.Wrap
        }

        RowLayout {
            Layout.fillWidth: true
            spacing: Theme.spacingMd

            Button {
                objectName: "importButton"
                text: qsTr("Nhập tệp…")
                enabled: !controller.busy
                onClicked: importDialog.open()
            }

            Item {
                Layout.fillWidth: true
            }

            Label {
                objectName: "charCountLabel"
                // Live length of the editor (TextArea.length, UTF-16 chars).
                text: qsTr("%1 ký tự").arg(paragraphEditor.length)
                color: Theme.textMuted
                font.family: Theme.fontFamily
                font.pixelSize: Theme.fontSizeSm
            }
        }

        ScrollView {
            Layout.fillWidth: true
            Layout.fillHeight: true
            // This tab exists for LONG text: guarantee a tall canvas even
            // when sibling rows squeeze the layout.
            Layout.minimumHeight: 240

            TextArea {
                id: paragraphEditor

                objectName: "paragraphEditor"
                placeholderText: qsTr("Dán văn bản dài / nhiều đoạn văn vào đây…")
                wrapMode: TextArea.Wrap
                color: Theme.text
                selectedTextColor: Theme.accentText
                selectionColor: Theme.accent
                font.family: Theme.fontFamily
                font.pixelSize: Theme.fontSizeBase
                background: Rectangle {
                    radius: 6
                    color: Theme.surfaceAlt
                    border.width: 1
                    border.color: Theme.border
                }
            }
        }

        Label {
            text: qsTr("Giọng đọc")
            color: Theme.text
            font.family: Theme.fontFamily
            font.pixelSize: Theme.fontSizeBase
        }

        ComboBox {
            id: voicePicker

            objectName: "voicePicker"
            Layout.fillWidth: true
            textRole: "label"

            property var flatModel: root.buildFlatModel(controller.voices)
            property string selectedVoice: ""

            onCurrentIndexChanged: {
                const row = currentIndex >= 0 ? flatModel[currentIndex] : null;
                selectedVoice = row && row.id !== "" ? row.id : "";
            }
            // Keyboard selection of a header row must not change the voice.
            onActivated: function(index) {
                const row = index >= 0 ? flatModel[index] : null;
                if (row && row.id !== "")
                    selectedVoice = row.id;
            }
            Component.onCompleted: {
                const target = controller.defaultVoice;
                for (let i = 0; i < flatModel.length; i++) {
                    if (flatModel[i].id !== "" && flatModel[i].id === target) {
                        currentIndex = i;
                        selectedVoice = target;
                        break;
                    }
                }
            }

            delegate: ItemDelegate {
                id: voiceRow

                required property var modelData
                required property int index

                width: ListView.view.width
                text: voiceRow.modelData ? voiceRow.modelData.label : ""
                // Header rows (id "") are display-only: greyed + unclickable.
                enabled: voiceRow.modelData ? voiceRow.modelData.id !== "" : false
                highlighted: voicePicker.highlightedIndex === voiceRow.index

                contentItem: Label {
                    leftPadding: Theme.spacingMd
                    text: voiceRow.text
                    color: voiceRow.enabled ? Theme.text : Theme.textMuted
                    font.family: Theme.fontFamily
                    font.pixelSize: Theme.fontSizeBase
                    verticalAlignment: Text.AlignVCenter
                }
            }
        }

        RowLayout {
            Layout.fillWidth: true
            spacing: Theme.spacingSm

            Button {
                objectName: "generateButton"
                text: qsTr("Tạo âm thanh")
                enabled: paragraphEditor.text.trim() !== "" && !controller.busy
                visible: !controller.busy
            onClicked: {
                // Header rows carry id "" — fall back to the configured
                // default voice instead of synthesizing with a group label.
                const voice = voicePicker.selectedVoice !== ""
                    ? voicePicker.selectedVoice
                    : controller.defaultVoice;
                controller.generateStream(paragraphEditor.text, voice);
            }
            }

            Button {
                objectName: "playButton"
                text: qsTr("Phát")
                // Play needs an exported file: simplest correct UX is
                // "export first, then play". No audio output device →
                // export-only mode (FR-4.6a): playback controls disabled.
                enabled: controller.hasAudio && !controller.busy
                          && controller.lastExportPath !== ""
                          && controller.audioAvailable
                ToolTip.text: qsTr("Xuất WAV trước khi phát")
                ToolTip.visible: hovered && !enabled
                ToolTip.delay: 200
                onClicked: {
                    if (controller.lastExportPath !== "")
                        playback.play(controller.lastExportPath);
                }
            }

            Button {
                objectName: "exportButton"
                // Quick path only by scope decision: exportWav("") writes to
                // the configured default output dir. A full save dialog (the
                // TextTab openExportDialog idiom) is deliberately deferred
                // for this tab.
                text: qsTr("Xuất WAV")
                enabled: controller.hasAudio && !controller.busy
                onClicked: controller.exportWav("")
            }
        }

        // Live rolling envelope (FR-4.5): bars mirror the recent peak
        // amplitudes computed Python-side (no samples reach QML); only the
        // flat baseline would show during the quiet head of a session.
        WaveformIndicator {
            objectName: "waveformIndicator"
            Layout.fillWidth: true
            Layout.preferredHeight: 48
            visible: controller.streamActive
            active: controller.streamActive
            level: controller.streamLevel
        }

        RowLayout {
            Layout.fillWidth: true
            spacing: Theme.spacingMd

            Label {
                objectName: "paraBusyLabel"
                text: qsTr("Đang tổng hợp…")
                visible: controller.busy
                color: Theme.textMuted
                font.family: Theme.fontFamily
                font.pixelSize: Theme.fontSizeBase
            }

            ProgressBar {
                id: progressBar

                objectName: "progressBar"
                Layout.fillWidth: true
                from: 0
                to: 1
                value: controller.progress
                indeterminate: controller.busy && controller.progress === 0
                visible: controller.busy
            }

            Button {
                objectName: "cancelButton"
                text: qsTr("Hủy")
                visible: controller.busy
                onClicked: controller.cancel()
            }
        }

        // Error / import-warning notice (FR-4.6b): a clearly visible banner,
        // not transient feedback. Shows controller.errorText first (e.g. the
        // exact 200 000-character IMPORT_CHAR_LIMIT refusal) and falls back
        // to the local importError (missing-slot guard, no-reason failures).
        // `||` (not a !== "" ternary): a controller without errorText reads
        // as undefined, which must fall through to importError.
        //
        // The inner Label keeps objectName "errorLabel" (the tested contract
        // since phase03); its explicit `visible` binding mirrors the banner
        // because a child's default `visible` property stays TRUE even when
        // its parent is hidden — tests read .property("visible"), not the
        // effective scene-graph visibility.
        Rectangle {
            id: errorBanner

            objectName: "errorBanner"
            Layout.fillWidth: true
            radius: 6
            color: Theme.surfaceAlt
            border.width: 1
            border.color: Theme.warning
            implicitHeight: errorLabel.implicitHeight + Theme.spacingMd * 2
            visible: (controller.errorText || root.importError) !== ""

            // Left accent bar warning tint.
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
                radius: 1
                color: Theme.warning
            }

            Label {
                id: errorLabel

                objectName: "errorLabel"
                visible: errorBanner.visible
                text: controller.errorText || root.importError
                color: Theme.text
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
