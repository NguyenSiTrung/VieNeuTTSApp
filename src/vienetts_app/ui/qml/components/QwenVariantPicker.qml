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
// GGUF is the DEFAULT format for the Qwen family (the settings default, so a
// fresh install lands on it): the managed native pack plus one 0.6–1.0 GB
// talker is a fraction of the full checkpoint's runtime+weights footprint, so
// the chip carries the recommendation marker and the official chip explains
// its own cost instead of leaving the trade-off implicit.
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
// qwenQuantizationChip_<quant>, qwenOfficialNotice,
// qwenOfficialNoticeMessage, qwenEngineReadout.
ColumnLayout {
    id: root

    objectName: root.named("VariantPicker")
    spacing: Theme.spacingMd
    visible: controller ? controller.engineProfileIsQwen : false

    // Host-owned copy: the Settings card supplies the row label/description,
    // and an empty label means "no header row" (embedded use).
    property string label: ""
    property string description: ""
    property string objectNamePrefix: "qwen"

    function named(suffix) { return objectNamePrefix + suffix; }

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
            //: The default/recommended Qwen weight format.
            return qsTr("GGUF (khuyến nghị)");
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

    ColumnLayout {
        Layout.fillWidth: true
        spacing: Theme.spacingXs

        Label {
            text: qsTr("Định dạng mô hình")
            color: Theme.textMuted
            font.family: Theme.fontFamily
            font.pixelSize: Theme.fontSizeXs
            font.weight: Theme.fontWeightMedium
        }

        Flow {
            Layout.fillWidth: true
            spacing: Theme.spacingSm

            Repeater {
                model: root.formatRows

                AppButton {
                    required property var modelData

                    objectName: root.named("FormatChip_") + modelData
                    variant: modelData === root.modelFormat ? "primary" : "chip"
                    size: "sm"
                    text: root.formatLabel(modelData)
                    enabled: !root.busy
                    accessibleLabel: qsTr("Định dạng mô hình %1").arg(root.formatLabel(modelData))
                    onClicked: controller.setQwenVariant(modelData, "")
                }
            }
        }
    }

    ColumnLayout {
        id: qwenQuantizationRow

        objectName: root.named("QuantizationRow")
        Layout.fillWidth: true
        spacing: Theme.spacingXs
        // GGUF-only: official full weights carry no quantization dimension,
        // so the row does not exist for them at all.
        visible: root.modelFormat === "gguf"

        Label {
            text: qsTr("Lượng tử hóa")
            color: Theme.textMuted
            font.family: Theme.fontFamily
            font.pixelSize: Theme.fontSizeXs
            font.weight: Theme.fontWeightMedium
        }

        Flow {
            Layout.fillWidth: true
            spacing: Theme.spacingSm

            Repeater {
                model: root.quantizationRows

                AppButton {
                    required property var modelData

                    objectName: root.named("QuantizationChip_") + modelData
                    variant: modelData === root.quantization ? "primary" : "chip"
                    size: "sm"
                    text: modelData
                    enabled: !root.busy
                    accessibleLabel: qsTr("Lượng tử hóa %1").arg(modelData)
                    onClicked: controller.setQwenVariant("gguf", modelData)
                }
            }
        }
    }

    // The official format's cost, stated where the choice is made: full
    // PyTorch weights are the heavy path (multi-GB runtime + ~2.5 GB per
    // profile, slower per audio second), so a user who lands on them — the
    // chip is one click from the default — reads what they are buying rather
    // than discovering it at the first download. Hidden under GGUF, the
    // default, which needs no such caveat.
    AppNotice {
        id: qwenOfficialNotice

        objectName: root.named("OfficialNotice")
        Layout.fillWidth: true
        tone: "warning"
        title: qsTr("Trọng lượng đầy đủ tốn tài nguyên hơn GGUF")
        message: qsTr("Bản PyTorch đầy đủ cần runtime Python 1,5–3 GB và khoảng 2,5 GB mô hình cho mỗi hồ sơ — nhiều RAM/VRAM, dung lượng và thời gian tải hơn, và tốc độ chậm hơn GGUF. GGUF (mặc định) cân bằng giữa tốc độ và tài nguyên; chỉ chọn bản đầy đủ khi bạn cần đúng trọng lượng gốc.")
        messageObjectName: root.named("OfficialNoticeMessage")
        visible: root.modelFormat === "official"
    }

    Label {
        id: qwenEngineReadout

        objectName: root.named("EngineReadout")
        Layout.fillWidth: true
        //: %1 is the engine name (PyTorch / qwentts.cpp) — readout only.
        text: qsTr("Engine tương thích: %1").arg(root.engineLabel)
        color: Theme.textMuted
        font.family: Theme.fontFamily
        font.pixelSize: Theme.fontSizeXs
        wrapMode: Text.Wrap
    }
}
