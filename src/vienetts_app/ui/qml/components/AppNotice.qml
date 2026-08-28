import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import ".."

// Shared inline feedback surface. The host owns visibility and may preserve a
// legacy message object name for existing smoke-test and accessibility seams.
Rectangle {
    id: root

    property string tone: "info" // info|success|warning|error
    property string title: ""
    property string message: ""
    property string messageObjectName: ""
    property string actionText: ""

    signal actionTriggered()

    readonly property color toneColor: {
        if (tone === "success") return Theme.success;
        if (tone === "warning") return Theme.warning;
        if (tone === "error") return Theme.error;
        return Theme.accent;
    }
    readonly property color toneBackground: {
        if (tone === "success") return Theme.successSubtle;
        if (tone === "warning") return Theme.warningSubtle;
        if (tone === "error") return Theme.errorSubtle;
        return Theme.accentSubtle;
    }
    readonly property color toneText: {
        if (tone === "success") return Theme.successText;
        if (tone === "warning") return Theme.warningText;
        if (tone === "error") return Theme.errorText;
        return Theme.text;
    }

    radius: Theme.radiusMd
    color: toneBackground
    border.color: toneColor
    border.width: 1
    implicitHeight: noticeRow.implicitHeight + Theme.spacingMd * 2

    RowLayout {
        id: noticeRow

        anchors.fill: parent
        anchors.margins: Theme.spacingMd
        spacing: Theme.spacingSm

        Rectangle {
            Layout.alignment: Qt.AlignTop
            width: 4
            height: Math.max(20, parent.height)
            radius: 2
            color: root.toneColor
        }

        ColumnLayout {
            Layout.fillWidth: true
            spacing: 2

            Label {
                Layout.fillWidth: true
                text: root.title
                visible: root.title !== ""
                color: root.toneText
                font.family: Theme.fontFamily
                font.pixelSize: Theme.fontSizeSm
                font.weight: Theme.fontWeightHeading
            }

            Label {
                id: messageLabel
                objectName: root.messageObjectName
                Layout.fillWidth: true
                visible: root.visible
                text: root.message
                color: root.toneText
                font.family: Theme.fontFamily
                font.pixelSize: Theme.fontSizeBase
                wrapMode: Text.Wrap
            }
        }

        AppButton {
            visible: root.actionText !== ""
            variant: "quiet"
            size: "sm"
            text: root.actionText
            onClicked: root.actionTriggered()
        }
    }
}
