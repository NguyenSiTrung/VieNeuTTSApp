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

    // One readiness word for the badge, derived from BOTH axes: a profile is
    // usable only when its model AND its runtime are ready.
    readonly property string readiness: {
        if (!controller)
            return "checking";
        if (controller.profileReady)
            return "ready";
        const model = controller.profileModelState;
        const runtime = controller.profileRuntimeState;
        if (model === "failed" || runtime === "failed")
            return "failed";
        if (runtime === "unsupported")
            return "unsupported";
        if (model === "downloading" || model === "verifying" || runtime === "downloading"
                || runtime === "verifying")
            return "busy";
        return "missing";
    }
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

    // What the choice will run on. "checking" is the honest pre-inspection
    // state — never a guess (the model host re-resolves at load).
    function deviceName(device) {
        switch (device) {
        case "cpu":
            return "CPU";
        case "cuda":
            return "CUDA";
        case "mps":
            return "MPS";
        case "":
        case "checking":
            return qsTr("đang kiểm tra…");
        default:
            return device;
        }
    }

    readonly property string deviceLabel: {
        if (!controller)
            return "";
        return qsTr("Thiết bị: %1").arg(deviceName(controller.engineDevice));
    }

    readonly property string statusText: {
        if (!controller)
            return "";
        switch (readiness) {
        case "ready":
            return qsTr("Mô hình và runtime đã sẵn sàng cho engine này.");
        case "busy":
            return qsTr("Đang chuẩn bị mô hình/runtime cho engine này…");
        case "failed":
            return controller.profileModelError !== ""
                ? controller.profileModelError
                : qsTr("Không thể chuẩn bị engine này. Mở Cài đặt để sửa hoặc cài lại.");
        case "unsupported":
            return qsTr("Máy này không có runtime cho engine đã chọn.");
        default:
            return qsTr("Cần cài mô hình và runtime trong Cài đặt trước khi dùng engine này.");
        }
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