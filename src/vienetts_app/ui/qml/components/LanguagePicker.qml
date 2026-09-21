import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import ".."

// Shared synthesis-language control (Phase 6 Task 6.1).
//
// Language names stay in their NATIVE form (中文, 日本語, Tiếng Việt…) — the
// standard practice for language pickers, and the only form a speaker of that
// language can read. The list is profile-scoped: it is exactly what the active
// engine accepts, so a code that would be refused can never be offered.
//
// Binds to the controller seam:
//   profileLanguages {code,label,isAuto} — the ACTIVE profile's languages
//   synthesisLanguage — the resolved code a submission would use
//   setSynthesisLanguage(code) — refused (with the capability reason) when the
//   profile cannot serve the code; "" resets to the profile default
//
// Whether a language control exists at all is capability truth, not a host
// decision: EngineState.languageTakesParameter is false for an engine that
// takes no language argument, and the note says so.
//
// objectNames are the tested contract (tests/smoke/test_ui_tabs.py):
// languagePicker, languagePickerCombo, languagePickerNote.
ColumnLayout {
    id: root

    objectName: "languagePicker"
    spacing: Theme.spacingSm

    property string label: ""
    property string description: ""
    property bool compact: false

    readonly property var languages: controller ? controller.profileLanguages : []
    readonly property string activeCode: controller ? controller.synthesisLanguage : ""
    // A profile whose engine takes no language argument (VieNeu's SDK) gets no
    // control: the combo would imply the choice changes the audio while the
    // engine ignores it. Its declared languages stay visible in Settings, and
    // the control appears once a language is actually in effect.
    readonly property bool takesLanguage: EngineState.languageTakesParameter

    // "auto" is the profile's own detection, not a language the user picked;
    // the note says so instead of implying the engine needs a language.
    readonly property bool autoSelected: {
        for (let i = 0; i < languages.length; i++)
            if (languages[i].code === activeCode)
                return languages[i].isAuto === true;
        return false;
    }
    readonly property string profileLabel: controller ? controller.engineProfileLabel : ""

    readonly property string note: {
        if (!takesLanguage)
            return qsTr("Engine này không nhận tham số ngôn ngữ.");
        if (autoSelected)
            return qsTr("%1 sẽ tự nhận diện ngôn ngữ của văn bản.").arg(profileLabel);
        return qsTr("Mọi yêu cầu tổng hợp sẽ dùng ngôn ngữ này cho %1.").arg(profileLabel);
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

    AppCombo {
        id: languagePickerCombo

        objectName: "languagePickerCombo"
        Layout.fillWidth: root.compact
        Layout.preferredWidth: root.compact ? 0 : 280
        comboWidth: 280
        accessibleLabel: qsTr("Ngôn ngữ tổng hợp")
        textRole: "label"
        model: root.languages
        visible: root.takesLanguage
        currentIndex: root.languageIndex(root.languages, root.activeCode)
        onActivated: function (index) {
            controller.setSynthesisLanguage(root.languages[index].code);
        }
    }

    Label {
        id: languagePickerNote

        objectName: "languagePickerNote"
        Layout.fillWidth: true
        text: root.note
        color: Theme.textMuted
        font.family: Theme.fontFamily
        font.pixelSize: Theme.fontSizeXs
        wrapMode: Text.Wrap
        lineHeight: 1.25
    }

    function languageIndex(options, value) {
        for (let i = 0; i < options.length; i++)
            if (options[i].code === value)
                return i;
        return 0;
    }
}