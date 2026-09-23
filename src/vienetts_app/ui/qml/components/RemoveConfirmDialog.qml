import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import ".."

// Shared remove-confirm dialog (extracted from SettingsTab for Task 5.2).
//
// Removing a managed runtime or checkpoint deletes multi-GB verified
// downloads — the confirm step is the only thing between a misclick and a
// full re-download. `confirmObjectName` is the tested contract: the smoke
// suite clicks the confirm button by name.
Dialog {
    id: confirmDialog

    property string body: ""
    property string confirmLabel: ""
    property string confirmObjectName: ""
    signal confirmed()

    modal: true
    focus: true
    anchors.centerIn: Overlay.overlay
    width: Math.min(
        (Overlay.overlay ? Overlay.overlay.width : 420) - Theme.spacingXl * 2,
        420
    )
    padding: Theme.spacingLg

    // The Basic style's default header paints with the system palette —
    // a white strip over the dark card. Theme it like the body.
    header: Label {
        text: confirmDialog.title
        color: Theme.text
        font.family: Theme.fontFamily
        font.pixelSize: Theme.fontSizeMd
        font.weight: Theme.fontWeightHeading
        wrapMode: Text.Wrap
        leftPadding: Theme.spacingLg
        rightPadding: Theme.spacingLg
        topPadding: Theme.spacingLg
    }

    background: Rectangle {
        radius: Theme.radiusLg
        color: Theme.surfaceCard
        border.color: Theme.border
        border.width: 1
    }

    contentItem: ColumnLayout {
        spacing: Theme.spacingMd

        Label {
            Layout.fillWidth: true
            text: confirmDialog.body
            color: Theme.textMuted
            font.family: Theme.fontFamily
            font.pixelSize: Theme.fontSizeBase
            wrapMode: Text.Wrap
        }

        RowLayout {
            Layout.fillWidth: true
            spacing: Theme.spacingSm

            Item { Layout.fillWidth: true }

            AppButton {
                variant: "quiet"
                text: qsTr("Hủy")
                onClicked: confirmDialog.close()
            }

            AppButton {
                objectName: confirmDialog.confirmObjectName
                variant: "danger"
                text: confirmDialog.confirmLabel
                onClicked: {
                    confirmDialog.close();
                    confirmDialog.confirmed();
                }
            }
        }
    }
}
