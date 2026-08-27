// Settings tab (FR-3.5, FR-UX-7): engine backend/precision (apply on next engine
// init — surfaced via the needsRestart banner), default voice, output
// directory, temperature, and theme. Engine/output settings flow through
// the `controller` seam (validated + persisted, invalid writes become
// errorText); the theme control writes `bridge.themePreference` — the
// phase-2 live-switch path that persists to the same settings.json field
// and re-resolves the effective theme immediately.
//
// objectNames are the tested contract (tests/smoke/test_ui_tabs.py):
// settingsTab, backendCombo, detectedEngineLabel, precisionCombo,
// needsRestartBanner, defaultVoiceCombo, outputDirLabel, outputDirBrowseButton,
// outputDirDialog, temperatureSpin, themeCombo, errorLabel.
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
        color: Theme.surface
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

    FolderDialog {
        id: outputDirDialog

        objectName: "outputDirDialog"
        title: qsTr("Chọn thư mục xuất âm thanh")
        onAccepted: root.setOutputDir(root.toLocalPath(outputDirDialog.selectedFolder))
    }

    // QUrl → local path string (same helper idiom as the other tabs).
    function toLocalPath(url) {
        const s = url.toString();
        return s.startsWith("file://") ? decodeURIComponent(s.substring(7)) : s;
    }

    ScrollView {
        anchors.fill: parent
        contentWidth: availableWidth
        clip: true

        ColumnLayout {
            width: Math.min(840, root.availableWidth - Theme.spacingLg * 2)
            anchors.horizontalCenter: parent.horizontalCenter
            spacing: Theme.spacingLg

            // Header Section
            RowLayout {
                Layout.fillWidth: true
                spacing: Theme.spacingMd

                Rectangle {
                    width: 42
                    height: 42
                    radius: Theme.radiusMd
                    color: Theme.accentSubtle
                    border.color: Theme.border
                    border.width: 1

                    Canvas {
                        anchors.centerIn: parent
                        width: 20
                        height: 20
                        renderTarget: Canvas.FramebufferObject
                        Component.onCompleted: requestPaint()
                        onPaint: {
                            const ctx = getContext("2d");
                            ctx.clearRect(0, 0, width, height);
                            ctx.strokeStyle = Theme.accent;
                            ctx.fillStyle = Theme.accent;
                            ctx.lineWidth = 1.5;
                            ctx.lineCap = "round";
                            ctx.beginPath();
                            ctx.moveTo(3, 6); ctx.lineTo(17, 6);
                            ctx.moveTo(3, 14); ctx.lineTo(17, 14);
                            ctx.stroke();
                            ctx.beginPath();
                            ctx.arc(8, 6, 2.5, 0, Math.PI * 2);
                            ctx.arc(13, 14, 2.5, 0, Math.PI * 2);
                            ctx.fill();
                        }
                    }
                }

                ColumnLayout {
                    Layout.fillWidth: true
                    spacing: 2

                    Label {
                        text: qsTr("Cài đặt hệ thống")
                        color: Theme.text
                        font.family: Theme.fontFamily
                        font.pixelSize: Theme.fontSizeXl
                        font.weight: Theme.fontWeightHeading
                    }

                    Label {
                        Layout.fillWidth: true
                        text: qsTr("Cấu hình engine suy luận, âm thanh, giọng mặc định và giao diện hiển thị.")
                        color: Theme.textMuted
                        font.family: Theme.fontFamily
                        font.pixelSize: Theme.fontSizeSm
                        wrapMode: Text.Wrap
                    }
                }
            }

            // ── 1. Engine & Hardware Card ─────────────────────────────────────
            AppCard {
                Layout.fillWidth: true
                title: qsTr("Engine & Phần cứng")
                subtitle: qsTr("Thiết lập môi trường tính toán AI cho VieNeu-TTS v3 Turbo")
                badgeText: bridge ? bridge.engineNote : ""
                badgeColor: Theme.accentSubtle
                badgeTextColor: Theme.accent

                ColumnLayout {
                    Layout.fillWidth: true
                    spacing: Theme.spacingMd

                    // Hardware capability note
                    Rectangle {
                        Layout.fillWidth: true
                        implicitHeight: detectedLayout.implicitHeight + Theme.spacingMd * 2
                        radius: Theme.radiusSm
                        color: Theme.surfaceAlt
                        border.color: Theme.border
                        border.width: 1

                        RowLayout {
                            id: detectedLayout
                            anchors.fill: parent
                            anchors.margins: Theme.spacingMd
                            spacing: Theme.spacingMd

                            Rectangle {
                                width: 8
                                height: 8
                                radius: 4
                                color: Theme.accent
                                Layout.alignment: Qt.AlignVCenter
                            }

                            ColumnLayout {
                                Layout.fillWidth: true
                                spacing: 2

                                Label {
                                    text: qsTr("Khả năng phần cứng phát hiện được")
                                    color: Theme.text
                                    font.family: Theme.fontFamily
                                    font.pixelSize: Theme.fontSizeSm
                                    font.weight: Theme.fontWeightMedium
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
                    }

                    // Backend selector
                    RowLayout {
                        Layout.fillWidth: true
                        spacing: Theme.spacingMd

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
                            }
                        }

                        ComboBox {
                            id: backendCombo
                            objectName: "backendCombo"
                            implicitWidth: 260
                            implicitHeight: 38
                            textRole: "label"
                            function openPopup() { popup.open(); }
                            function closePopup() { popup.close(); }
                            model: root.backendOptions
                            currentIndex: root.valueIndex(root.backendOptions, controller.backend)
                            onActivated: function (index) {
                                controller.backend = root.backendOptions[index].value;
                            }
                            contentItem: Label {
                                leftPadding: Theme.spacingMd
                                rightPadding: Theme.spacingMd
                                text: backendCombo.displayText
                                font.family: Theme.fontFamily
                                font.pixelSize: Theme.fontSizeBase
                                color: Theme.text
                                verticalAlignment: Text.AlignVCenter
                                elide: Text.ElideRight
                            }
                            background: Rectangle {
                                implicitHeight: 38
                                radius: Theme.radiusSm
                                color: Theme.surface
                                border.color: backendCombo.activeFocus ? Theme.accent : Theme.borderSubtle
                                border.width: 1
                            }
                            delegate: ItemDelegate {
                                required property var modelData
                                required property int index
                                width: backendCombo.width
                                text: modelData.label
                                highlighted: backendCombo.highlightedIndex === index
                            }
                        }
                    }

                    // Precision selector
                    RowLayout {
                        Layout.fillWidth: true
                        spacing: Theme.spacingMd

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
                            }
                        }

                        ComboBox {
                            id: precisionCombo
                            objectName: "precisionCombo"
                            implicitWidth: 260
                            implicitHeight: 38
                            textRole: "label"
                            function openPopup() { popup.open(); }
                            function closePopup() { popup.close(); }
                            model: root.precisionOptions
                            currentIndex: root.valueIndex(root.precisionOptions, controller.precision)
                            onActivated: function (index) {
                                controller.precision = root.precisionOptions[index].value;
                            }
                            contentItem: Label {
                                leftPadding: Theme.spacingMd
                                rightPadding: Theme.spacingMd
                                text: precisionCombo.displayText
                                font.family: Theme.fontFamily
                                font.pixelSize: Theme.fontSizeBase
                                color: Theme.text
                                verticalAlignment: Text.AlignVCenter
                                elide: Text.ElideRight
                            }
                            background: Rectangle {
                                implicitHeight: 38
                                radius: Theme.radiusSm
                                color: Theme.surface
                                border.color: precisionCombo.activeFocus ? Theme.accent : Theme.borderSubtle
                                border.width: 1
                            }
                            delegate: ItemDelegate {
                                required property var modelData
                                required property int index
                                width: precisionCombo.width
                                text: modelData.label
                                highlighted: precisionCombo.highlightedIndex === index
                            }
                        }
                    }

                    // Needs restart banner
                    Rectangle {
                        id: needsRestartCard
                        Layout.fillWidth: true
                        implicitHeight: needsRestartBanner.implicitHeight + Theme.spacingMd * 2
                        radius: Theme.radiusSm
                        color: Theme.warningSubtle
                        border.color: Theme.warning
                        border.width: 1
                        visible: controller.needsRestart

                        RowLayout {
                            anchors.fill: parent
                            anchors.margins: Theme.spacingMd
                            spacing: Theme.spacingSm

                            Rectangle {
                                width: 8
                                height: 8
                                radius: 4
                                color: Theme.warning
                                Layout.alignment: Qt.AlignVCenter
                            }

                            Label {
                                id: needsRestartBanner
                                objectName: "needsRestartBanner"
                                Layout.fillWidth: true
                                visible: controller.needsRestart
                                text: qsTr("Thay đổi backend/độ chính xác sẽ áp dụng ở lần khởi động engine tiếp theo.")
                                color: Theme.warning
                                font.family: Theme.fontFamily
                                font.pixelSize: Theme.fontSizeSm
                                font.weight: Theme.fontWeightMedium
                                wrapMode: Text.Wrap
                            }
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
                    spacing: Theme.spacingMd

                    // Default Voice
                    RowLayout {
                        Layout.fillWidth: true
                        spacing: Theme.spacingMd

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
                                text: qsTr("Giọng được tự động chọn khi mở ứng dụng")
                                color: Theme.textMuted
                                font.family: Theme.fontFamily
                                font.pixelSize: Theme.fontSizeXs
                            }
                        }

                        ComboBox {
                            id: defaultVoiceCombo
                            objectName: "defaultVoiceCombo"
                            implicitWidth: 260
                            implicitHeight: 38
                            property var flatModel: root.buildFlatModel(controller.voices)
                            property string selectedVoice: {
                                const row = flatModel[currentIndex];
                                return row && row.id !== "" ? row.id : controller.defaultVoice;
                            }
                            textRole: "label"
                            model: flatModel
                            currentIndex: {
                                for (let i = 0; i < flatModel.length; i++)
                                    if (flatModel[i].id === controller.defaultVoice)
                                        return i;
                                return 0;
                            }
                            onActivated: function (index) {
                                const row = flatModel[index];
                                if (row && row.id !== "")
                                    controller.defaultVoice = row.id;
                            }
                            contentItem: Label {
                                leftPadding: Theme.spacingMd
                                rightPadding: Theme.spacingMd
                                text: defaultVoiceCombo.displayText
                                font.family: Theme.fontFamily
                                font.pixelSize: Theme.fontSizeBase
                                color: Theme.text
                                verticalAlignment: Text.AlignVCenter
                                elide: Text.ElideRight
                            }
                            background: Rectangle {
                                implicitHeight: 38
                                radius: Theme.radiusSm
                                color: Theme.surface
                                border.color: defaultVoiceCombo.activeFocus ? Theme.accent : Theme.borderSubtle
                                border.width: 1
                            }
                            delegate: ItemDelegate {
                                required property var modelData
                                required property int index
                                width: defaultVoiceCombo.width
                                text: modelData ? modelData.label : ""
                                highlighted: defaultVoiceCombo.highlightedIndex === index
                                enabled: modelData ? modelData.id !== "" : true
                            }
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

                        RowLayout {
                            Layout.fillWidth: true

                            ColumnLayout {
                                Layout.fillWidth: true
                                spacing: 2

                                Label {
                                    text: qsTr("Thư mục xuất âm thanh")
                                    color: Theme.text
                                    font.family: Theme.fontFamily
                                    font.pixelSize: Theme.fontSizeBase
                                    font.weight: Theme.fontWeightMedium
                                }

                                Label {
                                    text: qsTr("Vị trí lưu trữ các tệp âm thanh xuất ra (.wav)")
                                    color: Theme.textMuted
                                    font.family: Theme.fontFamily
                                    font.pixelSize: Theme.fontSizeXs
                                }
                            }
                        }

                        Rectangle {
                            Layout.fillWidth: true
                            implicitHeight: 48
                            radius: Theme.radiusSm
                            color: Theme.surfaceAlt
                            border.color: Theme.border
                            border.width: 1

                            RowLayout {
                                anchors.fill: parent
                                anchors.margins: Theme.spacingMd
                                spacing: Theme.spacingMd

                                Rectangle {
                                    width: 8
                                    height: 8
                                    radius: 4
                                    color: Theme.accent
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
                                    font.family: Theme.fontFamily
                                    font.pixelSize: Theme.fontSizeSm
                                }

                                Button {
                                    id: outputDirBrowseButton
                                    objectName: "outputDirBrowseButton"
                                    text: qsTr("Thay đổi…")
                                    implicitHeight: 30
                                    font.family: Theme.fontFamily
                                    font.pixelSize: Theme.fontSizeSm

                                    background: Rectangle {
                                        radius: Theme.radiusSm
                                        color: outputDirBrowseButton.hovered ? Theme.surfaceHover : Theme.surfaceCard
                                        border.color: Theme.border
                                        border.width: 1
                                    }

                                    contentItem: Text {
                                        text: outputDirBrowseButton.text
                                        font: outputDirBrowseButton.font
                                        color: Theme.text
                                        horizontalAlignment: Text.AlignHCenter
                                        verticalAlignment: Text.AlignVCenter
                                    }

                                    onClicked: outputDirDialog.open()
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
                        spacing: Theme.spacingMd

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
                                text: qsTr("0.6 – 0.8: Chuẩn, ổn định tự nhiên; 0.9+: Nhiều biểu cảm và ngữ điệu hơn")
                                color: Theme.textMuted
                                font.family: Theme.fontFamily
                                font.pixelSize: Theme.fontSizeXs
                            }
                        }

                        SpinBox {
                            id: temperatureSpin
                            objectName: "temperatureSpin"
                            implicitWidth: 140
                            implicitHeight: 38
                            editable: true
                            from: 5            // ×100: bounds mirror Settings [0.05, 2.0]
                            to: 200
                            stepSize: 5
                            value: Math.round(controller.temperature * 100)

                            property int decimals: 2
                            property real realValue: value / 100

                            validator: DoubleValidator {
                                bottom: Math.min(temperatureSpin.from, temperatureSpin.to) / 100
                                top: Math.max(temperatureSpin.from, temperatureSpin.to) / 100
                                decimals: 2
                                locale: "C"
                            }

                            textFromValue: function (value, locale) {
                                return Number(value / 100).toLocaleString(locale, "f", 2);
                            }

                            valueFromText: function (text, locale) {
                                return Math.round(Number.fromLocaleString(locale, text) * 100);
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
                        spacing: Theme.spacingMd

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
                                text: qsTr("Chọn giao diện Tối (Obsidian), Sáng (Porcelain) hoặc theo hệ thống")
                                color: Theme.textMuted
                                font.family: Theme.fontFamily
                                font.pixelSize: Theme.fontSizeXs
                            }
                        }

                        ComboBox {
                            id: themeCombo
                            objectName: "themeCombo"
                            implicitWidth: 220
                            implicitHeight: 38
                            textRole: "label"
                            function openPopup() { popup.open(); }
                            function closePopup() { popup.close(); }
                            model: root.themeOptions
                            currentIndex: root.valueIndex(root.themeOptions, bridge ? bridge.themePreference : "system")
                            onActivated: function (index) {
                                if (bridge)
                                    bridge.themePreference = root.themeOptions[index].value;
                                controller.theme = root.themeOptions[index].value;
                            }
                            contentItem: Label {
                                leftPadding: Theme.spacingMd
                                rightPadding: Theme.spacingMd
                                text: themeCombo.displayText
                                font.family: Theme.fontFamily
                                font.pixelSize: Theme.fontSizeBase
                                color: Theme.text
                                verticalAlignment: Text.AlignVCenter
                                elide: Text.ElideRight
                            }
                            background: Rectangle {
                                implicitHeight: 38
                                radius: Theme.radiusSm
                                color: Theme.surface
                                border.color: themeCombo.activeFocus ? Theme.accent : Theme.borderSubtle
                                border.width: 1
                            }
                            delegate: ItemDelegate {
                                required property var modelData
                                required property int index
                                width: themeCombo.width
                                text: modelData.label
                                highlighted: themeCombo.highlightedIndex === index
                            }
                        }
                    }
                }
            }

            // Error notice banner
            Rectangle {
                Layout.fillWidth: true
                implicitHeight: errorLabel.implicitHeight + Theme.spacingMd * 2
                radius: Theme.radiusMd
                color: Theme.errorSubtle
                border.color: Theme.error
                border.width: 1
                visible: controller.errorText !== ""

                RowLayout {
                    anchors.fill: parent
                    anchors.margins: Theme.spacingMd
                    spacing: Theme.spacingSm

                    Rectangle {
                        width: 20
                        height: 20
                        radius: 10
                        color: Theme.errorSubtle
                        border.color: Theme.error
                        border.width: 1
                        Label {
                            anchors.centerIn: parent
                            text: "!"
                            color: Theme.error
                            font.family: Theme.fontFamily
                            font.pixelSize: Theme.fontSizeXs
                            font.weight: Theme.fontWeightBold
                        }
                    }

                    Label {
                        id: errorLabel
                        objectName: "errorLabel"
                        Layout.fillWidth: true
                        visible: controller.errorText !== ""
                        text: controller.errorText
                        color: Theme.error
                        font.family: Theme.fontFamily
                        font.pixelSize: Theme.fontSizeSm
                        wrapMode: Text.Wrap
                    }
                }
            }

            Item {
                Layout.fillHeight: true
            }
        }
    }
}
