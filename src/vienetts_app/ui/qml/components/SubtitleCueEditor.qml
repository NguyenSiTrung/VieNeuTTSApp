import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import ".."

// The cue list (FR-2.5/4.5): row-level model, keyboard navigation that seeks,
// follow-the-active-cue scrolling and inline text/speaker editing. The host
// owns sizing, the stage gate and every busy reason.
ListView {
    id: root

    property bool shown: false
    property bool available: false
    property bool busy: false
    property bool aiBusy: false
    property bool exporting: false
    property string disabledReason: ""

    objectName: "subtitleCueList"
    visible: root.shown
    clip: true
    focus: true
    activeFocusOnTab: true
    Accessible.role: Accessible.List
    Accessible.name: qsTr("Danh sách phụ đề")
    Keys.onPressed: (event) => {
        if (event.key !== Qt.Key_Up && event.key !== Qt.Key_Down)
            return;
        const delta = event.key === Qt.Key_Down ? 1 : -1;
        const next = Math.max(0, Math.min(root.count - 1,
            root.currentIndex + delta));
        if (next !== root.currentIndex) {
            root.currentIndex = next;
            if (root.available)
                subtitleController.seekToCue(next);
        }
        event.accepted = true;
    }
    model: root.available ? subtitleController.cueModel : null
    boundsBehavior: Flickable.StopAtBounds
    spacing: Theme.spacingXs

    ScrollBar.vertical: ScrollBar {
        objectName: "subtitleCueScrollBar"
        policy: ScrollBar.AsNeeded
    }

    delegate: Rectangle {
        id: cueRowRoot

        required property var modelData
        required property int index
        property bool editing: false

        readonly property bool active: root.available
            && index === subtitleController.activeCue

        objectName: "subtitleCueRow"
        width: root.width - (root.ScrollBar.vertical && root.ScrollBar.vertical.visible ? 8 : 0)
        implicitHeight: rowLayout.implicitHeight + 6
        radius: Theme.radiusSm
        color: active ? Theme.accentSubtle : "transparent"
        border.width: 1
        border.color: active ? Theme.accent : Theme.borderSubtle

        MouseArea {
            anchors.fill: parent
            cursorShape: Qt.PointingHandCursor
            onClicked: {
                if (root.available)
                    subtitleController.seekToCue(index);
            }
        }

        ColumnLayout {
            id: rowLayout

            anchors.fill: parent
            anchors.leftMargin: Theme.spacingSm + 2
            anchors.rightMargin: Theme.spacingXs
            anchors.topMargin: 3
            anchors.bottomMargin: 3
            spacing: Theme.spacingXs

            RowLayout {
                Layout.fillWidth: true
                spacing: Theme.spacingXs

                ColumnLayout {
                    Layout.fillWidth: true
                    Layout.alignment: Qt.AlignVCenter
                    spacing: 2

                    RowLayout {
                        Layout.fillWidth: true
                        spacing: Theme.spacingXs

                        Label {
                            text: (modelData.index !== undefined ? modelData.index : (index + 1)) + "."
                            color: Theme.textMuted
                            font.family: Theme.fontFamily
                            font.pixelSize: Theme.fontSizeXs
                            font.weight: Theme.fontWeightMedium
                        }

                        Label {
                            text: modelData.startLabel + " → " + modelData.endLabel
                            color: active ? Theme.accent : Theme.textMuted
                            font.family: Theme.fontFamilyMono !== "" ? Theme.fontFamilyMono : Theme.fontFamily
                            font.pixelSize: Theme.fontSizeXs
                        }

                        StatusBadge {
                            objectName: "subtitleCueSpeakerBadge"
                            visible: Boolean(modelData.speaker)
                            status: "info"
                            dotVisible: false
                            text: modelData.speaker || ""
                            customPadding: Theme.spacingXs
                        }

                        StatusBadge {
                            objectName: "subtitleCueFitBadge"
                            visible: Boolean(
                                (modelData.overflowMs && modelData.overflowMs > 0)
                                || (modelData.fitRate && modelData.fitRate > 1.02)
                                || modelData.overBudget
                            )
                            status: ((modelData.overflowMs && modelData.overflowMs > 0)
                                || modelData.overBudget) ? "warning" : "info"
                            dotVisible: false
                            text: (modelData.overflowMs && modelData.overflowMs > 0)
                                ? ("+" + modelData.overflowMs + " ms")
                                : (modelData.fitRate && modelData.fitRate > 1.02)
                                    ? (Number(modelData.fitRate).toFixed(2) + "×")
                                    : qsTr("Dài")
                            customPadding: Theme.spacingXs
                        }

                        Item { Layout.fillWidth: true }
                    }

                    Label {
                        Layout.fillWidth: true
                        visible: !cueRowRoot.editing
                        text: modelData.text
                        color: Theme.text
                        font.family: Theme.fontFamily
                        font.pixelSize: Theme.fontSizeSm
                        wrapMode: Text.WordWrap
                    }
                }

                AppIconButton {
                    objectName: "subtitleCueEditButton"
                    Layout.alignment: Qt.AlignVCenter
                    size: "sm"
                    iconKind: cueRowRoot.editing ? "close" : "edit"
                    enabled: root.available && !root.busy && !root.aiBusy && !root.exporting
                    disabledReason: root.disabledReason
                    tooltipText: qsTr("Chỉnh sửa lời thoại phụ đề")
                    accessibleLabel: tooltipText
                    onClicked: {
                        cueRowRoot.editing = !cueRowRoot.editing;
                    }
                }
            }

            RowLayout {
                Layout.fillWidth: true
                visible: cueRowRoot.editing
                spacing: Theme.spacingXs

                AppTextField {
                    id: cueEditTextField

                    objectName: "subtitleCueEditField"
                    Layout.fillWidth: true
                    accessibleLabel: qsTr("Lời thoại phụ đề")
                    text: modelData.text || ""
                    onAccepted: {
                        if (root.available && subtitleController.updateCue(
                                index, cueEditTextField.text, cueEditSpkField.text))
                            cueRowRoot.editing = false;
                    }
                }

                AppTextField {
                    id: cueEditSpkField

                    objectName: "subtitleCueEditSpeakerField"
                    Layout.preferredWidth: 110
                    accessibleLabel: qsTr("Người nói của phụ đề")
                    text: modelData.speaker || ""
                    placeholderText: qsTr("Người nói")
                }

                AppButton {
                    objectName: "subtitleCueSaveEditButton"
                    variant: "secondary"
                    size: "sm"
                    text: qsTr("Lưu")
                    onClicked: {
                        if (root.available && subtitleController.updateCue(
                                index, cueEditTextField.text, cueEditSpkField.text))
                            cueRowRoot.editing = false;
                    }
                }
            }
        }
    }

    // Follow the active cue during playback; activeCueChanged fires only on
    // transitions, so the user's scroll is never reset by word-level updates
    // and the model is never rebuilt.
    Connections {
        target: root.available ? subtitleController : null

        function onActiveCueChanged() {
            if (root.available && subtitleController.activeCue >= 0)
                root.positionViewAtIndex(
                    subtitleController.activeCue, ListView.Contain);
        }
    }
}
