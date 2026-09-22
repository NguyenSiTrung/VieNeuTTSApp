import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import ".."

// Standard scrollable page scaffold: a centered content column with a max
// reading width, so wide windows get margins instead of stretched rows.
// Hosts keep their own Pane padding; `contentPadding` adds extra horizontal
// inset on top of the centered column (default 0).
// `headerComponent` (optional) pins a bar ABOVE the scroll area — sticky
// content that must never scroll away (Settings' section nav). It is centered
// to the same max reading width as the content column.
// Default property: page content (laid out vertically with pageSpacing).
Item {
    id: root

    property int maxWidth: 840
    property int pageSpacing: Theme.spacingLg
    property int contentPadding: 0
    property Component headerComponent: null
    // Opt-in: let the content column grow to the viewport height so a page with
    // a single fill-height child (the Paragraph tab's queue mode) uses the space
    // instead of leaving a void above the docked bar. Off by default — the other
    // studios rely on intrinsic content height.
    property bool stretch: false
    property bool reserveVerticalScrollBar: false

    default property alias content: column.data

    // Reactive read of the scroll offset for scroll-spy bindings (the offset
    // lives on ScrollView's internal Flickable; ScrollView has no contentY).
    readonly property real scrollContentY: scrollView.contentItem
        ? scrollView.contentItem.contentY : 0

    // Mode switches swap the whole page content: the new mode must start at
    // the top instead of inheriting the old scroll offset (which pushed the
    // header and the mode switch itself out of place on every switch).
    function scrollToTop() {
        // The offset lives on ScrollView's internal Flickable (contentItem);
        // ScrollView itself exposes no contentY.
        scrollView.contentItem.contentY = 0;
    }

    // Jump to a content-column coordinate (section anchors). Clamped to the
    // scrollable range; a no-op when the content fits the viewport.
    function scrollToContentY(y) {
        const flick = scrollView.contentItem;
        if (!flick)
            return;
        flick.contentY = Math.max(0, Math.min(y, flick.contentHeight - flick.height));
    }

    ColumnLayout {
        anchors.fill: parent
        spacing: 0

        Loader {
            id: headerLoader
            active: root.headerComponent !== null
            sourceComponent: root.headerComponent
            visible: active && item !== null
            Layout.fillWidth: true
            Layout.maximumWidth: root.maxWidth
            Layout.alignment: Qt.AlignHCenter
            // The band is a distinct layer above the page: keep one clear
            // gap under it so section chips never touch the first card.
            Layout.bottomMargin: Theme.spacingXl
        }

        ScrollView {
            id: scrollView
            objectName: "pageScrollView"
            Layout.fillWidth: true
            Layout.fillHeight: true
            contentWidth: availableWidth
            clip: true

            ScrollBar.vertical: ScrollBar {
                objectName: "pageScrollBarV"
                // AlwaysOn, not AsNeeded, when the host reserves the gutter: the
                // bar appearing/disappearing changes availableWidth, which
                // recenters the content column and makes the header + mode switch
                // jump sideways between modes. Opacity keeps the reserved bar
                // invisible while there is nothing to scroll.
                policy: root.reserveVerticalScrollBar ? ScrollBar.AlwaysOn : ScrollBar.AsNeeded
                opacity: size < 1.0 ? 1.0 : 0.0
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
}
