import QtQuick
import QtQuick.Controls
import ".."

// Shared voice catalog picker — replaces three copy-pasted ComboBox blocks.
//
// Contract kept from the old inline pickers (tests/smoke/test_ui_tabs.py):
//   objectName "voicePicker"  (hosts may override, e.g. "defaultVoiceCombo")
//   property var flatModel     — the ▸/— prefixed flat rows (format is pinned)
//   property string selectedVoice
//   purpose "select"  — pick a synthesis voice (preselects controller.defaultVoice)
//   purpose "default" — edit controller.defaultVoice (live two-way index binding)
ComboBox {
    id: root

    property string purpose: "select"
    property var flatModel: {
        const groups = (typeof controller !== "undefined" && controller) ? controller.voices : [];
        const rows = [];
        for (let i = 0; i < groups.length; i++) {
            rows.push({ id: "", label: "▸ " + groups[i].label });
            const inner = groups[i].voices;
            for (let j = 0; j < inner.length; j++)
                rows.push({ id: inner[j].id, label: "— " + inner[j].label });
        }
        return rows;
    }
    property string selectedVoice: ""

    objectName: "voicePicker"
    textRole: "label"
    model: flatModel
    Layout.fillWidth: true
    implicitHeight: 38

    onCurrentIndexChanged: {
        const row = currentIndex >= 0 && currentIndex < flatModel.length ? flatModel[currentIndex] : null;
        selectedVoice = row && row.id !== "" ? row.id : "";
    }

    onActivated: function (index) {
        const row = index >= 0 && index < flatModel.length ? flatModel[index] : null;
        if (!row || row.id === "")
            return;
        if (purpose === "default")
            controller.defaultVoice = row.id;
        else
            selectedVoice = row.id;
    }

    function indexFor(id) {
        for (let i = 0; i < flatModel.length; i++)
            if (flatModel[i].id !== "" && flatModel[i].id === id)
                return i;
        return 0;
    }

    // Preselect the controller's default voice; default-mode keeps following
    // external defaultVoice writes (settings edits, persistence) via Connections
    // — a direct currentIndex binding would be circular.
    Component.onCompleted: {
        const target = (typeof controller !== "undefined" && controller) ? controller.defaultVoice : "";
        const i = indexFor(target);
        currentIndex = i;
        if (flatModel[i] && flatModel[i].id !== "")
            selectedVoice = flatModel[i].id;
    }

    Connections {
        target: (typeof controller !== "undefined" && controller) ? controller : null
        function onDefaultVoiceChanged() {
            if (root.purpose !== "default")
                return;
            const i = root.indexFor(controller.defaultVoice);
            if (root.currentIndex !== i)
                root.currentIndex = i;
        }
    }

    contentItem: Label {
        leftPadding: Theme.spacingMd
        rightPadding: Theme.spacingMd
        text: root.displayText
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
        border.color: root.activeFocus ? Theme.accent : Theme.borderSubtle
        border.width: 1
        Behavior on border.color { ColorAnimation { duration: Theme.durationFast } }
    }

    delegate: ItemDelegate {
        id: voiceRow

        required property var modelData
        required property int index

        width: root.width
        height: 36
        highlighted: root.highlightedIndex === voiceRow.index
        enabled: voiceRow.modelData ? voiceRow.modelData.id !== "" : false

        contentItem: Label {
            leftPadding: Theme.spacingMd
            rightPadding: Theme.spacingMd
            text: voiceRow.modelData ? voiceRow.modelData.label : ""
            color: voiceRow.enabled ? (voiceRow.highlighted ? Theme.accent : Theme.text) : Theme.textSubtle
            font.family: Theme.fontFamily
            font.pixelSize: Theme.fontSizeBase
            // Group headers are bold & inert; voices are regular & selectable.
            font.weight: voiceRow.enabled ? Theme.fontWeightRegular : Theme.fontWeightBold
            verticalAlignment: Text.AlignVCenter
            elide: Text.ElideRight
        }

        background: Rectangle {
            color: voiceRow.highlighted ? Theme.accentSubtle : "transparent"
        }
    }
}
