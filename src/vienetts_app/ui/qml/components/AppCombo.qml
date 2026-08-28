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
                iconColor: root.enabled ? Theme.textMuted : Theme.controlDisabledText
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
        radius: Theme.radiusSm
        color: root.enabled ? Theme.surface : Theme.controlDisabledBg
        border.color: root.activeFocus || root.popup.visible ? Theme.accent
            : (root.enabled ? Theme.borderSubtle : Theme.controlDisabledBorder)
        border.width: root.activeFocus || root.popup.visible ? Theme.focusRingWidth : 1
        Behavior on border.color { ColorAnimation { duration: Theme.durationFast } }
    }

    delegate: ItemDelegate {
        id: row

        required property var modelData
        required property int index

        width: root.width
        height: 36
        highlighted: root.highlightedIndex === row.index

        contentItem: Label {
            leftPadding: Theme.spacingMd
            rightPadding: Theme.spacingMd
            text: row.modelData && row.modelData[root.textRole] !== undefined
                ? row.modelData[root.textRole] : ""
            color: row.highlighted ? Theme.accent : Theme.text
            font.family: Theme.fontFamily
            font.pixelSize: Theme.fontSizeBase
            verticalAlignment: Text.AlignVCenter
            elide: Text.ElideRight
        }

        background: Rectangle {
            color: row.highlighted ? Theme.accentSubtle : "transparent"
        }
    }

    Accessible.name: root.accessibleLabel
}
