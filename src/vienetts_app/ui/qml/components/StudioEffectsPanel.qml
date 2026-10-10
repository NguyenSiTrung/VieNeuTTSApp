import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import ".."

// Hiệu ứng panel (FR-4.3): grouped effect controls that STAGE edits instead
// of pushing them. Each slider binds to studioPendingControls, the mix as it
// will sound once the staged edits apply, and a drag stages through
// studioStage*. The one-shot effects (peak normalize, silence trim) are
// toggles: on stages the effect, off unstages it. Staged edits are listed
// with a per-row unstage. Committing ("Áp dụng N thay đổi") and dropping
// them all ("Bỏ") live in StudioApplyBar, which the host pins under this
// panel. The Gốc / Đã chỉnh switch picks what Nghe thử plays
// (studioCompareMode).
//
// Reads stay defensive (`|| …` fallbacks) so a controller without the
// pending seam still renders the applied values.
//
// objectNames: studioOpStack (root), studioCompareSwitch, studioGainSlider,
// studioGainReadout, studioNormalizeToggle, studioSilenceToggle,
// studioSpeedSlider, studioGapSlider, studioFadeInSlider,
// studioFadeOutSlider, studioPendingList, studioPendingRow (per staged row,
// `rowKey`), studioUnstageButton.
AppCard {
    id: panel

    objectName: "studioOpStack"

    property bool rackEnabled: false

    readonly property var controls: controller.studioPendingControls || controller.studioControls || ({})
    readonly property var pendingOps: controller.studioPendingOps || []
    readonly property var pendingKeys: {
        const keys = [];
        for (let i = 0; i < panel.pendingOps.length; i++)
            keys.push(String(panel.pendingOps[i].key || ""));
        return keys;
    }

    function num(key, fallback) {
        const v = panel.controls ? panel.controls[key] : undefined;
        return typeof v === "number" ? v : fallback;
    }

    function staged(key) {
        return panel.pendingKeys.indexOf(key) !== -1;
    }

    function trimFixed(value, digits) {
        // 1.00 → "1.0", 1.25 → "1.25": one decimal minimum, no trailing noise.
        let s = Number(value).toFixed(digits);
        while (s.endsWith("0") && s.indexOf(".") !== -1 && s.length - s.indexOf(".") > 2)
            s = s.slice(0, -1);
        return s;
    }

    cardPadding: Theme.spacingLg

    // ── Header: title + A/B listen target ────────────────────────────────
    RowLayout {
        Layout.fillWidth: true
        spacing: Theme.spacingSm

        Label {
            Layout.fillWidth: true
            text: qsTr("Hiệu ứng")
            color: Theme.text
            font.family: Theme.fontFamily
            font.pixelSize: Theme.fontSizeMd
            font.weight: Theme.fontWeightHeading
            elide: Text.ElideRight
        }

        AppSegmented {
            objectName: "studioCompareSwitch"
            accessibleLabel: qsTr("Nghe so sánh")
            model: [
                { value: "base", label: qsTr("Gốc") },
                { value: "pending", label: qsTr("Đã chỉnh") }
            ]
            currentValue: controller.studioCompareMode || "pending"
            enabled: controller.hasStudioProject
            onActivated: (value) => controller.studioSetCompareMode(value)
        }
    }

    // ── Âm lượng ─────────────────────────────────────────────────────────
    ColumnLayout {
        Layout.fillWidth: true
        spacing: Theme.spacingXs
        SectionLabel {
            text: qsTr("ÂM LƯỢNG")
        }

        StudioEffectSlider {
            Layout.fillWidth: true
            label: qsTr("Khuếch đại")
            sliderName: "studioGainSlider"
            readoutName: "studioGainReadout"
            from: -20
            to: 12
            stepSize: 0.5
            value: panel.num("gain", 0)
            readout: (value > 0 ? "+" : "") + value.toFixed(1) + " dB"
            pending: panel.staged("gain")
            controlEnabled: panel.rackEnabled
            onMoved: (v) => controller.studioStageGain(v)
        }

        AppToggle {
            objectName: "studioNormalizeToggle"
            text: qsTr("Chuẩn hóa đỉnh")
            enabled: panel.rackEnabled
            checked: panel.staged("normalize")
            onClicked: {
                if (checked)
                    controller.studioStageNormalize();
                else
                    controller.studioUnstage("normalize");
                // A click writes `checked`; hand it back to the staged state.
                checked = Qt.binding(() => panel.staged("normalize"));
            }
        }

        AppToggle {
            objectName: "studioSilenceToggle"
            text: qsTr("Cắt khoảng lặng thừa")
            enabled: panel.rackEnabled
            checked: panel.staged("silence")
            onClicked: {
                if (checked)
                    controller.studioStageSilenceTrim();
                else
                    controller.studioUnstage("silence");
                checked = Qt.binding(() => panel.staged("silence"));
            }
        }
    }

    // ── Nhịp đọc ─────────────────────────────────────────────────────────
    ColumnLayout {
        Layout.fillWidth: true
        spacing: Theme.spacingXs
        SectionLabel {
            text: qsTr("NHỊP ĐỌC")
        }

        StudioEffectSlider {
            Layout.fillWidth: true
            label: qsTr("Tốc độ")
            sliderName: "studioSpeedSlider"
            readoutName: "studioSpeedReadout"
            from: 0.5
            to: 2.0
            stepSize: 0.05
            value: panel.num("speed", 1.0)
            readout: panel.trimFixed(value, 2) + "×"
            pending: panel.staged("speed")
            controlEnabled: panel.rackEnabled
            onMoved: (v) => controller.studioStageSpeed(v)
        }

        StudioEffectSlider {
            Layout.fillWidth: true
            label: qsTr("Khoảng lặng giữa đoạn")
            sliderName: "studioGapSlider"
            readoutName: "studioGapReadout"
            from: 0
            to: 2000
            stepSize: 50
            value: panel.num("gap", 500)
            readout: Math.round(value) + " ms"
            pending: panel.staged("gap")
            controlEnabled: panel.rackEnabled
            onMoved: (v) => controller.studioStageGap(Math.round(v))
        }
    }

    // ── Mờ dần ───────────────────────────────────────────────────────────
    ColumnLayout {
        Layout.fillWidth: true
        spacing: Theme.spacingXs
        SectionLabel {
            text: qsTr("MỜ DẦN")
        }

        StudioEffectSlider {
            Layout.fillWidth: true
            label: qsTr("Đầu")
            sliderName: "studioFadeInSlider"
            readoutName: "studioFadeInReadout"
            from: 0
            to: 2000
            stepSize: 50
            value: panel.num("fadeIn", 0)
            readout: Math.round(value) + " ms"
            pending: panel.staged("fade_in")
            controlEnabled: panel.rackEnabled
            onMoved: (v) => controller.studioStageFade("in", Math.round(v))
        }

        StudioEffectSlider {
            Layout.fillWidth: true
            label: qsTr("Cuối")
            sliderName: "studioFadeOutSlider"
            readoutName: "studioFadeOutReadout"
            from: 0
            to: 2000
            stepSize: 50
            value: panel.num("fadeOut", 0)
            readout: Math.round(value) + " ms"
            pending: panel.staged("fade_out")
            controlEnabled: panel.rackEnabled
            onMoved: (v) => controller.studioStageFade("out", Math.round(v))
        }
    }

    // ── Staged edits ─────────────────────────────────────────────────────
    ColumnLayout {
        objectName: "studioPendingList"
        Layout.fillWidth: true
        spacing: 0
        visible: panel.pendingOps.length > 0

        SectionLabel {
            text: qsTr("CHỜ ÁP DỤNG")
            Layout.bottomMargin: Theme.spacingXs
        }

        Repeater {
            model: panel.pendingOps

            RowLayout {
                id: pendingRow

                required property var modelData
                required property int index

                objectName: "studioPendingRow"
                readonly property string rowKey: String(modelData.key || "")

                Layout.fillWidth: true
                spacing: Theme.spacingSm

                Rectangle {
                    Layout.alignment: Qt.AlignVCenter
                    width: 6
                    height: 6
                    radius: 3
                    color: Theme.accent
                }

                Label {
                    Layout.fillWidth: true
                    text: pendingRow.modelData.desc || pendingRow.modelData.name || ""
                    color: Theme.text
                    font.family: Theme.fontFamily
                    font.pixelSize: Theme.fontSizeSm
                    elide: Text.ElideRight
                }

                AppButton {
                    objectName: "studioUnstageButton"
                    variant: "icon"
                    size: "sm"
                    iconKind: "close"
                    accessibleLabel: qsTr("Bỏ thay đổi %1").arg(pendingRow.modelData.name || "")
                    tooltipText: accessibleLabel
                    enabled: controller.studioBusy !== true
                    onClicked: controller.studioUnstage(pendingRow.rowKey)
                }
            }
        }
    }
}
