// Shell window: nav rail + tab content + engine readout (FR-2.3/FR-2.7).
// Shell only — tab content is placeholder (FR-2.6); §8's 3-column wide /
// stacked narrow split is indicative and lands with real tab content later.
//
// Edge-case surfaces (Phase 4):
// - modelsMissingOverlay (FR-4.6c): fullscreen scrim shown while
//   controller.modelsMissing is True. The Retry button DISMISSES the overlay
//   (the actual fix runs `python scripts/fetch_models.py` outside the app);
//   every fresh models-missing error raises it again, and the per-tab
//   errorLabels keep showing the raw engine text either way.
// - exportOnlyNotice (FR-4.6a): global banner while
//   !controller.audioAvailable. NOTE: playback *buttons* live inside the
//   tabs (TextTab/ParagraphTab own theirs; CloningTab disables its own
//   preview button) — see P2T3 report TODO for the tab-owned ones.
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

    // Local dismissal of the models-missing screen; re-armed whenever a NEW
    // models-missing state arrives (see Connections below).
    property bool modelsDismissed: false

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
                model: bridge ? bridge.tabs : []

                Button {
                    id: navButton
                    required property var modelData
                    Layout.fillWidth: true
                    Layout.leftMargin: Theme.spacingSm
                    Layout.rightMargin: Theme.spacingSm
                    flat: true
                    checked: bridge ? bridge.currentTab === modelData.id : false
                    onClicked: if (bridge) bridge.setCurrentTab(modelData.id)

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
                text: bridge ? bridge.engineNote : ""
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
                return bridge ? Math.max(0, ids.indexOf(bridge.currentTab)) : 0;
            }

            TextTab {}
            ParagraphTab {}
            CloningTab {}
            SettingsTab {}
        }
    }

    // --- Export-only notice (FR-4.6a) --------------------------------------
    // Global placement decision: Main.qml is the only scope this task owns
    // that spans every tab, and "no audio device" is a system-wide condition,
    // not a per-tab one. The notice sits above the tab content and shifts it
    // down instead of covering it (export stays fully usable — that's the
    // point of export-only mode). Per-tab PLAYBACK buttons belong to
    // TextTab/ParagraphTab (another task's files): TODO(P2T3 handoff) bind
    // their `enabled` to controller.audioAvailable; CloningTab's preview
    // button is disabled here in CloningTab.qml directly.
    Rectangle {
        id: exportOnlyNotice

        objectName: "exportOnlyNotice"
        anchors.top: parent.top
        anchors.left: parent.left
        anchors.right: parent.right
        visible: !controller.audioAvailable
        height: visible ? exportOnlyRow.implicitHeight + 2 * Theme.spacingSm : 0
        color: Theme.surfaceAlt
        border.width: 1
        border.color: Theme.warning
        z: 10

        // Absorb clicks on the strip so covered controls underneath are not
        // clickable through the banner (declared before the row ⇒ lower z).
        MouseArea {
            anchors.fill: parent
        }

        RowLayout {
            id: exportOnlyRow

            anchors.centerIn: parent
            spacing: Theme.spacingSm

            Label {
                text: qsTr("⚠ Không phát hiện thiết bị âm thanh — chế độ chỉ xuất tệp (export-only).")
                color: Theme.warning
                font.family: Theme.fontFamily
                font.pixelSize: Theme.fontSizeSm
            }

            Button {
                objectName: "audioRefreshButton"
                flat: true
                text: qsTr("Kiểm tra lại")
                onClicked: controller.refreshAudioAvailability()
            }
        }
    }

    // --- Models-missing screen (FR-4.6c) ------------------------------------
    // Fullscreen scrim ABOVE everything (z wins over the tab stack). Shown
    // while the LAST worker error is a models-missing error and the user has
    // not dismissed it since that error arrived.
    Rectangle {
        id: modelsMissingOverlay

        objectName: "modelsMissingOverlay"
        anchors.fill: parent
        visible: controller.modelsMissing && !window.modelsDismissed
        color: Qt.rgba(Theme.bg.r, Theme.bg.g, Theme.bg.b, 0.96)
        z: 20

        // Modal scrim: swallow every click so the UI beneath is inert while
        // the weights are missing (declared first ⇒ below the panel).
        MouseArea {
            anchors.fill: parent
        }

        ColumnLayout {
            anchors.centerIn: parent
            width: Math.min(parent.width - 2 * Theme.spacingXl, 560)
            spacing: Theme.spacingMd

            Label {
                Layout.fillWidth: true
                text: qsTr("Thiếu dữ liệu mô hình")
                color: Theme.error
                font.family: Theme.fontFamily
                font.pixelSize: Theme.fontSizeXl
                font.weight: Theme.fontWeightHeading
            }

            Label {
                Layout.fillWidth: true
                text: qsTr("Các tệp trọng lượng mô hình (model weights) chưa có trên máy, nên không thể tổng hợp giọng nói. Hãy tải gói ngoại tuyến một lần duy nhất bằng lệnh sau, chạy từ thư mục gốc của dự án:")
                color: Theme.text
                font.family: Theme.fontFamily
                font.pixelSize: Theme.fontSizeBase
                wrapMode: Text.Wrap
            }

            Label {
                objectName: "modelsMissingCommand"
                Layout.fillWidth: true
                text: qsTr("python scripts/fetch_models.py")
                color: Theme.accent
                font.family: Theme.fontFamilyMono
                font.pixelSize: Theme.fontSizeBase
            }

            Label {
                Layout.fillWidth: true
                text: qsTr("Sau khi tải xong, nhấn “Thử lại” và thử tạo lại âm thanh.")
                color: Theme.textMuted
                font.family: Theme.fontFamily
                font.pixelSize: Theme.fontSizeSm
                wrapMode: Text.Wrap
            }

            Button {
                objectName: "modelsRetryButton"
                text: qsTr("Thử lại")
                onClicked: window.modelsDismissed = true
            }
        }
    }

    // Re-arm dismissal whenever a NEW models-missing error arrives (the flag
    // flips False→True through modelsMissingChanged).
    Connections {
        target: controller
        function onModelsMissingChanged() {
            if (controller.modelsMissing)
                window.modelsDismissed = false;
        }
    }
}
