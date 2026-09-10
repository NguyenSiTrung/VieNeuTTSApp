import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import ".."

// Segmented mode switch: one neutral track with the active segment filled in the
// accent colour. Used by the Paragraph studio to split the single-document
// editor from the multi-file queue — two workflows that previously competed for
// the same scrolling column, pushing the primary action below the fold.
//
// `model` rows are {id, label, icon}; the OWNER keeps `currentId` (this control
// only reports `activated(id)`), so mode state never forks.
Item {
    id: root

    property var model: []
    property string currentId: ""
    property string accessibleLabel: ""

    signal activated(string id)

    readonly property int segmentPadding: Theme.spacingXs

    // Width comes from the segments' own preferred sizes: a fill-parent child
    // reports 0, which collapsed the switch onto its neighbours.
    implicitWidth: trackRow.implicitWidth + root.segmentPadding * 2
    implicitHeight: Theme.controlHeightMd
    Accessible.name: accessibleLabel

    Rectangle {
        id: track

        anchors.fill: parent
        radius: Theme.radiusMd
        color: Theme.surfaceAlt
        border.width: 1
        border.color: Theme.borderSubtle

        RowLayout {
            id: trackRow

            // Anchored left/top/bottom but NOT right: a fill-parent layout
            // reports no implicit width, which collapsed the whole switch.
            anchors.top: parent.top
            anchors.bottom: parent.bottom
            anchors.left: parent.left
            anchors.leftMargin: root.segmentPadding
            spacing: Theme.spacingXs

            Repeater {
                model: root.model

                delegate: Button {
                    id: segment

                    required property var modelData

                    objectName: "modeTab_" + modelData.id
                    Layout.fillHeight: true
                    Layout.minimumWidth: 104
                    // Side padding stays compact so the switch never crowds the
                    // page header's trailing slot on narrow windows.
                    leftPadding: Theme.spacingMd
                    rightPadding: Theme.spacingMd
                    topPadding: 0
                    bottomPadding: 0
                    text: modelData.label
                    checkable: true
                    checked: modelData.id === root.currentId
                    focusPolicy: Qt.StrongFocus
                    Accessible.name: modelData.label

                    onClicked: root.activated(modelData.id)

                    contentItem: RowLayout {
                        spacing: Theme.spacingSm

                        AppIcon {
                            visible: segment.modelData.icon !== undefined
                                && segment.modelData.icon !== ""
                            kind: visible ? segment.modelData.icon : "text"
                            iconColor: segment.checked ? Theme.accentText : Theme.textMuted
                            Layout.preferredWidth: 16
                            Layout.preferredHeight: 16
                            opacity: segment.enabled ? 1.0 : 0.6
                        }

                        Label {
                            text: segment.text
                            color: segment.checked ? Theme.accentText : Theme.textMuted
                            font.family: Theme.fontFamily
                            font.pixelSize: Theme.fontSizeSm
                            font.weight: segment.checked
                                ? Theme.fontWeightHeading : Theme.fontWeightMedium
                            verticalAlignment: Text.AlignVCenter
                            elide: Text.ElideRight

                            Behavior on color { ColorAnimation { duration: Theme.durationFast } }
                        }

                        Item { Layout.fillWidth: true }
                    }

                    background: Rectangle {
                        radius: Theme.radiusSm + 2
                        color: segment.checked
                            ? Theme.accent
                            : (segment.hovered ? Theme.surfaceHover : "transparent")
                        border.width: segment._keyboardFocus ? Theme.focusRingWidth : 0
                        border.color: Theme.borderFocus

                        readonly property bool _keyboardFocus: segment.activeFocus
                            && (segment.focusReason === Qt.TabFocusReason
                                || segment.focusReason === Qt.BacktabFocusReason)

                        Behavior on color { ColorAnimation { duration: Theme.durationFast } }
                    }
                }
            }
        }
    }
}
