// Shell window: nav rail + tab content + engine readout (FR-2.3/FR-2.7/FR-UX-3).
// Signal design system: teal brand tile, tracked section label, AppIcon nav
// glyphs, StatusBadge engine chip. All objectNames are the tested contract.
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "."
import "components"

ApplicationWindow {
    id: window

    objectName: "mainWindow"
    visible: true
    width: 1120
    height: 740
    minimumWidth: 640
    minimumHeight: 420
    title: qsTr("VieNeuTTS — On-Device AI Audio Workstation")
    color: Theme.bg

    // At the supported 640 px minimum, reserve a working canvas for studio
    // controls while keeping every navigation destination accessible by name.
    readonly property bool compactLayout: width < 800

    // Local dismissal of the models-missing screen; re-armed whenever a NEW
    // models-missing state arrives (see Connections below).
    property bool modelsDismissed: false

    RowLayout {
        anchors.fill: parent
        anchors.topMargin: exportOnlyNotice.visible
            ? exportOnlyNotice.height + Theme.spacingSm : 0
        spacing: 0

        // --- Navigation Sidebar / Rail (FR-UX-3.2) -----------------------------
        Rectangle {
            id: sidebar
            Layout.preferredWidth: window.compactLayout ? 64 : 232
            Layout.fillHeight: true
            color: Theme.surface
            border.width: 0

            // Right border line
            Rectangle {
                anchors.right: parent.right
                anchors.top: parent.top
                anchors.bottom: parent.bottom
                width: 1
                color: Theme.border
            }

            ColumnLayout {
                id: navColumn
                objectName: "navBar"
                anchors.fill: parent
                anchors.margins: window.compactLayout ? Theme.spacingSm : Theme.spacingMd
                spacing: Theme.spacingSm

                // --- Brand Header (FR-UX-3.1) ---
                RowLayout {
                    Layout.fillWidth: true
                    Layout.alignment: window.compactLayout ? Qt.AlignHCenter : Qt.AlignLeft
                    Layout.topMargin: Theme.spacingXs
                    Layout.bottomMargin: Theme.spacingMd
                    spacing: Theme.spacingSm

                    // Brand Icon / Micro Waveform Box
                    Rectangle {
                        width: 36
                        height: 36
                        radius: Theme.radiusMd
                        color: Theme.accentSubtle
                        border.color: Theme.borderFocus
                        border.width: 1

                        RowLayout {
                            anchors.centerIn: parent
                            spacing: 2
                            Rectangle { width: 3; height: 10; radius: 1.5; color: Theme.accent }
                            Rectangle { width: 3; height: 18; radius: 1.5; color: Theme.accent }
                            Rectangle { width: 3; height: 14; radius: 1.5; color: Theme.accent }
                            Rectangle { width: 3; height: 8; radius: 1.5; color: Theme.accent }
                        }
                    }

                    ColumnLayout {
                        Layout.fillWidth: !window.compactLayout
                        visible: !window.compactLayout
                        spacing: 0

                        RowLayout {
                            spacing: Theme.spacingXs
                            Label {
                                text: qsTr("VieNeuTTS")
                                color: Theme.text
                                font.family: Theme.fontFamily
                                font.pixelSize: Theme.fontSizeLg
                                font.weight: Theme.fontWeightBold
                                font.letterSpacing: Theme.trackingTight
                            }
                            Rectangle {
                                color: Theme.accentSubtle
                                radius: Theme.radiusPill
                                implicitHeight: 18
                                implicitWidth: vLabel.implicitWidth + 10
                                Label {
                                    id: vLabel
                                    anchors.centerIn: parent
                                    text: "v3 Turbo"
                                    color: Theme.accent
                                    font.family: Theme.fontFamily
                                    font.pixelSize: Theme.fontSizeXs
                                    font.weight: Theme.fontWeightHeading
                                }
                            }
                        }

                        Label {
                            text: qsTr("AI Audio Workstation")
                            color: Theme.textSubtle
                            font.family: Theme.fontFamily
                            font.pixelSize: Theme.fontSizeXs
                        }
                    }
                }

                // Section Label
                SectionLabel {
                    text: qsTr("Chức năng")
                    visible: !window.compactLayout
                    Layout.leftMargin: Theme.spacingXs
                    Layout.topMargin: Theme.spacingXs
                }

                // --- Navigation Tabs ---
                Repeater {
                    model: bridge ? bridge.tabs : []

                    Button {
                        id: navButton
                        required property var modelData
                        Layout.fillWidth: true
                        implicitHeight: window.compactLayout
                            ? Theme.controlHitTarget : 38
                        flat: true
                        checked: bridge ? bridge.currentTab === modelData.id : false
                        onClicked: if (bridge) bridge.setCurrentTab(modelData.id)
                        Accessible.name: navButton.modelData ? navButton.modelData.label : ""

                        contentItem: RowLayout {
                            spacing: Theme.spacingSm

                            // Active indicator pill
                            Rectangle {
                                width: 3
                                height: 16
                                radius: 1.5
                                color: Theme.accent
                                visible: navButton.checked
                            }

                            Item {
                                width: 3
                                height: 16
                                visible: !navButton.checked
                            }

                            AppIcon {
                                kind: navButton.modelData ? navButton.modelData.id : "text"
                                iconColor: navButton.checked ? Theme.accent
                                    : (navButton.hovered ? Theme.text : Theme.textMuted)
                            }

                            Label {
                                text: navButton.modelData ? navButton.modelData.label : ""
                                visible: !window.compactLayout
                                color: navButton.checked ? Theme.accent : (navButton.hovered ? Theme.text : Theme.textMuted)
                                font.family: Theme.fontFamily
                                font.pixelSize: Theme.fontSizeBase
                                font.weight: navButton.checked ? Theme.fontWeightHeading : Theme.fontWeightNormal
                                Layout.fillWidth: true
                            }
                        }

                        background: Rectangle {
                            radius: Theme.radiusMd
                            color: navButton.checked ? Theme.accentSubtle : (navButton.hovered ? Theme.surfaceHover : "transparent")
                            border.width: navButton.checked ? 1 : 0
                            border.color: Theme.borderFocus

                            Behavior on color { ColorAnimation { duration: Theme.durationFast } }
                        }
                    }
                }

                Item {
                    Layout.fillHeight: true
                }

                // --- Engine Hardware Status Card (FR-UX-3.3) -------------------
                Rectangle {
                    Layout.fillWidth: true
                    radius: Theme.radiusMd
                    color: Theme.surfaceAlt
                    border.color: Theme.borderSubtle
                    border.width: 1
                    implicitHeight: engineCardCol.implicitHeight + Theme.spacingSm * 2

                    ColumnLayout {
                        id: engineCardCol
                        anchors.fill: parent
                        anchors.margins: Theme.spacingSm
                        spacing: Theme.spacingXs

                        RowLayout {
                            spacing: Theme.spacingXs
                            Rectangle {
                                width: 6
                                height: 6
                                radius: 3
                                color: Theme.success
                            }
                            Label {
                                text: qsTr("Phần cứng & Engine")
                                visible: !window.compactLayout
                                color: Theme.text
                                font.family: Theme.fontFamily
                                font.pixelSize: Theme.fontSizeXs
                                font.weight: Theme.fontWeightHeading
                            }
                            Item {
                                Layout.fillWidth: !window.compactLayout
                                visible: !window.compactLayout
                            }
                            StatusBadge {
                                text: qsTr("Sẵn sàng")
                                visible: !window.compactLayout
                                status: "success"
                                dotVisible: false
                            }
                        }

                        Label {
                            objectName: "engineReadout"
                            Layout.fillWidth: true
                            visible: !window.compactLayout
                            text: bridge ? bridge.engineNote : ""
                            color: Theme.textMuted
                            font.family: Theme.fontFamily
                            font.pixelSize: Theme.fontSizeXs
                            wrapMode: Text.Wrap
                        }
                    }
                }
            }
        }

        // --- Tab Content Stack ------------------------------------------------
        StackLayout {
            id: tabStack
            objectName: "tabStack"
            Layout.fillWidth: true
            Layout.fillHeight: true
            currentIndex: {
                const ids = ["text", "paragraph", "audiobook", "cloning", "settings"];
                return bridge ? Math.max(0, ids.indexOf(bridge.currentTab)) : 0;
            }

            TextTab {}
            ParagraphTab {}
            AudiobookTab {}
            CloningTab {}
            SettingsTab {}
        }
    }

    // --- Export-Only Notice Banner (FR-4.6a & FR-UX-3.5) ---------------------
    Rectangle {
        id: exportOnlyNotice

        objectName: "exportOnlyNotice"
        anchors.top: parent.top
        anchors.horizontalCenter: parent.horizontalCenter
        anchors.topMargin: visible ? Theme.spacingSm : 0
        width: Math.min(parent.width - Theme.spacingXl * 2, exportOnlyRow.implicitWidth + Theme.spacingLg * 2)
        visible: !controller.audioAvailable
        height: visible ? exportOnlyRow.implicitHeight + Theme.spacingSm * 2 : 0
        radius: Theme.radiusPill
        color: Theme.warningSubtle
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

            Rectangle {
                width: 8
                height: 8
                radius: 4
                color: Theme.warning
            }

            Label {
                text: qsTr("Không phát hiện thiết bị âm thanh — chế độ chỉ xuất tệp (export-only).")
                color: Theme.warningText
                font.family: Theme.fontFamily
                font.pixelSize: Theme.fontSizeSm
                font.weight: Theme.fontWeightMedium
            }

            AppButton {
                objectName: "audioRefreshButton"
                variant: "quiet"
                size: "sm"
                text: qsTr("Kiểm tra lại")
                iconKind: "refresh"
                onClicked: controller.refreshAudioAvailability()
            }
        }
    }

    // --- Models-Missing Screen (FR-4.6c & FR-UX-3.4) -------------------------
    Rectangle {
        id: modelsMissingOverlay

        objectName: "modelsMissingOverlay"
        anchors.fill: parent
        visible: controller.modelsMissing && !window.modelsDismissed
        color: Qt.rgba(Theme.bg.r, Theme.bg.g, Theme.bg.b, 0.92)
        z: 20

        MouseArea {
            anchors.fill: parent
        }

        Rectangle {
            anchors.centerIn: parent
            width: Math.min(parent.width - 2 * Theme.spacingXl, 560)
            implicitHeight: missingCardCol.implicitHeight + Theme.spacingXl * 2
            radius: Theme.radiusXl
            color: Theme.surfaceCard
            border.color: Theme.border
            border.width: 1

            ColumnLayout {
                id: missingCardCol
                anchors.fill: parent
                anchors.margins: Theme.spacingXl
                spacing: Theme.spacingMd

                RowLayout {
                    spacing: Theme.spacingSm
                    Rectangle {
                        width: 32
                        height: 32
                        radius: Theme.radiusMd
                        color: Theme.errorSubtle
                        border.color: Theme.error
                        border.width: 1
                        Label {
                            anchors.centerIn: parent
                            text: "!"
                            color: Theme.error
                            font.family: Theme.fontFamily
                            font.pixelSize: Theme.fontSizeLg
                            font.weight: Theme.fontWeightBold
                        }
                    }
                    Label {
                        Layout.fillWidth: true
                        text: qsTr("Thiếu dữ liệu mô hình")
                        color: Theme.error
                        font.family: Theme.fontFamily
                        font.pixelSize: Theme.fontSizeXl
                        font.weight: Theme.fontWeightHeading
                        font.letterSpacing: Theme.trackingTight
                    }
                }

                Label {
                    Layout.fillWidth: true
                    text: qsTr("Các tệp trọng lượng mô hình (model weights) chưa có trên máy, nên không thể tổng hợp giọng nói. Hãy tải gói ngoại tuyến một lần duy nhất bằng lệnh sau, chạy từ thư mục gốc của dự án:")
                    color: Theme.text
                    font.family: Theme.fontFamily
                    font.pixelSize: Theme.fontSizeBase
                    lineHeight: 1.35
                    wrapMode: Text.Wrap
                }

                Rectangle {
                    Layout.fillWidth: true
                    radius: Theme.radiusMd
                    color: Theme.surfaceAlt
                    border.color: Theme.borderSubtle
                    border.width: 1
                    implicitHeight: cmdLabel.implicitHeight + Theme.spacingMd * 2

                    Label {
                        id: cmdLabel
                        objectName: "modelsMissingCommand"
                        anchors.centerIn: parent
                        text: qsTr("python scripts/fetch_models.py")
                        color: Theme.accent
                        font.family: Theme.fontFamilyMono
                        font.pixelSize: Theme.fontSizeBase
                        font.weight: Theme.fontWeightHeading
                    }
                }

                Label {
                    Layout.fillWidth: true
                    text: qsTr("Sau khi tải xong, nhấn “Thử lại” và thử tạo lại âm thanh.")
                    color: Theme.textMuted
                    font.family: Theme.fontFamily
                    font.pixelSize: Theme.fontSizeSm
                    wrapMode: Text.Wrap
                }

                RowLayout {
                    Layout.fillWidth: true
                    Item { Layout.fillWidth: true }
                    AppButton {
                        objectName: "modelsRetryButton"
                        variant: "primary"
                        size: "md"
                        text: qsTr("Thử lại")
                        onClicked: window.modelsDismissed = true
                    }
                }
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
