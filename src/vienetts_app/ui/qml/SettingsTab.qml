// Settings tab (FR-3.5): engine backend/precision (apply on next engine
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
        { value: "light", label: qsTr("Sáng") },
        { value: "dark", label: qsTr("Tối") }
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

        ColumnLayout {
            width: Math.min(720, root.availableWidth)
            spacing: Theme.spacingMd

            Label {
                text: qsTr("Cài đặt")
                color: Theme.text
                font.family: Theme.fontFamily
                font.pixelSize: Theme.fontSizeXl
                font.weight: Theme.fontWeightHeading
            }

            Label {
                Layout.fillWidth: true
                text: qsTr("Cấu hình engine, giọng mặc định, thư mục xuất âm thanh và giao diện.")
                color: Theme.textMuted
                font.family: Theme.fontFamily
                font.pixelSize: Theme.fontSizeBase
                wrapMode: Text.Wrap
            }

            // ── Engine ──────────────────────────────────────────────────────
            Label {
                text: qsTr("Engine")
                color: Theme.text
                font.family: Theme.fontFamily
                font.pixelSize: Theme.fontSizeLg
                font.weight: Theme.fontWeightHeading
            }

            Label {
                objectName: "detectedEngineLabel"
                Layout.fillWidth: true
                // Detector capability readout (model-free, FR-2.7 readout
                // repeated here per FR-3.5).
                text: bridge ? bridge.engineNote : ""
                color: Theme.textMuted
                font.family: Theme.fontFamily
                font.pixelSize: Theme.fontSizeSm
                wrapMode: Text.Wrap
            }

            RowLayout {
                Layout.fillWidth: true
                spacing: Theme.spacingSm

                Label {
                    text: qsTr("Backend")
                    color: Theme.text
                    font.family: Theme.fontFamily
                    font.pixelSize: Theme.fontSizeBase
                }

                ComboBox {
                    id: backendCombo

                    objectName: "backendCombo"
                    Layout.fillWidth: true
                    textRole: "label"
                    function openPopup() { popup.open(); }
                    function closePopup() { popup.close(); }
                    model: root.backendOptions
                    currentIndex: root.valueIndex(root.backendOptions, controller.backend)
                    onActivated: function (index) {
                        controller.backend = root.backendOptions[index].value;
                    }

                    delegate: ItemDelegate {
                        // `index` must be declared alongside modelData:
                        // required properties disable the implicit
                        // model/index context injection (same idiom as
                        // TextTab/ParagraphTab delegates).
                        required property var modelData
                        required property int index
                        width: backendCombo.width
                        text: modelData.label
                        highlighted: backendCombo.highlightedIndex === index
                    }
                }
            }

            RowLayout {
                Layout.fillWidth: true
                spacing: Theme.spacingSm

                Label {
                    text: qsTr("Độ chính xác")
                    color: Theme.text
                    font.family: Theme.fontFamily
                    font.pixelSize: Theme.fontSizeBase
                }

                ComboBox {
                    id: precisionCombo

                    objectName: "precisionCombo"
                    Layout.fillWidth: true
                    textRole: "label"
                    function openPopup() { popup.open(); }
                    function closePopup() { popup.close(); }
                    model: root.precisionOptions
                    currentIndex: root.valueIndex(root.precisionOptions, controller.precision)
                    onActivated: function (index) {
                        controller.precision = root.precisionOptions[index].value;
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

            // Apply-on-next-init semantics surfaced (FR-3.5): backend and
            // precision changes never mutate a running engine mid-flight.
            Label {
                objectName: "needsRestartBanner"
                Layout.fillWidth: true
                visible: controller.needsRestart
                text: qsTr("Thay đổi backend/độ chính xác sẽ áp dụng ở lần khởi động engine tiếp theo.")
                color: Theme.warning
                font.family: Theme.fontFamily
                font.pixelSize: Theme.fontSizeSm
                wrapMode: Text.Wrap
            }

            // ── Giọng mặc định ─────────────────────────────────────────────
            RowLayout {
                Layout.fillWidth: true
                spacing: Theme.spacingSm

                Label {
                    text: qsTr("Giọng mặc định")
                    color: Theme.text
                    font.family: Theme.fontFamily
                    font.pixelSize: Theme.fontSizeBase
                }

                ComboBox {
                    id: defaultVoiceCombo

                    objectName: "defaultVoiceCombo"
                    Layout.fillWidth: true
                    // Flat model with group headers (id "") like TextTab;
                    // header selections keep the current default (guarded).
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
                }
            }

            // ── Thư mục xuất ───────────────────────────────────────────────
            Label {
                text: qsTr("Thư mục xuất âm thanh")
                color: Theme.text
                font.family: Theme.fontFamily
                font.pixelSize: Theme.fontSizeBase
            }

            RowLayout {
                Layout.fillWidth: true
                spacing: Theme.spacingSm

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
                    font.pixelSize: Theme.fontSizeBase
                }

                Button {
                    objectName: "outputDirBrowseButton"
                    text: qsTr("Chọn thư mục…")
                    onClicked: outputDirDialog.open()
                }
            }

            // ── Temperature ────────────────────────────────────────────────
            RowLayout {
                Layout.fillWidth: true
                spacing: Theme.spacingSm

                Label {
                    text: qsTr("Temperature")
                    color: Theme.text
                    font.family: Theme.fontFamily
                    font.pixelSize: Theme.fontSizeBase
                }

                SpinBox {
                    id: temperatureSpin

                    objectName: "temperatureSpin"
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

            // ── Giao diện ──────────────────────────────────────────────────
            RowLayout {
                Layout.fillWidth: true
                spacing: Theme.spacingSm

                Label {
                    text: qsTr("Giao diện")
                    color: Theme.text
                    font.family: Theme.fontFamily
                    font.pixelSize: Theme.fontSizeBase
                }

                ComboBox {
                    id: themeCombo

                    objectName: "themeCombo"
                    Layout.fillWidth: true
                    textRole: "label"
                    function openPopup() { popup.open(); }
                    function closePopup() { popup.close(); }
                    model: root.themeOptions
                    // Preference comes from the bridge (the live-switch +
                    // persistence owner); the controller mirrors the same
                    // settings.json field for its own seam.
                    currentIndex: root.valueIndex(root.themeOptions, bridge ? bridge.themePreference : "system")
                    onActivated: function (index) {
                        if (bridge)
                            bridge.themePreference = root.themeOptions[index].value;
                        controller.theme = root.themeOptions[index].value;
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

            Label {
                objectName: "errorLabel"
                Layout.fillWidth: true
                visible: controller.errorText !== ""
                text: controller.errorText
                color: Theme.error
                font.family: Theme.fontFamily
                font.pixelSize: Theme.fontSizeBase
                wrapMode: Text.Wrap
            }

            Item {
                Layout.fillHeight: true
            }
        }
    }
}
