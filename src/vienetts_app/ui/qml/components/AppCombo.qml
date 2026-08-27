import QtQuick
import QtQuick.Controls
import ".."

// Styled value ComboBox for settings rows (backend/precision/theme…).
// Hosts pass model/currentIndex/onActivated; openPopup()/closePopup() are the
// tested seams (tests emit `activated` or drive the popup through them).
ComboBox {
    id: root

    property int comboWidth: 260

    implicitWidth: comboWidth
    implicitHeight: 38

    function openPopup() { popup.open(); }
    function closePopup() { popup.close(); }

    contentItem: Label {
        leftPadding: Theme.spacingMd
        rightPadding: Theme.spacingMd
        text: root.displayText
        font.family: Theme.fontFamily
        font.pixelSize: Theme.fontSizeBase
        color: Theme.text
        verticalAlignment: Text.AlignVCenter
        elide: Text.ElideRight
    }

    background: Rectangle {
        implicitHeight: 38
        radius: Theme.radiusSm
        color: Theme.surface
        border.color: root.activeFocus ? Theme.accent : Theme.borderSubtle
        border.width: 1
        Behavior on border.color { ColorAnimation { duration: Theme.durationFast } }
    }

    delegate: ItemDelegate {
        id: row

        required property var modelData
        required property int index

        width: root.width
        height: 36
        highlighted: root.highlightedIndex === row.index

        contentItem: Label {
            leftPadding: Theme.spacingMd
            rightPadding: Theme.spacingMd
            text: row.modelData && row.modelData[root.textRole] !== undefined
                ? row.modelData[root.textRole] : ""
            color: row.highlighted ? Theme.accent : Theme.text
            font.family: Theme.fontFamily
            font.pixelSize: Theme.fontSizeBase
            verticalAlignment: Text.AlignVCenter
            elide: Text.ElideRight
        }

        background: Rectangle {
            color: row.highlighted ? Theme.accentSubtle : "transparent"
        }
    }
}
