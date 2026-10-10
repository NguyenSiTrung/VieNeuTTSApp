import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import ".."

// One Hiệu ứng slider: label + mono readout over a full-width track.
// `value` is bound by the host (to studioPendingControls). The slider only
// reports a user drag through `moved`, never by writing the host's state, so
// a programmatic update (apply, Bỏ, undo) cannot echo back as a new edit.
// `pending` tints the readout: the value on screen is staged, not applied yet.
ColumnLayout {
    id: row

    property string label: ""
    property string readout: ""
    property bool pending: false
    property string sliderName: ""
    property string readoutName: ""
    property real from: 0
    property real to: 1
    property real stepSize: 0
    property real value: 0
    property bool controlEnabled: true
    readonly property alias slider: control

    signal moved(real value)

    spacing: 0

    RowLayout {
        Layout.fillWidth: true
        spacing: Theme.spacingSm

        Label {
            Layout.fillWidth: true
            text: row.label
            color: row.controlEnabled ? Theme.text : Theme.controlDisabledText
            font.family: Theme.fontFamily
            font.pixelSize: Theme.fontSizeSm
            elide: Text.ElideRight
        }

        Label {
            objectName: row.readoutName
            text: row.readout
            color: row.pending ? Theme.accent : Theme.textMuted
            font.family: Theme.fontFamilyMono !== "" ? Theme.fontFamilyMono : Theme.fontFamily
            font.pixelSize: Theme.fontSizeSm
            font.weight: row.pending ? Theme.fontWeightMedium : Font.Normal
        }
    }

    AppSlider {
        id: control

        objectName: row.sliderName
        Layout.fillWidth: true
        Layout.minimumWidth: control.minimumTrackWidth
        from: row.from
        to: row.to
        stepSize: row.stepSize
        snapMode: Slider.SnapAlways
        value: row.value
        enabled: row.controlEnabled
        accessibleLabel: row.label
        onMoved: row.moved(control.value)
    }
}
