import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import ".."

// Standard page scaffold header: tinted icon tile + title + subtitle + trailing
// slot. Every studio tab opens with this so page rhythm is identical across the
// app (Text/Paragraph previously had no icon tile while Cloning/Settings did).
RowLayout {
    id: root

    property string title: ""
    property string subtitle: ""
    property string iconKind: "text"
    property Item trailing: null

    spacing: Theme.spacingMd

    // Icon tile
    Rectangle {
        width: 42
        height: 42
        radius: Theme.radiusMd
        color: Theme.accentSubtle
        border.color: Theme.borderSubtle
        border.width: 1
        Layout.alignment: Qt.AlignTop

        AppIcon {
            anchors.centerIn: parent
            kind: root.iconKind
            iconColor: Theme.accent
        }
    }

    ColumnLayout {
        Layout.fillWidth: true
        spacing: 2

        Label {
            text: root.title
            color: Theme.text
            font.family: Theme.fontFamily
            font.pixelSize: Theme.fontSizeXl
            font.weight: Theme.fontWeightHeading
            font.letterSpacing: Theme.trackingTight
        }

        Label {
            Layout.fillWidth: true
            text: root.subtitle
            color: Theme.textMuted
            font.family: Theme.fontFamily
            font.pixelSize: Theme.fontSizeSm
            // Wrap, never truncate — a clipped subtitle is worse than none.
            wrapMode: Text.Wrap
            lineHeight: 1.25
            visible: root.subtitle !== ""
        }
    }

    // Optional trailing slot (metrics chip, status badge, …)
    Item {
        visible: root.trailing !== null
        implicitWidth: root.trailing ? root.trailing.implicitWidth : 0
        implicitHeight: root.trailing ? root.trailing.implicitHeight : 0
        Layout.alignment: Qt.AlignVCenter | Qt.AlignRight

        onChildrenChanged: if (root.trailing) root.trailing.parent = this
        Component.onCompleted: if (root.trailing) root.trailing.parent = this
    }
}
