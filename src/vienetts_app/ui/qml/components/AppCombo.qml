import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import ".."

// Styled value ComboBox for settings rows (backend/precision/theme…).
// Hosts pass model/currentIndex/onActivated; openPopup()/closePopup() are the
// tested seams (tests emit `activated` or drive the popup through them).
ComboBox {
    id: root

    readonly property string controlKind: "select"
    property int comboWidth: 260
    property string accessibleLabel: displayText

    implicitWidth: comboWidth
    implicitHeight: Theme.controlHeightMd

    function openPopup() { popup.open(); }
    function closePopup() { popup.close(); }

    HoverHandler {
        id: hoverHandler
        cursorShape: root.enabled ? Qt.PointingHandCursor : Qt.ArrowCursor
    }
    indicator: Item {
        visible: false
        width: 0
        height: 0
    }


    contentItem: Item {
        RowLayout {
            anchors.fill: parent
            anchors.leftMargin: Theme.spacingMd
            anchors.rightMargin: Theme.spacingMd
            spacing: Theme.spacingSm

            Label {
                Layout.fillWidth: true
                text: root.displayText
                font.family: Theme.fontFamily
                font.pixelSize: Theme.fontSizeBase
                font.weight: Theme.fontWeightMedium
                color: root.enabled ? Theme.text : Theme.controlDisabledText
                verticalAlignment: Text.AlignVCenter
                elide: Text.ElideRight
            }

            AppIcon {
                width: 16
                height: 16
                kind: "chevronDown"
                iconColor: root.enabled ? (root.popup.visible || root.hovered ? Theme.accent : Theme.textMuted) : Theme.controlDisabledText
                rotation: root.popup.visible ? 180 : 0

                Behavior on rotation {
                    NumberAnimation {
                        duration: Theme.durationFast
                        easing.type: Easing.OutCubic
                    }
                }
            }
        }
    }

    background: Rectangle {
        implicitHeight: Theme.controlHeightMd
        radius: Theme.radiusMd
        color: root.enabled ? (root.popup.visible ? Theme.surfaceAlt : (root.hovered ? Theme.surfaceHover : Theme.surface)) : Theme.controlDisabledBg
        border.color: root.activeFocus || root.popup.visible ? Theme.accent
            : (root.enabled ? (root.hovered ? Theme.border : Theme.borderSubtle) : Theme.controlDisabledBorder)
        border.width: root.activeFocus || root.popup.visible ? Theme.focusRingWidth : 1

        Behavior on color { ColorAnimation { duration: Theme.durationFast } }
        Behavior on border.color { ColorAnimation { duration: Theme.durationFast } }
    }

    popup: Popup {
        id: comboPopup

        y: root.height + Theme.spacingXs
        width: root.width
        implicitHeight: Math.min(Theme.popupMaxHeight, contentItem.implicitHeight + topPadding + bottomPadding)
        padding: Theme.spacingXs
        modal: false
        dim: false
        focus: true
        closePolicy: Popup.CloseOnEscape | Popup.CloseOnPressOutsideParent

        contentItem: ListView {
            clip: true
            implicitHeight: contentHeight
            model: root.delegateModel
            currentIndex: root.highlightedIndex
            highlightMoveDuration: 0

            ScrollBar.vertical: ScrollBar {
                policy: ScrollBar.AsNeeded
                implicitWidth: 6
                contentItem: Rectangle {
                    radius: 3
                    color: Theme.border
                }
            }
        }

        background: Rectangle {
            radius: Theme.radiusMd
            color: Theme.surfacePopup
            border.color: Theme.borderPopup
            border.width: 1

            // Drop shadow / elevation outline
            Rectangle {
                anchors.fill: parent
                anchors.margins: -1
                radius: parent.radius + 1
                color: "transparent"
                border.color: Theme.shadowPopup
                border.width: 1
                z: -1
            }
        }
    }

    delegate: ItemDelegate {
        id: row

        required property var modelData
        required property int index

        width: root.width - (Theme.spacingXs * 2)
        height: 38
        highlighted: root.highlightedIndex === row.index
        hoverEnabled: true

        HoverHandler {
            cursorShape: Qt.PointingHandCursor
        }

        contentItem: RowLayout {
            spacing: Theme.spacingSm

            Label {
                Layout.fillWidth: true
                Layout.leftMargin: Theme.spacingSm
                Layout.rightMargin: Theme.spacingXs
                text: row.modelData && row.modelData[root.textRole] !== undefined
                    ? row.modelData[root.textRole] : ""
                color: row.highlighted ? Theme.accent : Theme.text
                font.family: Theme.fontFamily
                font.pixelSize: Theme.fontSizeBase
                font.weight: (root.currentIndex === row.index || row.highlighted)
                    ? Theme.fontWeightMedium : Theme.fontWeightNormal
                verticalAlignment: Text.AlignVCenter
                elide: Text.ElideRight
            }

            AppIcon {
                Layout.rightMargin: Theme.spacingSm
                width: 14
                height: 14
                kind: "check"
                iconColor: Theme.accent
                visible: root.currentIndex === row.index
            }
        }

        background: Rectangle {
            radius: Theme.radiusSm
            color: row.highlighted ? Theme.accentSubtle : (row.hovered ? Theme.surfaceHover : "transparent")
            Behavior on color { ColorAnimation { duration: Theme.durationFast } }
        }
    }

    Accessible.name: root.accessibleLabel
}
