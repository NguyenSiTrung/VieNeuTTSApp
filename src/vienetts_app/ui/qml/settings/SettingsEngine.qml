import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "../components"
import "../components/StatusBarLogic.js" as Logic
import ".."
import "SettingsFilter.js" as Filter

// Bound: the local-runtime Repeater delegates read section state.
pragma ComponentBehavior: Bound

// Cài đặt › Engine & phần cứng (FR-4.2). Leads with a summary card — the
// readiness word (same derivation as the status bar), the active device ·
// backend · precision, WHY that engine was chosen (the detector note for
// VieNeu, the profile status sentence for Qwen) and the Tự động/CPU/GPU
// backend segmented control. Then the model family (engine profile +
// synthesis language), then "Nâng cao": precision, the managed CUDA runtime,
// the Qwen compute device and diagnostics, each a collapsed disclosure.
//
// CUDA (v0.1.14 rules, kept): both CUDA disclosures start collapsed and
// auto-expand when a state needs them — the runtime row on an install in
// flight/failed or an unusable driver, the diagnostics row on the same plus a
// local scan already run; a manual collapse sticks (page.cudaRuntimeExpanded /
// page.cudaRuntimeDetailsExpanded own the state and its rules).
//
// objectNames (tested contract): settingsSection_engine, engineSummaryCard,
// engineSummaryState, engineSummaryBackend, detectedEngineLabel (VieNeu
// reason), engineSummaryReason (Qwen reason), settingsBackendCard (the
// backend row), backendCombo (AppSegmented, backendCombo_<auto|onnx|torch>),
// engineProfileCard (EngineProfilePicker + LanguagePicker names),
// qwenContinueSetupButton, settingsAdvancedCard, precisionDisclosureToggle,
// precisionCombo, cudaRuntimeCard (the CUDA disclosure; header
// cudaRuntimeToggle), cudaRuntimeUnsupportedNotice/-Error,
// cudaRuntimeErrorLabel, cudaRuntimeDriverNotice, cudaRuntimeRestartNotice,
// cudaRuntimeInstallButton, cudaRuntimeDriverRecheckButton,
// cudaRuntimeCancelButton, cudaRuntimeRetryButton, cudaRuntimeRemoveButton,
// cudaRuntimeStorageLabel, cudaRuntimeProgress, qwenDeviceCard (the Qwen
// device disclosure; header qwenDeviceToggle, QwenDevicePicker names),
// cudaRuntimeDiagnostics (header cudaRuntimeDetailsToggle, body
// cudaRuntimeDetails: cudaRuntimeDriverGuide{,Linux,Windows},
// cudaRuntimeDriverDownloadButton, cudaRuntimeDetectLocalButton,
// cudaRuntimeLocalSummary).
SettingsSection {
    id: section

    // Guarded page reads (the page binds in after creation).
    readonly property bool qwenProfileActive: section.page ? section.page.qwenProfileActive : false
    readonly property bool cudaRuntimeSupported: section.page ? section.page.cudaRuntimeSupported : false
    readonly property bool cudaRuntimeDriverChecked: section.page ? section.page.cudaRuntimeDriverChecked : false
    readonly property bool cudaRuntimeDriverReady: section.page ? section.page.cudaRuntimeDriverReady : false
    readonly property bool cudaRuntimeInstallAllowed: section.page ? section.page.cudaRuntimeInstallAllowed : false
    readonly property string cudaRuntimeState: section.page ? section.page.cudaRuntimeState : "unavailable"
    readonly property int localCudaRuntimeCount: section.page ? section.page.localCudaRuntimeCount : 0
    readonly property bool localCudaRuntimeScanRequested: section.page
        ? section.page.localCudaRuntimeScanRequested : false
    readonly property bool qwenRuntimeSupported: section.page ? section.page.qwenRuntimeSupported : false
    readonly property string engineNoteText: section.page ? section.page.engineNoteText : ""
    readonly property bool engineNoteReady: section.page ? section.page.engineNoteReady : false

    readonly property var host: (typeof controller !== "undefined" && controller) ? controller : null

    // ── summary derivations ──────────────────────────────────────────────
    // Readiness: the status bar's key and words (components/StatusBar.qml),
    // so the two surfaces can never disagree about the same engine.
    readonly property bool managedProfile: EngineState.needsManagedInstall
    readonly property string modelState: section.host && section.host.modelState
        ? String(section.host.modelState) : ""
    readonly property string readiness: Logic.readinessKey(
        section.modelState, section.managedProfile, EngineState.readiness)
    readonly property string readinessText: {
        if (section.managedProfile) {
            switch (section.readiness) {
            case "ready":
                return qsTr("Sẵn sàng");
            case "busy":
                return qsTr("Đang chuẩn bị");
            case "failed":
                return qsTr("Cần chú ý");
            case "unsupported":
                return qsTr("Không hỗ trợ");
            case "missing":
                return qsTr("Chưa sẵn sàng");
            default:
                return qsTr("Đang kiểm tra...");
            }
        }
        switch (section.modelState) {
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
    readonly property color readinessColor: {
        switch (section.readiness) {
        case "ready":
            return Theme.success;
        case "busy":
            return Theme.accent;
        case "failed":
        case "unsupported":
            return Theme.error;
        case "missing":
            return Theme.warning;
        default:
            return Theme.textSubtle;
        }
    }
    // VieNeu runs ONNX Runtime unless PyTorch was forced or "auto" resolved
    // to a CUDA device; precision applies to ONNX only.
    readonly property bool torchActive: section.host
        ? (section.host.backend === "torch" || String(section.host.engineDevice || "") === "cuda")
        : false
    // The device leads only once it is known ("checking" until the
    // post-paint inspection lands — the line never opens with a placeholder).
    readonly property string deviceCode: section.host ? String(section.host.engineDevice || "") : ""
    readonly property string deviceText: section.deviceCode === "" || section.deviceCode === "checking"
        ? "" : EngineState.deviceName(section.deviceCode)
    readonly property string backendText: {
        const parts = [section.deviceText];
        if (section.qwenProfileActive) {
            parts.push(EngineState.variantLabel);
        } else {
            parts.push(section.torchActive ? "PyTorch" : "ONNX Runtime");
            if (!section.torchActive && section.host)
                parts.push(String(section.host.precision));
        }
        return parts.filter(part => part !== "").join(" · ");
    }
    readonly property var backendOptions: [
        { value: "auto", label: qsTr("Tự động") },
        { value: "onnx", label: qsTr("CPU") },
        { value: "torch", label: qsTr("GPU NVIDIA") }
    ]
    readonly property string cudaBadgeText: {
        if (!section.cudaRuntimeSupported)
            return qsTr("Không hỗ trợ");
        switch (section.cudaRuntimeState) {
        case "ready":
            return qsTr("Sẵn sàng");
        case "downloading":
            return qsTr("Đang tải");
        case "validating":
            return qsTr("Đang xác thực");
        case "failed":
            return qsTr("Cần chú ý");
        case "checking":
            return qsTr("Đang kiểm tra");
        default:
            return qsTr("Chưa cài đặt");
        }
    }
    readonly property string qwenDeviceText: {
        const options = section.host ? (section.host.qwenDeviceOptions || []) : [];
        for (let i = 0; i < options.length; i++)
            if (options[i].active)
                return options[i].label;
        return section.qwenRuntimeSupported ? "" : qsTr("Không hỗ trợ");
    }

    sectionId: "engine"
    title: qsTr("Engine & phần cứng")
    hasMatch: anyMatch([engineSummaryCard, engineProfileCard, precisionDisclosure,
                        cudaRuntimeCard, qwenDeviceCard, cudaRuntimeDiagnostics])

    // ── Summary ──────────────────────────────────────────────────────────
    AppCard {
        id: engineSummaryCard

        property bool shown: true
        readonly property bool matches: Filter.matches(section.filter, [
            section.title, qsTr("Backend suy luận"), section.backendText,
            "engine backend auto cpu gpu nvidia onnx pytorch"
        ])

        objectName: "engineSummaryCard"
        visible: shown && matches
        Layout.fillWidth: true

        RowLayout {
            Layout.fillWidth: true
            spacing: Theme.spacingSm

            Rectangle {
                Layout.alignment: Qt.AlignVCenter
                width: 10
                height: 10
                radius: 5
                color: section.readinessColor
            }

            Label {
                objectName: "engineSummaryState"
                text: section.readinessText
                color: Theme.text
                font.family: Theme.fontFamily
                font.pixelSize: Theme.fontSizeLg
                font.weight: Theme.fontWeightBold
            }
        }

        Label {
            objectName: "engineSummaryBackend"
            Layout.fillWidth: true
            text: section.backendText
            color: Theme.text
            font.family: Theme.fontFamily
            font.pixelSize: Theme.fontSizeBase
            font.weight: Theme.fontWeightMedium
            wrapMode: Text.Wrap
        }

        // Why this engine: the detector's resolved-engine note (same string
        // the status bar shows); hidden until the deferred probe lands.
        Label {
            id: detectedEngineLabel

            objectName: "detectedEngineLabel"
            Layout.fillWidth: true
            visible: !section.qwenProfileActive && section.engineNoteReady
            text: section.engineNoteText
            color: Theme.textMuted
            font.family: Theme.fontFamily
            font.pixelSize: Theme.fontSizeSm
            lineHeight: 1.3
            wrapMode: Text.Wrap
        }

        Label {
            objectName: "engineSummaryReason"
            Layout.fillWidth: true
            visible: section.qwenProfileActive && text !== ""
            text: EngineState.statusText
            color: Theme.textMuted
            font.family: Theme.fontFamily
            font.pixelSize: Theme.fontSizeSm
            lineHeight: 1.3
            wrapMode: Text.Wrap
        }

        // VieNeu's compute choice; Qwen picks its device under Nâng cao.
        SettingRow {
            objectName: "settingsBackendCard"
            shown: !section.qwenProfileActive
            compact: section.compact
            divider: true
            label: qsTr("Backend suy luận")
            description: qsTr("Tự động chọn GPU NVIDIA khi có runtime CUDA, nếu không thì CPU")

            AppSegmented {
                id: backendCombo

                objectName: "backendCombo"
                Layout.fillWidth: section.compact
                Layout.preferredWidth: section.compact ? 0 : 320
                accessibleLabel: qsTr("Backend suy luận")
                model: section.backendOptions
                currentValue: section.host ? section.host.backend : "auto"
                onActivated: (value) => {
                    controller.backend = value;
                }
            }
        }
    }

    // ── Model family ─────────────────────────────────────────────────────
    // Which engine serves synthesis plus the language its submissions use.
    // The shared component (components/EngineProfilePicker.qml) keeps the
    // synthesis surfaces from drifting from Settings.
    AppCard {
        id: engineProfileCard

        property bool shown: true
        readonly property bool matches: Filter.matches(section.filter, [
            title, qsTr("Engine suy luận"), qsTr("Ngôn ngữ tổng hợp"),
            "engine profile VieNeu Qwen language"
        ])

        objectName: "engineProfileCard"
        visible: shown && matches
        Layout.fillWidth: true
        title: qsTr("Họ mô hình")

        EngineProfilePicker {
            Layout.fillWidth: true
            label: qsTr("Engine suy luận")
            description: qsTr("Đổi engine sẽ giải phóng engine đang chạy; engine mới được nạp ở lần tổng hợp tiếp theo.")
            onProfileActivated: function(profileId) { section.page.requestQwenSetup(profileId); }
        }

        AppButton {
            id: qwenContinueSetupButton

            objectName: "qwenContinueSetupButton"
            visible: section.page ? section.page.qwenSetupNeeded : false
            variant: "primary"
            size: "sm"
            iconKind: "download"
            text: qsTr("Tiếp tục cài đặt")
            accessibleLabel: qsTr("Tiếp tục cài đặt Qwen")
            onClicked: section.page.openQwenSetup()
        }

        Rectangle {
            Layout.fillWidth: true
            implicitHeight: 1
            color: Theme.borderSubtle
            opacity: 0.7
        }

        LanguagePicker {
            Layout.fillWidth: true
            label: qsTr("Ngôn ngữ tổng hợp")
            description: qsTr("Danh sách chỉ gồm ngôn ngữ engine đang chọn chấp nhận.")
        }
    }

    // ── Nâng cao ─────────────────────────────────────────────────────────
    AppCard {
        id: advancedCard

        objectName: "settingsAdvancedCard"
        visible: section.anyMatch([precisionDisclosure, cudaRuntimeCard, qwenDeviceCard,
                                   cudaRuntimeDiagnostics])
        Layout.fillWidth: true
        title: qsTr("Nâng cao")

        SettingsDisclosure {
            id: precisionDisclosure

            toggleObjectName: "precisionDisclosureToggle"
            filter: section.filter
            shown: !section.qwenProfileActive
            title: qsTr("Độ chính xác mô hình")
            description: qsTr("int8 nhanh và nhẹ; fp32 cho chất lượng cao nhất")
            valueText: section.host ? String(section.host.precision) : ""
            keywords: "precision int8 fp32 onnx"

            AppCombo {
                id: precisionCombo

                objectName: "precisionCombo"
                Layout.fillWidth: section.compact
                Layout.preferredWidth: section.compact ? 0 : 280
                comboWidth: 280
                accessibleLabel: qsTr("Độ chính xác mô hình")
                textRole: "label"
                model: section.page ? section.page.precisionOptions : []
                currentIndex: section.page && section.host
                    ? section.page.valueIndex(section.page.precisionOptions, section.host.precision)
                    : 0
                onActivated: function (index) {
                    controller.precision = section.page.precisionOptions[index].value;
                }
            }
        }

        // Managed CUDA is always user-initiated: these controls call only the
        // explicit controller slots (no torch import, download or scan here).
        SettingsDisclosure {
            id: cudaRuntimeCard

            objectName: "cudaRuntimeCard"
            toggleObjectName: "cudaRuntimeToggle"
            filter: section.filter
            shown: !section.qwenProfileActive
            title: qsTr("Runtime CUDA được quản lý")
            description: section.cudaRuntimeSupported
                && section.cudaRuntimeDriverChecked && !section.cudaRuntimeDriverReady
                ? qsTr("Cài đặt bị tắt: cần GPU NVIDIA và driver hỗ trợ CUDA 12.0 trở lên.")
                : qsTr("Tải một lần để dùng GPU NVIDIA, sau đó chạy ngoại tuyến")
            valueText: section.cudaBadgeText
            keywords: "CUDA GPU NVIDIA PyTorch runtime"
            expanded: section.page ? section.page.cudaRuntimeExpanded : false
            onToggleRequested: section.page.cudaRuntimeExpanded = !section.page.cudaRuntimeExpanded

            Label {
                Layout.fillWidth: true
                text: {
                    if (!section.cudaRuntimeSupported)
                        return qsTr("Runtime CUDA không khả dụng trên nền tảng này.");
                    switch (section.cudaRuntimeState) {
                    case "ready":
                        return qsTr("Runtime CUDA đã sẵn sàng và đã được xác thực.");
                    case "downloading":
                        return qsTr("Đang tải runtime CUDA…");
                    case "validating":
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
                visible: !section.cudaRuntimeSupported
                    && (controller ? controller.cudaRuntimeError !== "" : false)
            }

            AppNotice {
                id: cudaRuntimeFailureNotice
                Layout.fillWidth: true
                tone: "error"
                title: qsTr("Cài đặt runtime CUDA thất bại")
                message: controller ? controller.cudaRuntimeError : ""
                messageObjectName: "cudaRuntimeErrorLabel"
                visible: section.cudaRuntimeSupported
                    && section.cudaRuntimeState === "failed"
                    && (controller ? controller.cudaRuntimeError !== "" : false)
            }

            AppNotice {
                id: cudaRuntimeDriverNotice
                objectName: "cudaRuntimeDriverNotice"
                Layout.fillWidth: true
                tone: "warning"
                title: qsTr("Cần GPU NVIDIA và driver CUDA")
                message: qsTr("Không thể cài đặt runtime CUDA nhiều GB cho đến khi phát hiện GPU NVIDIA và driver hỗ trợ CUDA 12.0 trở lên. Bạn vẫn có thể kiểm tra lại driver hoặc các runtime cục bộ để chẩn đoán.")
                visible: section.cudaRuntimeSupported
                    && section.cudaRuntimeDriverChecked
                    && !section.cudaRuntimeDriverReady
            }

            AppNotice {
                id: cudaRuntimeRestartNotice
                objectName: "cudaRuntimeRestartNotice"
                Layout.fillWidth: true
                tone: "warning"
                title: qsTr("Khởi động lại để áp dụng")
                message: qsTr("Khởi động lại ứng dụng để dùng runtime CUDA mới cài đặt. Nếu một engine CUDA đã chạy, hãy khởi động lại trước khi gỡ runtime.")
                visible: section.cudaRuntimeSupported
                    && section.cudaRuntimeState === "ready"
            }

            Flow {
                Layout.fillWidth: true
                spacing: Theme.spacingSm
                visible: section.cudaRuntimeSupported

                AppButton {
                    id: cudaRuntimeInstallButton
                    objectName: "cudaRuntimeInstallButton"
                    variant: "primary"
                    size: "sm"
                    iconKind: "download"
                    text: qsTr("Cài đặt runtime CUDA")
                    accessibleLabel: qsTr("Cài đặt runtime CUDA")
                    visible: section.cudaRuntimeState === "unavailable"
                        || section.cudaRuntimeState === "checking"
                    enabled: section.cudaRuntimeInstallAllowed
                    disabledReason: qsTr("Cần GPU NVIDIA và driver CUDA từ 12.0 trở lên — xem hướng dẫn trong Chẩn đoán.")
                    onClicked: controller.installCudaRuntime()
                }

                AppButton {
                    id: cudaRuntimeDriverRecheckButton
                    objectName: "cudaRuntimeDriverRecheckButton"
                    variant: "secondary"
                    size: "sm"
                    iconKind: "refresh"
                    text: qsTr("Kiểm tra lại driver")
                    accessibleLabel: qsTr("Kiểm tra lại driver")
                    visible: section.cudaRuntimeSupported
                        && section.cudaRuntimeDriverChecked
                        && !section.cudaRuntimeDriverReady
                    onClicked: controller.refreshCudaRuntimeState()
                }

                AppButton {
                    id: cudaRuntimeCancelButton
                    objectName: "cudaRuntimeCancelButton"
                    variant: "secondary"
                    size: "sm"
                    iconKind: "close"
                    text: qsTr("Hủy tải runtime CUDA")
                    accessibleLabel: qsTr("Hủy tải runtime CUDA")
                    visible: section.cudaRuntimeState === "downloading"
                        || section.cudaRuntimeState === "validating"
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
                    visible: section.cudaRuntimeState === "failed"
                    enabled: section.cudaRuntimeInstallAllowed
                    disabledReason: qsTr("Cần GPU NVIDIA và driver CUDA từ 12.0 trở lên — xem hướng dẫn trong Chẩn đoán.")
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
                    visible: section.cudaRuntimeState === "ready"
                    onClicked: section.page.openCudaRemoveDialog()
                }
            }

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
                    // formatBytes, not String(): the raw count rendered
                    // multi-GB installs as "7923410000 byte" while the
                    // Qwen card next door showed "7.9 GB".
                    text: qsTr("Đã tải %1 / cần %2")
                        .arg(section.page.formatBytes(controller ? controller.cudaRuntimeInstalledBytes : 0))
                        .arg(section.page.formatBytes(controller ? controller.cudaRuntimeRequiredBytes : 0))
                    color: Theme.textMuted
                    font.family: Theme.fontFamily
                    font.pixelSize: Theme.fontSizeXs
                    visible: section.cudaRuntimeState === "downloading"
                        || section.cudaRuntimeState === "validating"
                        || section.cudaRuntimeState === "ready"
                }

                ProgressBar {
                    id: cudaRuntimeProgress
                    objectName: "cudaRuntimeProgress"
                    Layout.fillWidth: true
                    from: 0
                    to: 1
                    value: controller ? controller.cudaRuntimeProgress : 0
                    visible: section.cudaRuntimeState === "downloading"
                        || section.cudaRuntimeState === "validating"
                    Accessible.name: qsTr("Tiến trình tải runtime CUDA")
                }
            }
        }

        // Where Qwen runs. Support is platform truth, so an impossible choice
        // shows disabled WITH its reason; the CPU warning appears before any
        // download (a CPU run is many times slower).
        SettingsDisclosure {
            id: qwenDeviceCard

            objectName: "qwenDeviceCard"
            toggleObjectName: "qwenDeviceToggle"
            filter: section.filter
            shown: section.qwenProfileActive
            title: qsTr("Thiết bị cho Qwen")
            description: section.qwenRuntimeSupported
                ? qsTr("Chọn nơi chạy Qwen. Thay đổi áp dụng ở lần khởi động engine tiếp theo.")
                : qsTr("Máy này không có runtime Qwen cho thiết bị nào.")
            valueText: section.qwenDeviceText
            keywords: "Qwen device CPU CUDA GPU Metal"

            QwenDevicePicker {
                Layout.fillWidth: true
                isCompact: section.compact
            }
        }

        // Driver guidance + the local runtime scan (diagnostic only).
        SettingsDisclosure {
            id: cudaRuntimeDiagnostics

            objectName: "cudaRuntimeDiagnostics"
            toggleObjectName: "cudaRuntimeDetailsToggle"
            filter: section.filter
            shown: !section.qwenProfileActive && section.cudaRuntimeSupported
            title: qsTr("Chẩn đoán")
            description: qsTr("Hướng dẫn driver NVIDIA và quét runtime CUDA cục bộ")
            keywords: "CUDA driver nvidia-smi diagnostics"
            expanded: section.page ? section.page.cudaRuntimeDetailsExpanded : false
            onToggleRequested: section.page.cudaRuntimeDetailsExpanded = !section.page.cudaRuntimeDetailsExpanded

            ColumnLayout {
                id: cudaRuntimeDetails

                objectName: "cudaRuntimeDetails"
                Layout.fillWidth: true
                spacing: Theme.spacingMd

                // OS-aware driver upgrade guide — same gate as the driver
                // notice above. Commands are copy targets, not prose.
                ColumnLayout {
                    id: cudaRuntimeDriverGuide
                    objectName: "cudaRuntimeDriverGuide"
                    Layout.fillWidth: true
                    spacing: Theme.spacingXs
                    visible: section.cudaRuntimeDriverChecked && !section.cudaRuntimeDriverReady

                    Label {
                        id: cudaRuntimeDriverGuideLinux
                        objectName: "cudaRuntimeDriverGuideLinux"
                        Layout.fillWidth: true
                        text: qsTr("Linux: kiểm tra driver và dòng `CUDA Version` (cần ≥ 12.0):")
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
                        text: qsTr("Windows: chạy lệnh sau trong Command Prompt hoặc PowerShell và xem dòng `CUDA Version` (cần ≥ 12.0):")
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
                            section.page.localCudaRuntimeScanRequested = true;
                            controller.discoverLocalCudaRuntimes();
                        }
                    }

                    Label {
                        id: cudaRuntimeLocalSummary
                        objectName: "cudaRuntimeLocalSummary"
                        Layout.fillWidth: true
                        text: (section.localCudaRuntimeScanRequested
                                || section.localCudaRuntimeCount > 0)
                            ? qsTr("Đã phát hiện %1 runtime CUDA cục bộ.")
                                .arg(section.localCudaRuntimeCount)
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
                        visible: section.localCudaRuntimeScanRequested || section.localCudaRuntimeCount > 0
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
}
