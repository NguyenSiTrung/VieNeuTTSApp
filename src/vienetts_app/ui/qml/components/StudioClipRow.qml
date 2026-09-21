import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import ".."

// One clip row in the Studio list. Line 1 is the control bar (number badge,
// audition, duration, reorder, regen, delete); line 2 is the clip's text at
// full card width — a single packed row left the excerpt ~78 px at the 640 px
// minimum and it wrapped mid-word.
//
// Repeater delegate: modelData/index are required on the root per this
// project's delegate contract. The host owns the regen dialog (it carries
// clip id/label/text back up through regenRequested); everything else talks
// to the controller directly.
Rectangle {
    id: rowRoot

    required property var modelData
    required property int index

    property int clipsCount: 0
    property string auditionClipId: ""
    property bool showDuration: true

    signal regenRequested(var clipData, int clipIndex)

    readonly property bool isRegenerating: (typeof controller.studioRegenClipId !== "undefined")
        && controller.studioRegenClipId !== ""
        && controller.studioRegenClipId === modelData.id
    readonly property bool isAuditioning: rowRoot.auditionClipId === modelData.id
    readonly property bool highlighted: isRegenerating || isAuditioning
    readonly property bool rackEnabled: controller.hasStudioProject
        && !controller.busy && controller.studioBusy !== true

    implicitHeight: rowLayout.implicitHeight + Theme.spacingMd * 2
    radius: Theme.radiusMd
    // Hover affordance on the whole row — previously only the audition/regen
    // highlight changed the surface, so rows read as static text blocks.
    color: highlighted ? Theme.accentSubtle
        : (rowHover.hovered ? Theme.surfaceHover : Theme.surfaceAlt)
    border.color: highlighted ? Theme.accent : Theme.borderSubtle
    border.width: highlighted ? 1.5 : 1

    HoverHandler { id: rowHover }

    Behavior on color { ColorAnimation { duration: Theme.durationFast } }
    Behavior on border.color { ColorAnimation { duration: Theme.durationFast } }

    ColumnLayout {
        id: rowLayout
        anchors.fill: parent
        anchors.margins: Theme.spacingMd
        spacing: Theme.spacingSm

        RowLayout {
            Layout.fillWidth: true
            spacing: Theme.spacingSm

            // Clip number badge
            Rectangle {
                Layout.preferredWidth: 32
                Layout.preferredHeight: 24
                Layout.alignment: Qt.AlignVCenter
                radius: Theme.radiusSm
                color: rowRoot.isRegenerating ? Theme.accent : Theme.accentSubtle

                Label {
                    anchors.centerIn: parent
                    text: "#" + (rowRoot.index + 1)
                    color: rowRoot.isRegenerating ? Theme.accentText : Theme.accent
                    font.family: Theme.fontFamily
                    font.pixelSize: Theme.fontSizeXs
                    font.bold: true
                }
            }

            // Audition this clip. The icon and the accessible name both follow
            // the controller's audition state, so the row never claims to be
            // playing a clip the transport has already moved off.
            AppButton {
                objectName: "studioClipPlayButton"
                variant: "icon"
                size: "sm"
                iconKind: rowRoot.isAuditioning && !controller.replayPaused ? "pause" : "play"
                accessibleLabel: rowRoot.isAuditioning
                    ? qsTr("Dừng nghe đoạn %1").arg(rowRoot.index + 1)
                    : qsTr("Nghe thử đoạn %1").arg(rowRoot.index + 1)
                tooltipText: accessibleLabel
                enabled: controller.hasStudioProject && !controller.busy
                onClicked: {
                    if (rowRoot.isAuditioning)
                        controller.stopReplay();
                    else
                        controller.studioPreviewClip(modelData.id);
                }
            }

            // Duration pill — the host drops it at the 640 px minimum, where
            // the control bar needs the room.
            Rectangle {
                Layout.preferredWidth: 48
                Layout.preferredHeight: 24
                Layout.alignment: Qt.AlignVCenter
                radius: Theme.radiusSm
                color: Theme.surfaceCard
                border.color: Theme.borderSubtle
                border.width: 1
                visible: rowRoot.showDuration && Boolean(modelData.duration_str)

                Label {
                    anchors.centerIn: parent
                    text: modelData.duration_str || ""
                    color: Theme.textMuted
                    font.family: Theme.fontFamily
                    font.pixelSize: Theme.fontSizeXs
                }
            }

            Item { Layout.fillWidth: true }

            // Reorder actions (only visible when > 1 clip)
            AppButton {
                variant: "icon"
                size: "sm"
                iconKind: "chevronUp"
                accessibleLabel: qsTr("Chuyển lên")
                tooltipText: qsTr("Chuyển đoạn này lên trước")
                visible: rowRoot.clipsCount > 1
                enabled: rowRoot.rackEnabled && rowRoot.index > 0
                onClicked: controller.studioMoveClip(modelData.id, rowRoot.index - 1)
            }

            AppButton {
                variant: "icon"
                size: "sm"
                iconKind: "chevronDown"
                accessibleLabel: qsTr("Chuyển xuống")
                tooltipText: qsTr("Chuyển đoạn này xuống sau")
                visible: rowRoot.clipsCount > 1
                enabled: rowRoot.rackEnabled && rowRoot.index < rowRoot.clipsCount - 1
                onClicked: controller.studioMoveClip(modelData.id, rowRoot.index + 1)
            }

            // Regenerate button
            AppButton {
                objectName: "studioRegenButton"
                variant: rowRoot.isRegenerating ? "primary" : "secondary"
                size: "sm"
                iconKind: rowRoot.isRegenerating ? "spinner" : "refresh"
                text: rowRoot.isRegenerating ? qsTr("Đang tạo lại…") : qsTr("Tạo lại…")
                busy: rowRoot.isRegenerating
                enabled: controller.hasStudioProject && !controller.busy
                onClicked: rowRoot.regenRequested(modelData, rowRoot.index)
            }

            // Drop the clip. Never the last one — a project with no clips
            // cannot render.
            AppButton {
                objectName: "studioDeleteClipButton"
                variant: "icon"
                size: "sm"
                iconKind: "close"
                accessibleLabel: qsTr("Xoá đoạn %1").arg(rowRoot.index + 1)
                tooltipText: rowRoot.clipsCount > 1
                    ? qsTr("Bỏ đoạn này khỏi bản trộn")
                    : qsTr("Không thể bỏ đoạn cuối cùng của dự án")
                enabled: rowRoot.rackEnabled && rowRoot.clipsCount > 1
                onClicked: controller.studioDeleteClip(modelData.id)
            }
        }

        // Text excerpt — full card width, so the line count is the card's,
        // not the control bar's leftover.
        Label {
            Layout.fillWidth: true
            text: modelData.text || modelData.label || ""
            color: Theme.text
            font.family: Theme.fontFamily
            font.pixelSize: Theme.fontSizeSm
            elide: Text.ElideRight
            maximumLineCount: 2
            wrapMode: Text.Wrap
        }

        // Provenance (Task 6.3): which engine produced this clip's audio, and
        // in which language. A clip with no recorded identity predates engine
        // provenance and is VieNeu's (the same legacy rule the caches use), so
        // the row says so rather than pretending the engine is unknown.
        RowLayout {
            Layout.fillWidth: true
            spacing: Theme.spacingSm

            Label {
                objectName: "studioClipProfile"
                text: qsTr("Hồ sơ: %1").arg(modelData.profileLabel || qsTr("VieNeu-TTS (bản cũ)"))
                color: Theme.textSubtle
                font.family: Theme.fontFamily
                font.pixelSize: Theme.fontSizeXs
            }

            Label {
                objectName: "studioClipLanguage"
                visible: Boolean(modelData.language)
                text: qsTr("Ngôn ngữ: %1").arg(modelData.language || "")
                color: Theme.textSubtle
                font.family: Theme.fontFamily
                font.pixelSize: Theme.fontSizeXs
            }
        }
    }
}
