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
    // The scroll viewport's height (hosts size a fold-filling block with it).
    readonly property real viewportHeight: scrollView.availableHeight

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

    // QML has no isAncestorOf(); walk the QObject parents instead.
    function ownsFocusItem(item) {
        let cursor = item;
        while (cursor) {
            if (cursor === root)
                return true;
            cursor = cursor.parent;
        }
        return false;
    }

    // Scroll the smallest amount that brings a focused control fully into view.
    function ensureFocusVisible(item) {
        if (!item || !root.ownsFocusItem(item))
            return;
        const flick = scrollView.contentItem;
        if (!flick)
            return;
        const y = item.mapToItem(flick, 0, 0).y;
        const bottom = y + item.height;
        if (y < flick.contentY)
            flick.contentY = y;
        else if (bottom > flick.contentY + flick.height)
            flick.contentY = Math.min(bottom - flick.height,
                Math.max(0, flick.contentHeight - flick.height));
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

    // FR-4.5: tabbing into a control below the fold scrolls it into view.
    // Active-focus changes arrive on the Window, so the item is mapped into
    // the scroll content and only moved when it sits outside the viewport.
    Connections {
        target: root.Window.window

        function onActiveFocusItemChanged() {
            const item = root.Window.window ? root.Window.window.activeFocusItem : null;
            if (item)
                root.ensureFocusVisible(item);
        }
    }
}
