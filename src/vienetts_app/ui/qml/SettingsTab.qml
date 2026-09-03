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
// outputDirDialog, temperatureSpin, themeCombo, languageCombo, errorLabel.
//
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

    // Tested seam for the folder dialog (native dialogs are unreliable
    // headless; the dialog's onAccepted just calls this).
    function setOutputDir(path) {
        controller.outputDir = path
    }

    // Flat picker model from controller.voices (same idiom as TextTab).
    function buildFlatModel(groups) {
        const rows = [];
        for (let i = 0; i < groups.length; i++) {
            rows.push({ id: "", label: "▸ " + groups[i].label });
            const inner = groups[i].voices;
            for (let j = 0; j < inner.length; j++)
                rows.push({ id: inner[j].id, label: "— " + inner[j].label });
        }
        return rows;
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

        // ── 1. Engine & Hardware Card ─────────────────────────────────────
        AppCard {
            Layout.fillWidth: true
            title: qsTr("Engine & Phần cứng")
            subtitle: qsTr("Thiết lập môi trường tính toán AI cho VieNeu-TTS v3 Turbo")

            ColumnLayout {
                Layout.fillWidth: true
                spacing: Theme.spacingLg

                // Hardware capability note — redesigned: left accent bar +
                // icon tile + two-line copy instead of the faint dot badge.
                // SurfaceAlt with accent border reads as “info”, not decor.
                Rectangle {
                    Layout.fillWidth: true
                    implicitHeight: Math.max(44, detectedLayout.implicitHeight + Theme.spacingMd * 2)
                    radius: Theme.radiusMd
                    color: Theme.surfaceAlt
                    border.color: Theme.borderSubtle
                    border.width: 1

                    // Accent bar
                    Rectangle {
                        width: 3
                        anchors.top: parent.top
                        anchors.bottom: parent.bottom
                        anchors.left: parent.left
                        anchors.topMargin: 8
                        anchors.bottomMargin: 8
                        anchors.leftMargin: 0
                        radius: 1.5
                        color: Theme.accent
                    }

                    RowLayout {
                        id: detectedLayout
                        anchors.fill: parent
                        anchors.leftMargin: Theme.spacingMd + 3
                        anchors.rightMargin: Theme.spacingMd
                        anchors.topMargin: Theme.spacingMd
                        anchors.bottomMargin: Theme.spacingMd
                        spacing: Theme.spacingSm

                        Rectangle {
                            width: 28
                            height: 28
                            radius: Theme.radiusSm
                            color: Theme.accentSubtle
                            border.color: Theme.borderFocus
                            border.width: 1
                            Layout.alignment: Qt.AlignVCenter

                            AppIcon {
                                anchors.centerIn: parent
                                width: 14
                                height: 14
                                kind: "settings"
                                iconColor: Theme.accent
                            }
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
                Rectangle {
                    Layout.fillWidth: true
                    height: 1
                    color: Theme.borderSubtle
                    opacity: 0.7
                }

                // -- Model source section (Hugging Face backbone & presets) --
                ColumnLayout {
                    Layout.fillWidth: true
                    spacing: Theme.spacingMd

                    // Header row with Icon + Title + StatusBadge
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
                                kind: "download"
                                iconColor: Theme.accent
                            }
                        }

                        ColumnLayout {
                            Layout.fillWidth: true
                            spacing: 3

                            RowLayout {
                                spacing: Theme.spacingSm
                                Layout.fillWidth: true

                                Label {
                                    text: qsTr("Nguồn mô hình (Hugging Face)")
                                    color: Theme.text
                                    font.family: Theme.fontFamily
                                    font.pixelSize: Theme.fontSizeBase
                                    font.weight: Theme.fontWeightMedium
                                }

                                StatusBadge {
                                    text: (controller && controller.modelRepo !== "")
                                        ? qsTr("Tùy chỉnh")
                                        : qsTr("Chính thức")
                                    status: (controller && controller.modelRepo !== "")
                                        ? "info"
                                        : "success"
                                    iconText: (controller && controller.modelRepo !== "")
                                        ? "★"
                                        : "✓"
                                }
                            }

                            Label {
                                text: qsTr("Sử dụng mô hình gốc chính thức hoặc chỉ định repository tùy chỉnh từ Hugging Face")
                                color: Theme.textMuted
                                font.family: Theme.fontFamily
                                font.pixelSize: Theme.fontSizeXs
                                wrapMode: Text.Wrap
                                Layout.fillWidth: true
                                lineHeight: 1.2
                            }
                            Label {
                                objectName: "modelSourceDetail"
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
                                Layout.fillWidth: true
                                lineHeight: 1.2
                            }
                            RowLayout {
                                Layout.fillWidth: true
                                spacing: Theme.spacingXs
                                visible: controller && controller.modelRepo === ""
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

                    // Preset Quick Selector Chips
                    RowLayout {
                        Layout.fillWidth: true
                        spacing: Theme.spacingSm

                        AppButton {
                            variant: (controller && controller.modelRepo === "") ? "primary" : "secondary"
                            size: "sm"
                            iconKind: "check"
                            text: qsTr("pnnbao-ump/VieNeu-TTS-v3-Turbo (Mặc định)")
                            accessibleLabel: qsTr("Chọn mô hình chính thức mặc định")
                            onClicked: {
                                controller.modelRepo = "";
                                modelRepoField.text = "";
                            }
                        }

                        AppButton {
                            variant: (controller && controller.modelRepo !== "") ? "primary" : "secondary"
                            size: "sm"
                            iconKind: "settings"
                            text: qsTr("Repo tùy chỉnh")
                            accessibleLabel: qsTr("Nhập repository tùy chỉnh")
                            onClicked: {
                                modelRepoField.forceActiveFocus();
                                modelRepoField.selectAll();
                            }
                        }
                    }

                    // Input Box Container & Action Bar
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
                        readonly property bool isDefault: currentText === ""
                        readonly property bool isValidRepo: /^[^\s/]+\/[^\s/]+$/.test(currentText)

                        AppIcon {
                            width: 14
                            height: 14
                            kind: parent.isDefault || parent.isValidRepo ? "check" : "close"
                            iconColor: parent.isDefault ? Theme.success : (parent.isValidRepo ? Theme.accent : Theme.error)
                            Layout.alignment: Qt.AlignVCenter
                        }

                        Label {
                            Layout.fillWidth: true
                            text: {
                                if (parent.isDefault) {
                                    return qsTr("Mô hình mặc định chính thức (48kHz, hỗ trợ tiếng Việt và tiếng Anh). Lưu tại thư mục mô hình bên trên.");
                                } else if (parent.isValidRepo) {
                                    return qsTr("Repository hợp lệ: huggingface.co/%1 (sẽ tự động tải khi khởi động engine)").arg(parent.currentText);
                                } else {
                                    return qsTr("Định dạng chưa đúng: cần có dạng 'tác_giả/tên_repo' (ví dụ: username/custom-model, không có khoảng trắng)");
                                }
                            }
                            color: parent.isDefault ? Theme.textMuted : (parent.isValidRepo ? Theme.text : Theme.errorText)
                            font.family: Theme.fontFamily
                            font.pixelSize: Theme.fontSizeXs
                            wrapMode: Text.Wrap
                            lineHeight: 1.2
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
                        Layout.preferredWidth: root.isCompact ? 0 : 280
                        Layout.alignment: root.isCompact ? Qt.AlignLeft : Qt.AlignRight | Qt.AlignVCenter
                        implicitWidth: 280
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
                                text: qsTr("Vị trí lưu trữ các tệp âm thanh xuất ra (.wav)")
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
                                onClicked: outputDirDialog.open()
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
