import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import ".."

// Themed menu row: 44 px hit target, 14 px label, optional leading check
// column and a trailing shortcut hint. The label mirrors the control's own
// `text` (text-pin rule), so tests read the MenuItem's `text` directly.
//
// Two check flavours:
//   checkable: true — a real toggle (Qt flips `checked` on click; bind
//                      `checked` strictly and write back in onToggled).
//   markable: true  — one choice of a set (export format): `marked` paints
//                      the check without making the row checkable, so
//                      re-picking the current choice can never untick it.
MenuItem {
    id: root

    property bool markable: false
    property bool marked: false
    property string shortcutText: ""
    property string accessibleLabel: text
    readonly property bool _showCheck: checkable || markable
    readonly property bool _checkOn: checkable ? checked : marked

    implicitHeight: Theme.controlHitTarget
    implicitWidth: Math.max(200, row.implicitWidth + leftPadding + rightPadding)
    leftPadding: Theme.spacingMd
    rightPadding: Theme.spacingMd
    topPadding: 0
    bottomPadding: 0
    spacing: Theme.spacingSm

    indicator: null
    arrow: null

    contentItem: RowLayout {
        id: row

        spacing: Theme.spacingSm

        Item {
            visible: root._showCheck
            Layout.preferredWidth: 16
            Layout.preferredHeight: 16

            AppIcon {
                anchors.fill: parent
                visible: root._checkOn
                kind: "check"
                iconColor: root.enabled ? Theme.accent : Theme.controlDisabledText
            }
        }

        Label {
            Layout.fillWidth: true
            text: root.text
            color: root.enabled ? Theme.text : Theme.controlDisabledText
            font.family: Theme.fontFamily
            font.pixelSize: Theme.fontSizeBase
            font.weight: root._checkOn ? Theme.fontWeightMedium : Theme.fontWeightRegular
            elide: Text.ElideRight
            verticalAlignment: Text.AlignVCenter
        }

        Label {
            visible: root.shortcutText !== ""
            text: root.shortcutText
            color: Theme.textSubtle
            font.family: Theme.fontFamily
            font.pixelSize: Theme.fontSizeXs
            verticalAlignment: Text.AlignVCenter
        }
    }

    background: Rectangle {
        implicitHeight: Theme.controlHitTarget
        radius: Theme.radiusSm
        color: root.enabled && (root.highlighted || root.hovered || root.down)
            ? Theme.surfaceHover : "transparent"
    }

    Accessible.role: markable ? Accessible.RadioButton
        : (checkable ? Accessible.CheckBox : Accessible.MenuItem)
    Accessible.name: root.accessibleLabel
    Accessible.checkable: root._showCheck
    Accessible.checked: root._checkOn
}
