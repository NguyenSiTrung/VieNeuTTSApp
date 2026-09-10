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
    // Opt-in: let the content column grow to the viewport height so a page with
    // a single fill-height child (the Paragraph tab's queue mode) uses the space
    // instead of leaving a void above the docked bar. Off by default — the other
    // studios rely on intrinsic content height.
    property bool stretch: false

    default property alias content: column.data

    ScrollView {
        id: scrollView
        objectName: "pageScrollView"
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
            height: root.stretch
                ? Math.max(implicitHeight, scrollView.availableHeight)
                : implicitHeight
            anchors.horizontalCenter: parent.horizontalCenter
            spacing: root.pageSpacing
        }
    }
}
