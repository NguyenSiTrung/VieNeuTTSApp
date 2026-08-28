// Voice cloning tab (FR-3.4, FR-UX-6): consent gate → reference-clip selection with
// 3–8 s guidance (file dialog OR drag & drop), optional denoise preview, clone
// enrollment and management of the cloned catalog group. All state flows through
// the `controller` / `playback` context properties registered by app.py.
//
// objectNames are the tested contract (tests/smoke/test_ui_tabs.py):
// consentPanel, consentAcceptButton, consentText, clonePanel, clipPathLabel,
// clipBrowseButton, clipDialog, denoiseCheck, denoiseButton,
// previewPlayButton, voiceNameField, cloneButton, clonedVoiceList,
// clonedVoiceName, cloneRemoveButton, cloneBusyLabel, progressBar, errorLabel.
// Pinned copy: "Sao chép giọng nói", "quyền sử dụng giọng nói",
// "người được sao chép", "Tôi đồng ý", "Chưa chọn tệp", "Chọn tệp…",
// "3–8 giây", "Khử nhiễu trước khi sao chép", "Nghe bản khử nhiễu",
// "Tạo giọng nói", "Xóa", placeholder "Tên giọng mới (vd: Giọng đọc truyện)".
//
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
        color: Theme.bg
    }

    // Selected reference clip (local filesystem path; "" until chosen).
    property string clipPath: ""
    property bool dragOver: false

    // QUrl → local path string (FileDialog gives a url; controller slots
    // take filesystem paths). %XX escapes decoded; non-file schemes pass
    // through untouched. Same helper idiom as ParagraphTab/TextTab.
    function toLocalPath(url) {
        const s = url.toString();
        return s.startsWith("file://") ? decodeURIComponent(s.substring(7)) : s;
    }

    // Shared entry point for clipDialog.onAccepted AND the offscreen tests,
    // which invoke it via QMetaObject on the "cloningTab" item (native open
    // dialogs are unreliable headless). Drag-and-drop routes here too.
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

    Dialog {
        id: removeConfirmDialog

        objectName: "cloneRemoveConfirmDialog"
        property string voiceId: ""
        title: qsTr("Xóa giọng nói?")
        modal: true
        focus: true
        anchors.centerIn: Overlay.overlay
        width: Math.min(root.width - Theme.spacingXl * 2, 420)
        padding: Theme.spacingLg

        background: Rectangle {
            radius: Theme.radiusLg
            color: Theme.surfaceCard
            border.color: Theme.border
            border.width: 1
        }

        contentItem: ColumnLayout {
            spacing: Theme.spacingMd

            Label {
                Layout.fillWidth: true
                text: qsTr("Giọng nói này sẽ bị xóa khỏi danh mục đã sao chép.")
                color: Theme.textMuted
                font.family: Theme.fontFamily
                font.pixelSize: Theme.fontSizeBase
                wrapMode: Text.Wrap
            }

            RowLayout {
                Layout.fillWidth: true
                spacing: Theme.spacingSm

                Item { Layout.fillWidth: true }

                AppButton {
                    variant: "quiet"
                    text: qsTr("Giữ lại")
                    onClicked: removeConfirmDialog.close()
                }

                AppButton {
                    objectName: "cloneRemoveConfirmButton"
                    variant: "danger"
                    text: qsTr("Xóa giọng")
                    onClicked: {
                        controller.removeVoice(removeConfirmDialog.voiceId)
                        removeConfirmDialog.close()
                    }
                }
            }
        }
    }

    PageShell {
        anchors.fill: parent
        maxWidth: 840

        // Studio Header
        PageHeader {
            Layout.fillWidth: true
            iconKind: "cloning"
            title: qsTr("Sao chép giọng nói")
            subtitle: qsTr("Tạo giọng đọc tùy chỉnh từ một đoạn âm thanh mẫu 3–8 giây, 100% riêng tư trên thiết bị.")
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
                badgeText: qsTr("Riêng tư trên thiết bị")
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

                        AppButton {
                            id: consentAcceptButton
                            objectName: "consentAcceptButton"
                            variant: "primary"
                            size: "lg"
                            implicitWidth: 150
                            text: qsTr("Tôi đồng ý")
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
                title: qsTr("Tệp âm thanh tham chiếu")
                subtitle: qsTr("Kéo thả tệp vào khung, hoặc chọn từ máy — đoạn giọng đọc rõ ràng, ít nhiễu")
                badgeText: "1"

                // (Reference-clip drag-and-drop lives inside the clip box below.)

                ColumnLayout {
                    Layout.fillWidth: true
                    spacing: Theme.spacingMd

                    Rectangle {
                        Layout.fillWidth: true
                        implicitHeight: 68
                        radius: Theme.radiusMd
                        color: root.clipPath !== "" || root.dragOver ? Theme.accentSubtle : Theme.surface
                        border.color: root.clipPath !== "" || root.dragOver ? Theme.accent : Theme.border
                        border.width: root.clipPath !== "" || root.dragOver ? Theme.focusRingWidth : 1
                        Behavior on color { ColorAnimation { duration: Theme.durationFast } }
                        Behavior on border.color { ColorAnimation { duration: Theme.durationFast } }

                        // Drag & drop audio import (same seam as the file dialog)
                        DropArea {
                            anchors.fill: parent
                            onEntered: if (drag.hasUrls) root.dragOver = true
                            onExited: root.dragOver = false
                            onDropped: if (drop.hasUrls && drop.urls.length > 0) {
                                root.dragOver = false;
                                root.selectClip(root.toLocalPath(drop.urls[0]));
                            }
                        }

                        RowLayout {
                            anchors.fill: parent
                            anchors.margins: Theme.spacingMd
                            spacing: Theme.spacingMd

                            Rectangle {
                                width: 34
                                height: 34
                                radius: Theme.radiusSm
                                color: root.clipPath !== "" ? Theme.successSubtle : Theme.surfaceAlt
                                border.color: root.clipPath !== "" ? Theme.success : Theme.borderSubtle
                                border.width: 1

                                AppIcon {
                                    anchors.centerIn: parent
                                    kind: "wave"
                                    width: 18
                                    height: 18
                                    iconColor: root.clipPath !== "" ? Theme.success : Theme.textSubtle
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

                            AppButton {
                                id: clipBrowseButton
                                objectName: "clipBrowseButton"
                                variant: "secondary"
                                text: qsTr("Chọn tệp…")
                                enabled: !controller.busy
                                onClicked: clipDialog.open()
                            }
                        }
                    }

                    // Denoise options & preview
                    RowLayout {
                        Layout.fillWidth: true
                        spacing: Theme.spacingMd

                        AppToggle {
                            id: denoiseCheck
                            objectName: "denoiseCheck"
                            text: qsTr("Khử nhiễu trước khi sao chép")
                            checked: true
                            accessibleLabel: qsTr("Khử nhiễu trước khi sao chép")
                        }

                        Item { Layout.fillWidth: true }

                        AppButton {
                            id: denoiseButton
                            objectName: "denoiseButton"
                            variant: "secondary"
                            size: "sm"
                            text: qsTr("Nghe bản khử nhiễu")
                            enabled: root.clipPath !== "" && !controller.busy
                            onClicked: controller.denoisePreview(root.clipPath)
                        }

                        AppButton {
                            id: previewPlayButton
                            objectName: "previewPlayButton"
                            variant: "primary"
                            size: "sm"
                            text: qsTr("Phát thử")
                            iconKind: "play"
                            visible: controller.previewPath !== ""
                            enabled: !controller.busy && controller.audioAvailable
                            onClicked: playback.play(controller.previewPath)
                        }
                    }
                }
            }

            // Step 2: Name & Enroll Voice
            AppCard {
                Layout.fillWidth: true
                title: qsTr("Đặt tên và tạo giọng")
                subtitle: qsTr("Giọng sau khi tạo sẽ hiển thị trong danh mục lựa chọn giọng đọc")
                badgeText: "2"

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
                            placeholderTextColor: Theme.textSubtle
                            color: Theme.text
                            selectedTextColor: Theme.accentText
                            selectionColor: Theme.accent
                            font.family: Theme.fontFamily
                            font.pixelSize: Theme.fontSizeBase
                            implicitHeight: 42
                            leftPadding: Theme.spacingMd
                            rightPadding: Theme.spacingMd
                            selectByMouse: true

                            background: Rectangle {
                                radius: Theme.radiusSm
                                color: Theme.surface
                                border.width: voiceNameField.activeFocus ? Theme.focusRingWidth : 1
                                border.color: voiceNameField.activeFocus ? Theme.accent : Theme.border
                                Behavior on border.color { ColorAnimation { duration: Theme.durationFast } }
                            }
                        }

                        AppButton {
                            id: cloneButton
                            objectName: "cloneButton"
                            variant: "primary"
                            size: "lg"
                            implicitWidth: 160
                            text: qsTr("Tạo giọng nói")
                            enabled: root.clipPath !== "" && voiceNameField.text.trim() !== ""
                                      && !controller.busy
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
                badgeText: cloneList.rows.length > 0 ? String(cloneList.rows.length) : ""
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
                                implicitHeight: 52
                                radius: Theme.radiusMd
                                color: Theme.surfaceAlt
                                border.color: Theme.borderSubtle
                                border.width: 1

                                RowLayout {
                                    anchors.fill: parent
                                    anchors.margins: Theme.spacingMd
                                    spacing: Theme.spacingMd

                                    Rectangle {
                                        width: 30
                                        height: 30
                                        radius: Theme.radiusSm
                                        color: Theme.accentSubtle
                                        border.color: Theme.borderFocus
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

                                    ColumnLayout {
                                        Layout.fillWidth: true
                                        spacing: 0

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

                                        Label {
                                            Layout.fillWidth: true
                                            text: qsTr("Sẵn sàng dùng trong mọi studio")
                                            color: Theme.textSubtle
                                            font.family: Theme.fontFamily
                                            font.pixelSize: Theme.fontSizeXs
                                        }
                                    }

                                    AppButton {
                                        id: cloneRemoveButton
                                        objectName: "cloneRemoveButton"
                                        variant: "quiet"
                                        size: "sm"
                                        text: qsTr("Xóa")
                                        iconKind: "close"
                                        onClicked: {
                                            removeConfirmDialog.voiceId = cloneRow.modelData.id
                                            removeConfirmDialog.open()
                                        }
                                    }
                                }
                            }
                        }
                    }

                    // Composed empty state (was a bare italic line)
                    Rectangle {
                        Layout.fillWidth: true
                        visible: cloneList.rows.length === 0
                        implicitHeight: emptyCol.implicitHeight + Theme.spacingLg * 2
                        radius: Theme.radiusMd
                        color: Theme.surfaceAlt
                        border.color: Theme.borderSubtle
                        border.width: 1

                        ColumnLayout {
                            id: emptyCol
                            anchors.centerIn: parent
                            spacing: Theme.spacingSm

                            AppIcon {
                                Layout.alignment: Qt.AlignHCenter
                                kind: "wave"
                                width: 24
                                height: 24
                                iconColor: Theme.textSubtle
                            }

                            Label {
                                Layout.alignment: Qt.AlignHCenter
                                text: qsTr("Chưa có giọng sao chép nào")
                                color: Theme.textMuted
                                font.family: Theme.fontFamily
                                font.pixelSize: Theme.fontSizeBase
                                font.weight: Theme.fontWeightMedium
                            }

                            Label {
                                Layout.alignment: Qt.AlignHCenter
                                text: qsTr("Giọng bạn tạo ở trên sẽ xuất hiện tại đây")
                                color: Theme.textSubtle
                                font.family: Theme.fontFamily
                                font.pixelSize: Theme.fontSizeXs
                            }
                        }
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
                    color: Theme.accent
                    font.family: Theme.fontFamily
                    font.pixelSize: Theme.fontSizeSm
                    font.weight: Theme.fontWeightMedium
                }

                ProgressBar {
                    id: progressBar
                    objectName: "progressBar"
                    visible: controller.busy
                    Layout.fillWidth: true
                    value: controller.progress
                    indeterminate: controller.progress === 0.0

                    background: Rectangle {
                        implicitHeight: 6
                        radius: 3
                        color: Theme.surfaceAlt
                    }
                    contentItem: Item {
                        clip: true
                        Rectangle {
                            visible: !progressBar.indeterminate
                            width: progressBar.visualPosition * parent.width
                            height: parent.height
                            radius: 3
                            color: Theme.accent
                        }
                    }
                }
            }

            AppNotice {
                objectName: "cloneErrorNotice"
                Layout.fillWidth: true
                tone: "error"
                title: qsTr("Không thể tạo giọng nói")
                message: controller.errorText
                messageObjectName: "errorLabel"
                visible: controller.errorText !== ""
            }
        }
    }
}
