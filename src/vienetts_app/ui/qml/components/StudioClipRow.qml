import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import ".."

// One row of the Studio clip table (FR-4.3): audition, number, the clip's
// text (one elided line) over its provenance, duration, reorder, regen and
// delete. Rows are flat table lines separated by hairlines. The auditioned or
// regenerating row is tinted, matching its block on the timeline.
//
// Repeater delegate: modelData/index are required on the root per this
// project's delegate contract. The host owns the regen dialog (it carries
// clip id/label/text back up through regenRequested); everything else talks
// to the controller directly.
Rectangle {
    id: rowRoot

    objectName: "studioClipRow"

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

    implicitHeight: rowLayout.implicitHeight + Theme.spacingXs * 2
    radius: Theme.radiusSm
    color: highlighted ? Theme.accentSubtle
        : (rowHover.hovered ? Theme.surfaceHover : "transparent")

    HoverHandler { id: rowHover }

    Behavior on color { ColorAnimation { duration: Theme.durationFast } }

    // Hairline under every row: the table reads as one list, not cards.
    Rectangle {
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.bottom: parent.bottom
        height: 1
        color: Theme.borderSubtle
        visible: !rowRoot.highlighted && rowRoot.index < rowRoot.clipsCount - 1
    }

    RowLayout {
        id: rowLayout

        anchors.fill: parent
        anchors.topMargin: Theme.spacingXs
        anchors.bottomMargin: Theme.spacingXs
        anchors.rightMargin: Theme.spacingXs
        spacing: Theme.spacingSm

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

        Label {
            objectName: "studioClipNumber"
            Layout.alignment: Qt.AlignVCenter
            text: String(rowRoot.index + 1).padStart(2, "0")
            color: rowRoot.highlighted ? Theme.accent : Theme.textSubtle
            font.family: Theme.fontFamilyMono !== "" ? Theme.fontFamilyMono : Theme.fontFamily
            font.pixelSize: Theme.fontSizeSm
            font.weight: Theme.fontWeightMedium
        }

        // Text on one line, provenance under it (Task 6.3): which engine
        // produced this clip's audio, and in which language. A clip with no
        // recorded identity predates engine provenance and is VieNeu's (the
        // same legacy rule the caches use), so the row says so rather than
        // pretending the engine is unknown.
        ColumnLayout {
            Layout.fillWidth: true
            Layout.alignment: Qt.AlignVCenter
            spacing: 0

            Label {
                objectName: "studioClipText"
                Layout.fillWidth: true
                text: modelData.text || modelData.label || ""
                color: Theme.text
                font.family: Theme.fontFamily
                font.pixelSize: Theme.fontSizeSm
                elide: Text.ElideRight
                maximumLineCount: 1
            }

            RowLayout {
                Layout.fillWidth: true
                spacing: Theme.spacingSm

                Label {
                    objectName: "studioClipProfile"
                    Layout.maximumWidth: implicitWidth
                    Layout.fillWidth: true
                    text: qsTr("Hồ sơ: %1").arg(modelData.profileLabel || qsTr("VieNeu-TTS (bản cũ)"))
                    color: Theme.textSubtle
                    font.family: Theme.fontFamily
                    font.pixelSize: Theme.fontSizeXs
                    elide: Text.ElideRight
                }

                // Weight-variant provenance for Qwen clips — a GGUF render
                // names its quantization + engine so a clip is never replayed
                // or re-synthesized under a format it doesn't record.
                Label {
                    objectName: "studioClipVariant"
                    visible: Boolean(modelData.variantLabel)
                    text: modelData.variantLabel || ""
                    color: Theme.textSubtle
                    font.family: Theme.fontFamily
                    font.pixelSize: Theme.fontSizeXs
                    elide: Text.ElideRight
                    Layout.maximumWidth: implicitWidth
                    Layout.fillWidth: true
                }

                Label {
                    objectName: "studioClipLanguage"
                    visible: Boolean(modelData.language)
                    text: qsTr("Ngôn ngữ: %1").arg(modelData.language || "")
                    color: Theme.textSubtle
                    font.family: Theme.fontFamily
                    font.pixelSize: Theme.fontSizeXs
                }

                Item { Layout.fillWidth: true }
            }
        }

        // Duration — the host drops it at narrow widths, where the action
        // cluster needs the room.
        Label {
            objectName: "studioClipDuration"
            Layout.alignment: Qt.AlignVCenter
            visible: rowRoot.showDuration && Boolean(modelData.duration_str)
            text: modelData.duration_str || ""
            color: Theme.textMuted
            font.family: Theme.fontFamilyMono !== "" ? Theme.fontFamilyMono : Theme.fontFamily
            font.pixelSize: Theme.fontSizeSm
        }

        // Reorder actions (only with more than one clip)
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

        AppButton {
            objectName: "studioRegenButton"
            variant: "secondary"
            checked: rowRoot.isRegenerating
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
}
