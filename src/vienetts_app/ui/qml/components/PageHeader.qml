import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import ".."

// Standard page header: ONE compact row (FR-1.4, ui_shell_redesign) — the
// page title at 22 px bold plus an optional trailing slot (counters, status
// chips, page actions). No icon tile and no subtitle line: that chrome crowded
// out content (audit item 4). The row stays within the 56 px header budget.
RowLayout {
    id: root

    property string title: ""
    // Deprecated (audit FR-1.4): no longer rendered. Kept so callers compile
    // unchanged; put information users need into the page body instead.
    property string subtitle: ""
    // Deprecated (audit FR-1.4): the icon tile is gone; kept as a no-op.
    property string iconKind: "text"
    property Item trailing: null

    spacing: Theme.spacingMd

    Label {
        objectName: "pageHeaderTitle"
        Layout.fillWidth: true
        Layout.alignment: Qt.AlignVCenter
        text: root.title
        color: Theme.text
        font.family: Theme.fontFamily
        font.pixelSize: Theme.fontSizeXl
        font.weight: Theme.fontWeightBold
        font.letterSpacing: Theme.trackingTight
        elide: Text.ElideRight
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
