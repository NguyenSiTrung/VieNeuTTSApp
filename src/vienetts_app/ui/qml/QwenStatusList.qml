// Qwen dependency checklist: one row per pack piece (runtime, torch,
// CustomVoice checkpoint, Base checkpoint) with ✓/✗ plus the fix hint.
// Shared by the Settings card and the setup wizard so both surfaces agree.
// Reads controller.qwenReadiness — re-evaluates on qwenReadinessChanged, so
// the wizard's re-check button visibly refreshes these rows.
//
// Tested contract (tests/smoke/test_ui_tabs.py settings_qwen_wizard):
// qwenStatusRuntime, qwenStatusTorch, qwenStatusCustomVoice, qwenStatusBase.
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "."

ColumnLayout {
    id: root

    // The wizard hides Base when the walk skipped cloning — no red marks on
    // pieces the user never asked for. The Settings card keeps all rows.
    property bool showBase: true

    spacing: 2

    readonly property var statusRows: {
        const r = controller ? controller.qwenReadiness : null;
        const models = (r && r.models) || {};
        const device = (r && r.device) || "cpu";
        return [
            {
                objectName: "qwenStatusRuntime",
                ok: !!(r && r.runtime),
                label: qsTr("runtime qwen-tts"),
                hint: qsTr("chạy lệnh pip bên dưới")
            },
            {
                objectName: "qwenStatusTorch",
                ok: !!(r && r.torch),
                label: "torch (" + device + ")",
                hint: qsTr("đi kèm gói qwen")
            },
            {
                objectName: "qwenStatusCustomVoice",
                ok: !!models.customvoice,
                label: qsTr("Checkpoint CustomVoice"),
                hint: qsTr("bấm Tải / lệnh fetch")
            },
            {
                objectName: "qwenStatusBase",
                ok: !!models.base,
                label: qsTr("Checkpoint Base"),
                hint: qsTr("bấm Tải / lệnh fetch")
            }
        ];
    }

    function statusText(row) {
        return (row.ok ? "✓ " : "✗ ") + row.label + (row.ok ? "" : " — " + row.hint);
    }

    function statusColor(ok) {
        return ok ? Theme.successText : Theme.errorText;
    }

    // Four explicit rows (not a Repeater): Repeater delegates have no QObject
    // parent, so the smoke driver could never find them by objectName.
    Label {
        Layout.fillWidth: true
        objectName: "qwenStatusRuntime"
        text: statusText(root.statusRows[0])
        color: statusColor(root.statusRows[0].ok)
        font.family: Theme.fontFamily
        font.pixelSize: Theme.fontSizeSm
        wrapMode: Text.Wrap
        lineHeight: 1.3
    }

    Label {
        Layout.fillWidth: true
        objectName: "qwenStatusTorch"
        text: statusText(root.statusRows[1])
        color: statusColor(root.statusRows[1].ok)
        font.family: Theme.fontFamily
        font.pixelSize: Theme.fontSizeSm
        wrapMode: Text.Wrap
        lineHeight: 1.3
    }

    Label {
        Layout.fillWidth: true
        objectName: "qwenStatusCustomVoice"
        text: statusText(root.statusRows[2])
        color: statusColor(root.statusRows[2].ok)
        font.family: Theme.fontFamily
        font.pixelSize: Theme.fontSizeSm
        wrapMode: Text.Wrap
        lineHeight: 1.3
    }

    Label {
        Layout.fillWidth: true
        objectName: "qwenStatusBase"
        visible: root.showBase
        text: statusText(root.statusRows[3])
        color: statusColor(root.statusRows[3].ok)
        font.family: Theme.fontFamily
        font.pixelSize: Theme.fontSizeSm
        wrapMode: Text.Wrap
        lineHeight: 1.3
    }
}
