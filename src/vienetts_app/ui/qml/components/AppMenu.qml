import QtQuick
import QtQuick.Controls
import ".."

// Themed Menu (one skin per control): a popup card in the surfacePopup
// colours holding AppMenuItem rows. Static items are created with the Menu
// (so their objectNames resolve while it is closed) but are only laid out
// and visible under the window overlay while it is open. `margins` keeps the
// menu inside the window; hosts set x/y (the TransportDock opens upward).
Menu {
    id: root

    padding: Theme.spacingXs
    margins: Theme.spacingSm
    modal: false
    dim: false

    background: Rectangle {
        implicitWidth: 220
        radius: Theme.radiusLg
        color: Theme.surfacePopup
        border.color: Theme.borderPopup
        border.width: 1

        // Soft outline depth, same treatment as the VoicePicker popup.
        Rectangle {
            anchors.fill: parent
            anchors.margins: -1
            radius: parent.radius + 1
            color: "transparent"
            border.color: Theme.shadowPopup
            border.width: 1
            z: -1
        }
    }
}
