// Voice cloning tab (FR-3.4, FR-UX-6): consent gate → reference-clip selection with
// 3–8 s guidance, optional denoise preview, clone enrollment and management
// of the cloned catalog group. All state flows through the `controller` /
// `playback` context properties registered by app.py.
//
// objectNames are the tested contract (tests/smoke/test_ui_tabs.py):
// consentPanel, consentAcceptButton, consentText, clonePanel, clipPathLabel,
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
import "components"
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

    ScrollView {
        anchors.fill: parent
        contentWidth: availableWidth
        clip: true

        ColumnLayout {
            width: Math.min(840, root.availableWidth - Theme.spacingLg * 2)
            anchors.horizontalCenter: parent.horizontalCenter
            spacing: Theme.spacingLg

            // Studio Header
            RowLayout {
                Layout.fillWidth: true
                spacing: Theme.spacingMd

                Rectangle {
                    width: 42
                    height: 42
                    radius: Theme.radiusMd
                    color: Theme.accentSubtle
                    border.color: Theme.border
                    border.width: 1

                    Canvas {
                        anchors.centerIn: parent
                        width: 20
                        height: 20
                        renderTarget: Canvas.FramebufferObject
                        Component.onCompleted: requestPaint()
                        onPaint: {
                            const ctx = getContext("2d");
                            ctx.clearRect(0, 0, width, height);
                            ctx.strokeStyle = Theme.accent;
                            ctx.lineWidth = 1.5;
                            ctx.lineCap = "round";
                            ctx.beginPath();
                            ctx.moveTo(4, 8); ctx.lineTo(4, 12);
                            ctx.moveTo(8, 4); ctx.lineTo(8, 16);
                            ctx.moveTo(12, 2); ctx.lineTo(12, 18);
                            ctx.moveTo(16, 6); ctx.lineTo(16, 14);
                            ctx.stroke();
                        }
                    }
                }

                ColumnLayout {
                    Layout.fillWidth: true
                    spacing: 2

                    Label {
                        text: qsTr("Sao chép giọng nói")
                        color: Theme.text
                        font.family: Theme.fontFamily
                        font.pixelSize: Theme.fontSizeXl
                        font.weight: Theme.fontWeightHeading
                    }

                    Label {
                        Layout.fillWidth: true
                        text: qsTr("Tạo giọng đọc tùy chỉnh từ một đoạn âm thanh mẫu 3–8 giây, 100% riêng tư trên thiết bị.")
                        color: Theme.textMuted
                        font.family: Theme.fontFamily
                        font.pixelSize: Theme.fontSizeSm
                        wrapMode: Text.Wrap
                    }
                }
            }

            // ── Consent Gate (FR-3.6 / FR-4.7) ────────────────────────────────
            // The cloning panel is unreachable until the user acknowledges; the
            // controller persists the acknowledgment (cloning_consent.json) so
            // this panel never reappears on later runs.
            ColumnLayout {
                id: consentPanel

                objectName: "consentPanel"
                visible: !controller.consentGiven
                Layout.fillWidth: true
                spacing: Theme.spacingMd

                AppCard {
                    Layout.fillWidth: true
                    title: qsTr("Cam kết bản quyền & Trách nhiệm sử dụng")
                    badgeText: qsTr("Bảo mật & Quyền riêng tư")
                    badgeColor: Theme.accentSubtle
                    badgeTextColor: Theme.accent

                    ColumnLayout {
                        Layout.fillWidth: true
                        spacing: Theme.spacingMd

                        Label {
                            id: consentText
                            objectName: "consentText"
                            Layout.fillWidth: true
                            // FR-4.7 legal-warning intent (PROJECT_PLAN §15/§20): a cloned
                            // voice requires the CONSENTING PERSON's authorization; the
                            // user carries retention/use responsibility; no impersonation.
                            // Keep BOTH phrases verbatim — parallel suites pin them:
                            //   "quyền sử dụng giọng nói"        (tests/smoke/test_ui_tabs.py)
                            //   "người được sao chép"            (tests/smoke/test_ui_tabs.py)
                            text: qsTr("Bạn xác nhận có quyền sử dụng giọng nói trong tệp tham chiếu này và đã có sự đồng ý của chính người được sao chép đối với việc tạo bản sao giọng nói. Bản sao được lưu trên máy của bạn; việc bảo quản và sử dụng bản sao giọng nói là trách nhiệm của bạn, và không được dùng để mạo danh hoặc gây nhầm lẫn cho người khác.")
                            color: Theme.text
                            font.family: Theme.fontFamily
                            font.pixelSize: Theme.fontSizeBase
                            lineHeight: 1.4
                            wrapMode: Text.Wrap
                        }

                        Rectangle {
                            Layout.fillWidth: true
                            height: 1
                            color: Theme.borderSubtle
                        }

                        RowLayout {
                            Layout.fillWidth: true
                            spacing: Theme.spacingMd

                            Item { Layout.fillWidth: true }

                            Button {
                                id: consentAcceptButton
                                objectName: "consentAcceptButton"
                                text: qsTr("Tôi đồng ý")
                                font.family: Theme.fontFamily
                                font.weight: Theme.fontWeightBold
                                font.pixelSize: Theme.fontSizeBase
                                implicitHeight: 40
                                implicitWidth: 140

                                background: Rectangle {
                                    radius: Theme.radiusMd
                                    color: consentAcceptButton.pressed ? Theme.accentHover : (consentAcceptButton.hovered ? Theme.accentHover : Theme.accent)
                                }

                                contentItem: Text {
                                    text: consentAcceptButton.text
                                    font: consentAcceptButton.font
                                    color: Theme.accentText
                                    horizontalAlignment: Text.AlignHCenter
                                    verticalAlignment: Text.AlignVCenter
                                }

                                onClicked: controller.acknowledgeConsent()
                            }
                        }
                    }
                }
            }

            // ── Main Cloning Workspace ───────────────────────────────────────
            ColumnLayout {
                id: clonePanel

                objectName: "clonePanel"
                visible: controller.consentGiven
                Layout.fillWidth: true
                spacing: Theme.spacingLg

                // Step 1: Reference Audio Selection
                AppCard {
                    Layout.fillWidth: true
                    title: qsTr("1. Tệp âm thanh tham chiếu")
                    subtitle: qsTr("Đoạn âm thanh mẫu giọng đọc rõ ràng")

                    ColumnLayout {
                        Layout.fillWidth: true
                        spacing: Theme.spacingMd

                        Rectangle {
                            Layout.fillWidth: true
                            implicitHeight: 64
                            radius: Theme.radiusMd
                            color: root.clipPath !== "" ? Theme.surfaceAlt : Theme.surface
                            border.color: root.clipPath !== "" ? Theme.accent : Theme.border
                            border.width: 1

                            RowLayout {
                                anchors.fill: parent
                                anchors.margins: Theme.spacingMd
                                spacing: Theme.spacingMd

                                Rectangle {
                                    width: 32
                                    height: 32
                                    radius: Theme.radiusSm
                                    color: root.clipPath !== "" ? Theme.successSubtle : Theme.surfaceAlt
                                    border.color: root.clipPath !== "" ? Theme.success : Theme.borderSubtle
                                    border.width: 1

                                    Rectangle {
                                        anchors.centerIn: parent
                                        width: 8
                                        height: 8
                                        radius: 4
                                        color: root.clipPath !== "" ? Theme.success : Theme.textSubtle
                                    }
                                }

                                ColumnLayout {
                                    Layout.fillWidth: true
                                    spacing: 2

                                    Label {
                                        id: clipPathLabel
                                        objectName: "clipPathLabel"
                                        Layout.fillWidth: true
                                        text: root.clipPath === "" ? qsTr("Chưa chọn tệp") : root.clipPath
                                        elide: Text.ElideMiddle
                                        color: root.clipPath === "" ? Theme.textMuted : Theme.text
                                        font.family: Theme.fontFamily
                                        font.pixelSize: Theme.fontSizeBase
                                        font.weight: root.clipPath !== "" ? Theme.fontWeightMedium : Theme.fontWeightNormal
                                    }

                                    Label {
                                        Layout.fillWidth: true
                                        text: qsTr("Chọn đoạn âm 3–8 giây, chỉ có tiếng nói, ít nhiễu.")
                                        color: Theme.textSubtle
                                        font.family: Theme.fontFamily
                                        font.pixelSize: Theme.fontSizeXs
                                    }
                                }

                                Button {
                                    id: clipBrowseButton
                                    objectName: "clipBrowseButton"
                                    text: qsTr("Chọn tệp…")
                                    enabled: !controller.busy
                                    implicitHeight: 36
                                    font.family: Theme.fontFamily
                                    font.weight: Theme.fontWeightMedium

                                    background: Rectangle {
                                        radius: Theme.radiusSm
                                        color: clipBrowseButton.pressed ? Theme.surfaceHover : (clipBrowseButton.hovered ? Theme.surfaceHover : Theme.surfaceCard)
                                        border.color: Theme.border
                                        border.width: 1
                                    }

                                    contentItem: Text {
                                        text: clipBrowseButton.text
                                        font: clipBrowseButton.font
                                        color: clipBrowseButton.enabled ? Theme.text : Theme.textMuted
                                        horizontalAlignment: Text.AlignHCenter
                                        verticalAlignment: Text.AlignVCenter
                                    }

                                    onClicked: clipDialog.open()
                                }
                            }
                        }

                        // Denoise options & preview
                        RowLayout {
                            Layout.fillWidth: true
                            spacing: Theme.spacingMd

                            CheckBox {
                                id: denoiseCheck
                                objectName: "denoiseCheck"
                                text: qsTr("Khử nhiễu trước khi sao chép")
                                checked: true
                                font.family: Theme.fontFamily
                                font.pixelSize: Theme.fontSizeSm
                            }

                            Item { Layout.fillWidth: true }

                            Button {
                                id: denoiseButton
                                objectName: "denoiseButton"
                                text: qsTr("Nghe bản khử nhiễu")
                                enabled: root.clipPath !== "" && !controller.busy
                                implicitHeight: 32
                                font.family: Theme.fontFamily
                                font.pixelSize: Theme.fontSizeSm

                                background: Rectangle {
                                    radius: Theme.radiusSm
                                    color: denoiseButton.pressed ? Theme.surfaceHover : (denoiseButton.hovered ? Theme.surfaceHover : Theme.surfaceAlt)
                                    border.color: Theme.border
                                    border.width: 1
                                }

                                contentItem: Text {
                                    text: denoiseButton.text
                                    font: denoiseButton.font
                                    color: denoiseButton.enabled ? Theme.text : Theme.textMuted
                                    horizontalAlignment: Text.AlignHCenter
                                    verticalAlignment: Text.AlignVCenter
                                }

                                onClicked: controller.denoisePreview(root.clipPath)
                            }

                            Button {
                                id: previewPlayButton
                                objectName: "previewPlayButton"
                                text: qsTr("Phát thử")
                                visible: controller.previewPath !== ""
                                enabled: !controller.busy && controller.audioAvailable
                                implicitHeight: 32
                                font.family: Theme.fontFamily
                                font.pixelSize: Theme.fontSizeSm
                                font.weight: Theme.fontWeightBold

                                background: Rectangle {
                                    radius: Theme.radiusSm
                                    color: previewPlayButton.pressed ? Theme.accentHover : (previewPlayButton.hovered ? Theme.accentHover : Theme.accent)
                                }

                                contentItem: Text {
                                    text: previewPlayButton.text
                                    font: previewPlayButton.font
                                    color: Theme.accentText
                                    horizontalAlignment: Text.AlignHCenter
                                    verticalAlignment: Text.AlignVCenter
                                }

                                onClicked: playback.play(controller.previewPath)
                            }
                        }
                    }
                }

                // Step 2: Name & Enroll Voice
                AppCard {
                    Layout.fillWidth: true
                    title: qsTr("2. Đặt tên và tạo giọng")
                    subtitle: qsTr("Giọng sau khi tạo sẽ hiển thị trong danh mục lựa chọn giọng đọc")

                    ColumnLayout {
                        Layout.fillWidth: true
                        spacing: Theme.spacingMd

                        RowLayout {
                            Layout.fillWidth: true
                            spacing: Theme.spacingMd

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
                                implicitHeight: 42

                                background: Rectangle {
                                    radius: Theme.radiusSm
                                    color: Theme.surfaceAlt
                                    border.width: voiceNameField.activeFocus ? 2 : 1
                                    border.color: voiceNameField.activeFocus ? Theme.accent : Theme.border
                                }
                            }

                            Button {
                                id: cloneButton
                                objectName: "cloneButton"
                                text: qsTr("Tạo giọng nói")
                                enabled: root.clipPath !== "" && voiceNameField.text.trim() !== ""
                                          && !controller.busy
                                implicitHeight: 42
                                implicitWidth: 150
                                font.family: Theme.fontFamily
                                font.weight: Theme.fontWeightBold
                                font.pixelSize: Theme.fontSizeBase

                                background: Rectangle {
                                    radius: Theme.radiusSm
                                    color: !cloneButton.enabled
                                           ? Theme.surfaceHover
                                           : (cloneButton.pressed ? Theme.accentHover : (cloneButton.hovered ? Theme.accentHover : Theme.accent))
                                    border.color: !cloneButton.enabled ? Theme.border : "transparent"
                                    border.width: 1
                                }

                                contentItem: Text {
                                    text: cloneButton.text
                                    font: cloneButton.font
                                    color: cloneButton.enabled ? Theme.accentText : Theme.textMuted
                                    horizontalAlignment: Text.AlignHCenter
                                    verticalAlignment: Text.AlignVCenter
                                }

                                onClicked: controller.addVoice(voiceNameField.text.trim(), root.clipPath, denoiseCheck.checked)
                            }
                        }
                    }
                }

                // Step 3: Catalog of Cloned Voices
                AppCard {
                    Layout.fillWidth: true
                    title: qsTr("Giọng đã sao chép")
                    subtitle: qsTr("Danh sách các giọng đọc tùy chỉnh đang lưu trên máy")
                    badgeText: String(cloneList.rows.length)
                    badgeColor: Theme.accentSubtle
                    badgeTextColor: Theme.accent

                    ColumnLayout {
                        Layout.fillWidth: true
                        spacing: Theme.spacingSm

                        Column {
                            id: cloneList
                            objectName: "clonedVoiceList"
                            Layout.fillWidth: true
                            spacing: Theme.spacingSm
                            visible: rows.length > 0

                            readonly property var rows: root.clonedVoices(controller.voices)

                            Repeater {
                                model: cloneList.rows

                                Rectangle {
                                    id: cloneRow
                                    required property var modelData
                                    width: cloneList.width
                                    implicitHeight: 48
                                    radius: Theme.radiusSm
                                    color: Theme.surfaceAlt
                                    border.color: Theme.border
                                    border.width: 1

                                    RowLayout {
                                        anchors.fill: parent
                                        anchors.margins: Theme.spacingMd
                                        spacing: Theme.spacingMd

                                        Rectangle {
                                            width: 28
                                            height: 28
                                            radius: 14
                                            color: Theme.accentSubtle
                                            border.color: Theme.accent
                                            border.width: 1

                                            Label {
                                                anchors.centerIn: parent
                                                text: cloneRow.modelData && cloneRow.modelData.label ? cloneRow.modelData.label.charAt(0).toUpperCase() : "V"
                                                color: Theme.accent
                                                font.family: Theme.fontFamily
                                                font.pixelSize: Theme.fontSizeSm
                                                font.weight: Theme.fontWeightBold
                                            }
                                        }

                                        Label {
                                            objectName: "clonedVoiceName"
                                            Layout.fillWidth: true
                                            text: cloneRow.modelData ? cloneRow.modelData.label : ""
                                            elide: Text.ElideMiddle
                                            color: Theme.text
                                            font.family: Theme.fontFamily
                                            font.pixelSize: Theme.fontSizeBase
                                            font.weight: Theme.fontWeightMedium
                                        }

                                        Button {
                                            id: cloneRemoveButton
                                            objectName: "cloneRemoveButton"
                                            text: qsTr("Xóa")
                                            font.family: Theme.fontFamily
                                            font.pixelSize: Theme.fontSizeSm
                                            implicitHeight: 30

                                            background: Rectangle {
                                                radius: Theme.radiusSm
                                                color: cloneRemoveButton.hovered ? Theme.errorSubtle : "transparent"
                                                border.color: cloneRemoveButton.hovered ? Theme.error : Theme.border
                                                border.width: 1
                                            }

                                            contentItem: Text {
                                                text: cloneRemoveButton.text
                                                font: cloneRemoveButton.font
                                                color: cloneRemoveButton.hovered ? Theme.error : Theme.textMuted
                                                horizontalAlignment: Text.AlignHCenter
                                                verticalAlignment: Text.AlignVCenter
                                            }

                                            onClicked: controller.removeVoice(cloneRow.modelData.id)
                                        }
                                    }
                                }
                            }
                        }

                        Label {
                            visible: cloneList.rows.length === 0
                            text: qsTr("Chưa có giọng sao chép nào.")
                            color: Theme.textMuted
                            font.family: Theme.fontFamily
                            font.pixelSize: Theme.fontSizeSm
                            font.italic: true
                        }
                    }
                }

                // ── Busy / Error Indicator ───────────────────────────────────
                RowLayout {
                    Layout.fillWidth: true
                    spacing: Theme.spacingMd

                    Label {
                        objectName: "cloneBusyLabel"
                        text: qsTr("Đang xử lý…")
                        visible: controller.busy
                        color: Theme.textMuted
                        font.family: Theme.fontFamily
                        font.pixelSize: Theme.fontSizeSm
                    }

                    ProgressBar {
                        id: progressBar
                        objectName: "progressBar"
                        visible: controller.busy
                        Layout.fillWidth: true
                        value: controller.progress
                        indeterminate: controller.progress === 0.0
                    }
                }

                Rectangle {
                    Layout.fillWidth: true
                    implicitHeight: errorLabel.implicitHeight + Theme.spacingMd * 2
                    radius: Theme.radiusMd
                    color: Theme.errorSubtle
                    border.color: Theme.error
                    border.width: 1
                    visible: controller.errorText !== ""

                    RowLayout {
                        anchors.fill: parent
                        anchors.margins: Theme.spacingMd
                        spacing: Theme.spacingSm

                        Rectangle {
                            width: 20
                            height: 20
                            radius: 10
                            color: Theme.errorSubtle
                            border.color: Theme.error
                            border.width: 1
                            Label {
                                anchors.centerIn: parent
                                text: "!"
                                color: Theme.error
                                font.family: Theme.fontFamily
                                font.pixelSize: Theme.fontSizeXs
                                font.weight: Theme.fontWeightBold
                            }
                        }

                        Label {
                            id: errorLabel
                            objectName: "errorLabel"
                            Layout.fillWidth: true
                            text: controller.errorText
                            color: Theme.error
                            font.family: Theme.fontFamily
                            font.pixelSize: Theme.fontSizeSm
                            wrapMode: Text.Wrap
                        }
                    }
                }
            }
        }
    }
}
