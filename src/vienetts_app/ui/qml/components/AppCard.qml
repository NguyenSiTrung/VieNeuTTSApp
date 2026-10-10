import QtQuick
import QtQuick.Effects
import QtQuick.Layouts
import QtQuick.Controls
import ".."

// Standard elevated card. elevation 1 (default) renders a soft tinted shadow so
// cards read as raised surfaces in BOTH themes (light mode previously had no
// depth at all); elevation 0 is a flat bordered card for nested use. The
// header is a single quiet row: title, optional badge, optional headerAction.
Rectangle {
    id: root

    property string title: ""
    // Deprecated (audit FR-1.4): no longer rendered. Kept so callers compile
    // unchanged (and SubtitleCard's `subtitle` name shadowing keeps working);
    // information users need belongs in the card body or the badge.
    property string subtitle: ""
    property string badgeText: ""
    property color badgeColor: Theme.accentSubtle
    property color badgeTextColor: Theme.accent
    property color cardColor: Theme.surfaceCard
    property color cardBorderColor: Theme.border
    property int cardRadius: Theme.radiusLg
    property int cardPadding: Theme.spacingLg
    property bool showBorder: true
    property int elevation: 1
    property Item headerAction: null
    // Whole-card tap target (guide/navigation cards). The MouseArea sits UNDER
    // the content so inner controls still get their own clicks; `cardHovered`
    // lets hosts tint the surface on hover.
    property bool clickable: false
    readonly property bool cardHovered: cardHoverHandler.hovered
    signal cardClicked()

    default property alias content: contentColumn.data

    color: cardColor
    radius: cardRadius
    border.width: showBorder ? 1 : 0
    border.color: cardBorderColor

    implicitHeight: mainLayout.implicitHeight + root.cardPadding * 2
    implicitWidth: mainLayout.implicitWidth + root.cardPadding * 2

    // Smooth color animation when switching theme
    Behavior on color { ColorAnimation { duration: Theme.durationBase } }
    Behavior on border.color { ColorAnimation { duration: Theme.durationBase } }

    // --- Elevation shadow ---
    // Analytic: one shader draws the blurred rounded rectangle, with no
    // hidden source item and no offscreen blur pass per card (the old
    // multi-pass effect cost a texture + blur for each of ~30 cards). It sits
    // under the card surface (z: -1). spread -2 keeps the old 2 px inset;
    // blur ~ the old 0.6 x 32 px effect radius.
    RectangularShadow {
        objectName: "cardShadow"
        anchors.fill: parent
        z: -1
        visible: root.elevation > 0
        radius: root.cardRadius
        color: root.elevation > 1 ? Theme.shadowColor : Theme.shadowSubtle
        blur: 18
        spread: -2
        offset.y: root.elevation > 1 ? 4 : 2
    }

    HoverHandler {
        id: cardHoverHandler
        enabled: root.clickable
        cursorShape: Qt.PointingHandCursor
    }

    // Declared before the content layout so it renders/receives UNDER it —
    // buttons inside a clickable card keep working, everything else taps card.
    MouseArea {
        anchors.fill: parent
        enabled: root.clickable
        onClicked: root.cardClicked()
    }

    ColumnLayout {
        id: mainLayout
        anchors.fill: parent
        anchors.margins: root.cardPadding
        spacing: Theme.spacingMd

        // Header row (visible when a title, badge or headerAction is set). No
        // subtitle line and no divider under it (FR-1.4): spacing alone
        // separates the header from the content.
        RowLayout {
            Layout.fillWidth: true
            visible: root.title !== "" || root.badgeText !== "" || root.headerAction !== null
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

        // Inner content slot. fillHeight so hosts can pin a trailing control
        // (e.g. a guide card's CTA) to the bottom of a stretched card with a
        // plain spacer Item — cards without a spacer are visually unchanged.
        ColumnLayout {
            id: contentColumn
            Layout.fillWidth: true
            Layout.fillHeight: true
            spacing: Theme.spacingMd
        }
    }
}
