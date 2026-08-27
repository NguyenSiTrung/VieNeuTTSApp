// Voice cloning tab (FR-3.4): consent gate → reference-clip selection with
// 3–8 s guidance, optional denoise preview, clone enrollment and management
// of the cloned catalog group. All state flows through the `controller` /
// `playback` context properties registered by app.py.
//
// objectNames are the tested contract (tests/smoke/test_ui_tabs.py):
// consentPanel, consentAcceptButton, clonePanel, clipPathLabel,
// clipBrowseButton, clipDialog, denoiseCheck, denoiseButton,
// previewPlayButton, voiceNameField, cloneButton, clonedVoiceList,
// clonedVoiceName, cloneRemoveButton, cloneBusyLabel, progressBar, errorLabel.
//
// The FileDialog is authored but deliberately NOT exercised offscreen (native
// dialogs are unreliable headless — same policy as TextTab/ParagraphTab);
// selectClip(path), the dialog's onAccepted entry point, is the tested seam.
// The group label "Đã sao chép" mirrors CLONED_GROUP in ui/controller.py —
// QML cannot import Python constants; keep the two in sync.
import QtQuick
import QtQuick.Controls
import QtQuick.Dialogs
import QtQuick.Layouts
import "."

Pane {
    id: root

    objectName: "cloningTab"
    padding: Theme.spacingLg

    background: Rectangle {
        color: Theme.surface
    }

    // Selected reference clip (local filesystem path; "" until chosen).
    property string clipPath: ""

    // QUrl → local path string (FileDialog gives a url; controller slots
    // take filesystem paths). %XX escapes decoded; non-file schemes pass
    // through untouched. Same helper idiom as ParagraphTab/TextTab.
    function toLocalPath(url) {
        const s = url.toString();
        return s.startsWith("file://") ? decodeURIComponent(s.substring(7)) : s;
    }

    // Shared entry point for clipDialog.onAccepted AND the offscreen tests,
    // which invoke it via QMetaObject on the "cloningTab" item (native open
    // dialogs are unreliable headless).
    function selectClip(path) {
        clipPath = path;
    }

    // Voices of the cloned catalog group — the "Đã sao chép" entry of
    // controller.voices (label mirrors CLONED_GROUP; see header comment).
    function clonedVoices(groups) {
        for (let i = 0; i < groups.length; i++) {
            if (groups[i].label === "Đã sao chép")
                return groups[i].voices;
        }
        return [];
    }

    FileDialog {
        id: clipDialog

        objectName: "clipDialog"
        fileMode: FileDialog.OpenFile
        title: qsTr("Chọn tệp âm thanh tham chiếu")
        // mp3/wav per FR-3.4; no duration decoding this phase — the guidance
        // label asks the user for a 3–8 s speech-only clip instead.
        nameFilters: ["Âm thanh (*.wav *.mp3)"]
        onAccepted: root.selectClip(root.toLocalPath(clipDialog.selectedFile))
    }

    ColumnLayout {
        anchors.fill: parent
        spacing: Theme.spacingMd

        Label {
            text: qsTr("Sao chép giọng nói")
            color: Theme.text
            font.family: Theme.fontFamily
            font.pixelSize: Theme.fontSizeXl
            font.weight: Theme.fontWeightHeading
        }

        Label {
            Layout.fillWidth: true
            text: qsTr("Tạo giọng đọc tùy chỉnh từ một đoạn âm thanh tham chiếu ngắn.")
            color: Theme.textMuted
            font.family: Theme.fontFamily
            font.pixelSize: Theme.fontSizeBase
            wrapMode: Text.Wrap
        }

        // ── Consent gate (FR-3.6) ────────────────────────────────────────────
        // The cloning panel is unreachable until the user acknowledges; the
        // controller persists the acknowledgment (cloning_consent.json) so
        // this panel never reappears on later runs.
        ColumnLayout {
            id: consentPanel

            objectName: "consentPanel"
            visible: !controller.consentGiven
            Layout.fillWidth: true
            spacing: Theme.spacingMd

            Label {
                Layout.fillWidth: true
                text: qsTr("Bạn xác nhận có quyền sử dụng giọng nói trong tệp tham chiếu này và đồng ý với việc tạo bản sao giọng nói cục bộ.")
                color: Theme.text
                font.family: Theme.fontFamily
                font.pixelSize: Theme.fontSizeBase
                wrapMode: Text.Wrap
            }

            Button {
                objectName: "consentAcceptButton"
                text: qsTr("Tôi đồng ý")
                onClicked: controller.acknowledgeConsent()
            }
        }

        // ── Main cloning panel ───────────────────────────────────────────────
        ColumnLayout {
            id: clonePanel

            objectName: "clonePanel"
            visible: controller.consentGiven
            Layout.fillWidth: true
            spacing: Theme.spacingMd

            Label {
                text: qsTr("Tệp tham chiếu")
                color: Theme.text
                font.family: Theme.fontFamily
                font.pixelSize: Theme.fontSizeBase
            }

            RowLayout {
                Layout.fillWidth: true
                spacing: Theme.spacingSm

                Label {
                    id: clipPathLabel

                    objectName: "clipPathLabel"
                    Layout.fillWidth: true
                    // Binding (not imperative assignment): clipPath is the
                    // single source of truth, the label just renders it.
                    text: root.clipPath === "" ? qsTr("Chưa chọn tệp") : root.clipPath
                    elide: Text.ElideMiddle
                    color: root.clipPath === "" ? Theme.textMuted : Theme.text
                    font.family: Theme.fontFamily
                    font.pixelSize: Theme.fontSizeBase
                }

                Button {
                    objectName: "clipBrowseButton"
                    text: qsTr("Chọn tệp…")
                    enabled: !controller.busy
                    onClicked: clipDialog.open()
                }
            }

            Label {
                Layout.fillWidth: true
                // No duration decoding this phase — guidance only.
                text: qsTr("Chọn đoạn âm 3–8 giây, chỉ có tiếng nói, ít nhiễu.")
                color: Theme.textMuted
                font.family: Theme.fontFamily
                font.pixelSize: Theme.fontSizeSm
                wrapMode: Text.Wrap
            }

            CheckBox {
                id: denoiseCheck

                objectName: "denoiseCheck"
                text: qsTr("Khử nhiễu trước khi sao chép")
                checked: true
            }

            RowLayout {
                Layout.fillWidth: true
                spacing: Theme.spacingSm

                Button {
                    objectName: "denoiseButton"
                    text: qsTr("Nghe bản khử nhiễu")
                    enabled: root.clipPath !== "" && !controller.busy
                    onClicked: controller.denoisePreview(root.clipPath)
                }

                // The denoise preview is written at 44.1 kHz (the denoise
                // sample rate) while synthesis audio is 48 kHz — QMediaPlayer
                // resolves the rate transparently, so play() needs no hint.
                Button {
                    objectName: "previewPlayButton"
                    text: qsTr("Phát thử")
                    visible: controller.previewPath !== ""
                    enabled: !controller.busy
                    onClicked: playback.play(controller.previewPath)
                }
            }

            TextField {
                id: voiceNameField

                objectName: "voiceNameField"
                Layout.fillWidth: true
                placeholderText: qsTr("Tên giọng mới (vd: Giọng đọc truyện)")
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

            Button {
                objectName: "cloneButton"
                text: qsTr("Tạo giọng nói")
                enabled: root.clipPath !== "" && voiceNameField.text.trim() !== ""
                          && !controller.busy
                onClicked: controller.addVoice(voiceNameField.text.trim(), root.clipPath, denoiseCheck.checked)
            }

            // ── Cloned voices (catalog group "Đã sao chép") ─────────────────
            Label {
                text: qsTr("Giọng đã sao chép")
                visible: cloneList.rows.length > 0
                color: Theme.text
                font.family: Theme.fontFamily
                font.pixelSize: Theme.fontSizeBase
            }

            Column {
                id: cloneList

                objectName: "clonedVoiceList"
                Layout.fillWidth: true
                spacing: Theme.spacingSm
                visible: rows.length > 0

                readonly property var rows: root.clonedVoices(controller.voices)

                Repeater {
                    model: cloneList.rows

                    RowLayout {
                        id: cloneRow

                        required property var modelData
                        width: parent.width
                        spacing: Theme.spacingSm

                        Label {
                            objectName: "clonedVoiceName"
                            Layout.fillWidth: true
                            text: cloneRow.modelData ? cloneRow.modelData.label : ""
                            elide: Text.ElideMiddle
                            color: Theme.text
                            font.family: Theme.fontFamily
                            font.pixelSize: Theme.fontSizeBase
                        }

                        Button {
                            objectName: "cloneRemoveButton"
                            text: qsTr("Xóa")
                            flat: true
                            onClicked: controller.removeVoice(cloneRow.modelData.id)
                        }
                    }
                }
            }

            // ── Busy / error contracts (same as other tabs) ─────────────────
            RowLayout {
                Layout.fillWidth: true
                spacing: Theme.spacingMd

                Label {
                    objectName: "cloneBusyLabel"
                    text: qsTr("Đang xử lý…")
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
            }

            Label {
                objectName: "errorLabel"
                Layout.fillWidth: true
                visible: controller.errorText !== ""
                text: controller.errorText
                color: Theme.error
                font.family: Theme.fontFamily
                font.pixelSize: Theme.fontSizeBase
                wrapMode: Text.Wrap
            }
        }
    }
}
