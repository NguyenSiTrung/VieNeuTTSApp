import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "../components"
import ".."
import "SettingsFilter.js" as Filter

// One "Nâng cao" row (FR-4.2): a full-width header button — title, one-line
// description, the current value and a chevron — over a body that renders
// only while `expanded`. Collapsed by default; owners with their own
// auto-expand rules bind `expanded` and flip their state in
// `onToggleRequested` (the CUDA rows); otherwise the header flips it here.
//
// API
//   title, description, valueText   header copy
//   expanded                        body shown (read/write)
//   toggleObjectName                tested name for the header button
//   keywords, filter, shown         filter terms / bound filter / host gate
//   default property                the body
ColumnLayout {
    id: root

    property string title: ""
    property string description: ""
    property string valueText: ""
    property bool expanded: false
    property string toggleObjectName: ""
    property string keywords: ""
    property string filter: ""
    property bool shown: true
    readonly property bool matches: Filter.matches(root.filter, [root.title, root.keywords])

    signal toggleRequested()

    default property alias body: bodyColumn.data

    // The owner may flip its own state in onToggleRequested (a bound
    // `expanded` then follows); when nobody did, the row flips itself.
    function toggle() {
        const before = root.expanded;
        root.toggleRequested();
        if (root.expanded === before)
            root.expanded = !before;
    }

    Layout.fillWidth: true
    spacing: 0
    visible: root.shown && root.matches

    Rectangle {
        Layout.fillWidth: true
        implicitHeight: 1
        color: Theme.borderSubtle
    }

    AbstractButton {
        id: header

        objectName: root.toggleObjectName
        Layout.fillWidth: true
        implicitHeight: Math.max(56, headerRow.implicitHeight + Theme.spacingSm * 2)
        focusPolicy: Qt.StrongFocus
        hoverEnabled: true
        onClicked: root.toggle()

        Accessible.role: Accessible.Button
        Accessible.name: root.valueText !== "" ? root.title + " · " + root.valueText : root.title
        Accessible.description: root.description

        background: Rectangle {
            radius: Theme.radiusSm
            color: header.hovered ? Theme.surfaceHover : "transparent"
            border.width: header.visualFocus ? Theme.focusRingWidth : 0
            border.color: Theme.accent
        }

        contentItem: RowLayout {
            id: headerRow

            spacing: Theme.spacingMd

            ColumnLayout {
                Layout.fillWidth: true
                Layout.leftMargin: Theme.spacingXs
                spacing: Theme.spacingXxs

                Label {
                    Layout.fillWidth: true
                    text: root.title
                    color: Theme.text
                    font.family: Theme.fontFamily
                    font.pixelSize: Theme.fontSizeBase
                    font.weight: Theme.fontWeightMedium
                    wrapMode: Text.Wrap
                }

                Label {
                    Layout.fillWidth: true
                    visible: root.description !== ""
                    text: root.description
                    color: Theme.textMuted
                    font.family: Theme.fontFamily
                    font.pixelSize: Theme.fontSizeXs
                    wrapMode: Text.Wrap
                }
            }

            Label {
                visible: root.valueText !== ""
                Layout.maximumWidth: 180
                text: root.valueText
                color: Theme.textMuted
                font.family: Theme.fontFamily
                font.pixelSize: Theme.fontSizeSm
                elide: Text.ElideRight
            }

            AppIcon {
                Layout.rightMargin: Theme.spacingXs
                width: 16
                height: 16
                kind: root.expanded ? "chevronUp" : "chevronDown"
                iconColor: Theme.textMuted
            }
        }
    }

    ColumnLayout {
        id: bodyColumn

        Layout.fillWidth: true
        Layout.topMargin: Theme.spacingXs
        Layout.bottomMargin: Theme.spacingMd
        Layout.leftMargin: Theme.spacingXs
        Layout.rightMargin: Theme.spacingXs
        visible: root.expanded
        spacing: Theme.spacingMd
    }
}
