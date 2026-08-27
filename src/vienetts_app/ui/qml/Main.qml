// Shell window: nav rail + tab content + engine readout (FR-2.3/FR-2.7).
// Shell only — tab content is placeholder (FR-2.6); §8's 3-column wide /
// stacked narrow split is indicative and lands with real tab content later.
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "."

ApplicationWindow {
    id: window

    objectName: "mainWindow"
    visible: true
    width: 1000
    height: 640
    minimumWidth: 640
    minimumHeight: 420
    title: qsTr("VieNeuTTS")
    color: Theme.bg

    RowLayout {
        anchors.fill: parent
        spacing: 0

        // --- Nav rail -------------------------------------------------------
        ColumnLayout {
            objectName: "navBar"
            spacing: Theme.spacingSm
            Layout.preferredWidth: 200
            Layout.fillHeight: true

            Label {
                text: qsTr("VieNeuTTS")
                color: Theme.accent
                font.family: Theme.fontFamily
                font.pixelSize: Theme.fontSizeXl
                font.weight: Theme.fontWeightHeading
                Layout.topMargin: Theme.spacingLg
                Layout.leftMargin: Theme.spacingLg
                Layout.rightMargin: Theme.spacingLg
            }

            Repeater {
                model: bridge.tabs

                Button {
                    id: navButton
                    required property var modelData
                    Layout.fillWidth: true
                    Layout.leftMargin: Theme.spacingSm
                    Layout.rightMargin: Theme.spacingSm
                    flat: true
                    checked: bridge.currentTab === modelData.id
                    onClicked: bridge.setCurrentTab(modelData.id)

                    contentItem: Label {
                        text: navButton.modelData.label
                        color: navButton.checked ? Theme.accent : Theme.textMuted
                        font.family: Theme.fontFamily
                        font.pixelSize: Theme.fontSizeBase
                        font.weight: navButton.checked ? Theme.fontWeightHeading : Font.Normal
                        leftPadding: Theme.spacingMd
                    }

                    background: Rectangle {
                        radius: 6
                        color: navButton.checked ? Theme.surfaceAlt : "transparent"
                        border.width: navButton.checked ? 1 : 0
                        border.color: Theme.border
                    }
                }
            }

            Item {
                Layout.fillHeight: true
            }

            // --- Engine readout (FR-2.7, display-only) -------------------------
            Label {
                objectName: "engineReadout"
                Layout.fillWidth: true
                Layout.leftMargin: Theme.spacingLg
                Layout.rightMargin: Theme.spacingLg
                Layout.bottomMargin: Theme.spacingLg
                text: bridge.engineNote
                color: Theme.textMuted
                font.family: Theme.fontFamily
                font.pixelSize: Theme.fontSizeSm
                wrapMode: Text.Wrap
            }
        }

        Rectangle {
            color: Theme.border
            Layout.fillHeight: true
            Layout.preferredWidth: 1
        }

        // --- Tab content (placeholder swap on currentTab) ----------------------
        StackLayout {
            id: tabStack
            objectName: "tabStack"
            Layout.fillWidth: true
            Layout.fillHeight: true
            currentIndex: {
                const ids = ["text", "paragraph", "cloning", "settings"];
                return Math.max(0, ids.indexOf(bridge.currentTab));
            }

            TextTab {}
            ParagraphTab {}
            CloningTab {}
            SettingsTab {}
        }
    }
}
