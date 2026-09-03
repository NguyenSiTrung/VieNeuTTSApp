// Shell window: nav rail + tab content + engine readout (FR-2.3/FR-2.7/FR-UX-3).
// Signal design system: teal brand tile, tracked section label, AppIcon nav
// glyphs, StatusBadge engine chip. All objectNames are the tested contract.
import QtQuick
import QtQuick.Controls
import QtQuick.Dialogs
import QtQuick.Layouts
import "."
import "components"

ApplicationWindow {
    id: window

    objectName: "mainWindow"
    visible: true

    // Restored placement: absent keys fall back to the default 1120×740,
    // centered on the screen (first run, or the saved position left every
    // connected monitor). Persisted on close so the next launch reopens here.
    readonly property var savedGeo: bridge.initialWindowGeometry
    width: savedGeo.width || 1120
    height: savedGeo.height || 740
    x: savedGeo.x !== undefined ? savedGeo.x : (Screen.width - width) / 2
    y: savedGeo.y !== undefined ? savedGeo.y : (Screen.height - height) / 2
    visibility: savedGeo.maximized === true ? Window.Maximized : Window.AutomaticVisibility
    minimumWidth: 640
    minimumHeight: 420

    // Last WINDOWED frame — updated only while unmaximized, so closing while
    // maximized still restores the user's normal size (not the maximized
    // frame) behind the restored maximized state.
    property var lastNormal: ({})
    Component.onCompleted: lastNormal = { "x": x, "y": y, "width": width, "height": height }
    onXChanged: if (visibility === Window.Windowed) updateLastNormal()
    onYChanged: if (visibility === Window.Windowed) updateLastNormal()
    onWidthChanged: if (visibility === Window.Windowed) updateLastNormal()
    onHeightChanged: if (visibility === Window.Windowed) updateLastNormal()
    function updateLastNormal() {
        lastNormal = { "x": x, "y": y, "width": width, "height": height }
    }
    onClosing: bridge.saveWindowGeometry(
        Math.round(lastNormal.x), Math.round(lastNormal.y),
        Math.round(lastNormal.width), Math.round(lastNormal.height),
        visibility === Window.Maximized)

    title: qsTr("VieNeuTTS — On-Device AI Audio Workstation")
    color: Theme.bg

    // At the supported 640 px minimum, reserve a working canvas for studio
    // controls while keeping every navigation destination accessible by name.
    readonly property bool compactLayout: width < 800

    function formatModelBytes(bytes) {
        if (bytes <= 0)
            return "0 B";
        var units = ["B", "KB", "MB", "GB"];
        var value = bytes;
        var unit = 0;
        while (value >= 1024 && unit < units.length - 1) {
            value /= 1024;
            unit += 1;
        }
        return (unit === 0 ? Math.round(value) : value.toFixed(1)) + " " + units[unit];
    }

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
                        clip: true

                        Image {
                            id: brandLogoImg
                            anchors.fill: parent
                            anchors.margins: 1
                            source: "../assets/icons/icon_64x64.png"
                            fillMode: Image.PreserveAspectFit
                            mipmap: true
                            visible: status === Image.Ready
                        }

                        RowLayout {
                            anchors.centerIn: parent
                            spacing: 2
                            visible: !brandLogoImg.visible
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

                        HoverHandler {
                            cursorShape: Qt.PointingHandCursor
                        }
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
                                visible: !window.compactLayout
                                dotVisible: false
                                text: {
                                    if (!controller)
                                        return qsTr("Đang kiểm tra...");
                                    switch (controller.modelState) {
                                    case "ready":
                                        return qsTr("Sẵn sàng");
                                    case "downloading":
                                        return qsTr("Đang tải mô hình...");
                                    case "validating":
                                        return qsTr("Đang kiểm tra...");
                                    case "failed":
                                        return qsTr("Lỗi mô hình");
                                    case "unavailable":
                                        return qsTr("Chưa có mô hình");
                                    default:
                                        return qsTr("Đang kiểm tra...");
                                    }
                                }
                                status: {
                                    if (!controller)
                                        return "neutral";
                                    switch (controller.modelState) {
                                    case "ready":
                                        return "success";
                                    case "failed":
                                        return "error";
                                    case "unavailable":
                                        return "warning";
                                    default:
                                        return "neutral";
                                    }
                                }
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

            // Heavy studios load on FIRST VISIT and stay cached: audiobook
            // (~1.2k lines), cloning, and settings each build a Canvas-icon
            // + MultiEffect tree that a first-run user landing on "text"
            // never sees — eager instantiation cost ~40 icon FBOs before
            // the first paint. Loaded once: re-entry is instant and state
            // (reader position, cloned-clip pick, settings form) survives.
            Loader {
                property bool visited: false
                active: bridge.currentTab === "audiobook" || visited
                onActiveChanged: if (active) visited = true
                sourceComponent: Component { AudiobookTab {} }
            }
            Loader {
                property bool visited: false
                active: bridge.currentTab === "cloning" || visited
                onActiveChanged: if (active) visited = true
                sourceComponent: Component { CloningTab {} }
            }
            Loader {
                property bool visited: false
                active: bridge.currentTab === "settings" || visited
                onActiveChanged: if (active) visited = true
                sourceComponent: Component { SettingsTab {} }
            }
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

    // --- Model Setup Screen (Phase 1 Task 4) ---------------------------------
    // Truthful readiness: clean profiles install the official baseline once
    // through the UI, then run offline. No repository/Python commands here.
    Rectangle {
        id: modelSetupOverlay
        objectName: "modelSetupOverlay"
        anchors.fill: parent
        visible: controller && !controller.modelReady && controller.modelRepo === ""
        color: Qt.rgba(Theme.bg.r, Theme.bg.g, Theme.bg.b, 0.92)
        z: 20
        MouseArea {
            anchors.fill: parent
        }
        Rectangle {
            anchors.centerIn: parent
            width: Math.min(parent.width - 2 * Theme.spacingXl, 560)
            implicitHeight: modelSetupCol.implicitHeight + Theme.spacingXl * 2
            radius: Theme.radiusXl
            color: Theme.surfaceCard
            border.color: Theme.border
            border.width: 1
            ColumnLayout {
                id: modelSetupCol
                anchors.fill: parent
                anchors.margins: Theme.spacingXl
                spacing: Theme.spacingMd
                Label {
                    Layout.fillWidth: true
                    text: {
                        switch (controller.modelState) {
                        case "ready":
                            return qsTr("Mô hình đã sẵn sàng");
                        case "downloading":
                            return qsTr("Đang tải mô hình chính thức...");
                        case "validating":
                            return qsTr("Đang kiểm tra mô hình...");
                        case "failed":
                            return qsTr("Không thể chuẩn bị mô hình");
                        case "unavailable":
                            return qsTr("Cần tải mô hình một lần");
                        default:
                            return qsTr("Đang kiểm tra mô hình...");
                        }
                    }
                    color: Theme.text
                    font.family: Theme.fontFamily
                    font.pixelSize: Theme.fontSizeXl
                    font.weight: Theme.fontWeightHeading
                    font.letterSpacing: Theme.trackingTight
                }
                Label {
                    id: modelStatusText
                    objectName: "modelStatusText"
                    Layout.fillWidth: true
                    text: {
                        var base = "";
                        switch (controller.modelState) {
                        case "ready":
                            base = qsTr("Ứng dụng đã ngoại tuyến sau khi cài đặt một lần.");
                            break;
                        case "downloading":
                            base = qsTr("Đang tải xuống, giữ ứng dụng mở. Có thể hủy bất cứ lúc nào.");
                            break;
                        case "validating":
                            base = qsTr("Đang xác thực kích thước và checksum SHA-256.");
                            break;
                        case "failed":
                            base = controller.modelError !== "" ? controller.modelError : qsTr("Hãy kiểm tra mạng/ổ đĩa rồi thử lại.");
                            break;
                        case "unavailable":
                            base = qsTr("Mô hình CPU chính thức chưa có trên máy. Tải một lần để dùng ngoại tuyến.");
                            break;
                        default:
                            base = qsTr("Đang kiểm tra thư mục mô hình...");
                        }
                        var storage = qsTr("Đã lưu %1 / cần %2").arg(formatModelBytes(controller.modelInstalledBytes)).arg(formatModelBytes(controller.modelRequiredBytes));
                        return base + "\n" + storage;
                    }
                    color: Theme.textMuted
                    font.family: Theme.fontFamily
                    font.pixelSize: Theme.fontSizeBase
                    lineHeight: 1.35
                    wrapMode: Text.Wrap
                }
                ProgressBar {
                    id: modelProgressBar
                    objectName: "modelProgressBar"
                    Layout.fillWidth: true
                    visible: controller.modelState === "downloading" || controller.modelState === "validating"
                    from: 0
                    to: 1
                    value: controller.modelProgress
                }
                Label {
                    Layout.fillWidth: true
                    visible: controller.modelState === "failed" && controller.modelError !== ""
                    text: controller.modelError
                    color: Theme.error
                    font.family: Theme.fontFamily
                    font.pixelSize: Theme.fontSizeSm
                    wrapMode: Text.Wrap
                }
                Label {
                    Layout.fillWidth: true
                    text: qsTr("Hoặc chép gói ngoại tuyến đã xác thực vào thư mục bên dưới (gồm 2 thư mục con backbone/ và codec/), rồi nhấn “Thử lại”. Không cần lệnh terminal.")
                    color: Theme.textMuted
                    font.family: Theme.fontFamily
                    font.pixelSize: Theme.fontSizeSm
                    wrapMode: Text.Wrap
                }
                Rectangle {
                    Layout.fillWidth: true
                    implicitHeight: modelDirRow.implicitHeight + Theme.spacingSm * 2
                    radius: Theme.radiusMd
                    color: Theme.surfaceAlt
                    border.color: Theme.borderSubtle
                    border.width: 1
                    RowLayout {
                        id: modelDirRow
                        anchors.fill: parent
                        anchors.margins: Theme.spacingSm
                        spacing: Theme.spacingSm
                        TextField {
                            id: modelDirField
                            objectName: "modelDirField"
                            Layout.fillWidth: true
                            readOnly: true
                            selectByMouse: true
                            text: controller ? controller.modelDir : ""
                            color: Theme.text
                            font.family: Theme.fontFamilyMono !== "" ? Theme.fontFamilyMono : Theme.fontFamily
                            font.pixelSize: Theme.fontSizeSm
                            implicitHeight: 32
                            background: Rectangle {
                                color: "transparent"
                            }
                            Accessible.name: qsTr("Thư mục mô hình")
                        }
                        AppIconButton {
                            id: modelDirCopyButton
                            objectName: "modelDirCopyButton"
                            size: "sm"
                            iconKind: "copy"
                            tooltipText: qsTr("Sao chép đường dẫn thư mục mô hình")
                            accessibleLabel: qsTr("Sao chép đường dẫn thư mục mô hình")
                            onClicked: controller.copyModelDir()
                        }
                        AppIconButton {
                            id: modelDirOpenButton
                            objectName: "modelDirOpenButton"
                            size: "sm"
                            iconKind: "folder"
                            tooltipText: qsTr("Mở thư mục mô hình")
                            accessibleLabel: qsTr("Mở thư mục mô hình")
                            onClicked: controller.openModelDir()
                        }
                    }
                }
                RowLayout {
                    Layout.fillWidth: true
                    spacing: Theme.spacingSm
                    Item {
                        Layout.fillWidth: true
                    }
                    AppButton {
                        objectName: "modelRetryButton"
                        variant: "secondary"
                        size: "md"
                        text: qsTr("Thử lại")
                        tooltipText: qsTr("Quét lại thư mục mô hình")
                        visible: controller.modelState !== "downloading" && controller.modelState !== "validating"
                        onClicked: controller.refreshModelState()
                    }
                    AppButton {
                        objectName: "modelImportButton"
                        variant: "secondary"
                        size: "md"
                        text: qsTr("Nhập gói ngoại tuyến…")
                        tooltipText: qsTr("Chọn thư mục chứa backbone/ và codec/ để nhập ngoại tuyến")
                        visible: controller.modelState !== "downloading" && controller.modelState !== "validating" && !controller.modelReady
                        onClicked: offlinePackDialog.open()
                    }
                    AppButton {
                        objectName: "modelCancelButton"
                        variant: "secondary"
                        size: "md"
                        text: qsTr("Hủy")
                        tooltipText: qsTr("Hủy tải mô hình")
                        visible: controller.modelState === "downloading" || controller.modelState === "validating"
                        onClicked: controller.cancelModelDownload()
                    }
                    AppButton {
                        objectName: "modelDownloadButton"
                        variant: "primary"
                        size: "md"
                        text: qsTr("Tải mô hình")
                        tooltipText: qsTr("Tải mô hình CPU chính thức một lần")
                        visible: controller.modelState !== "downloading" && controller.modelState !== "validating" && !controller.modelReady
                        onClicked: controller.downloadOfficialModel()
                    }
                }
            }
        }
    }
    FolderDialog {
        id: offlinePackDialog
        objectName: "offlinePackDialog"
        title: qsTr("Chọn thư mục gói ngoại tuyến (chứa backbone/ và codec/)")
        onAccepted: controller.importOfflinePack(selectedFolder.toString())
    }
}
