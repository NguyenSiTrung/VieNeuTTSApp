import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import ".."

Rectangle {
    id: root

    property string text: ""
    property string status: "neutral" // "success" | "warning" | "error" | "info" | "neutral"
    property string iconText: ""
    property bool dotVisible: true
    property int customPadding: Theme.spacingSm

    implicitHeight: 24
    implicitWidth: badgeRow.implicitWidth + customPadding * 2
    radius: Theme.radiusPill

    readonly property color statusDotColor: {
        if (status === "success") return Theme.success
        if (status === "warning") return Theme.warning
        if (status === "error") return Theme.error
        if (status === "info") return Theme.accent
        return Theme.textMuted
    }

    readonly property color statusBgColor: {
        if (status === "success") return Theme.successSubtle
        if (status === "warning") return Theme.warningSubtle
        if (status === "error") return Theme.errorSubtle
        if (status === "info") return Theme.accentSubtle
        return Theme.surfaceAlt
    }

    readonly property color statusTextColor: {
        if (status === "success") return Theme.successText
        if (status === "warning") return Theme.warningText
        if (status === "error") return Theme.errorText
        if (status === "info") return Theme.accent
        return Theme.text
    }

    color: statusBgColor
    border.color: Theme.borderSubtle
    border.width: 1

    Behavior on color { ColorAnimation { duration: 150 } }

    RowLayout {
        id: badgeRow
        anchors.centerIn: parent
        spacing: Theme.spacingXs

        Rectangle {
            width: 6
            height: 6
            radius: 3
            color: root.statusDotColor
            visible: root.dotVisible && root.iconText === ""
        }

        Label {
            text: root.iconText
            visible: root.iconText !== ""
            font.pixelSize: Theme.fontSizeXs
        }

        Label {
            text: root.text
            color: root.statusTextColor
            font.family: Theme.fontFamily
            font.pixelSize: Theme.fontSizeXs
            font.weight: Theme.fontWeightMedium
        }
    }
}
