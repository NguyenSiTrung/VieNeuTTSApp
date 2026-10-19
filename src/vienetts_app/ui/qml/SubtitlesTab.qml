// Lồng tiếng & Phụ đề: dedicated top-level destination for SRT subtitle
// audio synthesis and AI video dubbing (ASR, speaker diarization, LLM
// refinement/translation, multi-speaker voice casting, and video muxing).
//
// Owns SubtitleCard in a full-height workspace with its own PageHeader,
// error banner, and Escape shortcut so CreateTab only serves text/document/
// batch-file synthesis.
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "."
import "components"

Pane {
    id: root

    objectName: "subtitlesTab"
    padding: Theme.spacingLg

    background: Rectangle {
        color: Theme.bg
    }

    readonly property bool shown: typeof bridge !== "undefined" && bridge !== null
        && bridge.currentTab === "subtitles"
    readonly property int pageMaxWidth: 1080
    readonly property string subtitleErrorText: (typeof subtitleController !== "undefined"
        && subtitleController !== null) ? (subtitleController.errorText || "") : ""
    readonly property string synthesisErrorText: (typeof controller !== "undefined"
        && controller !== null) ? (controller.errorText || "") : ""

    // Escape cancels an active subtitle cue render first, or falls back to
    // cancelling a regular foreground synthesis job. Keeps the historical
    // objectName contract (`paragraphEscapeShortcut`, NFR-1).
    Shortcut {
        objectName: "paragraphEscapeShortcut"
        sequence: "Escape"
        enabled: root.shown
            && ((typeof controller !== "undefined" && controller !== null
                 && controller.busy && controller.foregroundJobState !== "cancel_requested")
                || (typeof subtitleController !== "undefined"
                    && subtitleController !== null && subtitleController.rendering))
        onActivated: {
            if (typeof subtitleController !== "undefined" && subtitleController !== null
                    && subtitleController.rendering) {
                subtitleController.cancelRender();
            } else if (typeof controller !== "undefined" && controller !== null) {
                controller.cancel();
            }
        }
        context: Qt.WindowShortcut
    }

    ColumnLayout {
        anchors.fill: parent
        spacing: Theme.spacingMd

        PageHeader {
            id: titleHeader

            objectName: "subtitlesPageHeader"
            Layout.fillWidth: true
            Layout.maximumWidth: root.pageMaxWidth
            Layout.alignment: Qt.AlignHCenter
            title: qsTr("Lồng tiếng & Phụ đề")
        }

        PageShell {
            id: page

            objectName: "subtitlesWorkspace"
            Layout.fillWidth: true
            Layout.fillHeight: true
            maxWidth: root.pageMaxWidth
            stretch: true
            reserveVerticalScrollBar: true
            pageSpacing: Theme.spacingMd

            AppNotice {
                id: errorBanner

                objectName: "subtitleErrorBanner"
                readonly property bool synthesisError: root.synthesisErrorText !== ""

                Layout.fillWidth: true
                tone: !synthesisError && message !== "" ? "warning" : "error"
                title: !synthesisError ? qsTr("Cần chú ý") : qsTr("Không thể tạo âm thanh")
                message: root.subtitleErrorText || root.synthesisErrorText
                messageObjectName: "subtitleErrorLabel"
                visible: message !== ""
            }

            SubtitleCard {
                id: subtitleCard

                Layout.fillWidth: true
                Layout.fillHeight: subtitleCard.loaded
            }

            Item {
                Layout.fillHeight: !subtitleCard.loaded
            }
        }
    }
}
