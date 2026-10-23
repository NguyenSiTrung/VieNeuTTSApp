import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import ".."
import "."

// Pinned action dock for the "Lồng tiếng & Phụ đề" destination (FR-2.1).
//
// The audit measured the primary action 702 px below the fold at 640×420 while
// it lived inside SubtitleCard's scrolling column; this dock is placed by
// SubtitlesTab OUTSIDE the PageShell, so "Tạo âm thanh", transport, exports,
// progress and cancel never scroll away. It mirrors TransportDock's skin and
// single-primary rule: one primary (Tạo âm thanh) per screen state.
//
// Reads the `subtitleController` context property (named to avoid AppCard's
// own `subtitle` header string), plus the app-wide `controller`/`bridge`.
//
// objectNames (tested contract): subtitleActionDock, subtitleRenderButton,
// subtitleCancelButton, subtitlePlayButton, subtitleExportMenuButton,
// subtitleExportWavButton, subtitleExportSrtButton, subtitleExportVideoButton,
// subtitleExportSpeakersToggle, subtitleStudioButton, subtitleProgressBar,
// subtitleExportProgressLabel, subtitleExportCancelButton, subtitleStaleHint,
// subtitleStatusLabel.
Rectangle {
    id: root

    readonly property bool available: typeof subtitleController !== "undefined"
        && subtitleController !== null
    readonly property bool loaded: available && subtitleController.loaded === true
    readonly property bool busy: available && subtitleController.rendering === true
    readonly property bool aiBusy: available && subtitleController.aiBusy === true
    readonly property bool exporting: available && subtitleController.exporting === true
    readonly property bool hasTrack: available && subtitleController.rendered === true
    readonly property bool hasVideo: available && subtitleController.hasVideo === true
    readonly property bool hasSpeakers: available && subtitleController.hasSpeakers === true
    readonly property string exportFormat: (typeof controller !== "undefined" && controller
        && controller.exportFormat === "mp3") ? "mp3" : "wav"
    readonly property string playerState: available ? subtitleController.playerState : "stopped"

    // Short windows: hide the secondary status lines and tighten padding so
    // the dock never eats the editor's height at 640×420.
    property bool compact: false

    objectName: "subtitleActionDock"
    implicitHeight: dockColumn.implicitHeight + (compact ? Theme.spacingSm : Theme.spacingMd) * 2
    radius: Theme.radiusLg
    color: Theme.surfaceCard
    border.width: 1
    border.color: Theme.border

    function exportDir() {
        return (typeof controller !== "undefined" && controller && controller.outputDir)
            ? controller.outputDir : "";
    }

    function openStudio() {
        if (!available)
            return;
        const pid = typeof subtitleController.projectId !== "undefined"
            ? subtitleController.projectId : "";
        const cue = subtitleController.activeCue >= 0 ? subtitleController.activeCue : 0;
        if (pid !== "" && typeof controller !== "undefined" && controller
                && controller.openSubtitleInStudio(pid, cue)) {
            if (typeof bridge !== "undefined" && bridge !== null)
                bridge.setCurrentTab("studio");
        }
    }

    ColumnLayout {
        id: dockColumn

        anchors.fill: parent
        anchors.margins: root.compact ? Theme.spacingSm : Theme.spacingMd
        spacing: Theme.spacingXs

        // Stale affordance (FR-1.2): the dock, not a vanishing stats line,
        // explains why exports are unavailable.
        Label {
            objectName: "subtitleStaleHint"
            Layout.fillWidth: true
            visible: root.available && subtitleController.renderStale && !root.compact
            text: root.available ? subtitleController.renderStaleReason : ""
            color: Theme.warningText
            font.family: Theme.fontFamily
            font.pixelSize: Theme.fontSizeXs
            wrapMode: Text.WordWrap
        }

        Flow {
            Layout.fillWidth: true
            spacing: Theme.spacingSm

            AppButton {
                id: renderBtn

                objectName: "subtitleRenderButton"
                variant: "primary"
                size: "lg"
                iconKind: "wave"
                text: qsTr("Tạo âm thanh")
                tooltipText: qsTr("Tạo bản lồng tiếng và phụ đề đã căn chỉnh (Ctrl+Enter)")
                accessibleLabel: text
                visible: root.loaded
                enabled: root.loaded && !root.busy && !root.exporting
                    && EngineState.blockerReason === ""
                disabledReason: EngineState.blockerReason
                busy: root.busy
                onClicked: {
                    if (root.available)
                        subtitleController.render();
                }
            }

            AppButton {
                id: cancelBtn

                objectName: "subtitleCancelButton"
                variant: "danger"
                size: "sm"
                text: qsTr("Hủy")
                visible: root.busy
                onClicked: {
                    if (root.available)
                        subtitleController.cancelRender();
                }
            }

            AppButton {
                id: playBtn

                objectName: "subtitlePlayButton"
                variant: "secondary"
                checked: root.playerState === "playing"
                size: "md"
                // No track yet: play() renders first — label says so.
                tooltipText: !root.hasTrack
                    ? qsTr("Chưa có bản âm thanh: tạo trước rồi phát") : qsTr("Phát hoặc tạm dừng bản lồng tiếng")
                accessibleLabel: text
                text: !root.hasTrack ? qsTr("Tạo và phát")
                    : root.playerState === "playing" ? qsTr("Tạm dừng") : qsTr("Phát")
                iconKind: root.playerState === "playing" ? "pause" : "play"
                visible: root.loaded
                enabled: root.available && (root.hasTrack || !root.busy) && !root.exporting
                    && (root.hasTrack || EngineState.blockerReason === "")
                disabledReason: root.hasTrack ? "" : EngineState.blockerReason
                onClicked: {
                    if (!root.available)
                        return;
                    if (root.playerState === "playing")
                        subtitleController.pause();
                    else if (root.playerState === "paused")
                        subtitleController.resume();
                    else
                        subtitleController.play();
                }
            }

            AppButton {
                id: exportMenuBtn

                objectName: "subtitleExportMenuButton"
                variant: "secondary"
                size: "md"
                iconKind: "download"
                text: qsTr("Xuất")
                visible: root.loaded
                enabled: root.available && !root.busy && !root.exporting
                accessibleLabel: qsTr("Xuất âm thanh, phụ đề hoặc video lồng tiếng")
                tooltipText: accessibleLabel
                onClicked: exportMenu.opened ? exportMenu.close() : exportMenu.open()

                AppMenu {
                    id: exportMenu

                    objectName: "subtitleExportMenu"
                    x: exportMenuBtn.width - width
                    y: -height - Theme.spacingXs

                    AppMenuItem {
                        objectName: "subtitleExportWavButton"
                        text: root.exportFormat === "mp3" ? qsTr("Xuất MP3") : qsTr("Xuất WAV")
                        enabled: root.available && subtitleController.canExportTrack
                            && !root.busy && !root.exporting
                        onTriggered: root.available && subtitleController.exportTrack(root.exportDir())
                    }

                    AppMenuItem {
                        objectName: "subtitleExportSrtButton"
                        text: qsTr("Xuất SRT")
                        enabled: root.available && subtitleController.canExportSrt
                            && !root.busy && !root.exporting
                        onTriggered: root.available && subtitleController.exportSrt(root.exportDir())
                    }

                    AppMenuItem {
                        objectName: "subtitleExportVideoButton"
                        text: qsTr("Xuất Video")
                        visible: root.hasVideo
                        enabled: root.available && subtitleController.canExportTrack && root.hasVideo
                            && !root.busy && !root.aiBusy && !root.exporting
                        onTriggered: root.available
                            && subtitleController.exportVideo(root.exportDir())
                    }

                    AppMenuItem {
                        objectName: "subtitleExportSpeakersToggle"
                        text: qsTr("Kèm nhãn người nói khi xuất SRT")
                        visible: root.hasSpeakers
                        checkable: true
                        checked: Boolean(typeof controller !== "undefined" && controller
                            && controller.subtitleExportSpeakerLabels)
                        enabled: root.available && !root.busy && !root.exporting
                        onToggled: {
                            if (typeof controller !== "undefined" && controller
                                    && typeof controller.subtitleExportSpeakerLabels !== "undefined")
                                controller.subtitleExportSpeakerLabels = checked;
                        }
                    }
                }
            }

            AppButton {
                objectName: "subtitleStudioButton"
                variant: "secondary"
                size: "md"
                iconKind: "studio"
                text: qsTr("Mở trong Studio")
                tooltipText: qsTr("Mở dự án phụ đề này trong Studio để chỉnh từng đoạn")
                accessibleLabel: text
                visible: root.loaded
                enabled: root.hasTrack && !root.busy && !root.exporting
                onClicked: root.openStudio()
            }
        }

        // One progress owner at a time (FR-2.2): a render or an export, never
        // both bars animating.
        RowLayout {
            Layout.fillWidth: true
            spacing: Theme.spacingSm
            visible: root.busy || root.exporting

            AppProgressBar {
                id: dockProgress

                objectName: "subtitleProgressBar"
                Layout.fillWidth: true
                accessibleLabel: qsTr("Tiến độ tạo âm thanh")
                value: root.exporting
                    ? (root.available ? subtitleController.exportProgress : 0)
                    : (root.available ? subtitleController.renderProgress : 0)
                indeterminate: root.exporting
                    ? (root.available ? subtitleController.exportProgress === 0 : true)
                    : (root.busy && subtitleController.renderProgress === 0)
            }

            Label {
                objectName: "subtitleExportProgressLabel"
                visible: root.exporting
                text: qsTr("Đang xuất video… %1%").arg(
                    Math.round((root.available ? subtitleController.exportProgress : 0) * 100))
                color: Theme.textMuted
                font.family: Theme.fontFamily
                font.pixelSize: Theme.fontSizeXs
            }

            AppButton {
                objectName: "subtitleExportCancelButton"
                variant: "danger"
                size: "sm"
                text: qsTr("Hủy xuất")
                visible: root.exporting
                onClicked: {
                    if (root.available)
                        subtitleController.cancelExport();
                }
            }
        }

        Label {
            objectName: "subtitleStatusLabel"
            Layout.fillWidth: true
            visible: !root.compact && text !== ""
            text: subtitleStatus.text
            color: Theme.textMuted
            font.family: Theme.fontFamily
            font.pixelSize: Theme.fontSizeSm
            elide: Text.ElideMiddle
        }
    }

    QtObject {
        id: subtitleStatus

        property string text: ""

        function set(path, error) {
            if (error !== "")
                text = qsTr("Xuất thất bại: %1").arg(error);
            else if (path !== "")
                text = qsTr("Đã xuất: %1").arg(path);
        }
    }

    Connections {
        target: root.available ? subtitleController : null

        function onExportFinished(path, error) {
            subtitleStatus.set(path, error);
        }
    }
}
