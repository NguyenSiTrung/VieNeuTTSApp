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
    readonly property bool subtitleAvailable: typeof subtitleController !== "undefined"
        && subtitleController !== null
    readonly property int pageMaxWidth: 960
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
                    && subtitleController !== null
                    && (subtitleController.rendering || subtitleController.aiBusy)))
        onActivated: {
            if (typeof subtitleController !== "undefined" && subtitleController !== null
                    && subtitleController.rendering) {
                subtitleController.cancelRender();
            } else if (typeof subtitleController !== "undefined" && subtitleController !== null
                    && subtitleController.aiBusy) {
                subtitleController.cancelAiTask();
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

            trailing: RowLayout {
                spacing: Theme.spacingXs

                AppButton {
                    objectName: "subtitleProjectsButton"
                    variant: "secondary"
                    size: "sm"
                    iconKind: "folder"
                    text: qsTr("Dự án (%1)").arg(root.subtitleAvailable
                        ? subtitleController.projectCount : 0)
                    tooltipText: qsTr("Mở, chuyển hoặc xóa dự án phụ đề đã lưu")
                    accessibleLabel: text
                    onClicked: projectsDialog.open()
                }

                AppButton {
                    objectName: "subtitleNewProjectButton"
                    variant: "secondary"
                    size: "sm"
                    text: qsTr("Dự án mới")
                    tooltipText: qsTr("Đóng dự án hiện tại và bắt đầu từ đầu")
                    accessibleLabel: text
                    enabled: root.subtitleAvailable
                        && (subtitleController.loaded || subtitleController.hasVideo)
                    onClicked: {
                        if (root.subtitleAvailable)
                            subtitleController.clear();
                    }
                }
            }
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
                pageShell: page
            }

            Item {
                Layout.fillHeight: !subtitleCard.loaded
            }
        }

        // FR-2.1: the primary action, transport, exports, progress and cancel
        // live in this pinned dock — OUTSIDE the PageShell — so they never
        // scroll away (the card used to bury the CTA 702 px below the fold at
        // 640×420). conductor/patterns.md:96 is reversed accordingly.
        SubtitleActionDock {
            id: actionDock

            objectName: "subtitleActionDock"
            Layout.fillWidth: true
            Layout.maximumWidth: root.pageMaxWidth
            Layout.alignment: Qt.AlignHCenter
            visible: root.subtitleAvailable
                && (subtitleController.loaded || subtitleController.hasVideo)
            compact: root.height < 560
        }
    }

    // FR-1.3: workspaces were previously unreachable (clear()/list_projects()/
    // remove() had no callers), so dubs accumulated forever. The picker is the
    // only surface that closes, reopens and deletes them.
    property string pendingDeleteProjectId: ""

    Dialog {
        id: projectsDialog

        objectName: "subtitleProjectsDialog"
        modal: true
        focus: true
        anchors.centerIn: Overlay.overlay
        width: Math.min((Overlay.overlay ? Overlay.overlay.width : 560) - Theme.spacingXl * 2, 560)
        padding: Theme.spacingLg

        header: Label {
            text: qsTr("Dự án phụ đề đã lưu")
            color: Theme.text
            font.family: Theme.fontFamily
            font.pixelSize: Theme.fontSizeMd
            font.weight: Theme.fontWeightHeading
            leftPadding: Theme.spacingLg
            rightPadding: Theme.spacingLg
            topPadding: Theme.spacingLg
        }

        background: Rectangle {
            radius: Theme.radiusLg
            color: Theme.surfaceCard
            border.color: Theme.border
            border.width: 1
        }

        contentItem: ColumnLayout {
            spacing: Theme.spacingSm

            Label {
                objectName: "subtitleProjectsEmpty"
                Layout.fillWidth: true
                visible: root.subtitleAvailable && subtitleController.projectCount === 0
                text: qsTr("Chưa có dự án nào. Nhập một tệp .srt hoặc video để bắt đầu.")
                color: Theme.textMuted
                font.family: Theme.fontFamily
                font.pixelSize: Theme.fontSizeBase
                wrapMode: Text.Wrap
            }

            Repeater {
                model: root.subtitleAvailable ? subtitleController.projects : []

                delegate: RowLayout {
                    required property var modelData

                    objectName: "subtitleProjectRow"
                    Layout.fillWidth: true
                    spacing: Theme.spacingSm

                    ColumnLayout {
                        Layout.fillWidth: true
                        spacing: 1

                        Label {
                            Layout.fillWidth: true
                            text: modelData.title || modelData.id
                            color: Theme.text
                            font.family: Theme.fontFamily
                            font.pixelSize: Theme.fontSizeBase
                            font.weight: modelData.active ? Theme.fontWeightMedium
                                : Theme.fontWeightRegular
                            elide: Text.ElideRight
                        }

                        Label {
                            Layout.fillWidth: true
                            text: qsTr("%1 phụ đề · %2").arg(modelData.cueCount).arg(
                                modelData.rendered ? qsTr("đã tạo âm thanh")
                                    : qsTr("chưa tạo âm thanh"))
                                + (modelData.modifiedAt ? " · " + modelData.modifiedAt.slice(0, 10) : "")
                            color: Theme.textMuted
                            font.family: Theme.fontFamily
                            font.pixelSize: Theme.fontSizeXs
                        }
                    }

                    AppButton {
                        objectName: "subtitleOpenProjectButton"
                        variant: modelData.active ? "secondary" : "quiet"
                        size: "sm"
                        text: modelData.active ? qsTr("Đang mở") : qsTr("Mở")
                        enabled: !modelData.active
                        onClicked: {
                            if (root.subtitleAvailable
                                    && subtitleController.openProject(modelData.id))
                                projectsDialog.close();
                        }
                    }

                    AppIconButton {
                        objectName: "subtitleDeleteProjectButton"
                        size: "sm"
                        iconKind: "close"
                        tooltipText: qsTr("Xóa dự án này")
                        accessibleLabel: tooltipText
                        onClicked: {
                            root.pendingDeleteProjectId = modelData.id;
                            deleteProjectDialog.title = qsTr("Xóa '%1'?").arg(
                                modelData.title || modelData.id);
                            deleteProjectDialog.open();
                        }
                    }
                }
            }

            RowLayout {
                Layout.fillWidth: true
                spacing: Theme.spacingSm

                Item { Layout.fillWidth: true }

                AppButton {
                    objectName: "subtitleCloseProjectsButton"
                    variant: "quiet"
                    text: qsTr("Đóng")
                    onClicked: projectsDialog.close()
                }
            }
        }
    }

    RemoveConfirmDialog {
        id: deleteProjectDialog

        objectName: "subtitleDeleteProjectDialog"
        body: qsTr("Dự án và bản lồng tiếng đã tạo sẽ bị xóa khỏi máy. Không thể hoàn tác.")
        confirmLabel: qsTr("Xóa dự án")
        confirmObjectName: "subtitleDeleteProjectConfirmButton"
        onConfirmed: {
            if (root.subtitleAvailable && root.pendingDeleteProjectId !== "") {
                subtitleController.removeProject(root.pendingDeleteProjectId);
                root.pendingDeleteProjectId = "";
            }
        }
    }
}
