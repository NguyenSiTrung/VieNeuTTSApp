// Shell window: nav rail + tab content above a full-width StatusBar
// (FR-2.3/FR-UX-3; ui_shell_redesign FR-2.1 moved the engine readout and the
// export-only notice into the status bar). Signal design system: teal brand
// tile, tracked section label, AppIcon nav glyphs. All objectNames are the
// tested contract.
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

    // Idle tab prebuild: app.py flips this once, after the first frame was
    // presented, so the deferred tab Loaders incubate while the user reads
    // the landing tab instead of delaying its first paint.
    property bool prebuildTabs: false
    // Giọng đọc opened from Tạo giọng đọc's "Đổi giọng…" (FR-3.3): the
    // library offers "Dùng giọng này" and returns the choice to the Create
    // dock. Any navigation away from Giọng đọc ends it, so the sidebar route
    // always shows the plain library.
    property bool voicesPickForCreate: false

    function openVoicesForCreate() {
        voicesPickForCreate = true;
        bridge.setVoicesView("library");
        bridge.setCurrentTab("voices");
    }

    function endVoicesPick(voiceId) {
        voicesPickForCreate = false;
        if (voiceId !== "")
            createTab.useVoice(voiceId);
        bridge.setCurrentTab("create");
    }

    Connections {
        target: bridge
        function onCurrentTabChanged() {
            if (bridge.currentTab !== "voices")
                window.voicesPickForCreate = false;
        }
    }
    readonly property bool tabsReady: createTab.modesReady
        && studioLoader.ready
        && audiobookLoader.ready
        && voicesLoader.ready
        && settingsLoader.ready

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

    // Sidebar route (FR-3.7): Tạo giọng đọc keeps its current mode, and
    // Giọng đọc always opens the plain library flow — never the Create pick
    // flow, even when that flow is the page already shown.
    function navigateTo(tabId) {
        if (tabId === "voices")
            voicesPickForCreate = false;
        bridge.setCurrentTab(tabId);
    }

    function tabLabel(tabId) {
        const tabs = bridge ? bridge.tabs : [];
        for (let i = 0; i < tabs.length; ++i) {
            if (tabs[i].id === tabId)
                return tabs[i].label;
        }
        return "";
    }

    // One sidebar row: icon + label (icon-only in the compact rail, where
    // the label stays the accessible name and the hover tooltip). Selected
    // is the checked state with the chip's accent tint, never a primary fill.
    component NavItem: Button {
        id: navItem

        property string tabId: ""
        property bool compact: false
        property bool showDot: false
        signal activated()

        objectName: "navItem_" + tabId
        implicitHeight: Theme.controlHitTarget
        flat: true
        leftPadding: compact ? 0 : Theme.spacingMd
        rightPadding: compact ? 0 : Theme.spacingMd
        checked: bridge ? bridge.currentTab === tabId : false
        onClicked: activated()
        Accessible.name: text
        Accessible.checkable: true
        Accessible.checked: checked
        ToolTip.visible: compact && hovered
        ToolTip.delay: 400
        ToolTip.text: text

        HoverHandler {
            cursorShape: Qt.PointingHandCursor
        }

        contentItem: Item {
            implicitHeight: Theme.controlHitTarget

            AppIcon {
                id: navIcon
                kind: navItem.tabId
                x: navItem.compact ? (parent.width - width) / 2 : 0
                anchors.verticalCenter: parent.verticalCenter
                iconColor: navItem.checked ? Theme.accent
                    : (navItem.hovered ? Theme.text : Theme.textMuted)
            }

            Label {
                anchors.left: navIcon.right
                anchors.leftMargin: Theme.spacingMd
                anchors.right: parent.right
                anchors.rightMargin: navItem.showDot ? Theme.spacingMd : 0
                anchors.verticalCenter: parent.verticalCenter
                visible: !navItem.compact
                text: navItem.text
                elide: Text.ElideRight
                color: navItem.checked ? Theme.accent
                    : (navItem.hovered ? Theme.text : Theme.textMuted)
                font.family: Theme.fontFamily
                font.pixelSize: Theme.fontSizeBase
                font.weight: navItem.checked ? Theme.fontWeightHeading : Theme.fontWeightNormal
            }
        }

        background: Rectangle {
            radius: Theme.radiusMd
            color: navItem.checked ? Theme.accentSubtle
                : (navItem.hovered ? Theme.surfaceHover : "transparent")

            Behavior on color { ColorAnimation { duration: Theme.durationFast } }
        }
    }

    // Settings › Cập nhật: the status bar's update link lands on the update
    // card (the nav dot only selects the Settings row).
    function openUpdates() {
        bridge.setCurrentTab("settings");
        if (settingsLoader.item)
            Qt.callLater(() => settingsLoader.item.jumpToSection("updates"));
    }

    RowLayout {
        anchors.top: parent.top
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.bottom: statusBar.top
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
                spacing: Theme.spacingXs

                // --- Brand Header (FR-UX-3.1) ---
                RowLayout {
                    Layout.fillWidth: true
                    Layout.alignment: window.compactLayout ? Qt.AlignHCenter : Qt.AlignLeft
                    Layout.topMargin: Theme.spacingXs
                    Layout.bottomMargin: Theme.spacingLg
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

                // --- Destinations (FR-3.7) ---
                // The four content destinations stack at the top; Cài đặt is
                // pinned to the rail's bottom edge below the spacer.
                Repeater {
                    model: bridge ? bridge.tabs.filter(tab => tab.id !== "settings") : []

                    NavItem {
                        required property var modelData
                        tabId: modelData.id
                        text: modelData.label
                        compact: window.compactLayout
                        Layout.fillWidth: true
                        onActivated: window.navigateTo(tabId)
                    }
                }

                Item {
                    Layout.fillHeight: true
                    Layout.minimumHeight: Theme.spacingSm
                }

                NavItem {
                    id: settingsNav
                    tabId: "settings"
                    text: window.tabLabel("settings")
                    compact: window.compactLayout
                    // The silent startup/hourly check flips
                    // controller.updateAvailable (sticky till restart).
                    showDot: !!controller && controller.updateAvailable
                    Layout.fillWidth: true
                    onActivated: window.navigateTo(tabId)

                    // Update dot: trailing in the full sidebar, on the
                    // centred icon's top-right corner in the compact rail.
                    Rectangle {
                        objectName: "navUpdateDot"
                        visible: settingsNav.showDot
                        width: 8
                        height: 8
                        radius: 4
                        color: Theme.accent
                        x: settingsNav.compact ? settingsNav.width / 2 + 6
                            : settingsNav.width - settingsNav.rightPadding - width
                        y: settingsNav.compact ? settingsNav.height / 2 - 13
                            : (settingsNav.height - height) / 2
                        Accessible.name: qsTr("Có bản cập nhật mới")
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
            // Five destinations (FR-3.1). Tạo giọng đọc is CreateTab (its
            // mode follows bridge.createMode); Giọng đọc is VoicesTab (the
            // library, or the cloning flow as its clone view —
            // bridge.voicesView).
            currentIndex: {
                if (!bridge)
                    return 0;
                switch (bridge.currentTab) {
                case "create":
                    return 0;
                case "studio":
                    return 1;
                case "audiobook":
                    return 2;
                case "voices":
                    return 3;
                case "settings":
                    return 4;
                }
                return 0;
            }

            // The landing page is built before the first frame — CreateTab
            // itself defers its document/files/subtitles workspaces to an
            // async Loader that joins the idle prebuild (prebuildModes).
            // Every other tab is an ASYNCHRONOUS Loader: it incubates in time
            // slices between frames (a synchronous Settings build blocked the
            // GUI ~200 ms), either on first visit or — normally earlier — in
            // the idle prebuild app.py starts after the first frame
            // (window.prebuildTabs). Once loaded a tab stays cached, so
            // re-entry is instant and its state (reader position, cloned-clip
            // pick, settings form) survives.
            CreateTab {
                id: createTab

                prebuildModes: window.prebuildTabs
                onVoiceSelectionRequested: window.openVoicesForCreate()
            }
            Loader {
                id: studioLoader
                objectName: "studioLoader"
                property bool visited: false
                readonly property bool ready: status === Loader.Ready
                asynchronous: true
                active: window.prebuildTabs || bridge.currentTab === "studio" || visited
                onActiveChanged: if (active) visited = true
                sourceComponent: Component { StudioTab {} }
            }
            Loader {
                id: audiobookLoader
                objectName: "audiobookLoader"
                property bool visited: false
                readonly property bool ready: status === Loader.Ready
                asynchronous: true
                active: window.prebuildTabs || bridge.currentTab === "audiobook" || visited
                onActiveChanged: if (active) visited = true
                sourceComponent: Component { AudiobookTab {} }
            }
            Loader {
                id: voicesLoader
                objectName: "voicesLoader"
                property bool visited: false
                readonly property bool ready: status === Loader.Ready
                asynchronous: true
                active: window.prebuildTabs || bridge.currentTab === "voices" || visited
                onActiveChanged: if (active) visited = true
                sourceComponent: Component {
                    VoicesTab {
                        pickForCreate: window.voicesPickForCreate
                        createVoiceId: createTab.currentVoice
                        onUseVoiceRequested: function (voiceId) {
                            window.endVoicesPick(voiceId);
                        }
                        onPickCancelled: window.endVoicesPick("")
                    }
                }
            }
            Loader {
                id: settingsLoader
                objectName: "settingsLoader"
                property bool visited: false
                readonly property bool ready: status === Loader.Ready
                asynchronous: true
                active: window.prebuildTabs || bridge.currentTab === "settings" || visited
                onActiveChanged: if (active) visited = true
                sourceComponent: Component { SettingsTab {} }
            }
        }
    }

    // --- Status bar (FR-2.1): the ONE status surface, under nav + content ---
    // Readiness, engine note, audio output (exportOnlyNotice +
    // audioRefreshButton) and the update link at every window width.
    StatusBar {
        id: statusBar

        anchors.left: parent.left
        anchors.right: parent.right
        anchors.bottom: parent.bottom
        onUpdateRequested: window.openUpdates()
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
