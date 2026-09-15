import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import ".."

// One FX-rack parameter: title + applied note + preset chips on the first
// line; slider + numeric field + Apply (+ optional second Apply) + pending
// hint on the second. StudioTab's gain/speed/gap/fade rows shared this exact
// shape — the differences (ranges, decimals, fade's two-edge apply) are data.
//
//   presets   : [{ text, value, tip? }] — chips write straight to the slider
//   dirty     : host-computed "differs from the mix" flag; shows the pending
//               hint and (with promoteDirty) upgrades Apply to primary
//   rowEnabled: slider/field/presets guard — project loaded && worker free
//   applyEnabled: commit guard — rowEnabled && no render in flight
ColumnLayout {
    id: prow

    property string title: ""
    property string appliedNote: ""
    property var presets: []
    property int decimals: 0

    property string sliderObjectName: ""
    property string applyObjectName: ""
    property string fieldObjectName: ""

    property alias from: slider.from
    property alias to: slider.to
    property alias stepSize: slider.stepSize
    property alias sliderValue: slider.value

    property string applyText: qsTr("Áp dụng")
    property string applyTooltip: ""
    property string secondApplyText: ""
    property string secondApplyTooltip: ""

    property bool dirty: false
    property bool busy: false
    property bool promoteDirty: true
    property bool rowEnabled: true
    property bool applyEnabled: true

    signal applied()
    signal secondApplied()

    spacing: Theme.spacingSm

    RowLayout {
        Layout.fillWidth: true
        spacing: Theme.spacingSm

        Label {
            text: prow.title
            color: Theme.text
            font.family: Theme.fontFamily
            font.pixelSize: Theme.fontSizeSm
            font.weight: Theme.fontWeightMedium
        }

        Label {
            Layout.fillWidth: true
            visible: prow.appliedNote !== ""
            text: prow.appliedNote
            color: Theme.textSubtle
            font.family: Theme.fontFamily
            font.pixelSize: Theme.fontSizeXs
            elide: Text.ElideRight
        }

        Item { Layout.fillWidth: prow.appliedNote === "" }

        Repeater {
            model: prow.presets

            AppButton {
                required property var modelData

                variant: "chip"
                size: "sm"
                text: modelData.text
                tooltipText: modelData.tip || ""
                enabled: prow.rowEnabled
                onClicked: slider.value = modelData.value
            }
        }
    }

    RowLayout {
        Layout.fillWidth: true
        spacing: Theme.spacingSm

        AppSlider {
            id: slider

            objectName: prow.sliderObjectName
            Layout.fillWidth: true
            Layout.minimumWidth: minimumTrackWidth
            enabled: prow.rowEnabled
            accessibleLabel: prow.title
        }

        // Direct numeric entry: the field clamps to the slider's range and
        // steps in the slider's own increments. Typing never fights the
        // slider — sync is skipped while the field holds focus, and the
        // _syncing guard keeps slider→field echoes from writing back.
        AppNumberField {
            id: valueField

            objectName: prow.fieldObjectName
            implicitWidth: 96
            decimals: prow.decimals
            scaleFactor: Math.pow(10, prow.decimals)
            from: Math.round(prow.from * scaleFactor)
            to: Math.round(prow.to * scaleFactor)
            stepSize: Math.max(1, Math.round(prow.stepSize * scaleFactor))
            enabled: prow.rowEnabled
            accessibleLabel: prow.title

            property bool _syncing: false
            function syncFromSlider() {
                _syncing = true;
                value = Math.round(slider.value * scaleFactor);
                _syncing = false;
            }

            Component.onCompleted: syncFromSlider()
            // valueChanged also fires on our own sync — the guard drops it.
            // Edits commit on Enter/focus-loss/arrows, never per keystroke.
            onValueChanged: if (!_syncing) slider.value = realValue

            Connections {
                target: slider
                function onValueChanged() {
                    // SpinBox forwards focus to its editor TextInput, so check
                    // both — syncing mid-edit would clobber the typed text.
                    if (!valueField.activeFocus
                            && !(valueField.contentItem && valueField.contentItem.activeFocus))
                        valueField.syncFromSlider();
                }
            }
        }

        AppButton {
            objectName: prow.applyObjectName
            variant: prow.dirty && prow.promoteDirty ? "primary" : "secondary"
            size: "md"
            text: prow.applyText
            tooltipText: prow.applyTooltip
            enabled: prow.applyEnabled
            busy: prow.busy
            onClicked: prow.applied()
        }

        AppButton {
            visible: prow.secondApplyText !== ""
            variant: "secondary"
            size: "md"
            text: prow.secondApplyText
            tooltipText: prow.secondApplyTooltip
            enabled: prow.applyEnabled
            busy: prow.busy
            onClicked: prow.secondApplied()
        }

        Label {
            visible: prow.dirty
            text: qsTr("Chưa áp dụng")
            color: Theme.warning
            font.family: Theme.fontFamily
            font.pixelSize: Theme.fontSizeXs
            font.weight: Theme.fontWeightMedium
        }
    }
}
