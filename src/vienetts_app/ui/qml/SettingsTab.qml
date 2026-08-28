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
        return s.startsWith("file://") ? decodeURIComponent(s.substring(7)) : s;
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
                spacing: Theme.spacingMd

                // Hardware capability note — the ONE engine-note surface on
                // this tab (the sidebar carries the compact copy).
                Rectangle {
                    Layout.fillWidth: true
                    implicitHeight: detectedLayout.implicitHeight + Theme.spacingSm * 2
                    radius: Theme.radiusSm
                    color: Theme.surfaceAlt
                    border.color: Theme.borderSubtle
                    border.width: 1

                    RowLayout {
                        id: detectedLayout
                        anchors.fill: parent
                        anchors.margins: Theme.spacingSm
                        spacing: Theme.spacingSm

                        Rectangle {
                            width: 8
                            height: 8
                            radius: 4
                            color: Theme.accent
                            Layout.alignment: Qt.AlignVCenter
                        }

                        Label {
                            id: detectedEngineLabel
                            objectName: "detectedEngineLabel"
                            Layout.fillWidth: true
                            text: bridge ? bridge.engineNote : ""
                            color: Theme.textMuted
                            font.family: Theme.fontFamily
                            font.pixelSize: Theme.fontSizeXs
                            wrapMode: Text.Wrap
                        }
                    }
                }

                // Backend selector
                RowLayout {
                    Layout.fillWidth: true
                    spacing: Theme.spacingLg

                    ColumnLayout {
                        Layout.fillWidth: true
                        spacing: 2

                        Label {
                            text: qsTr("Backend suy luận")
                            color: Theme.text
                            font.family: Theme.fontFamily
                            font.pixelSize: Theme.fontSizeBase
                            font.weight: Theme.fontWeightMedium
                        }

                        Label {
                            text: qsTr("Chọn ONNX Runtime (CPU) hoặc PyTorch (NVIDIA GPU)")
                            color: Theme.textMuted
                            font.family: Theme.fontFamily
                            font.pixelSize: Theme.fontSizeXs
                            wrapMode: Text.Wrap
                        }
                    }

                    AppCombo {
                        id: backendCombo
                        objectName: "backendCombo"
                        comboWidth: root.width < 700 ? 240 : 260
                        accessibleLabel: qsTr("Backend suy luận")
                        textRole: "label"
                        model: root.backendOptions
                        currentIndex: root.valueIndex(root.backendOptions, controller.backend)
                        onActivated: function (index) {
                            controller.backend = root.backendOptions[index].value;
                        }
                    }
                }

                // Precision selector
                RowLayout {
                    Layout.fillWidth: true
                    spacing: Theme.spacingLg

                    ColumnLayout {
                        Layout.fillWidth: true
                        spacing: 2

                        Label {
                            text: qsTr("Độ chính xác mô hình (ONNX)")
                            color: Theme.text
                            font.family: Theme.fontFamily
                            font.pixelSize: Theme.fontSizeBase
                            font.weight: Theme.fontWeightMedium
                        }

                        Label {
                            text: qsTr("int8: tối ưu tốc độ & bộ nhớ; fp32: chất lượng cao nhất")
                            color: Theme.textMuted
                            font.family: Theme.fontFamily
                            font.pixelSize: Theme.fontSizeXs
                            wrapMode: Text.Wrap
                        }
                    }

                    AppCombo {
                        id: precisionCombo
                        objectName: "precisionCombo"
                        comboWidth: root.width < 700 ? 240 : 260
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
                    message: qsTr("Thay đổi backend/độ chính xác sẽ áp dụng ở lần khởi động engine tiếp theo.")
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
                spacing: Theme.spacingMd

                // Default Voice
                RowLayout {
                    Layout.fillWidth: true
                    spacing: Theme.spacingLg

                    ColumnLayout {
                        Layout.fillWidth: true
                        spacing: 2

                        Label {
                            text: qsTr("Giọng đọc mặc định")
                            color: Theme.text
                            font.family: Theme.fontFamily
                            font.pixelSize: Theme.fontSizeBase
                            font.weight: Theme.fontWeightMedium
                        }

                        Label {
                            Layout.fillWidth: true
                            text: qsTr("Giọng được tự động chọn khi mở ứng dụng")
                            color: Theme.textMuted
                            font.family: Theme.fontFamily
                            font.pixelSize: Theme.fontSizeXs
                            wrapMode: Text.Wrap
                        }
                    }

                    VoicePicker {
                        id: defaultVoiceCombo
                        objectName: "defaultVoiceCombo"
                        purpose: "default"
                        implicitWidth: 260
                    }
                }

                Rectangle {
                    Layout.fillWidth: true
                    height: 1
                    color: Theme.borderSubtle
                }

                // Output Directory
                ColumnLayout {
                    Layout.fillWidth: true
                    spacing: Theme.spacingSm

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
                    }

                    Rectangle {
                        Layout.fillWidth: true
                        implicitHeight: 48
                        radius: Theme.radiusSm
                        color: Theme.surfaceAlt
                        border.color: Theme.borderSubtle
                        border.width: 1

                        RowLayout {
                            anchors.fill: parent
                            anchors.margins: Theme.spacingSm
                            spacing: Theme.spacingMd

                            AppIcon {
                                kind: "file"
                                width: 16
                                height: 16
                                iconColor: Theme.accent
                                Layout.alignment: Qt.AlignVCenter
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
                                font.family: Theme.fontFamilyMono
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
                }

                // Temperature Setting
                RowLayout {
                    Layout.fillWidth: true
                    spacing: Theme.spacingLg

                    ColumnLayout {
                        Layout.fillWidth: true
                        spacing: 2

                        Label {
                            text: qsTr("Temperature (Độ biến thiên)")
                            color: Theme.text
                            font.family: Theme.fontFamily
                            font.pixelSize: Theme.fontSizeBase
                            font.weight: Theme.fontWeightMedium
                        }

                        Label {
                            Layout.fillWidth: true
                            text: qsTr("0.6 – 0.8: Chuẩn, ổn định tự nhiên; 0.9+: Nhiều biểu cảm và ngữ điệu hơn")
                            color: Theme.textMuted
                            font.family: Theme.fontFamily
                            font.pixelSize: Theme.fontSizeXs
                            wrapMode: Text.Wrap
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

                        validator: DoubleValidator {
                            bottom: Math.min(temperatureSpin.from, temperatureSpin.to) / 100
                            top: Math.max(temperatureSpin.from, temperatureSpin.to) / 100
                            decimals: 2
                            locale: "C"
                        }

                        onRealValueChanged: controller.temperature = realValue
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
                spacing: Theme.spacingMd

                RowLayout {
                    Layout.fillWidth: true
                    spacing: Theme.spacingLg

                    ColumnLayout {
                        Layout.fillWidth: true
                        spacing: 2

                        Label {
                            text: qsTr("Chế độ màu sắc")
                            color: Theme.text
                            font.family: Theme.fontFamily
                            font.pixelSize: Theme.fontSizeBase
                            font.weight: Theme.fontWeightMedium
                        }

                        Label {
                            Layout.fillWidth: true
                            text: qsTr("Chọn giao diện Tối, Sáng hoặc theo hệ thống — áp dụng ngay lập tức")
                            color: Theme.textMuted
                            font.family: Theme.fontFamily
                            font.pixelSize: Theme.fontSizeXs
                            wrapMode: Text.Wrap
                        }
                    }

                    AppCombo {
                        id: themeCombo
                        objectName: "themeCombo"
                        comboWidth: 220
                        accessibleLabel: qsTr("Chế độ màu sắc")
                        textRole: "label"
                        model: root.themeOptions
                        currentIndex: root.valueIndex(root.themeOptions, bridge ? bridge.themePreference : "system")
                        onActivated: function (index) {
                            if (bridge)
                                bridge.themePreference = root.themeOptions[index].value;
                            controller.theme = root.themeOptions[index].value;
                        }

                        // Delegate with a theme preview dot (system · light · dark)
                        delegate: ItemDelegate {
                            id: themeRow

                            required property var modelData
                            required property int index

                            width: themeCombo.width - (Theme.spacingXs * 2)
                            height: 38
                            highlighted: themeCombo.highlightedIndex === themeRow.index
                            hoverEnabled: true

                            HoverHandler {
                                cursorShape: Qt.PointingHandCursor
                            }

                            contentItem: RowLayout {
                                spacing: Theme.spacingSm

                                Rectangle {
                                    Layout.leftMargin: Theme.spacingSm
                                    width: 14
                                    height: 14
                                    radius: 7
                                    color: themeRow.highlighted ? Theme.accentSubtle : Theme.surfaceAlt
                                    border.color: themeRow.highlighted ? Theme.accent : Theme.border
                                    border.width: 1

                                    // Dark: filled · System: half dot · Light: empty
                                    Rectangle {
                                        anchors.fill: parent
                                        anchors.margins: 3
                                        radius: 5
                                        color: themeRow.highlighted ? Theme.accent : Theme.textMuted
                                        visible: themeRow.modelData && themeRow.modelData.value === "dark"
                                    }

                                    Rectangle {
                                        anchors.centerIn: parent
                                        width: 6
                                        height: 6
                                        radius: 3
                                        color: themeRow.highlighted ? Theme.accent : Theme.textSubtle
                                        visible: themeRow.modelData && themeRow.modelData.value === "system"
                                    }
                                }

                                Label {
                                    Layout.fillWidth: true
                                    text: themeRow.modelData ? themeRow.modelData.label : ""
                                    color: themeRow.highlighted ? Theme.accent : Theme.text
                                    font.family: Theme.fontFamily
                                    font.pixelSize: Theme.fontSizeBase
                                    font.weight: (themeCombo.currentIndex === themeRow.index || themeRow.highlighted)
                                        ? Theme.fontWeightMedium : Theme.fontWeightNormal
                                    verticalAlignment: Text.AlignVCenter
                                }

                                AppIcon {
                                    Layout.rightMargin: Theme.spacingSm
                                    width: 14
                                    height: 14
                                    kind: "check"
                                    iconColor: Theme.accent
                                    visible: themeCombo.currentIndex === themeRow.index
                                }
                            }

                            background: Rectangle {
                                radius: Theme.radiusSm
                                color: themeRow.highlighted ? Theme.accentSubtle : (themeRow.hovered ? Theme.surfaceHover : "transparent")
                                Behavior on color { ColorAnimation { duration: Theme.durationFast } }
                            }
                        }
                    }
                }

                // Language picker — applies LIVE (like the theme combo above):
                // the shell swaps translators and retranslate()s on change.
                RowLayout {
                    Layout.fillWidth: true
                    spacing: Theme.spacingLg

                    ColumnLayout {
                        Layout.fillWidth: true
                        spacing: 2

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
                        }
                    }

                    AppCombo {
                        id: languageCombo
                        objectName: "languageCombo"
                        comboWidth: 220
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
