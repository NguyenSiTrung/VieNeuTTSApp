import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import ".."

// Shared Qwen model-variant control (GGUF track, Task 5.2).
//
// After the profile picker chooses WHICH Qwen model serves synthesis, this
// control chooses its WEIGHTS: the official full checkpoint or a managed GGUF
// quantization. The three questions it answers all come from the controller's
// variant seam — the control keeps no local truth:
//
//   * which formats exist?          -> qwenVariantOptions (format field)
//   * which is armed?               -> qwenModelFormat / qwenGgufQuantization
//   * which engine will serve it?   -> qwenEngineLabel (a READOUT, never a
//     choice: each format has exactly one compatible engine, so a selector
//     would only offer illegal combinations — PyTorch+GGUF or
//     qwentts.cpp+official weights)
//
// Quantization is GGUF-only: under official weights the row does not exist
// rather than showing a choice the engine would ignore. All chips disable
// while a job runs, is queued, or an install owns the Qwen lane — the
// controller refuses the same switch, so the control must not offer it.
//
// The control self-hides under a non-Qwen profile: the model format is a
// Qwen concern, and offering it to VieNeu would imply it can change an
// in-process engine it does not govern.
//
// objectNames are the tested contract (tests/smoke/test_ui_tabs.py):
// qwenVariantPicker, qwenFormatChip_<format>, qwenQuantizationRow,
// qwenQuantizationChip_<quant>, qwenEngineReadout.
ColumnLayout {
    id: root

    objectName: "qwenVariantPicker"
    spacing: Theme.spacingSm
    visible: controller ? controller.engineProfileIsQwen : false

    // Host-owned copy: the Settings card supplies the row label/description,
    // and an empty label means "no header row" (embedded use).
    property string label: ""
    property string description: ""

    readonly property var variantOptions: controller ? controller.qwenVariantOptions : []
    readonly property string modelFormat: controller ? controller.qwenModelFormat : "official"
    readonly property string quantization: controller ? controller.qwenGgufQuantization : ""
    readonly property string engineLabel: controller ? controller.qwenEngineLabel : ""
    readonly property bool busy: controller
        ? (controller.busy || controller.qwenRuntimeBusy || controller.qwenModelBusy)
        : false

    // One chip per FORMAT, derived from the variant options — adding a format
    // to the controller's matrix adds a chip here with no QML change.
    readonly property var formatRows: {
        const seen = [];
        for (let i = 0; i < variantOptions.length; i++) {
            const fmt = variantOptions[i].format;
            if (seen.indexOf(fmt) === -1)
                seen.push(fmt);
        }
        return seen;
    }
    // One chip per GGUF quantization — same derivation, so exactly the
    // quantizations the manifest ships are offered.
    readonly property var quantizationRows: {
        const quants = [];
        for (let i = 0; i < variantOptions.length; i++) {
            const opt = variantOptions[i];
            if (opt.format === "gguf" && quants.indexOf(opt.quantization) === -1)
                quants.push(opt.quantization);
        }
        return quants;
    }

    function formatLabel(fmt) {
        if (fmt === "official")
            //: Qwen model weight format — full-size official checkpoints.
            return qsTr("Trọng lượng đầy đủ chính thức");
        if (fmt === "gguf")
            return "GGUF";
        return fmt;
    }

    RowLayout {
        Layout.fillWidth: true
        spacing: Theme.spacingSm
        visible: root.label !== ""

        ColumnLayout {
            Layout.fillWidth: true
            spacing: 2

            Label {
                Layout.fillWidth: true
                text: root.label
                color: Theme.text
                font.family: Theme.fontFamily
                font.pixelSize: Theme.fontSizeBase
                font.weight: Theme.fontWeightMedium
                wrapMode: Text.Wrap
            }

            Label {
                Layout.fillWidth: true
                text: root.description
                visible: root.description !== ""
                color: Theme.textMuted
                font.family: Theme.fontFamily
                font.pixelSize: Theme.fontSizeXs
                wrapMode: Text.Wrap
                lineHeight: 1.2
            }
        }
    }

    Flow {
        Layout.fillWidth: true
        spacing: Theme.spacingSm

        Repeater {
            model: root.formatRows

            AppButton {
                required property var modelData

                objectName: "qwenFormatChip_" + modelData
                variant: modelData === root.modelFormat ? "primary" : "chip"
                size: "sm"
                text: root.formatLabel(modelData)
                enabled: !root.busy
                accessibleLabel: qsTr("Định dạng mô hình %1").arg(root.formatLabel(modelData))
                onClicked: controller.setQwenVariant(modelData, "")
            }
        }
    }

    Flow {
        id: qwenQuantizationRow

        objectName: "qwenQuantizationRow"
        Layout.fillWidth: true
        spacing: Theme.spacingSm
        // GGUF-only: official full weights carry no quantization dimension,
        // so the row does not exist for them at all.
        visible: root.modelFormat === "gguf"

        Label {
            text: qsTr("Lượng tử hóa")
            color: Theme.textMuted
            font.family: Theme.fontFamily
            font.pixelSize: Theme.fontSizeXs
        }

        Repeater {
            model: root.quantizationRows

            AppButton {
                required property var modelData

                objectName: "qwenQuantizationChip_" + modelData
                variant: modelData === root.quantization ? "primary" : "chip"
                size: "sm"
                text: modelData
                enabled: !root.busy
                accessibleLabel: qsTr("Lượng tử hóa %1").arg(modelData)
                onClicked: controller.setQwenVariant("gguf", modelData)
            }
        }
    }

    Label {
        id: qwenEngineReadout

        objectName: "qwenEngineReadout"
        Layout.fillWidth: true
        //: %1 is the engine name (PyTorch / qwentts.cpp) — readout only.
        text: qsTr("Engine tương thích: %1").arg(root.engineLabel)
        color: Theme.textMuted
        font.family: Theme.fontFamily
        font.pixelSize: Theme.fontSizeXs
        wrapMode: Text.Wrap
    }
}
