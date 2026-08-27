import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import ".."

// Standard scrollable page scaffold: a centered content column with a max
// reading width, so wide windows get margins instead of stretched rows.
// Hosts keep their own Pane padding; `contentPadding` adds extra horizontal
// inset on top of the centered column (default 0).
// Default property: page content (laid out vertically with pageSpacing).
Item {
    id: root

    property int maxWidth: 840
    property int pageSpacing: Theme.spacingLg
    property int contentPadding: 0

    default property alias content: column.data

    ScrollView {
        id: scrollView
        anchors.fill: parent
        contentWidth: availableWidth
        clip: true

        ScrollBar.vertical: ScrollBar {
            implicitWidth: 8
            contentItem: Rectangle {
                radius: 4
                color: Theme.border
                opacity: 0.7
            }
            background: Rectangle {
                radius: 4
                color: "transparent"
            }
        }

        ColumnLayout {
            id: column
            width: Math.max(1, Math.min(root.maxWidth, scrollView.availableWidth - root.contentPadding * 2))
            anchors.horizontalCenter: parent.horizontalCenter
            spacing: root.pageSpacing
        }
    }
}
