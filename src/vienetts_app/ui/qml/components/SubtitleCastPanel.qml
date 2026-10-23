import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import ".."

// Speaker → voice mapping (FR-3.4). One row per speaker, wrapping on narrow
// windows: an audition button (video only), a catalog-backed VoicePicker and
// two shortcuts for bulk assignment. Re-running diarization lives here because
// this is the surface it produces.
//
// The host supplies the gating reasons so the disabled tooltips match the
// controls that own the same state; this component owns only presentation.
AppCard {
    id: root

    property bool shown: false
    property bool available: false
    property bool busy: false
    property bool aiBusy: false
    property bool exporting: false
    property bool hasVideo: false
    property string busyReason: ""
    property string diarizeReason: ""
    property string currentVoice: ""

    objectName: "subtitleSpeakerBar"
    visible: root.shown
    elevation: 0
    cardColor: Theme.surfaceAlt
    cardBorderColor: Theme.borderSubtle
    cardPadding: Theme.spacingSm

    ColumnLayout {
        id: speakerBarCol

        Layout.fillWidth: true
        spacing: Theme.spacingXxs

        RowLayout {
            Layout.fillWidth: true
            spacing: Theme.spacingSm

            Label {
                Layout.fillWidth: true
                text: qsTr("Phân vai giọng đọc theo người nói:")
                color: Theme.textMuted
                font.family: Theme.fontFamily
                font.pixelSize: Theme.fontSizeXs
                font.weight: Theme.fontWeightMedium
            }

            AppButton {
                objectName: "subtitleDiarizeButton"
                variant: "secondary"
                size: "sm"
                text: qsTr("Phân tách lại người nói")
                tooltipText: qsTr("Chạy lại phân tách trên bản phụ đề hiện tại.")
                enabled: root.available && !root.busy && !root.aiBusy && !root.exporting
                    && root.hasVideo
                disabledReason: root.diarizeReason
                onClicked: {
                    if (root.available)
                        subtitleController.runDiarization();
                }
            }
        }

        Repeater {
            model: root.available ? subtitleController.speakers : []

            delegate: Flow {
                id: speakerRow

                required property var modelData
                required property int index

                objectName: "subtitleSpeakerRow"
                Layout.fillWidth: true
                spacing: Theme.spacingSm

                Rectangle {
                    radius: Theme.radiusSm
                    color: Theme.accentSubtle
                    implicitWidth: spkBadgeLbl.implicitWidth + Theme.spacingSm * 2
                    implicitHeight: Theme.controlHeightSm

                    Label {
                        id: spkBadgeLbl

                        anchors.centerIn: parent
                        text: modelData.label + " (" + modelData.cueCount + ")"
                        color: Theme.accent
                        font.family: Theme.fontFamilyMono !== "" ? Theme.fontFamilyMono : Theme.fontFamily
                        font.pixelSize: Theme.fontSizeXs
                        font.weight: Theme.fontWeightMedium
                    }
                }

                AppButton {
                    objectName: "subtitleSpeakerSampleButton"
                    variant: "secondary"
                    size: "sm"
                    visible: root.hasVideo
                    text: qsTr("Nghe mẫu")
                    enabled: root.available && !root.busy && !root.aiBusy && !root.exporting
                    disabledReason: root.busyReason
                    onClicked: {
                        if (root.available)
                            subtitleController.playSpeakerSample(modelData.id);
                    }
                }

                VoicePicker {
                    objectName: "subtitleSpeakerVoicePicker"
                    compact: true
                    fieldLabel: qsTr("Giọng cho %1").arg(modelData.label)
                    selectedVoice: modelData.voice || ""
                    enabled: root.available && !root.busy && !root.exporting
                    onVoiceActivated: (id) => {
                        if (root.available && id !== "")
                            subtitleController.setSpeakerVoice(modelData.id, id);
                    }
                }

                AppButton {
                    objectName: "subtitleUseCurrentVoiceForSpeakerButton"
                    variant: "secondary"
                    size: "sm"
                    text: qsTr("Gán giọng đang chọn")
                    enabled: root.available && !root.busy && !root.exporting
                        && root.currentVoice !== ""
                    disabledReason: root.busyReason !== ""
                        ? root.busyReason : qsTr("Chọn một giọng đọc trước khi gán.")
                    onClicked: {
                        if (root.available)
                            subtitleController.setSpeakerVoice(
                                modelData.id, root.currentVoice);
                    }
                }

                AppButton {
                    objectName: "subtitleCloneSpeakerButton"
                    variant: "secondary"
                    size: "sm"
                    visible: root.hasVideo
                    text: qsTr("Lấy mẫu giọng từ video")
                    enabled: root.available && !root.busy && !root.aiBusy && !root.exporting
                        && EngineState.supportsCloning
                    disabledReason: root.diarizeReason !== ""
                        ? root.diarizeReason
                        : qsTr("Engine hiện tại không hỗ trợ nhân bản giọng.")
                    onClicked: {
                        if (root.available)
                            subtitleController.cloneSpeakerFromVideo(modelData.id);
                    }
                }
            }
        }
    }
}
