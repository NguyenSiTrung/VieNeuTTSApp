import QtQuick
import QtQuick.Controls
import QtQuick.Dialogs
import QtQuick.Layouts
import ".."

// Qwen managed install cards (extracted from SettingsTab for Task 5.2).
//
// The whole install surface for the SELECTED model format: the managed
// runtime card (install / cancel / repair / remove / offline import, with
// the storage row and the variant actually pinned for this device) and the
// model card (one row per install the format ships — two official
// checkpoints, or the profile × quantization GGUF matrix — plus the shared
// codec/tokenizer line and per-row actions).
//
// Every byte of state is controller truth; nothing here inspects or
// downloads until the user asks. The copy is variant-aware: under GGUF no
// string may describe a PyTorch wheel bundle — a qwentts.cpp pack is native
// libraries and its shared payload is a per-quantization codec, not the
// official tokenizer tree.
//
// objectNames are the tested contract (tests/smoke/test_ui_tabs.py) — they
// are unchanged from the pre-extraction cards: qwenInstallCards,
// qwenRuntimeCard (qwenRuntimeStatusLabel, qwenRuntimeVariantLabel,
// qwenRuntimeStorageLabel, qwenRuntimeOpenDirButton, qwenRuntimeProgress,
// qwenRuntimeInstallButton, qwenRuntimeCancelButton, qwenRuntimeRepairButton,
// qwenRuntimeRemoveButton, qwenRuntimeImportButton, qwenRuntimeImportDialog,
// qwenRuntimeUnsupportedNotice, qwenRuntimeFailureNotice,
// qwenRuntimeCpuNotice, qwenRuntimeImportHint, qwenRuntimeRemoveDialog /
// qwenRuntimeRemoveConfirmButton) and qwenModelCard (qwenModelRow_<key>,
// qwenModelLabel_<key>, qwenModelStateBadge_<key>, qwenModelStateLabel_<key>,
// qwenModelActiveBadge_<key>, qwenModelSelectedBadge_<key>,
// qwenModelStorageLabel_<key>, qwenModelProgress_<key>,
// qwenModelErrorLabel_<key>, qwenModelInstallButton_<key>,
// qwenModelCancelButton_<key>, qwenModelRepairButton_<key>,
// qwenModelRemoveButton_<key>, qwenModelImportButton_<key>,
// qwenSharedStorageLabel, qwenModelStoragePathLabel, qwenModelOpenDirButton,
// qwenModelCpuNotice, qwenModelPackDialog, qwenModelRemoveDialog /
// qwenModelRemoveConfirmButton).
ColumnLayout {
    id: root

    objectName: root.named("InstallCards")
    spacing: Theme.spacingLg

    // Compact hosts stack the variant/storage columns vertically.
    property bool isCompact: false
    property string objectNamePrefix: "qwen"
    property bool selectedOnly: false
    property bool allowRemoval: true

    function named(suffix) { return objectNamePrefix + suffix; }

    readonly property bool qwenProfileActive: controller
        ? controller.engineProfileIsQwen : false
    readonly property bool qwenRuntimeSupported: controller
        ? controller.qwenRuntimeSupported : false
    readonly property string qwenRuntimeState: controller
        ? controller.qwenRuntimeState : "unsupported"
    readonly property bool qwenRuntimeBusy: controller ? controller.qwenRuntimeBusy : false
    readonly property bool qwenModelBusy: controller ? controller.qwenModelBusy : false
    readonly property var qwenModels: controller ? controller.qwenModels : []
    readonly property var visibleQwenModels: {
        if (!selectedOnly)
            return qwenModels;
        const rows = [];
        for (let i = 0; i < qwenModels.length; i++) {
            if (qwenModels[i].isActive && qwenModels[i].isSelected)
                rows.push(qwenModels[i]);
        }
        return rows;
    }
    readonly property string qwenCpuGuidance: controller ? controller.qwenCpuGuidance : ""
    readonly property int qwenReadyModelCount: {
        let count = 0;
        for (let i = 0; i < visibleQwenModels.length; i++)
            if (visibleQwenModels[i].ready)
                count++;
        return count;
    }

    // The selected weight format decides which managed installs — and which
    // words — this surface shows. A GGUF pack is native libraries for
    // qwentts.cpp, never a PyTorch wheel bundle.
    readonly property bool ggufSelected: controller
        ? controller.qwenModelFormat === "gguf" : false

    // The pinned runtime variant for the chosen device; "đang kiểm tra…" until
    // the first inspection lands (never a guessed variant).
    readonly property string qwenRuntimeVariantText: {
        const label = controller ? controller.qwenRuntimeVariantLabel : "";
        if (label !== "")
            return label;
        return root.qwenRuntimeSupported ? qsTr("đang kiểm tra…") : "";
    }

    // Human-readable byte size. The pinned Qwen installs are GB-scale, so raw
    // counts are unreadable; String() stays the fallback for exact counts.
    function formatBytes(bytes) {
        const value = Number(bytes);
        if (!isFinite(value) || value <= 0)
            return "0 B";
        const units = ["B", "KB", "MB", "GB", "TB"];
        let index = 0;
        let scaled = value;
        while (scaled >= 1024 && index < units.length - 1) {
            scaled /= 1024;
            index++;
        }
        const digits = (index === 0 || scaled >= 100) ? 0 : 1;
        return scaled.toFixed(digits) + " " + units[index];
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

    // Tested seams for the offline-pack dialogs. Native dialogs are
    // unreliable headless (same policy as the output-dir/import dialogs),
    // so the smoke suite drives these functions with a plain path and the
    // dialogs' onAccepted call them.
    function pickQwenRuntimePack(url) {
        controller.importQwenRuntimePack(root.toLocalPath(url));
    }

    function openQwenModelPackDialog(modelKey) {
        qwenModelPackDialog.pendingProfileKey = modelKey;
        qwenModelPackDialog.open();
    }

    function pickQwenModelPack(modelKey, url) {
        controller.importQwenModelPack(modelKey, root.toLocalPath(url));
    }

    // Remove-confirm seam for the per-variant rows: the delegate stores the
    // key it was asked about, exactly like qwenModelPackDialog below.
    function confirmQwenModelRemove(modelKey) {
        qwenModelRemoveDialog.pendingProfileKey = modelKey;
        qwenModelRemoveDialog.open();
    }

    // ── Qwen runtime Card ─────────────────────────────────────────────────
    // The managed runtime the selected format runs on: a verified wheel
    // closure under official weights, a verified qwentts.cpp native pack
    // under GGUF. The variant label names the pinned cell/platform so an
    // install always names the exact artifact it targets.
    AppCard {
        id: qwenRuntimeCard

        objectName: root.named("RuntimeCard")
        // Engine-conditional: only meaningful while a Qwen profile is
        // active — the profile picker above is the entry point.
        visible: root.qwenProfileActive
        Layout.fillWidth: true
        title: qsTr("Runtime Qwen được quản lý")
        // The variant label is "đang kiểm tra…" until the first inspection
        // lands; interpolating it made the subtitle read "a verified bundle
        // for checking…". Fall back to a complete sentence instead.
        subtitle: {
            if (!root.qwenRuntimeSupported)
                return qsTr("Runtime Qwen được quản lý chỉ hỗ trợ Windows/Linux x64 và Apple Silicon.");
            if (controller ? controller.qwenRuntimeVariantLabel !== "" : false)
                return qsTr("Gói runtime đã xác thực cho %1.").arg(controller.qwenRuntimeVariantLabel);
            return qsTr("Đang kiểm tra runtime Qwen trên máy này…");
        }
        badgeText: {
            if (!root.qwenRuntimeSupported)
                return qsTr("Không hỗ trợ");
            switch (root.qwenRuntimeState) {
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
        badgeColor: {
            if (!root.qwenRuntimeSupported || root.qwenRuntimeState === "failed")
                return Theme.errorSubtle;
            if (root.qwenRuntimeState === "ready")
                return Theme.successSubtle;
            if (root.qwenRuntimeState === "downloading"
                    || root.qwenRuntimeState === "validating")
                return Theme.accentSubtle;
            return Theme.warningSubtle;
        }
        badgeTextColor: {
            if (!root.qwenRuntimeSupported || root.qwenRuntimeState === "failed")
                return Theme.errorText;
            if (root.qwenRuntimeState === "ready")
                return Theme.successText;
            if (root.qwenRuntimeState === "downloading"
                    || root.qwenRuntimeState === "validating")
                return Theme.accent;
            return Theme.warningText;
        }

        ColumnLayout {
            Layout.fillWidth: true
            spacing: Theme.spacingMd

            Label {
                id: qwenRuntimeStatusLabel

                objectName: root.named("RuntimeStatusLabel")
                Layout.fillWidth: true
                text: {
                    if (!root.qwenRuntimeSupported)
                        return qsTr("Runtime Qwen không khả dụng trên nền tảng này.");
                    switch (root.qwenRuntimeState) {
                    case "ready":
                        return qsTr("Runtime Qwen đã sẵn sàng và đã được xác thực.");
                    case "downloading":
                        return qsTr("Đang tải runtime Qwen…");
                    case "validating":
                        return qsTr("Đang xác thực các tệp runtime Qwen…");
                    case "failed":
                        return qsTr("Không thể chuẩn bị runtime Qwen.");
                    case "checking":
                        return qsTr("Đang kiểm tra runtime Qwen trên máy này…");
                    default:
                        return qsTr("Chưa cài đặt runtime Qwen được quản lý.");
                    }
                }
                color: Theme.textMuted
                font.family: Theme.fontFamily
                font.pixelSize: Theme.fontSizeSm
                wrapMode: Text.Wrap
                lineHeight: 1.25
            }

            AppNotice {
                id: qwenRuntimeUnsupportedNotice

                objectName: root.named("RuntimeUnsupportedNotice")
                Layout.fillWidth: true
                tone: "warning"
                title: qsTr("Không hỗ trợ runtime Qwen")
                message: controller ? controller.qwenRuntimeError : ""
                messageObjectName: root.named("RuntimeErrorLabel")
                visible: !root.qwenRuntimeSupported
                    && (controller ? controller.qwenRuntimeError !== "" : false)
            }

            AppNotice {
                id: qwenRuntimeFailureNotice

                objectName: root.named("RuntimeFailureNotice")
                Layout.fillWidth: true
                tone: "error"
                title: qsTr("Cài đặt runtime Qwen thất bại")
                message: controller ? controller.qwenRuntimeError : ""
                messageObjectName: root.named("RuntimeFailureErrorLabel")
                visible: root.qwenRuntimeSupported
                    && root.qwenRuntimeState === "failed"
                    && (controller ? controller.qwenRuntimeError !== "" : false)
            }

            AppNotice {
                id: qwenRuntimeCpuNotice

                objectName: root.named("RuntimeCpuNotice")
                Layout.fillWidth: true
                tone: "warning"
                title: qsTr("Chạy Qwen trên CPU rất chậm")
                message: root.qwenCpuGuidance
                messageObjectName: root.named("RuntimeCpuNoticeMessage")
                visible: root.qwenRuntimeSupported && root.qwenCpuGuidance !== ""
            }

            // Variant + storage. The path is a copy/open target, never
            // prose: a user locating a multi-GB install should not retype
            // it.
            GridLayout {
                Layout.fillWidth: true
                columns: root.isCompact ? 1 : 2
                columnSpacing: Theme.spacingLg
                rowSpacing: Theme.spacingSm

                Label {
                    id: qwenRuntimeVariantLabel

                    objectName: root.named("RuntimeVariantLabel")
                    Layout.fillWidth: true
                    text: root.qwenRuntimeVariantText
                    color: Theme.text
                    font.family: Theme.fontFamily
                    font.pixelSize: Theme.fontSizeSm
                    wrapMode: Text.Wrap
                }

                RowLayout {
                    Layout.fillWidth: true
                    Layout.alignment: Qt.AlignRight
                    spacing: Theme.spacingSm

                    Label {
                        id: qwenRuntimeStorageLabel

                        objectName: root.named("RuntimeStorageLabel")
                        Layout.fillWidth: true
                        text: root.qwenRuntimeState === "unavailable"
                            || root.qwenRuntimeState === "checking"
                            || root.qwenRuntimeState === "unsupported"
                            ? qsTr("Cần %1 dung lượng tải").arg(root.formatBytes(
                                controller ? controller.qwenRuntimeRequiredBytes : 0))
                            : qsTr("Đã tải %1 / cần %2").arg(root.formatBytes(
                                controller ? controller.qwenRuntimeInstalledBytes : 0)).arg(
                                root.formatBytes(controller ? controller.qwenRuntimeRequiredBytes : 0))
                        color: Theme.textMuted
                        font.family: Theme.fontFamily
                        font.pixelSize: Theme.fontSizeXs
                        wrapMode: Text.Wrap
                    }

                    AppButton {
                        id: qwenRuntimeOpenDirButton

                        objectName: root.named("RuntimeOpenDirButton")
                        variant: "quiet"
                        size: "sm"
                        iconKind: "folder"
                        text: qsTr("Mở thư mục")
                        accessibleLabel: qsTr("Mở thư mục runtime Qwen")
                        onClicked: controller.openQwenRuntimeDir()
                    }
                }
            }

            ProgressBar {
                id: qwenRuntimeProgress

                objectName: root.named("RuntimeProgress")
                Layout.fillWidth: true
                from: 0
                to: 1
                value: controller ? controller.qwenRuntimeProgress : 0
                visible: root.qwenRuntimeState === "downloading"
                    || root.qwenRuntimeState === "validating"
                Accessible.name: qsTr("Tiến trình tải runtime Qwen")
            }

            Flow {
                Layout.fillWidth: true
                spacing: Theme.spacingSm
                visible: root.qwenRuntimeSupported

                AppButton {
                    id: qwenRuntimeInstallButton

                    objectName: root.named("RuntimeInstallButton")
                    variant: "primary"
                    size: "sm"
                    iconKind: "download"
                    text: qsTr("Cài đặt runtime Qwen")
                    accessibleLabel: qsTr("Cài đặt runtime Qwen")
                    visible: root.qwenRuntimeState === "unavailable"
                        || root.qwenRuntimeState === "checking"
                    enabled: !root.qwenRuntimeBusy
                    onClicked: controller.installQwenRuntime()
                }

                AppButton {
                    id: qwenRuntimeCancelButton

                    objectName: root.named("RuntimeCancelButton")
                    variant: "secondary"
                    size: "sm"
                    iconKind: "close"
                    text: qsTr("Hủy tải runtime Qwen")
                    accessibleLabel: qsTr("Hủy tải runtime Qwen")
                    visible: root.qwenRuntimeState === "downloading"
                        || root.qwenRuntimeState === "validating"
                    onClicked: controller.cancelQwenRuntimeInstall()
                }

                AppButton {
                    id: qwenRuntimeRepairButton

                    objectName: root.named("RuntimeRepairButton")
                    variant: "primary"
                    size: "sm"
                    iconKind: "refresh"
                    text: qsTr("Sửa chữa runtime Qwen")
                    accessibleLabel: qsTr("Sửa chữa runtime Qwen")
                    visible: root.qwenRuntimeState === "failed"
                    enabled: !root.qwenRuntimeBusy
                    onClicked: controller.repairQwenRuntime()
                }

                AppButton {
                    id: qwenRuntimeRemoveButton

                    objectName: root.named("RuntimeRemoveButton")
                    variant: "danger"
                    size: "sm"
                    iconKind: "close"
                    text: qsTr("Gỡ runtime Qwen")
                    accessibleLabel: qsTr("Gỡ runtime Qwen")
                    visible: root.allowRemoval && root.qwenRuntimeState === "ready"
                    enabled: !root.qwenRuntimeBusy
                    onClicked: qwenRuntimeRemoveDialog.open()
                }

                AppButton {
                    id: qwenRuntimeImportButton

                    objectName: root.named("RuntimeImportButton")
                    variant: "secondary"
                    size: "sm"
                    iconKind: "folder"
                    text: qsTr("Nhập gói runtime ngoại tuyến")
                    accessibleLabel: qsTr("Nhập gói runtime Qwen ngoại tuyến")
                    enabled: !root.qwenRuntimeBusy
                    onClicked: qwenRuntimeImportDialog.open()
                }
            }

            Label {
                id: qwenRuntimeImportHint

                objectName: root.named("RuntimeImportHint")
                Layout.fillWidth: true
                // The pack's contents are format truth: wheels under official
                // weights, a verified native-library bundle under GGUF —
                // never a PyTorch install suggested for the native backend.
                text: root.ggufSelected
                    ? qsTr("Gói ngoại tuyến là thư mục chứa đúng các tệp runtime đã ghim; dùng khi máy không có mạng.")
                    : qsTr("Gói ngoại tuyến là thư mục chứa đúng các tệp wheel đã ghim; dùng khi máy không có mạng.")
                visible: root.qwenRuntimeSupported
                color: Theme.textSubtle
                font.family: Theme.fontFamily
                font.pixelSize: Theme.fontSizeXs
                wrapMode: Text.Wrap
                lineHeight: 1.25
            }
        }

        FolderDialog {
            id: qwenRuntimeImportDialog

            objectName: root.named("RuntimeImportDialog")
            title: qsTr("Chọn thư mục gói runtime Qwen")
            onAccepted: root.pickQwenRuntimePack(qwenRuntimeImportDialog.selectedFolder)
        }
    }

    // ── Qwen model Card ───────────────────────────────────────────────────
    // One row per install the selected format ships: the two official
    // checkpoints, or the full profile × quantization GGUF matrix (each pair
    // is a separately verified install). The shared codec/tokenizer line
    // states the format's real sharing: one tokenizer tree under official,
    // one codec per quantization under GGUF. The running engine's row is
    // marked, and under GGUF the armed quantization is marked too, so the
    // card answers "what is running" and "what will be used next" as well as
    // "what is installed".
    AppCard {
        id: qwenModelCard

        objectName: root.named("ModelCard")
        visible: root.qwenProfileActive
        Layout.fillWidth: true
        title: qsTr("Mô hình Qwen")
        subtitle: root.selectedOnly
            ? qsTr("Chỉ mô hình đang chọn được hiển thị và cài đặt tại đây.")
            : (root.ggufSelected
                ? qsTr("Bốn gói GGUF 0.6B (hai hồ sơ × hai lượng tử hóa): mỗi lượng tử hóa dùng chung một codec.")
                : qsTr("Hai checkpoint 0.6B dùng chung bộ tokenizer: cài một lần, cả hai dùng lại."))
        badgeText: qsTr("%1/%2 đã cài").arg(root.qwenReadyModelCount).arg(root.visibleQwenModels.length)
        badgeColor: root.qwenReadyModelCount === root.visibleQwenModels.length
            && root.visibleQwenModels.length > 0 ? Theme.successSubtle : Theme.warningSubtle
        badgeTextColor: root.qwenReadyModelCount === root.visibleQwenModels.length
            && root.visibleQwenModels.length > 0 ? Theme.successText : Theme.warningText

        ColumnLayout {
            Layout.fillWidth: true
            spacing: Theme.spacingMd

            AppNotice {
                id: qwenModelCpuNotice

                objectName: root.named("ModelCpuNotice")
                Layout.fillWidth: true
                tone: "warning"
                title: qsTr("Chạy Qwen trên CPU rất chậm")
                message: root.qwenCpuGuidance
                messageObjectName: root.named("ModelCpuNoticeMessage")
                visible: root.qwenCpuGuidance !== ""
            }

            Repeater {
                model: root.visibleQwenModels

                Rectangle {
                    id: qwenModelRow

                    required property var modelData

                    objectName: root.named("ModelRow_") + modelData.key
                    Layout.fillWidth: true
                    implicitHeight: rowLayout.implicitHeight + Theme.spacingMd * 2
                    radius: Theme.radiusMd
                    color: Theme.surfaceAlt
                    border.color: Theme.borderSubtle
                    border.width: 1

                    ColumnLayout {
                        id: rowLayout

                        anchors.fill: parent
                        anchors.margins: Theme.spacingMd
                        spacing: Theme.spacingSm

                        RowLayout {
                            Layout.fillWidth: true
                            spacing: Theme.spacingSm

                            Label {
                                id: qwenModelLabel

                                objectName: root.named("ModelLabel_") + qwenModelRow.modelData.key
                                Layout.fillWidth: true
                                text: qwenModelRow.modelData.label
                                color: Theme.text
                                font.family: Theme.fontFamily
                                font.pixelSize: Theme.fontSizeBase
                                font.weight: Theme.fontWeightMedium
                                wrapMode: Text.Wrap
                            }

                            Rectangle {
                                id: qwenModelSelectedBadge

                                // The armed variant — under GGUF several rows
                                // can be installed while exactly one pair is
                                // what the next job will use.
                                objectName: root.named("ModelSelectedBadge_") + qwenModelRow.modelData.key
                                visible: root.ggufSelected && qwenModelRow.modelData.isSelected
                                implicitWidth: selectedLabel.implicitWidth + Theme.spacingSm * 2
                                implicitHeight: selectedLabel.implicitHeight + Theme.spacingXxs * 2
                                radius: Theme.radiusPill
                                color: Theme.warningSubtle

                                Label {
                                    id: selectedLabel
                                    anchors.centerIn: parent
                                    text: qsTr("Đã chọn")
                                    color: Theme.warningText
                                    font.family: Theme.fontFamily
                                    font.pixelSize: Theme.fontSizeXs
                                    font.weight: Theme.fontWeightMedium
                                }
                            }

                            Rectangle {
                                id: qwenModelActiveBadge

                                objectName: root.named("ModelActiveBadge_") + qwenModelRow.modelData.key
                                visible: qwenModelRow.modelData.isActive
                                implicitWidth: activeLabel.implicitWidth + Theme.spacingSm * 2
                                implicitHeight: activeLabel.implicitHeight + Theme.spacingXxs * 2
                                radius: Theme.radiusPill
                                color: Theme.accentSubtle

                                Label {
                                    id: activeLabel
                                    anchors.centerIn: parent
                                    text: qsTr("Đang dùng")
                                    color: Theme.accent
                                    font.family: Theme.fontFamily
                                    font.pixelSize: Theme.fontSizeXs
                                    font.weight: Theme.fontWeightMedium
                                }
                            }

                            Rectangle {
                                id: qwenModelStateBadge

                                objectName: root.named("ModelStateBadge_") + qwenModelRow.modelData.key
                                implicitWidth: stateLabel.implicitWidth + Theme.spacingSm * 2
                                implicitHeight: stateLabel.implicitHeight + Theme.spacingXxs * 2
                                radius: Theme.radiusPill
                                color: {
                                    switch (qwenModelRow.modelData.state) {
                                    case "ready":
                                        return Theme.successSubtle;
                                    case "failed":
                                        return Theme.errorSubtle;
                                    case "downloading":
                                    case "validating":
                                        return Theme.accentSubtle;
                                    default:
                                        return Theme.warningSubtle;
                                    }
                                }

                                Label {
                                    id: stateLabel

                                    objectName: root.named("ModelStateLabel_") + qwenModelRow.modelData.key
                                    anchors.centerIn: parent
                                    text: {
                                        switch (qwenModelRow.modelData.state) {
                                        case "ready":
                                            return qsTr("Sẵn sàng");
                                        case "failed":
                                            return qsTr("Cần chú ý");
                                        case "downloading":
                                            return qsTr("Đang tải");
                                        case "validating":
                                            return qsTr("Đang xác thực");
                                        case "checking":
                                            return qsTr("Đang kiểm tra");
                                        default:
                                            return qsTr("Chưa cài đặt");
                                        }
                                    }
                                    color: {
                                        switch (qwenModelRow.modelData.state) {
                                        case "ready":
                                            return Theme.successText;
                                        case "failed":
                                            return Theme.errorText;
                                        case "downloading":
                                        case "validating":
                                            return Theme.accent;
                                        default:
                                            return Theme.warningText;
                                        }
                                    }
                                    font.family: Theme.fontFamily
                                    font.pixelSize: Theme.fontSizeXs
                                    font.weight: Theme.fontWeightMedium
                                }
                            }
                        }

                        Label {
                            id: qwenModelStorageLabel

                            objectName: root.named("ModelStorageLabel_") + qwenModelRow.modelData.key
                            Layout.fillWidth: true
                            text: qwenModelRow.modelData.ready
                                ? qsTr("Đã cài %1 · tải về %2").arg(
                                    root.formatBytes(qwenModelRow.modelData.installedBytes)).arg(
                                    root.formatBytes(qwenModelRow.modelData.requiredBytes))
                                : qsTr("Cần tải %1").arg(
                                    root.formatBytes(qwenModelRow.modelData.requiredBytes))
                            color: Theme.textMuted
                            font.family: Theme.fontFamily
                            font.pixelSize: Theme.fontSizeXs
                            wrapMode: Text.Wrap
                        }

                        ProgressBar {
                            id: qwenModelProgress

                            objectName: root.named("ModelProgress_") + qwenModelRow.modelData.key
                            Layout.fillWidth: true
                            from: 0
                            to: 1
                            value: qwenModelRow.modelData.progress
                            visible: qwenModelRow.modelData.state === "downloading"
                                || qwenModelRow.modelData.state === "validating"
                            Accessible.name: qsTr("Tiến trình tải mô hình Qwen")
                        }

                        Label {
                            id: qwenModelErrorLabel

                            objectName: root.named("ModelErrorLabel_") + qwenModelRow.modelData.key
                            Layout.fillWidth: true
                            text: qwenModelRow.modelData.error
                            visible: qwenModelRow.modelData.error !== ""
                            color: Theme.errorText
                            font.family: Theme.fontFamily
                            font.pixelSize: Theme.fontSizeXs
                            wrapMode: Text.Wrap
                            lineHeight: 1.25
                        }

                        Flow {
                            Layout.fillWidth: true
                            spacing: Theme.spacingSm

                            AppButton {
                                id: qwenModelInstallButton

                                objectName: root.named("ModelInstallButton_") + qwenModelRow.modelData.key
                                variant: "primary"
                                size: "sm"
                                iconKind: "download"
                                // "Cài đặt" alone also means "Settings" in
                                // Vietnamese; the bare source translated to
                                // "Settings" on this button.
                                text: qsTr("Cài đặt mô hình")
                                accessibleLabel: qsTr("Cài đặt %1").arg(qwenModelRow.modelData.label)
                                visible: qwenModelRow.modelData.state === "unavailable"
                                    || qwenModelRow.modelData.state === "checking"
                                // Platform truth: a model download is
                                // GB-scale and pointless without a runnable
                                // runtime — same gate the runtime card's
                                // action row enforces by hiding itself.
                                enabled: !root.qwenModelBusy && root.qwenRuntimeSupported
                                disabledReason: !root.qwenRuntimeSupported
                                    ? qsTr("Máy này không có runtime Qwen — xem thẻ Runtime ở trên.")
                                    : ""
                                onClicked: controller.installQwenModel(qwenModelRow.modelData.key)
                            }

                            AppButton {
                                id: qwenModelCancelButton

                                objectName: root.named("ModelCancelButton_") + qwenModelRow.modelData.key
                                variant: "secondary"
                                size: "sm"
                                iconKind: "close"
                                text: qsTr("Hủy tải")
                                accessibleLabel: qsTr("Hủy tải %1").arg(qwenModelRow.modelData.label)
                                visible: qwenModelRow.modelData.busy
                                onClicked: controller.cancelQwenModelDownload(qwenModelRow.modelData.key)
                            }

                            AppButton {
                                id: qwenModelRepairButton

                                objectName: root.named("ModelRepairButton_") + qwenModelRow.modelData.key
                                variant: "primary"
                                size: "sm"
                                iconKind: "refresh"
                                text: qsTr("Sửa chữa")
                                accessibleLabel: qsTr("Sửa chữa %1").arg(qwenModelRow.modelData.label)
                                visible: qwenModelRow.modelData.state === "failed"
                                enabled: !root.qwenModelBusy
                                onClicked: controller.repairQwenModel(qwenModelRow.modelData.key)
                            }

                            AppButton {
                                id: qwenModelRemoveButton

                                objectName: root.named("ModelRemoveButton_") + qwenModelRow.modelData.key
                                variant: "danger"
                                size: "sm"
                                iconKind: "close"
                                text: qsTr("Gỡ mô hình")
                                accessibleLabel: qsTr("Gỡ %1").arg(qwenModelRow.modelData.label)
                                visible: root.allowRemoval && qwenModelRow.modelData.state === "ready"
                                enabled: !root.qwenModelBusy
                                onClicked: root.confirmQwenModelRemove(qwenModelRow.modelData.key)
                            }

                            AppButton {
                                id: qwenModelImportButton

                                objectName: root.named("ModelImportButton_") + qwenModelRow.modelData.key
                                variant: "secondary"
                                size: "sm"
                                iconKind: "folder"
                                text: qsTr("Nhập gói ngoại tuyến")
                                accessibleLabel: qsTr("Nhập gói ngoại tuyến cho %1").arg(
                                    qwenModelRow.modelData.label)
                                enabled: !root.qwenModelBusy
                                onClicked: root.openQwenModelPackDialog(qwenModelRow.modelData.key)
                            }
                        }
                    }
                }
            }

            GridLayout {
                Layout.fillWidth: true
                columns: root.isCompact ? 1 : 2
                columnSpacing: Theme.spacingLg
                rowSpacing: Theme.spacingSm

                Label {
                    id: qwenSharedStorageLabel

                    objectName: root.named("SharedStorageLabel")
                    Layout.fillWidth: true
                    // Per-variant truth: official shares one tokenizer tree
                    // across both checkpoints; GGUF ships one codec per
                    // quantization shared by both profiles of that variant.
                    text: root.ggufSelected
                        ? qsTr("Dùng chung: %1 codec cho mỗi lượng tử hóa").arg(
                            root.formatBytes(controller ? controller.qwenSharedBytes : 0))
                        : qsTr("Dùng chung: %1 tokenizer cho cả hai checkpoint").arg(
                            root.formatBytes(controller ? controller.qwenSharedBytes : 0))
                    color: Theme.textMuted
                    font.family: Theme.fontFamily
                    font.pixelSize: Theme.fontSizeXs
                    wrapMode: Text.Wrap
                }

                RowLayout {
                    Layout.fillWidth: true
                    Layout.alignment: Qt.AlignRight
                    spacing: Theme.spacingSm

                    Label {
                        id: qwenModelStoragePathLabel

                        objectName: root.named("ModelStoragePathLabel")
                        Layout.fillWidth: true
                        text: controller ? controller.qwenModelStoragePath : ""
                        color: Theme.textMuted
                        font.family: Theme.fontFamily
                        font.pixelSize: Theme.fontSizeXs
                        elide: Text.ElideMiddle
                    }

                    AppButton {
                        id: qwenModelOpenDirButton

                        objectName: root.named("ModelOpenDirButton")
                        variant: "quiet"
                        size: "sm"
                        iconKind: "folder"
                        text: qsTr("Mở thư mục")
                        accessibleLabel: qsTr("Mở thư mục mô hình Qwen")
                        onClicked: controller.openQwenModelDir()
                    }
                }
            }
        }

        FolderDialog {
            id: qwenModelPackDialog

            objectName: root.named("ModelPackDialog")
            title: qsTr("Chọn thư mục gói mô hình Qwen")
            property string pendingProfileKey: ""
            onAccepted: root.pickQwenModelPack(pendingProfileKey, qwenModelPackDialog.selectedFolder)
        }
    }

    RemoveConfirmDialog {
        id: qwenRuntimeRemoveDialog

        objectName: root.named("RuntimeRemoveDialog")
        title: qsTr("Gỡ runtime Qwen?")
        body: qsTr("Toàn bộ tệp đã tải sẽ bị xóa khỏi máy. Bạn sẽ cần tải lại để dùng lại.")
        confirmLabel: qsTr("Gỡ runtime")
        confirmObjectName: root.named("RuntimeRemoveConfirmButton")
        onConfirmed: controller.removeQwenRuntime()
    }

    RemoveConfirmDialog {
        id: qwenModelRemoveDialog

        objectName: root.named("ModelRemoveDialog")
        property string pendingProfileKey: ""
        title: qsTr("Gỡ mô hình Qwen?")
        body: qsTr("Toàn bộ tệp đã tải sẽ bị xóa khỏi máy. Bạn sẽ cần tải lại để dùng lại.")
        confirmLabel: qsTr("Gỡ mô hình")
        confirmObjectName: root.named("ModelRemoveConfirmButton")
        onConfirmed: controller.removeQwenModel(qwenModelRemoveDialog.pendingProfileKey)
    }
}
