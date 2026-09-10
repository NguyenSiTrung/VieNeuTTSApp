// Settings tab (FR-3.5, FR-UX-7): engine backend/precision (apply on next engine
// init — surfaced via the needsRestart banner), default voice, output
// directory, temperature, and theme. Engine/output settings flow through
// the `controller` seam (validated + persisted, invalid writes become
// errorText); the theme control writes `bridge.themePreference` — the
// live-switch path that persists to the same settings.json field and
// re-resolves the effective theme immediately.
//
// objectNames are the tested contract (tests/smoke/test_ui_tabs.py):
// settingsTab, backendCombo, detectedEngineLabel, precisionCombo,
// needsRestartBanner, defaultVoiceCombo, outputDirLabel, outputDirBrowseButton,
// outputDirDialog, temperatureSpin, themeCombo, languageCombo, errorLabel,
// checkUpdatesButton, downloadUpdateButton, viewReleaseButton,
// otherPlatformsToggle, otherPlatformsList, updateBanner, updateErrorLabel,
// modelRepoField, modelSourceDetail, settingsModelDirLabel,
// settingsModelDirCopyButton, settingsModelDirOpenButton, customRepoChip.
// CUDA runtime: cudaRuntimeCard, cudaRuntimeInstallButton,
// cudaRuntimeCancelButton, cudaRuntimeRetryButton, cudaRuntimeRemoveButton,
// cudaRuntimeDetectLocalButton, cudaRuntimeDriverNotice,
// cudaRuntimeDriverGuide, cudaRuntimeDriverGuideLinux,
// cudaRuntimeDriverGuideWindows, cudaRuntimeDriverDownloadButton,
// cudaRuntimeStorageLabel, cudaRuntimeProgress, cudaRuntimeLocalSummary,
// cudaRuntimeDetails, cudaRuntimeDetailsToggle.
//
// Section layout: engine compute choice / managed CUDA runtime / model source
// are three sibling cards. The CUDA card keeps every actionable state notice
// outside its collapsed `cudaRuntimeDetails` disclosure, so no condition the
// user must act on can hide behind a toggle.
// The FolderDialog is authored but NOT exercised offscreen (native dialogs
// are unreliable headless — same policy as the other tabs); setting the
// output dir through the tested seam `setOutputDir(path)`.
import QtQuick
import QtQuick.Controls
import QtQuick.Dialogs
import QtQuick.Layouts
import "components"
import "."

Pane {
    id: root

    objectName: "settingsTab"
    padding: Theme.spacingLg

    background: Rectangle {
        color: Theme.bg
    }

    // Backend choices mirror Settings._BACKENDS in core/models.py; QML
    // cannot import Python constants — keep the two in sync.
    readonly property var backendOptions: [
        { value: "auto", label: qsTr("Tự động (ONNX/CPU hoặc CUDA)") },
        { value: "onnx", label: qsTr("ONNX Runtime (CPU)") },
        { value: "torch", label: qsTr("PyTorch (NVIDIA CUDA)") }
    ]

    // Precision choices mirror Settings._PRECISIONS (ONNX-only; the torch
    // path ignores precision).
    readonly property var precisionOptions: [
        { value: "int8", label: qsTr("int8 — nhanh (mặc định)") },
        { value: "fp32", label: qsTr("fp32 — chất lượng tối đa") }
    ]

    // Export container choices mirror Settings._EXPORT_FORMATS in
    // core/models.py. Batch + audiobook outputs and Save-dialog defaults use
    // this (quick-export stays WAV); Save dialogs still pick per file.
    readonly property var exportFormatOptions: [
        { value: "wav", label: qsTr("WAV — chuẩn, dung lượng lớn") },
        { value: "mp3", label: qsTr("MP3 — gọn nhẹ") }
    ]

    readonly property var themeOptions: [
        { value: "system", label: qsTr("Theo hệ điều hành") },
        { value: "light", label: qsTr("Giao diện Sáng") },
        { value: "dark", label: qsTr("Giao diện Tối") }
    ]

    // Language names stay in their native form (standard practice — each
    // name is readable by its own speakers); only the "system" row label
    // is translatable. Values mirror Settings._LANGUAGES in core/models.py.
    readonly property var languageOptions: [
        { value: "system", label: qsTr("Theo hệ điều hành") },
        { value: "vi", label: "Tiếng Việt" },
        { value: "en", label: "English" }
    ]

    // Responsive breakpoint — when the pane narrows below ~640 px the
    // setting rows stack vertically (label on top, control full-width
    // below) so no ComboBox text is truncated (the “Tự động …” cut in
    // the screenshot was caused by a fixed 260 px control fighting a
    // flexible label in a RowLayout at ~400 px available width).
    readonly property bool isCompact: root.width < 640
    readonly property bool cudaRuntimeSupported: controller
        ? controller.cudaRuntimeSupported : false
    readonly property bool cudaRuntimeDriverChecked: controller
        ? controller.cudaRuntimeDriverChecked : false
    readonly property bool cudaRuntimeDriverReady: controller
        ? controller.cudaRuntimeDriverReady : false
    readonly property bool cudaRuntimeInstallAllowed: controller
        ? controller.cudaRuntimeInstallAllowed : false
    readonly property string cudaRuntimeState: controller
        ? controller.cudaRuntimeState : "unavailable"
    readonly property int localCudaRuntimeCount: controller
        ? controller.localCudaRuntimes.length : 0
    property bool localCudaRuntimeScanRequested: false

    // Verified repo override vs. the official baseline. `customRepoRequested`
    // is the chip the user pressed; the repo field appears in custom mode
    // only, so the official id is not repeated by chip + field + feedback
    // line at once.
    property bool customRepoRequested: false
    readonly property bool customRepoMode: customRepoRequested
        || (controller ? controller.modelRepo !== "" : false)

    // CUDA diagnostics disclosure: verbose guidance and the diagnostic scan
    // start collapsed and auto-expand when a state actually needs them —
    // install in flight or failed, an unusable driver, or a scan already run.
    property bool cudaRuntimeDetailsExpanded: false
    readonly property bool cudaRuntimeDetailsWanted: root.cudaRuntimeState === "downloading"
        || root.cudaRuntimeState === "verifying"
        || root.cudaRuntimeState === "failed"
        || (root.cudaRuntimeDriverChecked && !root.cudaRuntimeDriverReady)
        || root.localCudaRuntimeScanRequested
    onCudaRuntimeDetailsWantedChanged: if (cudaRuntimeDetailsWanted) cudaRuntimeDetailsExpanded = true
    Component.onCompleted: cudaRuntimeDetailsExpanded = cudaRuntimeDetailsWanted

    // Driver-guide commands are copy targets, not prose: a user pasting
    // `sudo ubuntu-drivers autoinstall` should never have to retype it.
    component CommandRow: Rectangle {
        id: commandRow

        required property string command

        Layout.fillWidth: true
        implicitHeight: commandLabel.implicitHeight + Theme.spacingSm * 2
        radius: Theme.radiusSm
        // surfaceAlt, not surface: the row sits on a surfaceCard-colored card
        // and `surface` is the same white in light mode.
        color: Theme.surfaceAlt
        border.color: Theme.borderSubtle
        border.width: 1

        RowLayout {
            anchors.fill: parent
            anchors.leftMargin: Theme.spacingSm
            anchors.rightMargin: Theme.spacingXxs
            spacing: Theme.spacingXs

            Label {
                id: commandLabel
                Layout.fillWidth: true
                text: commandRow.command
                elide: Text.ElideRight
                color: Theme.text
                font.family: Theme.fontFamilyMono !== "" ? Theme.fontFamilyMono : Theme.fontFamily
                font.pixelSize: Theme.fontSizeSm
            }

            AppIconButton {
                size: "sm"
                iconKind: "copy"
                tooltipText: qsTr("Sao chép lệnh")
                accessibleLabel: qsTr("Sao chép lệnh: %1").arg(commandRow.command)
                onClicked: controller.copyText(commandRow.command)
            }
        }
    }

    // Tested seam for the folder dialog (native dialogs are unreliable
    // headless; the dialog's onAccepted just calls this).
    function setOutputDir(path) {
        controller.outputDir = path
    }

    function valueIndex(options, value) {
        for (let i = 0; i < options.length; i++)
            if (options[i].value === value)
                return i;
        return 0;
    }

    // QUrl → local path string (same helper idiom as the other tabs).
    function toLocalPath(url) {
        const s = url.toString();
        if (!s.startsWith("file://"))
            return s;
        let path = decodeURIComponent(s.substring(7));
        // Windows: toString() is file:///C:/... — drop the stray slash the
        // empty host slot leaves before the drive letter, or downstream
        // slots receive /C:/... and every filesystem call fails.
        if (/^\/[A-Za-z]:\//.test(path))
            path = path.substring(1);
        return path;
    }

    // Local path string → valid QUrl string for FolderDialog currentFolder
    function toFolderUrl(path) {
        if (!path || path.trim() === "")
            return "";
        if (typeof controller !== "undefined" && controller && typeof controller.pathToUrl === "function") {
            const u = controller.pathToUrl(path);
            if (u !== "")
                return u;
        }
        if (path.startsWith("file://"))
            return path;
        const clean = path.replace(/\\/g, "/");
        if (/^[A-Za-z]:\//.test(clean))
            return "file:///" + clean;
        if (clean.startsWith("//"))
            return "file:" + clean;
        if (clean.startsWith("/"))
            return "file://" + clean;
        return "file:///" + clean;
    }

    FolderDialog {
        id: outputDirDialog

        objectName: "outputDirDialog"
        title: qsTr("Chọn thư mục xuất âm thanh")
        onAccepted: root.setOutputDir(root.toLocalPath(outputDirDialog.selectedFolder))
    }

    PageShell {
        anchors.fill: parent
        maxWidth: 840

        // Header Section
        PageHeader {
            Layout.fillWidth: true
            iconKind: "settings"
            title: qsTr("Cài đặt hệ thống")
            subtitle: qsTr("Cấu hình engine suy luận, âm thanh, giọng mặc định và giao diện hiển thị.")
        }

        // ── 1. Engine Card ────────────────────────────────────────────────
        // Compute selection only: backend + precision and the detector's
        // resolved-engine readout. The CUDA runtime that accelerates the
        // PyTorch backend is its own card below; the model source (which
        // repo the weights come from) is a third — three separate concerns
        // used to share one 800 px card.
        AppCard {
            Layout.fillWidth: true
            title: qsTr("Engine suy luận")
            subtitle: qsTr("Backend tính toán và độ chính xác mô hình cho VieNeu-TTS v3 Turbo")

            ColumnLayout {
                Layout.fillWidth: true
                spacing: Theme.spacingLg

                // Resolved-engine readout (detector capability view — same
                // string the sidebar rail shows). Deliberately a plain status
                // line: the bordered accent panel it replaced added chrome
                // without adding information.
                RowLayout {
                    Layout.fillWidth: true
                    spacing: Theme.spacingSm

                    AppIcon {
                        width: 14
                        height: 14
                        kind: "settings"
                        iconColor: Theme.accent
                        Layout.alignment: Qt.AlignTop
                    }

                    Label {
                        id: detectedEngineLabel
                        objectName: "detectedEngineLabel"
                        Layout.fillWidth: true
                        text: bridge ? bridge.engineNote : ""
                        color: Theme.textMuted
                        font.family: Theme.fontFamily
                        font.pixelSize: Theme.fontSizeSm
                        lineHeight: 1.3
                        wrapMode: Text.Wrap
                    }
                }

                Rectangle {
                    Layout.fillWidth: true
                    height: 1
                    color: Theme.borderSubtle
                    opacity: 0.7
                }

                // -- Backend row (responsive Grid: side-by-side → stacked) --
                GridLayout {
                    Layout.fillWidth: true
                    columns: root.isCompact ? 1 : 2
                    columnSpacing: Theme.spacingLg
                    rowSpacing: root.isCompact ? Theme.spacingSm : Theme.spacingLg

                    RowLayout {
                        Layout.fillWidth: true
                        Layout.alignment: root.isCompact ? Qt.AlignLeft : Qt.AlignVCenter
                        spacing: Theme.spacingMd

                        Rectangle {
                            width: 36
                            height: 36
                            radius: Theme.radiusMd
                            color: Theme.surfaceAlt
                            border.color: Theme.borderSubtle
                            border.width: 1
                            Layout.alignment: Qt.AlignTop
                            AppIcon {
                                anchors.centerIn: parent
                                width: 18
                                height: 18
                                kind: "settings"
                                iconColor: Theme.accent
                            }
                        }

                        ColumnLayout {
                            Layout.fillWidth: true
                            spacing: 3
                            Label {
                                text: qsTr("Backend suy luận")
                                color: Theme.text
                                font.family: Theme.fontFamily
                                font.pixelSize: Theme.fontSizeBase
                                font.weight: Theme.fontWeightMedium
                                wrapMode: Text.Wrap
                                Layout.fillWidth: true
                            }
                            Label {
                                text: qsTr("Chọn ONNX Runtime (CPU) hoặc PyTorch (NVIDIA GPU)")
                                color: Theme.textMuted
                                font.family: Theme.fontFamily
                                font.pixelSize: Theme.fontSizeXs
                                wrapMode: Text.Wrap
                                Layout.fillWidth: true
                                lineHeight: 1.2
                            }
                        }
                    }

                    AppCombo {
                        id: backendCombo
                        objectName: "backendCombo"
                        // 280 px fits the longest VI label ("Tự động … CUDA")
                        // without eliding; on compact it stretches full-width.
                        Layout.fillWidth: root.isCompact
                        Layout.preferredWidth: root.isCompact ? 0 : 280
                        Layout.alignment: root.isCompact ? Qt.AlignLeft : Qt.AlignRight | Qt.AlignVCenter
                        comboWidth: 280
                        accessibleLabel: qsTr("Backend suy luận")
                        textRole: "label"
                        model: root.backendOptions
                        currentIndex: root.valueIndex(root.backendOptions, controller.backend)
                        onActivated: function (index) {
                            controller.backend = root.backendOptions[index].value;
                        }
                    }
                }

                Rectangle {
                    Layout.fillWidth: true
                    height: 1
                    color: Theme.borderSubtle
                    opacity: 0.7
                }

                // -- Precision row --
                GridLayout {
                    Layout.fillWidth: true
                    columns: root.isCompact ? 1 : 2
                    columnSpacing: Theme.spacingLg
                    rowSpacing: root.isCompact ? Theme.spacingSm : Theme.spacingLg

                    RowLayout {
                        Layout.fillWidth: true
                        Layout.alignment: root.isCompact ? Qt.AlignLeft : Qt.AlignVCenter
                        spacing: Theme.spacingMd

                        Rectangle {
                            width: 36
                            height: 36
                            radius: Theme.radiusMd
                            color: Theme.surfaceAlt
                            border.color: Theme.borderSubtle
                            border.width: 1
                            Layout.alignment: Qt.AlignTop
                            AppIcon {
                                anchors.centerIn: parent
                                width: 18
                                height: 18
                                kind: "refresh"
                                iconColor: Theme.accent
                            }
                        }

                        ColumnLayout {
                            Layout.fillWidth: true
                            spacing: 3
                            Label {
                                text: qsTr("Độ chính xác mô hình (ONNX)")
                                color: Theme.text
                                font.family: Theme.fontFamily
                                font.pixelSize: Theme.fontSizeBase
                                font.weight: Theme.fontWeightMedium
                                wrapMode: Text.Wrap
                                Layout.fillWidth: true
                            }
                            Label {
                                text: qsTr("int8: tối ưu tốc độ & bộ nhớ; fp32: chất lượng cao nhất")
                                color: Theme.textMuted
                                font.family: Theme.fontFamily
                                font.pixelSize: Theme.fontSizeXs
                                wrapMode: Text.Wrap
                                Layout.fillWidth: true
                                lineHeight: 1.2
                            }
                        }
                    }

                    AppCombo {
                        id: precisionCombo
                        objectName: "precisionCombo"
                        Layout.fillWidth: root.isCompact
                        Layout.preferredWidth: root.isCompact ? 0 : 280
                        Layout.alignment: root.isCompact ? Qt.AlignLeft : Qt.AlignRight | Qt.AlignVCenter
                        comboWidth: 280
                        accessibleLabel: qsTr("Độ chính xác mô hình")
                        textRole: "label"
                        model: root.precisionOptions
                        currentIndex: root.valueIndex(root.precisionOptions, controller.precision)
                        onActivated: function (index) {
                            controller.precision = root.precisionOptions[index].value;
                        }
                    }
                }

                // Needs restart banner
                AppNotice {
                    Layout.fillWidth: true
                    tone: "warning"
                    title: qsTr("Áp dụng khi khởi động lại")
                    message: qsTr("Thay đổi backend/độ chính xác/nguồn mô hình sẽ áp dụng ở lần khởi động engine tiếp theo.")
                    messageObjectName: "needsRestartBanner"
                    visible: controller.needsRestart
                }
            }
        }

        // ── 2. CUDA acceleration Card ─────────────────────────────────────
        // Managed CUDA is always user-initiated. This card never imports
        // torch, starts a download, or scans local installs; its controls
        // call only the explicit controller slots. Only the states that need
        // them (install in flight/failed, unusable driver, a scan already
        // run) expand the diagnostics below — an idle install flow should not
        // occupy a third of the settings page.
        AppCard {
            id: cudaRuntimeCard
            objectName: "cudaRuntimeCard"
            Layout.fillWidth: true
            title: qsTr("Runtime CUDA được quản lý")
            subtitle: root.cudaRuntimeSupported
                ? (root.cudaRuntimeDriverChecked && !root.cudaRuntimeDriverReady
                    ? qsTr("Cài đặt bị tắt: cần GPU NVIDIA và driver hỗ trợ CUDA 12.8 trở lên.")
                    : qsTr("Cài đặt runtime NVIDIA CUDA đã xác thực để tăng tốc PyTorch trên GPU tương thích."))
                : qsTr("Runtime CUDA được quản lý chỉ hỗ trợ trên Windows và Linux x64.")
            badgeText: {
                if (!root.cudaRuntimeSupported)
                    return qsTr("Không hỗ trợ");
                switch (root.cudaRuntimeState) {
                case "ready":
                    return qsTr("Sẵn sàng");
                case "downloading":
                    return qsTr("Đang tải");
                case "verifying":
                    return qsTr("Đang xác thực");
                case "failed":
                    return qsTr("Cần chú ý");
                case "checking":
                    return qsTr("Đang kiểm tra");
                default:
                    return qsTr("Chưa cài đặt");
                }
            }
            badgeColor: {
                if (!root.cudaRuntimeSupported || root.cudaRuntimeState === "failed")
                    return Theme.errorSubtle;
                if (root.cudaRuntimeState === "ready")
                    return Theme.successSubtle;
                if (root.cudaRuntimeState === "downloading"
                        || root.cudaRuntimeState === "verifying")
                    return Theme.accentSubtle;
                return Theme.warningSubtle;
            }
            badgeTextColor: {
                if (!root.cudaRuntimeSupported || root.cudaRuntimeState === "failed")
                    return Theme.errorText;
                if (root.cudaRuntimeState === "ready")
                    return Theme.successText;
                if (root.cudaRuntimeState === "downloading"
                        || root.cudaRuntimeState === "verifying")
                    return Theme.accent;
                return Theme.warningText;
            }

            ColumnLayout {
                Layout.fillWidth: true
                spacing: Theme.spacingMd

                Label {
                    Layout.fillWidth: true
                    text: {
                        if (!root.cudaRuntimeSupported)
                            return qsTr("Runtime CUDA không khả dụng trên nền tảng này.");
                        switch (root.cudaRuntimeState) {
                        case "ready":
                            return qsTr("Runtime CUDA đã sẵn sàng và đã được xác thực.");
                        case "downloading":
                            return qsTr("Đang tải runtime CUDA…");
                        case "verifying":
                            return qsTr("Đang xác thực các tệp runtime CUDA…");
                        case "failed":
                            return qsTr("Không thể chuẩn bị runtime CUDA.");
                        case "checking":
                            return qsTr("Runtime CUDA sẽ chỉ được kiểm tra khi bạn yêu cầu.");
                        default:
                            return qsTr("Chưa cài đặt runtime CUDA được quản lý.");
                        }
                    }
                    color: Theme.textMuted
                    font.family: Theme.fontFamily
                    font.pixelSize: Theme.fontSizeSm
                    wrapMode: Text.Wrap
                    lineHeight: 1.25
                }

                // Notices stay outside the disclosure: every one of them is an
                // actionable state the user must see without expanding
                // anything.
                AppNotice {
                    id: cudaRuntimeUnsupportedNotice
                    objectName: "cudaRuntimeUnsupportedNotice"
                    Layout.fillWidth: true
                    tone: "warning"
                    title: qsTr("Không hỗ trợ runtime CUDA")
                    message: controller ? controller.cudaRuntimeError : ""
                    messageObjectName: "cudaRuntimeUnsupportedError"
                    visible: !root.cudaRuntimeSupported
                        && (controller ? controller.cudaRuntimeError !== "" : false)
                }

                AppNotice {
                    id: cudaRuntimeFailureNotice
                    Layout.fillWidth: true
                    tone: "error"
                    title: qsTr("Cài đặt runtime CUDA thất bại")
                    message: controller ? controller.cudaRuntimeError : ""
                    messageObjectName: "cudaRuntimeErrorLabel"
                    visible: root.cudaRuntimeSupported
                        && root.cudaRuntimeState === "failed"
                        && (controller ? controller.cudaRuntimeError !== "" : false)
                }

                AppNotice {
                    id: cudaRuntimeDriverNotice
                    objectName: "cudaRuntimeDriverNotice"
                    Layout.fillWidth: true
                    tone: "warning"
                    title: qsTr("Cần GPU NVIDIA và driver CUDA")
                    message: qsTr("Không thể cài đặt runtime CUDA nhiều GB cho đến khi phát hiện GPU NVIDIA và driver hỗ trợ CUDA 12.8 trở lên. Bạn vẫn có thể kiểm tra các runtime cục bộ để chẩn đoán.")
                    visible: root.cudaRuntimeSupported
                        && root.cudaRuntimeDriverChecked
                        && !root.cudaRuntimeDriverReady
                }

                AppNotice {
                    id: cudaRuntimeRestartNotice
                    objectName: "cudaRuntimeRestartNotice"
                    Layout.fillWidth: true
                    tone: "warning"
                    title: qsTr("Khởi động lại để áp dụng")
                    message: qsTr("Khởi động lại ứng dụng để dùng runtime CUDA mới cài đặt. Nếu một engine CUDA đã chạy, hãy khởi động lại trước khi gỡ runtime.")
                    visible: root.cudaRuntimeSupported
                        && root.cudaRuntimeState === "ready"
                }

                Flow {
                    Layout.fillWidth: true
                    spacing: Theme.spacingSm
                    visible: root.cudaRuntimeSupported

                    AppButton {
                        id: cudaRuntimeInstallButton
                        objectName: "cudaRuntimeInstallButton"
                        variant: "primary"
                        size: "sm"
                        iconKind: "download"
                        text: qsTr("Cài đặt runtime CUDA")
                        accessibleLabel: qsTr("Cài đặt runtime CUDA")
                        visible: root.cudaRuntimeState === "unavailable"
                            || root.cudaRuntimeState === "checking"
                        enabled: root.cudaRuntimeInstallAllowed
                        disabledReason: qsTr("Cần GPU NVIDIA và driver CUDA từ 12.8 trở lên — xem hướng dẫn ở trên.")
                        onClicked: controller.installCudaRuntime()
                    }

                    AppButton {
                        id: cudaRuntimeCancelButton
                        objectName: "cudaRuntimeCancelButton"
                        variant: "secondary"
                        size: "sm"
                        iconKind: "close"
                        text: qsTr("Hủy tải runtime CUDA")
                        accessibleLabel: qsTr("Hủy tải runtime CUDA")
                        visible: root.cudaRuntimeState === "downloading"
                            || root.cudaRuntimeState === "verifying"
                        onClicked: controller.cancelCudaRuntimeInstall()
                    }

                    AppButton {
                        id: cudaRuntimeRetryButton
                        objectName: "cudaRuntimeRetryButton"
                        variant: "primary"
                        size: "sm"
                        iconKind: "refresh"
                        text: qsTr("Thử lại cài đặt runtime CUDA")
                        accessibleLabel: qsTr("Thử lại cài đặt runtime CUDA")
                        visible: root.cudaRuntimeState === "failed"
                        enabled: root.cudaRuntimeInstallAllowed
                        disabledReason: qsTr("Cần GPU NVIDIA và driver CUDA từ 12.8 trở lên — xem hướng dẫn ở trên.")
                        onClicked: controller.installCudaRuntime()
                    }

                    AppButton {
                        id: cudaRuntimeRemoveButton
                        objectName: "cudaRuntimeRemoveButton"
                        variant: "danger"
                        size: "sm"
                        iconKind: "close"
                        text: qsTr("Gỡ runtime CUDA")
                        accessibleLabel: qsTr("Gỡ runtime CUDA")
                        visible: root.cudaRuntimeState === "ready"
                        onClicked: controller.removeCudaRuntime()
                    }
                }

                AppButton {
                    id: cudaRuntimeDetailsToggle
                    objectName: "cudaRuntimeDetailsToggle"
                    variant: "quiet"
                    size: "sm"
                    iconKind: root.cudaRuntimeDetailsExpanded ? "chevronUp" : "chevronDown"
                    text: root.cudaRuntimeDetailsExpanded
                        ? qsTr("Ẩn chi tiết & chẩn đoán")
                        : qsTr("Chi tiết & chẩn đoán")
                    accessibleLabel: qsTr("Chi tiết & chẩn đoán runtime CUDA")
                    visible: root.cudaRuntimeSupported
                    onClicked: root.cudaRuntimeDetailsExpanded = !root.cudaRuntimeDetailsExpanded
                }

                // ── Diagnostics & guidance (collapsed unless needed) ──────
                ColumnLayout {
                    id: cudaRuntimeDetails
                    objectName: "cudaRuntimeDetails"
                    Layout.fillWidth: true
                    spacing: Theme.spacingMd
                    visible: root.cudaRuntimeSupported && root.cudaRuntimeDetailsExpanded

                    // Transfer progress — the byte counts are only meaningful
                    // while an install is in flight or already verified; the
                    // idle "Đã tải 0 / cần 0 byte" was pure noise.
                    ColumnLayout {
                        Layout.fillWidth: true
                        spacing: Theme.spacingXs

                        Label {
                            id: cudaRuntimeStorageLabel
                            objectName: "cudaRuntimeStorageLabel"
                            Layout.fillWidth: true
                            text: qsTr("Đã tải %1 / cần %2 byte")
                                // String(): QML's number→text conversion renders
                                // multi-GB counts as "7.92341e+09" otherwise.
                                .arg(controller ? String(controller.cudaRuntimeInstalledBytes) : "0")
                                .arg(controller ? String(controller.cudaRuntimeRequiredBytes) : "0")
                            color: Theme.textMuted
                            font.family: Theme.fontFamily
                            font.pixelSize: Theme.fontSizeXs
                            visible: root.cudaRuntimeState === "downloading"
                                || root.cudaRuntimeState === "verifying"
                                || root.cudaRuntimeState === "ready"
                        }

                        ProgressBar {
                            id: cudaRuntimeProgress
                            objectName: "cudaRuntimeProgress"
                            Layout.fillWidth: true
                            from: 0
                            to: 1
                            value: controller ? controller.cudaRuntimeProgress : 0
                            visible: root.cudaRuntimeState === "downloading"
                                || root.cudaRuntimeState === "verifying"
                            Accessible.name: qsTr("Tiến trình tải runtime CUDA")
                        }
                    }

                    // OS-aware driver upgrade guide — same gate as the driver
                    // notice above. Commands are copy targets, not prose.
                    ColumnLayout {
                        id: cudaRuntimeDriverGuide
                        objectName: "cudaRuntimeDriverGuide"
                        Layout.fillWidth: true
                        spacing: Theme.spacingXs
                        visible: root.cudaRuntimeDriverChecked && !root.cudaRuntimeDriverReady

                        Label {
                            id: cudaRuntimeDriverGuideLinux
                            objectName: "cudaRuntimeDriverGuideLinux"
                            Layout.fillWidth: true
                            text: qsTr("Linux: kiểm tra driver và dòng `CUDA Version` (cần ≥ 12.8):")
                            color: Theme.textMuted
                            font.family: Theme.fontFamily
                            font.pixelSize: Theme.fontSizeSm
                            wrapMode: Text.Wrap
                            lineHeight: 1.25
                            visible: Qt.platform.os === "linux"
                        }

                        ColumnLayout {
                            Layout.fillWidth: true
                            spacing: Theme.spacingXxs
                            visible: Qt.platform.os === "linux"

                            CommandRow { command: "nvidia-smi" }
                            CommandRow { command: "ubuntu-drivers devices" }
                            CommandRow { command: "sudo ubuntu-drivers autoinstall" }
                        }

                        Label {
                            objectName: "cudaRuntimeDriverGuideLinuxNote"
                            Layout.fillWidth: true
                            text: qsTr("Sau khi cài driver, khởi động lại máy. Chỉ cần driver — không cần cài CUDA Toolkit.")
                            color: Theme.textSubtle
                            font.family: Theme.fontFamily
                            font.pixelSize: Theme.fontSizeXs
                            wrapMode: Text.Wrap
                            lineHeight: 1.25
                            visible: Qt.platform.os === "linux"
                        }

                        Label {
                            id: cudaRuntimeDriverGuideWindows
                            objectName: "cudaRuntimeDriverGuideWindows"
                            Layout.fillWidth: true
                            text: qsTr("Windows: chạy lệnh sau trong Command Prompt hoặc PowerShell và xem dòng `CUDA Version` (cần ≥ 12.8):")
                            color: Theme.textMuted
                            font.family: Theme.fontFamily
                            font.pixelSize: Theme.fontSizeSm
                            wrapMode: Text.Wrap
                            lineHeight: 1.25
                            visible: Qt.platform.os === "windows"
                        }

                        ColumnLayout {
                            Layout.fillWidth: true
                            spacing: Theme.spacingXxs
                            visible: Qt.platform.os === "windows"

                            CommandRow { command: "nvidia-smi" }
                        }

                        Label {
                            objectName: "cudaRuntimeDriverGuideWindowsNote"
                            Layout.fillWidth: true
                            text: qsTr("Nếu chưa có driver, cập nhật qua GeForce Experience hoặc nút tải bên dưới (Game Ready / Studio), rồi khởi động lại.")
                            color: Theme.textSubtle
                            font.family: Theme.fontFamily
                            font.pixelSize: Theme.fontSizeXs
                            wrapMode: Text.Wrap
                            lineHeight: 1.25
                            visible: Qt.platform.os === "windows"
                        }

                        Label {
                            Layout.fillWidth: true
                            text: qsTr("Máy không có GPU NVIDIA thì không dùng được runtime CUDA — dùng backend ONNX (CPU).")
                            color: Theme.textSubtle
                            font.family: Theme.fontFamily
                            font.pixelSize: Theme.fontSizeXs
                            wrapMode: Text.Wrap
                            visible: Qt.platform.os !== "linux" && Qt.platform.os !== "windows"
                        }

                        AppButton {
                            id: cudaRuntimeDriverDownloadButton
                            objectName: "cudaRuntimeDriverDownloadButton"
                            variant: "quiet"
                            size: "sm"
                            iconKind: "externalLink"
                            text: qsTr("Mở trang tải driver NVIDIA")
                            accessibleLabel: qsTr("Mở trang tải driver NVIDIA")
                            onClicked: Qt.openUrlExternally("https://www.nvidia.com/Download/index.aspx")
                        }
                    }

                    // Local install scan — diagnostic only, and the disclaimer
                    // is only relevant once a scan has actually run.
                    ColumnLayout {
                        Layout.fillWidth: true
                        spacing: Theme.spacingXs

                        AppButton {
                            id: cudaRuntimeDetectLocalButton
                            objectName: "cudaRuntimeDetectLocalButton"
                            variant: "quiet"
                            size: "sm"
                            iconKind: "settings"
                            text: qsTr("Kiểm tra runtime CUDA cục bộ")
                            accessibleLabel: qsTr("Kiểm tra runtime CUDA cục bộ")
                            onClicked: {
                                root.localCudaRuntimeScanRequested = true;
                                controller.discoverLocalCudaRuntimes();
                            }
                        }

                        Label {
                            id: cudaRuntimeLocalSummary
                            objectName: "cudaRuntimeLocalSummary"
                            Layout.fillWidth: true
                            text: (root.localCudaRuntimeScanRequested
                                    || root.localCudaRuntimeCount > 0)
                                ? qsTr("Đã phát hiện %1 runtime CUDA cục bộ.")
                                    .arg(root.localCudaRuntimeCount)
                                : qsTr("Chưa quét runtime CUDA cục bộ.")
                            color: Theme.textMuted
                            font.family: Theme.fontFamily
                            font.pixelSize: Theme.fontSizeSm
                        }

                        Label {
                            Layout.fillWidth: true
                            text: qsTr("Quét này chỉ để chẩn đoán. Ứng dụng chỉ sử dụng runtime được quản lý đã xác thực.")
                            color: Theme.textSubtle
                            font.family: Theme.fontFamily
                            font.pixelSize: Theme.fontSizeXs
                            wrapMode: Text.Wrap
                            visible: root.localCudaRuntimeScanRequested || root.localCudaRuntimeCount > 0
                        }

                        Repeater {
                            model: controller ? controller.localCudaRuntimes : []

                            delegate: Label {
                                required property var modelData
                                Layout.fillWidth: true
                                text: modelData.compatible
                                    ? qsTr("%1 — tương thích").arg(modelData.label)
                                    : qsTr("%1 — không tương thích: %2").arg(modelData.label).arg(modelData.reason)
                                color: modelData.compatible ? Theme.successText : Theme.warningText
                                font.family: Theme.fontFamily
                                font.pixelSize: Theme.fontSizeXs
                                wrapMode: Text.Wrap
                            }
                        }
                    }
                }
            }
        }

        // ── 3. Model source Card ──────────────────────────────────────────
        // Which weights the engine loads. The mode chips are the single
        // affordance for the choice and the repo field only exists in custom
        // mode — the official id used to be restated by a chip, the field and
        // the validation line in the same viewport.
        AppCard {
            Layout.fillWidth: true
            title: qsTr("Nguồn mô hình (Hugging Face)")
            subtitle: qsTr("Dùng mô hình gốc chính thức hoặc trỏ tới repository Hugging Face tùy chỉnh")

            ColumnLayout {
                Layout.fillWidth: true
                spacing: Theme.spacingMd

                // Preset Quick Selector Chips
                RowLayout {
                    Layout.fillWidth: true
                    spacing: Theme.spacingSm

                    AppButton {
                        id: officialRepoChip
                        objectName: "officialRepoChip"
                        variant: root.customRepoMode ? "secondary" : "primary"
                        size: "sm"
                        iconKind: "check"
                        text: qsTr("pnnbao-ump/VieNeu-TTS-v3-Turbo (mặc định)")
                        accessibleLabel: qsTr("Chọn mô hình chính thức mặc định")
                        onClicked: {
                            root.customRepoRequested = false;
                            controller.modelRepo = "";
                            modelRepoField.text = "";
                        }
                    }

                    AppButton {
                        id: customRepoChip
                        objectName: "customRepoChip"
                        variant: root.customRepoMode ? "primary" : "secondary"
                        size: "sm"
                        iconKind: "settings"
                        text: qsTr("Repo tùy chỉnh")
                        accessibleLabel: qsTr("Nhập repository tùy chỉnh")
                        onClicked: {
                            root.customRepoRequested = true;
                            modelRepoField.forceActiveFocus();
                            modelRepoField.selectAll();
                        }
                    }
                }

                // Custom repository input — only while a custom repo is in
                // effect or being entered.
                ColumnLayout {
                    Layout.fillWidth: true
                    spacing: Theme.spacingXs
                    visible: root.customRepoMode

                    Rectangle {
                        Layout.fillWidth: true
                        implicitHeight: inputRow.implicitHeight + Theme.spacingSm * 2
                        radius: Theme.radiusMd
                        color: Theme.surfaceAlt
                        border.color: modelRepoField.activeFocus ? Theme.borderFocus : Theme.borderSubtle
                        border.width: 1

                        Behavior on border.color { ColorAnimation { duration: Theme.durationFast } }

                        RowLayout {
                            id: inputRow
                            anchors.fill: parent
                            anchors.margins: Theme.spacingSm
                            spacing: Theme.spacingSm

                            // Prefix badge: "hf.co/"
                            Rectangle {
                                implicitHeight: 32
                                implicitWidth: prefixLabel.implicitWidth + Theme.spacingMd
                                radius: Theme.radiusSm
                                color: Theme.surface
                                border.color: Theme.borderSubtle
                                border.width: 1
                                Layout.alignment: Qt.AlignVCenter

                                Label {
                                    id: prefixLabel
                                    anchors.centerIn: parent
                                    text: "hf.co/"
                                    color: Theme.textMuted
                                    font.family: Theme.fontFamilyMono !== "" ? Theme.fontFamilyMono : Theme.fontFamily
                                    font.pixelSize: Theme.fontSizeSm
                                    font.weight: Theme.fontWeightMedium
                                }
                            }

                            TextField {
                                id: modelRepoField
                                objectName: "modelRepoField"
                                Layout.fillWidth: true
                                Layout.alignment: Qt.AlignVCenter
                                placeholderText: "pnnbao-ump/VieNeu-TTS-v3-Turbo"
                                placeholderTextColor: Theme.textSubtle
                                color: Theme.text
                                selectedTextColor: Theme.accentText
                                selectionColor: Theme.accent
                                font.family: Theme.fontFamilyMono !== "" ? Theme.fontFamilyMono : Theme.fontFamily
                                font.pixelSize: Theme.fontSizeBase
                                implicitHeight: 36
                                leftPadding: Theme.spacingSm
                                rightPadding: Theme.spacingSm
                                selectByMouse: true
                                Accessible.name: qsTr("Nguồn mô hình")

                                background: Rectangle {
                                    color: "transparent"
                                }

                                text: controller.modelRepo
                                // Commit on focus-loss/Enter only — never per keystroke
                                onEditingFinished: controller.modelRepo = text
                            }

                            // Quick Reset Button
                            AppIconButton {
                                id: modelRepoResetButton
                                objectName: "modelRepoResetButton"
                                size: "sm"
                                iconKind: "reset"
                                tooltipText: qsTr("Khôi phục repo chính thức mặc định")
                                accessibleLabel: qsTr("Khôi phục repo chính thức mặc định")
                                visible: (controller && controller.modelRepo !== "") || (modelRepoField.text.trim() !== "")
                                Layout.alignment: Qt.AlignVCenter
                                onClicked: {
                                    root.customRepoRequested = false;
                                    controller.modelRepo = "";
                                    modelRepoField.text = "";
                                }
                            }

                            // Open on Hugging Face Button
                            AppButton {
                                id: openHfButton
                                objectName: "openHfButton"
                                variant: "secondary"
                                size: "sm"
                                iconKind: "externalLink"
                                text: qsTr("Hugging Face")
                                tooltipText: qsTr("Mở trang mô hình trên Hugging Face")
                                accessibleLabel: qsTr("Mở trang mô hình trên Hugging Face")
                                Layout.alignment: Qt.AlignVCenter
                                onClicked: {
                                    const repo = (controller && controller.modelRepo !== "") ? controller.modelRepo : "pnnbao-ump/VieNeu-TTS-v3-Turbo";
                                    Qt.openUrlExternally("https://huggingface.co/" + repo);
                                }
                            }
                        }
                    }

                    // Contextual format feedback note
                    RowLayout {
                        Layout.fillWidth: true
                        spacing: Theme.spacingXs

                        readonly property string currentText: modelRepoField.text.trim()
                        readonly property bool isValidRepo: /^[^\s/]+\/[^\s/]+$/.test(currentText)

                        AppIcon {
                            width: 14
                            height: 14
                            kind: parent.isValidRepo ? "check" : "close"
                            iconColor: parent.isValidRepo ? Theme.accent : Theme.error
                            Layout.alignment: Qt.AlignVCenter
                        }

                        Label {
                            Layout.fillWidth: true
                            text: parent.currentText === ""
                                ? qsTr("Để trống để dùng mô hình chính thức, hoặc nhập dạng 'tác_giả/tên_repo'")
                                : (parent.isValidRepo
                                    ? qsTr("Repository hợp lệ: huggingface.co/%1 (sẽ tự động tải khi khởi động engine)").arg(parent.currentText)
                                    : qsTr("Định dạng chưa đúng: cần có dạng 'tác_giả/tên_repo' (ví dụ: username/custom-model, không có khoảng trắng)"))
                            color: parent.currentText === "" || parent.isValidRepo ? Theme.textMuted : Theme.errorText
                            font.family: Theme.fontFamily
                            font.pixelSize: Theme.fontSizeXs
                            wrapMode: Text.Wrap
                            lineHeight: 1.2
                        }
                    }
                }

                // Capability of the official baseline. The removed feedback
                // row restated the chip and the folder path, but these two
                // facts (48 kHz, both shipped languages) exist nowhere else on
                // this card.
                Label {
                    Layout.fillWidth: true
                    visible: !root.customRepoMode
                    text: qsTr("Mô hình gốc 48 kHz, hỗ trợ tiếng Việt và tiếng Anh.")
                    color: Theme.textMuted
                    font.family: Theme.fontFamily
                    font.pixelSize: Theme.fontSizeXs
                    wrapMode: Text.Wrap
                }

                // Model state (managed install health) — independent of which
                // source is selected.
                Label {
                    objectName: "modelSourceDetail"
                    Layout.fillWidth: true
                    text: {
                        if (!controller)
                            return "";
                        if (controller.modelRepo !== "")
                            return qsTr("Nguồn tùy chỉnh nâng cao — bản tải chính thức không áp dụng.");
                        switch (controller.modelState) {
                        case "ready":
                            return qsTr("Baseline chính thức đã xác thực, sẵn sàng ngoại tuyến.");
                        case "downloading":
                            return qsTr("Đang tải baseline chính thức...");
                        case "validating":
                            return qsTr("Đang xác thực baseline chính thức...");
                        case "failed":
                            return qsTr("Baseline chính thức lỗi — xem màn hình thiết lập.");
                        default:
                            return qsTr("Baseline chính thức được quản lý tại thư mục dữ liệu.");
                        }
                    }
                    color: Theme.textMuted
                    font.family: Theme.fontFamily
                    font.pixelSize: Theme.fontSizeXs
                    wrapMode: Text.Wrap
                    lineHeight: 1.2
                }

                // Local location of the official weights. Hidden in custom
                // mode: `modelDir` is the managed baseline dir, and showing it
                // next to "custom source — the official download does not
                // apply" reads as a contradiction.
                RowLayout {
                    Layout.fillWidth: true
                    spacing: Theme.spacingXs
                    visible: !root.customRepoMode

                    Label {
                        text: qsTr("Thư mục:")
                        color: Theme.textMuted
                        font.family: Theme.fontFamily
                        font.pixelSize: Theme.fontSizeXs
                    }
                    Label {
                        id: settingsModelDirLabel
                        objectName: "settingsModelDirLabel"
                        Layout.fillWidth: true
                        text: controller ? controller.modelDir : ""
                        elide: Text.ElideMiddle
                        color: Theme.text
                        font.family: Theme.fontFamilyMono !== "" ? Theme.fontFamilyMono : Theme.fontFamily
                        font.pixelSize: Theme.fontSizeXs
                    }
                    AppIconButton {
                        id: settingsModelDirCopyButton
                        objectName: "settingsModelDirCopyButton"
                        size: "sm"
                        iconKind: "copy"
                        tooltipText: qsTr("Sao chép đường dẫn thư mục mô hình")
                        accessibleLabel: qsTr("Sao chép đường dẫn thư mục mô hình")
                        onClicked: controller.copyModelDir()
                    }
                    AppIconButton {
                        id: settingsModelDirOpenButton
                        objectName: "settingsModelDirOpenButton"
                        size: "sm"
                        iconKind: "folder"
                        tooltipText: qsTr("Mở thư mục mô hình")
                        accessibleLabel: qsTr("Mở thư mục mô hình")
                        onClicked: controller.openModelDir()
                    }
                }
            }
        }

        // ── 2. Audio & Synthesis Card ─────────────────────────────────────
        AppCard {
            Layout.fillWidth: true
            title: qsTr("Tổng hợp & Âm thanh")
            subtitle: qsTr("Thiết lập thông số giọng đọc và thư mục lưu trữ")

            ColumnLayout {
                Layout.fillWidth: true
                spacing: Theme.spacingLg

                // -- Default Voice (Wave icon + VoicePicker) --
                GridLayout {
                    Layout.fillWidth: true
                    columns: root.isCompact ? 1 : 2
                    columnSpacing: Theme.spacingLg
                    rowSpacing: root.isCompact ? Theme.spacingSm : Theme.spacingLg

                    RowLayout {
                        Layout.fillWidth: true
                        Layout.alignment: root.isCompact ? Qt.AlignLeft : Qt.AlignVCenter
                        spacing: Theme.spacingMd
                        Rectangle {
                            width: 36
                            height: 36
                            radius: Theme.radiusMd
                            color: Theme.surfaceAlt
                            border.color: Theme.borderSubtle
                            border.width: 1
                            Layout.alignment: Qt.AlignTop
                            AppIcon {
                                anchors.centerIn: parent
                                width: 18
                                height: 18
                                kind: "wave"
                                iconColor: Theme.accent
                            }
                        }
                        ColumnLayout {
                            Layout.fillWidth: true
                            spacing: 3
                            Label {
                                text: qsTr("Giọng đọc mặc định")
                                color: Theme.text
                                font.family: Theme.fontFamily
                                font.pixelSize: Theme.fontSizeBase
                                font.weight: Theme.fontWeightMedium
                                wrapMode: Text.Wrap
                                Layout.fillWidth: true
                            }
                            Label {
                                Layout.fillWidth: true
                                text: qsTr("Giọng được tự động chọn khi mở ứng dụng")
                                color: Theme.textMuted
                                font.family: Theme.fontFamily
                                font.pixelSize: Theme.fontSizeXs
                                wrapMode: Text.Wrap
                                lineHeight: 1.2
                            }
                        }
                    }

                    VoicePicker {
                        id: defaultVoiceCombo
                        objectName: "defaultVoiceCombo"
                        purpose: "default"
                        Layout.fillWidth: root.isCompact
                        Layout.preferredWidth: root.isCompact ? 0 : 320
                        Layout.alignment: root.isCompact ? Qt.AlignLeft : Qt.AlignRight | Qt.AlignVCenter
                        implicitWidth: 320
                    }
                }

                Rectangle {
                    Layout.fillWidth: true
                    height: 1
                    color: Theme.borderSubtle
                    opacity: 0.7
                }

                // -- Output Directory (full-width field) --
                ColumnLayout {
                    Layout.fillWidth: true
                    spacing: Theme.spacingSm

                    RowLayout {
                        Layout.fillWidth: true
                        spacing: Theme.spacingMd

                        Rectangle {
                            width: 36
                            height: 36
                            radius: Theme.radiusMd
                            color: Theme.surfaceAlt
                            border.color: Theme.borderSubtle
                            border.width: 1
                            Layout.alignment: Qt.AlignTop
                            AppIcon {
                                anchors.centerIn: parent
                                width: 18
                                height: 18
                                kind: "folder"
                                iconColor: Theme.accent
                            }
                        }

                        ColumnLayout {
                            Layout.fillWidth: true
                            spacing: 3
                            Label {
                                text: qsTr("Thư mục xuất âm thanh")
                                color: Theme.text
                                font.family: Theme.fontFamily
                                font.pixelSize: Theme.fontSizeBase
                                font.weight: Theme.fontWeightMedium
                            }
                            Label {
                                Layout.fillWidth: true
                                text: qsTr("Vị trí lưu trữ các tệp âm thanh xuất ra (.wav/.mp3)")
                                color: Theme.textMuted
                                font.family: Theme.fontFamily
                                font.pixelSize: Theme.fontSizeXs
                                wrapMode: Text.Wrap
                                lineHeight: 1.2
                            }
                        }
                    }

                    Rectangle {
                        Layout.fillWidth: true
                        implicitHeight: 52
                        radius: Theme.radiusMd
                        color: Theme.surfaceAlt
                        border.color: Theme.borderSubtle
                        border.width: 1

                        RowLayout {
                            anchors.fill: parent
                            anchors.margins: Theme.spacingSm
                            spacing: Theme.spacingSm

                            Rectangle {
                                width: 28
                                height: 28
                                radius: Theme.radiusSm
                                color: Theme.surface
                                border.color: Theme.borderSubtle
                                border.width: 1
                                Layout.alignment: Qt.AlignVCenter
                                AppIcon {
                                    anchors.centerIn: parent
                                    width: 14
                                    height: 14
                                    kind: "file"
                                    iconColor: Theme.textMuted
                                }
                            }

                            Label {
                                id: outputDirLabel
                                objectName: "outputDirLabel"
                                Layout.fillWidth: true
                                text: controller.outputDir !== ""
                                    ? controller.outputDir
                                    : qsTr("Mặc định: ~/Music/VieNeuTTS")
                                elide: Text.ElideMiddle
                                color: controller.outputDir !== "" ? Theme.text : Theme.textMuted
                                font.family: Theme.fontFamilyMono !== "" ? Theme.fontFamilyMono : Theme.fontFamily
                                font.pixelSize: Theme.fontSizeSm
                            }

                            AppButton {
                                id: outputDirBrowseButton
                                objectName: "outputDirBrowseButton"
                                variant: "secondary"
                                size: "sm"
                                text: qsTr("Thay đổi…")
                                iconKind: "folder"
                                onClicked: {
                                    if (controller.outputDir !== "") {
                                        const folder = root.toFolderUrl(controller.outputDir);
                                        if (folder !== "")
                                            outputDirDialog.currentFolder = folder;
                                    }
                                    outputDirDialog.open();
                                }
                            }

                            AppIconButton {
                                id: outputDirResetButton
                                objectName: "outputDirResetButton"
                                iconKind: "reset"
                                tooltipText: qsTr("Khôi phục thư mục mặc định")
                                accessibleLabel: qsTr("Khôi phục thư mục mặc định")
                                visible: controller.outputDir !== ""
                                onClicked: controller.outputDir = ""
                            }
                        }
                    }
                }

                Rectangle {
                    Layout.fillWidth: true
                    height: 1
                    color: Theme.borderSubtle
                    opacity: 0.7
                }

                // -- Export format (batch + audiobook container; dialogs pick per-file) --
                GridLayout {
                    Layout.fillWidth: true
                    columns: root.isCompact ? 1 : 2
                    columnSpacing: Theme.spacingLg
                    rowSpacing: root.isCompact ? Theme.spacingSm : Theme.spacingLg

                    RowLayout {
                        Layout.fillWidth: true
                        Layout.alignment: root.isCompact ? Qt.AlignLeft : Qt.AlignVCenter
                        spacing: Theme.spacingMd
                        Rectangle {
                            width: 36
                            height: 36
                            radius: Theme.radiusMd
                            color: Theme.surfaceAlt
                            border.color: Theme.borderSubtle
                            border.width: 1
                            Layout.alignment: Qt.AlignTop
                            AppIcon {
                                anchors.centerIn: parent
                                width: 18
                                height: 18
                                kind: "file"
                                iconColor: Theme.accent
                            }
                        }
                        ColumnLayout {
                            Layout.fillWidth: true
                            spacing: 3
                            Label {
                                text: qsTr("Định dạng xuất âm thanh")
                                color: Theme.text
                                font.family: Theme.fontFamily
                                font.pixelSize: Theme.fontSizeBase
                                font.weight: Theme.fontWeightMedium
                                wrapMode: Text.Wrap
                                Layout.fillWidth: true
                            }
                            Label {
                                Layout.fillWidth: true
                                text: qsTr("Hàng loạt và sách nói dùng định dạng này (WAV ~10 MB/phút, MP3 ~1 MB/phút)")
                                color: Theme.textMuted
                                font.family: Theme.fontFamily
                                font.pixelSize: Theme.fontSizeXs
                                wrapMode: Text.Wrap
                                lineHeight: 1.2
                            }
                        }
                    }

                    AppCombo {
                        id: exportFormatCombo
                        objectName: "exportFormatCombo"
                        Layout.fillWidth: root.isCompact
                        Layout.preferredWidth: root.isCompact ? 0 : 280
                        Layout.alignment: root.isCompact ? Qt.AlignLeft : Qt.AlignRight | Qt.AlignVCenter
                        comboWidth: 280
                        accessibleLabel: qsTr("Định dạng xuất âm thanh")
                        textRole: "label"
                        model: root.exportFormatOptions
                        currentIndex: root.valueIndex(root.exportFormatOptions, controller.exportFormat)
                        onActivated: function (index) {
                            controller.exportFormat = root.exportFormatOptions[index].value;
                        }
                    }
                }

                // -- Temperature (Number field 140 px stays compact even when stacked) --
                GridLayout {
                    Layout.fillWidth: true
                    columns: root.isCompact ? 1 : 2
                    columnSpacing: Theme.spacingLg
                    rowSpacing: root.isCompact ? Theme.spacingSm : Theme.spacingLg

                    RowLayout {
                        Layout.fillWidth: true
                        Layout.alignment: root.isCompact ? Qt.AlignLeft : Qt.AlignVCenter
                        spacing: Theme.spacingMd
                        Rectangle {
                            width: 36
                            height: 36
                            radius: Theme.radiusMd
                            color: Theme.surfaceAlt
                            border.color: Theme.borderSubtle
                            border.width: 1
                            Layout.alignment: Qt.AlignTop
                            AppIcon {
                                anchors.centerIn: parent
                                width: 18
                                height: 18
                                kind: "wave"
                                iconColor: Theme.accent
                            }
                        }
                        ColumnLayout {
                            Layout.fillWidth: true
                            spacing: 3
                            Label {
                                text: qsTr("Temperature (Độ biến thiên)")
                                color: Theme.text
                                font.family: Theme.fontFamily
                                font.pixelSize: Theme.fontSizeBase
                                font.weight: Theme.fontWeightMedium
                                wrapMode: Text.Wrap
                                Layout.fillWidth: true
                            }
                            Label {
                                Layout.fillWidth: true
                                text: qsTr("0.6 – 0.8: Chuẩn, ổn định tự nhiên; 0.9+: Nhiều biểu cảm và ngữ điệu hơn")
                                color: Theme.textMuted
                                font.family: Theme.fontFamily
                                font.pixelSize: Theme.fontSizeXs
                                wrapMode: Text.Wrap
                                lineHeight: 1.2
                            }
                        }
                    }

                    AppNumberField {
                        id: temperatureSpin
                        objectName: "temperatureSpin"
                        from: 5            // ×100: bounds mirror Settings [0.05, 2.0]
                        to: 200
                        stepSize: 5
                        value: Math.round(controller.temperature * 100)

                        accessibleLabel: qsTr("Temperature")
                        Layout.alignment: root.isCompact ? Qt.AlignLeft : Qt.AlignRight | Qt.AlignVCenter
                        Layout.preferredWidth: 140
                        implicitWidth: 140

                        validator: DoubleValidator {
                            bottom: Math.min(temperatureSpin.from, temperatureSpin.to) / 100
                            top: Math.max(temperatureSpin.from, temperatureSpin.to) / 100
                            decimals: 2
                            locale: "C"
                        }

                        onRealValueChanged: controller.temperature = realValue
                    }
                }

                Rectangle {
                    Layout.fillWidth: true
                    height: 1
                    color: Theme.borderSubtle
                    opacity: 0.7
                }

                // -- Reading Speed (Speed) --
                GridLayout {
                    Layout.fillWidth: true
                    columns: root.isCompact ? 1 : 2
                    columnSpacing: Theme.spacingLg
                    rowSpacing: root.isCompact ? Theme.spacingSm : Theme.spacingLg

                    RowLayout {
                        Layout.fillWidth: true
                        Layout.alignment: root.isCompact ? Qt.AlignLeft : Qt.AlignVCenter
                        spacing: Theme.spacingMd
                        Rectangle {
                            width: 36
                            height: 36
                            radius: Theme.radiusMd
                            color: Theme.surfaceAlt
                            border.color: Theme.borderSubtle
                            border.width: 1
                            Layout.alignment: Qt.AlignTop
                            AppIcon {
                                anchors.centerIn: parent
                                width: 18
                                height: 18
                                kind: "play"
                                iconColor: Theme.accent
                            }
                        }
                        ColumnLayout {
                            Layout.fillWidth: true
                            spacing: 3
                            Label {
                                text: qsTr("Tốc độ đọc (Speed)")
                                color: Theme.text
                                font.family: Theme.fontFamily
                                font.pixelSize: Theme.fontSizeBase
                                font.weight: Theme.fontWeightMedium
                                wrapMode: Text.Wrap
                                Layout.fillWidth: true
                            }
                            Label {
                                Layout.fillWidth: true
                                text: qsTr("0.5× – 2.0×: Điều chỉnh tốc độ phát giọng đọc (mặc định 1.0×)")
                                color: Theme.textMuted
                                font.family: Theme.fontFamily
                                font.pixelSize: Theme.fontSizeXs
                                wrapMode: Text.Wrap
                                lineHeight: 1.2
                            }
                        }
                    }

                    AppNumberField {
                        id: speedSpin
                        objectName: "speedSpin"
                        from: 50           // ×100: bounds mirror Settings [0.5, 2.0]
                        to: 200
                        stepSize: 5
                        value: Math.round(controller.speed * 100)

                        accessibleLabel: qsTr("Tốc độ đọc")
                        Layout.alignment: root.isCompact ? Qt.AlignLeft : Qt.AlignRight | Qt.AlignVCenter
                        Layout.preferredWidth: 140
                        implicitWidth: 140

                        validator: DoubleValidator {
                            bottom: Math.min(speedSpin.from, speedSpin.to) / 100
                            top: Math.max(speedSpin.from, speedSpin.to) / 100
                            decimals: 2
                            locale: "C"
                        }

                        onRealValueChanged: controller.speed = realValue
                    }
                }

                Rectangle {
                    Layout.fillWidth: true
                    height: 1
                    color: Theme.borderSubtle
                    opacity: 0.7
                }

                // -- Pause Duration (Silence between sentences/paragraphs) --
                GridLayout {
                    Layout.fillWidth: true
                    columns: root.isCompact ? 1 : 2
                    columnSpacing: Theme.spacingLg
                    rowSpacing: root.isCompact ? Theme.spacingSm : Theme.spacingLg

                    RowLayout {
                        Layout.fillWidth: true
                        Layout.alignment: root.isCompact ? Qt.AlignLeft : Qt.AlignVCenter
                        spacing: Theme.spacingMd
                        Rectangle {
                            width: 36
                            height: 36
                            radius: Theme.radiusMd
                            color: Theme.surfaceAlt
                            border.color: Theme.borderSubtle
                            border.width: 1
                            Layout.alignment: Qt.AlignTop
                            AppIcon {
                                anchors.centerIn: parent
                                width: 18
                                height: 18
                                kind: "pause"
                                iconColor: Theme.accent
                            }
                        }
                        ColumnLayout {
                            Layout.fillWidth: true
                            spacing: 3
                            Label {
                                text: qsTr("Khoảng lặng ngắt câu (Pause)")
                                color: Theme.text
                                font.family: Theme.fontFamily
                                font.pixelSize: Theme.fontSizeBase
                                font.weight: Theme.fontWeightMedium
                                wrapMode: Text.Wrap
                                Layout.fillWidth: true
                            }
                            Label {
                                Layout.fillWidth: true
                                text: qsTr("0.0s – 2.0s: Độ dài khoảng lặng giữa các câu và đoạn văn (mặc định 0.15s)")
                                color: Theme.textMuted
                                font.family: Theme.fontFamily
                                font.pixelSize: Theme.fontSizeXs
                                wrapMode: Text.Wrap
                                lineHeight: 1.2
                            }
                        }
                    }

                    AppNumberField {
                        id: silencePSpin
                        objectName: "silencePSpin"
                        from: 0            // ×100: bounds mirror Settings [0.0, 2.0]
                        to: 200
                        stepSize: 5
                        value: Math.round(controller.silenceP * 100)

                        accessibleLabel: qsTr("Khoảng lặng ngắt câu")
                        Layout.alignment: root.isCompact ? Qt.AlignLeft : Qt.AlignRight | Qt.AlignVCenter
                        Layout.preferredWidth: 140
                        implicitWidth: 140

                        validator: DoubleValidator {
                            bottom: Math.min(silencePSpin.from, silencePSpin.to) / 100
                            top: Math.max(silencePSpin.from, silencePSpin.to) / 100
                            decimals: 2
                            locale: "C"
                        }

                        onRealValueChanged: controller.silenceP = realValue
                    }
                }
                Rectangle {
                    Layout.fillWidth: true
                    height: 1
                    color: Theme.borderSubtle
                    opacity: 0.7
                }

                // -- Live preview (real-time playback vs generate-then-replay) --
                GridLayout {
                    Layout.fillWidth: true
                    columns: root.isCompact ? 1 : 2
                    columnSpacing: Theme.spacingLg
                    rowSpacing: root.isCompact ? Theme.spacingSm : Theme.spacingLg

                    RowLayout {
                        Layout.fillWidth: true
                        Layout.alignment: root.isCompact ? Qt.AlignLeft : Qt.AlignVCenter
                        spacing: Theme.spacingMd
                        Rectangle {
                            width: 36
                            height: 36
                            radius: Theme.radiusMd
                            color: Theme.surfaceAlt
                            border.color: Theme.borderSubtle
                            border.width: 1
                            Layout.alignment: Qt.AlignTop
                            AppIcon {
                                anchors.centerIn: parent
                                width: 18
                                height: 18
                                kind: "play"
                                iconColor: Theme.accent
                            }
                        }
                        ColumnLayout {
                            Layout.fillWidth: true
                            spacing: 3
                            Label {
                                text: qsTr("Phát trực tiếp khi đang tạo")
                                color: Theme.text
                                font.family: Theme.fontFamily
                                font.pixelSize: Theme.fontSizeBase
                                font.weight: Theme.fontWeightMedium
                                wrapMode: Text.Wrap
                                Layout.fillWidth: true
                            }
                            Label {
                                Layout.fillWidth: true
                                text: qsTr("Bật: nghe ngay khi tổng hợp. Tắt: tạo xong tự phát lại từ đầu")
                                color: Theme.textMuted
                                font.family: Theme.fontFamily
                                font.pixelSize: Theme.fontSizeXs
                                wrapMode: Text.Wrap
                                lineHeight: 1.2
                            }
                        }
                    }

                    AppToggle {
                        id: livePreviewToggle
                        objectName: "livePreviewToggle"
                        text: qsTr("Phát trực tiếp")
                        checked: controller.livePreview === true
                        onToggled: controller.livePreview = checked
                        accessibleLabel: qsTr("Phát trực tiếp khi đang tạo")
                        Layout.alignment: root.isCompact ? Qt.AlignLeft : Qt.AlignRight | Qt.AlignVCenter
                    }
                }
            }
        }

        // ── 3. Appearance Card ───────────────────────────────────────────
        AppCard {
            Layout.fillWidth: true
            title: qsTr("Giao diện & Trải nghiệm")
            subtitle: qsTr("Tùy chỉnh chế độ hiển thị màu sắc và phong cách giao diện")

            ColumnLayout {
                Layout.fillWidth: true
                spacing: Theme.spacingLg

                GridLayout {
                    Layout.fillWidth: true
                    columns: root.isCompact ? 1 : 2
                    columnSpacing: Theme.spacingLg
                    rowSpacing: root.isCompact ? Theme.spacingSm : Theme.spacingLg

                    RowLayout {
                        Layout.fillWidth: true
                        Layout.alignment: root.isCompact ? Qt.AlignLeft : Qt.AlignVCenter
                        spacing: Theme.spacingMd
                        Rectangle {
                            width: 36
                            height: 36
                            radius: Theme.radiusMd
                            color: Theme.surfaceAlt
                            border.color: Theme.borderSubtle
                            border.width: 1
                            Layout.alignment: Qt.AlignTop
                            AppIcon {
                                anchors.centerIn: parent
                                width: 18
                                height: 18
                                kind: "settings"
                                iconColor: Theme.accent
                            }
                        }
                        ColumnLayout {
                            Layout.fillWidth: true
                            spacing: 3
                            Label {
                                text: qsTr("Chế độ màu sắc")
                                color: Theme.text
                                font.family: Theme.fontFamily
                                font.pixelSize: Theme.fontSizeBase
                                font.weight: Theme.fontWeightMedium
                                wrapMode: Text.Wrap
                                Layout.fillWidth: true
                            }
                            Label {
                                Layout.fillWidth: true
                                text: qsTr("Chọn giao diện Tối, Sáng hoặc theo hệ thống — áp dụng ngay lập tức")
                                color: Theme.textMuted
                                font.family: Theme.fontFamily
                                font.pixelSize: Theme.fontSizeXs
                                wrapMode: Text.Wrap
                                lineHeight: 1.2
                            }
                        }
                    }

                    AppCombo {
                        id: themeCombo
                        objectName: "themeCombo"
                        Layout.fillWidth: root.isCompact
                        Layout.preferredWidth: root.isCompact ? 0 : 280
                        Layout.alignment: root.isCompact ? Qt.AlignLeft : Qt.AlignRight | Qt.AlignVCenter
                        comboWidth: 280
                        accessibleLabel: qsTr("Chế độ màu sắc")
                        textRole: "label"
                        model: root.themeOptions
                        currentIndex: root.valueIndex(root.themeOptions, bridge ? bridge.themePreference : "system")
                        onActivated: function (index) {
                            if (bridge)
                                bridge.themePreference = root.themeOptions[index].value;
                            controller.theme = root.themeOptions[index].value;
                        }
                    }
                }

                Rectangle {
                    Layout.fillWidth: true
                    height: 1
                    color: Theme.borderSubtle
                    opacity: 0.7
                }

                // Language picker — applies LIVE (like the theme combo above):
                // the shell swaps translators and retranslate()s on change.
                GridLayout {
                    Layout.fillWidth: true
                    columns: root.isCompact ? 1 : 2
                    columnSpacing: Theme.spacingLg
                    rowSpacing: root.isCompact ? Theme.spacingSm : Theme.spacingLg

                    RowLayout {
                        Layout.fillWidth: true
                        Layout.alignment: root.isCompact ? Qt.AlignLeft : Qt.AlignVCenter
                        spacing: Theme.spacingMd
                        Rectangle {
                            width: 36
                            height: 36
                            radius: Theme.radiusMd
                            color: Theme.surfaceAlt
                            border.color: Theme.borderSubtle
                            border.width: 1
                            Layout.alignment: Qt.AlignTop
                            AppIcon {
                                anchors.centerIn: parent
                                width: 18
                                height: 18
                                kind: "text"
                                iconColor: Theme.accent
                            }
                        }
                        ColumnLayout {
                            Layout.fillWidth: true
                            spacing: 3
                            Label {
                                text: qsTr("Ngôn ngữ")
                                color: Theme.text
                                font.family: Theme.fontFamily
                                font.pixelSize: Theme.fontSizeBase
                                font.weight: Theme.fontWeightMedium
                            }
                            Label {
                                Layout.fillWidth: true
                                text: qsTr("Ngôn ngữ hiển thị của giao diện — áp dụng ngay lập tức")
                                color: Theme.textMuted
                                font.family: Theme.fontFamily
                                font.pixelSize: Theme.fontSizeXs
                                wrapMode: Text.Wrap
                                lineHeight: 1.2
                            }
                        }
                    }

                    AppCombo {
                        id: languageCombo
                        objectName: "languageCombo"
                        Layout.fillWidth: root.isCompact
                        Layout.preferredWidth: root.isCompact ? 0 : 280
                        Layout.alignment: root.isCompact ? Qt.AlignLeft : Qt.AlignRight | Qt.AlignVCenter
                        comboWidth: 280
                        accessibleLabel: qsTr("Ngôn ngữ")
                        textRole: "label"
                        model: root.languageOptions
                        currentIndex: root.valueIndex(
                            root.languageOptions,
                            controller ? controller.language : "system"
                        )
                        onActivated: function (index) {
                            if (controller)
                                controller.language = root.languageOptions[index].value;
                        }
                    }
                }
            }
        }

        // ── 4. Updates Card ────────────────────────────────────────────────
        // GitHub Releases check: silent auto-check at startup (app.py), manual
        // refresh here. Suggests the running platform's file first (asset name
        // contains windows-x64 / linux-x64 / macos-arm64); other files expand
        // below. Download opens the file URL in the browser — the user
        // re-extracts over the old install (Linux: re-run share/linux/install.sh).
        AppCard {
            Layout.fillWidth: true
            title: qsTr("Cập nhật")
            subtitle: qsTr("Phiên bản hiện tại: %1").arg(controller ? controller.appVersion : "")

            ColumnLayout {
                Layout.fillWidth: true
                spacing: Theme.spacingMd

                // New-version banner (sticky once any check finds one).
                AppNotice {
                    Layout.fillWidth: true
                    tone: "info"
                    title: qsTr("Có bản mới %1").arg(controller ? controller.updateLatestVersion : "")
                    message: controller && controller.updateAssetName !== ""
                        ? qsTr("Bản dành cho %1: %2").arg(controller.updatePlatformLabel).arg(controller.updateAssetName)
                        : qsTr("Bản mới không có tệp cho nền tảng này — xem các tệp khác bên dưới.")
                    messageObjectName: "updateBanner"
                    visible: controller ? controller.updateAvailable : false
                }

                RowLayout {
                    Layout.fillWidth: true
                    spacing: Theme.spacingMd

                    Label {
                        Layout.fillWidth: true
                        text: controller && controller.updateLatestVersion !== ""
                            ? (controller.updateAvailable
                                ? qsTr("Bản mới nhất: %1").arg(controller.updateLatestVersion)
                                : qsTr("Đã là bản mới nhất (%1)").arg(controller.appVersion))
                            : qsTr("Chưa kiểm tra cập nhật")
                        color: Theme.textMuted
                        font.family: Theme.fontFamily
                        font.pixelSize: Theme.fontSizeSm
                        wrapMode: Text.Wrap
                    }

                    AppButton {
                        id: checkUpdatesButton
                        objectName: "checkUpdatesButton"
                        variant: "secondary"
                        size: "sm"
                        iconKind: "refresh"
                        text: qsTr("Kiểm tra")
                        tooltipText: qsTr("Kiểm tra bản mới trên GitHub Releases")
                        accessibleLabel: qsTr("Kiểm tra bản mới")
                        busy: controller ? controller.updateChecking : false
                        onClicked: controller.checkForUpdates()
                    }
                }

                // Failure note (manual check only; startup failures stay silent).
                AppNotice {
                    Layout.fillWidth: true
                    tone: "warning"
                    title: qsTr("Không kiểm tra được")
                    message: controller ? controller.updateError : ""
                    messageObjectName: "updateErrorLabel"
                    visible: controller
                        ? (controller.updateError !== "" && !controller.updateAvailable) : false
                }

                // Suggested platform download.
                AppButton {
                    id: downloadUpdateButton
                    objectName: "downloadUpdateButton"
                    variant: "primary"
                    size: "md"
                    iconKind: "download"
                    text: qsTr("Tải bản %1 cho %2").arg(controller ? controller.updateLatestVersion : "").arg(controller ? controller.updatePlatformLabel : "")
                    tooltipText: controller ? controller.updateAssetName : ""
                    accessibleLabel: qsTr("Tải bản cập nhật cho nền tảng này")
                    visible: controller
                        ? (controller.updateAvailable && controller.updateAssetUrl !== "") : false
                    onClicked: Qt.openUrlExternally(controller.updateAssetUrl)
                }

                // Link to the full release page (notes + every file) when there
                // is no matching platform file, or as a secondary path.
                AppButton {
                    id: viewReleaseButton
                    objectName: "viewReleaseButton"
                    variant: "quiet"
                    size: "sm"
                    iconKind: "externalLink"
                    text: qsTr("Xem ghi chú phát hành")
                    accessibleLabel: qsTr("Mở trang phát hành trên GitHub")
                    visible: controller
                        ? (controller.updateAvailable && controller.updateReleaseUrl !== "") : false
                    onClicked: Qt.openUrlExternally(controller.updateReleaseUrl)
                }

                // Other-platform files expander.
                ColumnLayout {
                    Layout.fillWidth: true
                    spacing: Theme.spacingXs
                    visible: controller
                        ? (controller.updateAvailable && controller.updateOtherAssets.length > 0) : false

                    AppButton {
                        id: otherPlatformsToggle
                        objectName: "otherPlatformsToggle"
                        property bool showOtherPlatforms: false
                        variant: "quiet"
                        size: "sm"
                        iconKind: showOtherPlatforms ? "chevronUp" : "chevronDown"
                        text: showOtherPlatforms ? qsTr("Ẩn các bản khác") : qsTr("Tải cho nền tảng khác")
                        accessibleLabel: qsTr("Hiện các tệp cho nền tảng khác")
                        onClicked: showOtherPlatforms = !showOtherPlatforms
                    }

                    Repeater {
                        id: otherPlatformsList
                        objectName: "otherPlatformsList"
                        model: (controller && otherPlatformsToggle.showOtherPlatforms)
                            ? controller.updateOtherAssets : []
                        delegate: AppButton {
                            objectName: "otherPlatformAssetButton"
                            required property var modelData
                            variant: "secondary"
                            size: "sm"
                            iconKind: "download"
                            text: modelData.name
                            tooltipText: modelData.url
                            Layout.fillWidth: true
                            onClicked: Qt.openUrlExternally(modelData.url)
                        }
                    }
                }
            }
        }

        // Error notice banner
        AppNotice {
            Layout.fillWidth: true
            tone: "error"
            title: qsTr("Không thể lưu cài đặt")
            message: controller.errorText
            messageObjectName: "errorLabel"
            visible: controller.errorText !== ""
        }

        Item {
            Layout.fillHeight: true
        }
    }
}
