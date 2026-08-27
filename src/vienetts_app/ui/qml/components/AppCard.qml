import QtQuick
import QtQuick.Layouts
import QtQuick.Controls
import ".."

Rectangle {
    id: root

    property string title: ""
    property string subtitle: ""
    property string badgeText: ""
    property color badgeColor: Theme.accentSubtle
    property color badgeTextColor: Theme.accent
    property color cardColor: Theme.surfaceCard
    property color cardBorderColor: Theme.border
    property int cardRadius: Theme.radiusLg
    property int cardPadding: Theme.spacingLg
    property bool showBorder: true
    property Item headerAction: null

    default property alias content: contentColumn.data

    color: cardColor
    radius: cardRadius
    border.width: showBorder ? 1 : 0
    border.color: cardBorderColor

    implicitHeight: mainLayout.implicitHeight + root.cardPadding * 2
    implicitWidth: mainLayout.implicitWidth + root.cardPadding * 2

    // Smooth color animation when switching theme
    Behavior on color { ColorAnimation { duration: 150 } }
    Behavior on border.color { ColorAnimation { duration: 150 } }

    ColumnLayout {
        id: mainLayout
        anchors.fill: parent
        anchors.margins: root.cardPadding
        spacing: Theme.spacingMd

        // Header section (visible when title, subtitle, or headerAction is set)
        RowLayout {
            Layout.fillWidth: true
            visible: root.title !== "" || root.subtitle !== "" || root.headerAction !== null
            spacing: Theme.spacingSm

            ColumnLayout {
                Layout.fillWidth: true
                spacing: Theme.spacingXxs

                RowLayout {
                    spacing: Theme.spacingSm
                    Label {
                        text: root.title
                        color: Theme.text
                        font.family: Theme.fontFamily
                        font.pixelSize: Theme.fontSizeLg
                        font.weight: Theme.fontWeightHeading
                        visible: root.title !== ""
                    }
                    Rectangle {
                        visible: root.badgeText !== ""
                        color: root.badgeColor
                        radius: Theme.radiusPill
                        implicitHeight: 20
                        implicitWidth: badgeLabel.implicitWidth + Theme.spacingMd
                        Label {
                            id: badgeLabel
                            anchors.centerIn: parent
                            text: root.badgeText
                            color: root.badgeTextColor
                            font.family: Theme.fontFamily
                            font.pixelSize: Theme.fontSizeXs
                            font.weight: Theme.fontWeightMedium
                        }
                    }
                }

                Label {
                    text: root.subtitle
                    color: Theme.textMuted
                    font.family: Theme.fontFamily
                    font.pixelSize: Theme.fontSizeSm
                    visible: root.subtitle !== ""
                    wrapMode: Text.Wrap
                    Layout.fillWidth: true
                }
            }

            // Header Action Item Container
            Item {
                id: headerActionContainer
                visible: root.headerAction !== null
                implicitWidth: root.headerAction ? root.headerAction.implicitWidth : 0
                implicitHeight: root.headerAction ? root.headerAction.implicitHeight : 0
                Layout.alignment: Qt.AlignVCenter | Qt.AlignRight

                onChildrenChanged: {
                    if (root.headerAction)
                        root.headerAction.parent = headerActionContainer;
                }

                Component.onCompleted: {
                    if (root.headerAction)
                        root.headerAction.parent = headerActionContainer;
                }
            }
        }

        Rectangle {
            Layout.fillWidth: true
            height: 1
            color: Theme.borderSubtle
            visible: root.title !== "" || root.subtitle !== "" || root.headerAction !== null
        }

        // Inner content slot
        ColumnLayout {
            id: contentColumn
            Layout.fillWidth: true
            spacing: Theme.spacingMd
        }
    }
}
