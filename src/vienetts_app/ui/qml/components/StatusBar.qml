import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import ".."
import "StatusBarLogic.js" as Logic

// Window status bar (FR-2.1, ui_shell_redesign): the app's ONE status
// surface, pinned under the sidebar and the content. It replaces the floating
// export-only pill and the sidebar "Phần cứng & Engine" card. Groups, left to
// right:
//   1. readiness: a dot coloured by state plus the model/engine state word
//   2. engine note (`engineReadout`): "backend · precision · mode". Until the
//      note is known it carries the state word itself, so the readout is
//      never "" or "…" (StatusBarLogic.readoutText)
//   3. audio output (`exportOnlyNotice`): shown only without an output
//      device, with the "Kiểm tra lại" re-probe (`audioRefreshButton`)
//   4. right: app version, then "Có bản cập nhật" (`statusUpdateButton`)
//      while an update is available; it emits updateRequested()
//
// Height: the design draws a 34 px strip, but the bar holds real buttons and
// every target must be at least Theme.controlHitTarget (44 px, AC-1). A hit
// area hanging out of a 34 px strip would overlap the bottom of the tab
// content and steal its clicks, so the strip is 44 px tall. The buttons
// still paint the compact 32 px `sm` shape centred in it.
//
// Narrow windows: groups never overflow the bar. The engine note is dropped
// first, then the version; the audio sentence elides last (its full copy
// stays in the tooltip and the accessible name).
//
// objectNames are the tested contract (tests/smoke/test_ui_shell.py).
Rectangle {
    id: root

    objectName: "statusBar"
    implicitHeight: Theme.controlHitTarget
    color: Theme.statusBarBg

    /// The user asked to see the available update (Main opens Settings ›
    /// Cập nhật).
    signal updateRequested()

    // Context properties are absent in a few harnesses; `typeof` keeps the
    // reads safe there (a bare unresolved identifier is a ReferenceError).
    readonly property var host: (typeof controller !== "undefined" && controller) ? controller : null
    readonly property var shell: (typeof bridge !== "undefined" && bridge) ? bridge : null

    readonly property bool audioMissing: host !== null && !host.audioAvailable
    readonly property bool updateAvailable: host !== null && host.updateAvailable === true
    readonly property string appVersion: host && host.appVersion ? String(host.appVersion) : ""

    // ── readiness ─────────────────────────────────────────────────────────
    readonly property bool managedProfile: EngineState.needsManagedInstall
    readonly property string modelState: host && host.modelState ? String(host.modelState) : ""
    /// ready | busy | failed | unsupported | missing | checking
    readonly property string readiness: Logic.readinessKey(
        modelState, managedProfile, EngineState.readiness)
    /// The model state text. Every branch returns a word, so the readout
    /// fallback below is never empty.
    readonly property string readinessText: {
        if (managedProfile) {
            switch (readiness) {
            case "ready":
                return qsTr("Sẵn sàng");
            case "busy":
                return qsTr("Đang chuẩn bị");
            case "failed":
                return qsTr("Cần chú ý");
            case "unsupported":
                return qsTr("Không hỗ trợ");
            case "missing":
                return qsTr("Chưa sẵn sàng");
            default:
                return qsTr("Đang kiểm tra...");
            }
        }
        switch (modelState) {
        case "ready":
            return qsTr("Sẵn sàng");
        case "downloading":
            return qsTr("Đang tải mô hình...");
        case "validating":
            return qsTr("Đang kiểm tra...");
        case "failed":
            return qsTr("Lỗi mô hình");
        case "unavailable":
            return qsTr("Chưa có mô hình");
        default:
            return qsTr("Đang kiểm tra...");
        }
    }
    readonly property color readinessColor: {
        switch (readiness) {
        case "ready":
            return Theme.success;
        case "busy":
            return Theme.accent;
        case "failed":
        case "unsupported":
            return Theme.error;
        case "missing":
            return Theme.warning;
        default:
            return Theme.textSubtle;
        }
    }

    // ── engine note ───────────────────────────────────────────────────────
    // The detector's capability view (bridge.engineNote, "…" until the
    // deferred hardware probe lands).
    readonly property string engineNote: shell ? String(shell.engineNote) : ""
    readonly property bool noteKnown: Logic.isMeaningful(engineNote)
    readonly property string readoutText: Logic.readoutText(engineNote, readinessText)

    // ── width budget (implicit widths only, so no binding loops) ──────────
    readonly property int gap: Theme.spacingLg
    readonly property int dotGap: 6
    readonly property real _avail: width - row.anchors.leftMargin - row.anchors.rightMargin
    // Dot + state word (or the readout standing in for it).
    readonly property real _headW: readinessDot.width + dotGap
        + (noteKnown ? readinessLabel.implicitWidth : engineReadout.implicitWidth)
    readonly property real _audioFixedW: audioMissing
        ? gap + audioDot.width + dotGap + dotGap + audioRefreshButton.implicitWidth : 0
    readonly property real _updateW: updateAvailable
        ? Theme.spacingXs + statusUpdateButton.implicitWidth : 0
    readonly property real audioLabelWidth: Math.max(48, Math.min(audioLabel.implicitWidth,
        _avail - _headW - _audioFixedW - _updateW))
    readonly property real _restW: _avail - _headW - _updateW
        - (audioMissing ? _audioFixedW + audioLabelWidth : 0)
    /// The resolved note is the first thing dropped when space runs out.
    readonly property bool noteShown: !noteKnown || _restW - gap >= 96
    readonly property real noteWidth: noteKnown
        ? Math.min(engineReadout.implicitWidth, Math.max(0, _restW - gap))
        : engineReadout.implicitWidth
    readonly property bool versionShown: appVersion !== ""
        && _restW - (noteKnown && noteShown ? gap + noteWidth : 0)
            >= gap + versionLabel.implicitWidth + Theme.spacingSm

    // Top hairline (the strip sits on bg, under sidebar + content).
    Rectangle {
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.top: parent.top
        height: 1
        color: Theme.borderSubtle
    }

    RowLayout {
        id: row

        anchors.fill: parent
        anchors.leftMargin: Theme.spacingLg
        anchors.rightMargin: Theme.spacingSm
        spacing: 0

        // 1. readiness dot + state word
        Rectangle {
            id: readinessDot

            objectName: "statusReadinessDot"
            Layout.alignment: Qt.AlignVCenter
            width: 8
            height: 8
            radius: 4
            color: root.readinessColor
        }

        Label {
            id: readinessLabel

            objectName: "statusModelState"
            // While the note is unknown, engineReadout shows this same word
            // right beside the dot, so this label steps aside.
            visible: root.noteKnown
            Layout.leftMargin: root.dotGap
            text: root.readinessText
            color: Theme.text
            font.family: Theme.fontFamily
            font.pixelSize: Theme.fontSizeXs
            font.weight: Theme.fontWeightMedium
        }

        // 2. engine note (falls back to the state word)
        Label {
            id: engineReadout

            objectName: "engineReadout"
            visible: root.noteShown
            Layout.leftMargin: root.noteKnown ? root.gap : root.dotGap
            Layout.preferredWidth: root.noteWidth
            Layout.minimumWidth: 0
            text: root.readoutText
            color: root.noteKnown ? Theme.textMuted : Theme.text
            font.family: Theme.fontFamily
            font.pixelSize: Theme.fontSizeXs
            font.weight: root.noteKnown ? Theme.fontWeightNormal : Theme.fontWeightMedium
            elide: Text.ElideRight
            Accessible.name: root.noteKnown
                ? root.readinessText + " · " + root.readoutText : root.readoutText
        }

        // 3. audio output: only when there is no output device (FR-4.6a)
        RowLayout {
            id: exportOnlyNotice

            objectName: "exportOnlyNotice"
            visible: root.audioMissing
            Layout.leftMargin: root.gap
            spacing: 0
            Accessible.role: Accessible.AlertMessage
            Accessible.name: qsTr("Không phát hiện thiết bị âm thanh — chế độ chỉ xuất tệp (export-only).")

            Rectangle {
                id: audioDot

                Layout.alignment: Qt.AlignVCenter
                width: 8
                height: 8
                radius: 4
                color: Theme.warning
            }

            Label {
                id: audioLabel

                objectName: "exportOnlyText"
                Layout.leftMargin: root.dotGap
                Layout.preferredWidth: root.audioLabelWidth
                text: qsTr("Không có thiết bị âm thanh — chỉ xuất tệp")
                color: Theme.warningText
                font.family: Theme.fontFamily
                font.pixelSize: Theme.fontSizeXs
                font.weight: Theme.fontWeightMedium
                elide: Text.ElideRight

                HoverHandler {
                    id: audioHover
                }
                ToolTip.visible: audioHover.hovered
                ToolTip.delay: 500
                ToolTip.text: qsTr("Không phát hiện thiết bị âm thanh — chế độ chỉ xuất tệp (export-only).")
            }

            AppButton {
                id: audioRefreshButton

                objectName: "audioRefreshButton"
                Layout.leftMargin: root.dotGap
                variant: "quiet"
                size: "sm"
                text: qsTr("Kiểm tra lại")
                accessibleLabel: qsTr("Kiểm tra lại thiết bị âm thanh")
                tooltipText: qsTr("Kiểm tra lại thiết bị âm thanh")
                onClicked: if (root.host) root.host.refreshAudioAvailability()
            }
        }

        Item {
            Layout.fillWidth: true
        }

        // 4. version + update indicator, right-aligned
        Label {
            id: versionLabel

            objectName: "statusAppVersion"
            visible: root.versionShown
            Layout.rightMargin: root.updateAvailable ? 0 : Theme.spacingSm
            text: "v" + root.appVersion
            color: Theme.textMuted
            font.family: Theme.fontFamily
            font.pixelSize: Theme.fontSizeXs
        }

        AppButton {
            id: statusUpdateButton

            objectName: "statusUpdateButton"
            visible: root.updateAvailable
            Layout.leftMargin: Theme.spacingXs
            variant: "quiet"
            size: "sm"
            iconKind: "download"
            text: qsTr("Có bản cập nhật")
            accessibleLabel: qsTr("Có bản cập nhật mới — mở Cài đặt › Cập nhật")
            tooltipText: qsTr("Có bản cập nhật mới — mở Cài đặt › Cập nhật")
            onClicked: root.updateRequested()
        }
    }
}
