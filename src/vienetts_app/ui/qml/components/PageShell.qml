import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import ".."

// Standard scrollable page scaffold: a centered content column with a max
// reading width, so wide windows get margins instead of stretched rows.
// Studios pick their own `maxWidth` (wider for editors, narrower for forms).
Item {
    id: root

    property int maxWidth: 840
    property int pageSpacing: Theme.spacingLg
    property int contentPadding: Theme.spacingXl

    default property alias content: column.data

    ScrollView {
        id: scrollView
        anchors.fill: parent
        contentWidth: availableWidth
        clip: true

        ColumnLayout {
            id: column
            width: Math.min(root.maxWidth, scrollView.availableWidth - root.contentPadding * 2)
            anchors.horizontalCenter: parent.horizontalCenter
            spacing: root.pageSpacing
        }
    }
}
