import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import ".."

// Shared engine-profile control (Phase 6 Task 6.1).
//
// Picks which engine serves synthesis (VieNeu / Qwen CustomVoice / Qwen Base)
// and reads back what that choice will actually run on. It is a CONTROL, not a
// card: Settings embeds it in the "Model family" card and the synthesis
// surfaces reuse the same instance shape, so profile switching has exactly one
// implementation and one readout.
//
// Everything binds to the controller seam — the widget keeps no local truth:
//   engineProfiles {id,label,isActive,devices,voiceCount,…} / engineProfile
//   switchEngineProfile(id) — refused while a job runs or is queued
//   profileModelState / profileRuntimeState / profileReady / profileModelError
//   engineDevice — the resolved compute device for the active profile
//
// The readiness word, its sentences and the device readout come from the
// EngineState singleton (Task 6.2): the synthesis surfaces gate their primary
// actions on the same derivation, so the badge and a disabled Generate button
// can never disagree.
//
// objectNames are the tested contract (tests/smoke/test_ui_tabs.py):
// engineProfilePicker, engineProfileCombo, engineProfileStatusLabel,
// engineProfileDeviceLabel, engineProfileReadinessBadge,
// engineProfileReadinessText.
ColumnLayout {
    id: root

    objectName: "engineProfilePicker"
    spacing: Theme.spacingSm

    // Host-owned copy: the Settings card supplies the row label/description,
    // and an empty label means "no header row" (embedded use).
    property string label: ""
    property string description: ""
    // Compact hosts stretch the combo full-width instead of a fixed column.
    property bool compact: false

    readonly property var profiles: controller ? controller.engineProfiles : []
    readonly property string activeId: controller ? controller.engineProfile : ""
    readonly property bool busy: controller ? controller.busy : false

    readonly property string readiness: EngineState.readiness
    readonly property string readinessText: {
        switch (readiness) {
        case "ready":
            return qsTr("Sẵn sàng");
        case "busy":
            return qsTr("Đang chuẩn bị");
        case "failed":
            return qsTr("Cần chú ý");
        case "unsupported":
            return qsTr("Không hỗ trợ");
        default:
            return qsTr("Chưa sẵn sàng");
        }
    }
    readonly property color readinessColor: {
        if (readiness === "ready")
            return Theme.successSubtle;
        if (readiness === "failed" || readiness === "unsupported")
            return Theme.errorSubtle;
        if (readiness === "busy")
            return Theme.accentSubtle;
        return Theme.warningSubtle;
    }
    readonly property color readinessTextColor: {
        if (readiness === "ready")
            return Theme.successText;
        if (readiness === "failed" || readiness === "unsupported")
            return Theme.errorText;
        if (readiness === "busy")
            return Theme.accent;
        return Theme.warningText;
    }

    readonly property string deviceLabel: EngineState.deviceLabel
    readonly property string statusText: EngineState.statusText

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
        id: engineProfileCombo

        objectName: "engineProfileCombo"
        Layout.fillWidth: root.compact
        Layout.preferredWidth: root.compact ? 0 : 280
        Layout.alignment: root.compact ? Qt.AlignLeft : Qt.AlignLeft
        comboWidth: 280
        accessibleLabel: qsTr("Engine suy luận")
        textRole: "label"
        model: root.profiles
        currentIndex: root.profileIndex(root.profiles, root.activeId)
        // One engine at a time: switching mid-job is refused by the
        // controller, so the control must not offer it.
        enabled: !root.busy
        onActivated: function (index) {
            controller.switchEngineProfile(root.profiles[index].id);
        }
    }

    RowLayout {
        Layout.fillWidth: true
        spacing: Theme.spacingSm

        Rectangle {
            id: engineProfileReadinessBadge

            objectName: "engineProfileReadinessBadge"
            implicitWidth: badgeLabel.implicitWidth + Theme.spacingSm * 2
            implicitHeight: badgeLabel.implicitHeight + Theme.spacingXxs * 2
            radius: Theme.radiusPill
            color: root.readinessColor

            Label {
                id: badgeLabel
                objectName: "engineProfileReadinessText"
                anchors.centerIn: parent
                text: root.readinessText
                color: root.readinessTextColor
                font.family: Theme.fontFamily
                font.pixelSize: Theme.fontSizeXs
                font.weight: Theme.fontWeightMedium
            }
        }

        Label {
            id: engineProfileDeviceLabel

            objectName: "engineProfileDeviceLabel"
            Layout.fillWidth: true
            text: root.deviceLabel
            color: Theme.textMuted
            font.family: Theme.fontFamily
            font.pixelSize: Theme.fontSizeXs
            elide: Text.ElideRight
        }
    }

    Label {
        id: engineProfileStatusLabel

        objectName: "engineProfileStatusLabel"
        Layout.fillWidth: true
        text: root.statusText
        color: root.readiness === "failed" ? Theme.errorText : Theme.textMuted
        font.family: Theme.fontFamily
        font.pixelSize: Theme.fontSizeSm
        wrapMode: Text.Wrap
        lineHeight: 1.25
    }

    // Index helper — QML cannot call into Python constants, and the active id
    // may be missing from a stale list during a profile switch.
    function profileIndex(options, value) {
        for (let i = 0; i < options.length; i++)
            if (options[i].id === value)
                return i;
        return 0;
    }
}