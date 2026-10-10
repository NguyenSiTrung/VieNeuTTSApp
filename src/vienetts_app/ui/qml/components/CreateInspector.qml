import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import ".."
import "."

// Create inspector (Tạo giọng đọc, ui_shell_redesign FR-3.3 / AC-6): the
// column beside the editor (stacked under it on narrow windows — the host
// decides where it sits).
//
//   Giọng đọc     avatar · name · "gender · region · style"
//                 [Nghe thử] [Đổi giọng…]
//                 Gần đây: up to 3 recent voices, each with its own audition
//   Lần tạo này   Tốc độ · Ngắt giữa câu sliders · Phát trực tiếp
//
// ONE selection source of truth: the dock's VoicePicker (`picker`). The card
// reads the picker's effective voice and every selection made here goes back
// through `picker.selectVoice(id)`, so the dock chip and this card can never
// disagree. "Đổi giọng…" only raises `changeVoiceRequested()`: the host
// decides where voice selection happens (today the chip's catalog popup).
//
// The sliders are quick access to the SAME settings the Settings page edits
// (controller.speed / controller.silenceP, persisted per profile) — not a
// second per-run state. The live-playback toggle is the one on this screen
// (FR-2.5: it left the dock's overflow menu) and binds controller.livePreview
// strictly with write-back.
//
// objectNames (tested contract): createInspector, inspectorVoiceCard,
// inspectorVoiceAvatar, inspectorVoiceName, inspectorVoicePersona,
// inspectorAuditionButton, inspectorChangeVoiceButton, inspectorRecentVoices,
// inspectorRecentRow, inspectorRecentName, inspectorRecentPersona,
// inspectorRecentAudition, inspectorRecentEmpty, inspectorRunCard,
// inspectorSpeedSlider, inspectorSpeedValue, inspectorPauseSlider,
// inspectorPauseValue, livePreviewToggle.
ColumnLayout {
    id: root

    objectName: "createInspector"

    // The dock's VoicePicker: the selected voice lives there.
    property var picker: null
    // Live playback applies to text the dock plays as it streams (compose,
    // document); batch file renders are silent, so files mode hides it.
    property bool showLivePreview: true

    signal changeVoiceRequested()

    readonly property bool hasController: typeof controller !== "undefined" && controller !== null
    readonly property string voiceId: picker ? picker.effectiveVoice : ""
    readonly property var voiceInfo: picker ? picker.voiceInfoFor(picker.rowForId(voiceId))
        : { name: "", gender: "", region: "", style: "" }
    readonly property string unavailableReason: picker ? picker.unavailableReason : ""
    readonly property var recentRows: hasController && controller.recentVoices
        ? controller.recentVoices : []

    spacing: Theme.spacingLg

    // "Nữ · Miền Bắc · Tự nhiên": the picker's display mapping, empty fields
    // dropped (a pinned speaker or a clone carries fewer of them — never a
    // dangling separator).
    function personaLine(info) {
        if (!picker || !info)
            return "";
        const style = String(picker.styleLabel(info.style || ""));
        const parts = [
            picker.genderLabel(info.gender || ""),
            picker.regionLabel(info.region || ""),
            style !== "" ? style.charAt(0).toLocaleUpperCase() + style.substring(1) : ""
        ];
        return parts.filter(function (p) { return String(p || "").trim() !== ""; }).join(" · ");
    }

    function initials(name) {
        const clean = String(name || "").trim();
        if (clean === "")
            return "";
        const words = clean.split(/\s+/);
        if (words.length >= 2)
            return (words[0].charAt(0) + words[words.length - 1].charAt(0)).toLocaleUpperCase();
        return clean.substring(0, 2).toLocaleUpperCase();
    }

    function isAuditioning(id) {
        return hasController && id !== "" && controller.auditionVoiceId === id
            && controller.auditionState !== "idle";
    }

    function audition(id) {
        if (hasController && id !== "")
            controller.auditionVoice(id);
    }

    function selectVoice(id) {
        return picker ? picker.selectVoice(id) : false;
    }

    // ── Giọng đọc: the current voice + recents ─────────────────────────────
    AppCard {
        id: voiceCard

        objectName: "inspectorVoiceCard"
        Layout.fillWidth: true
        cardPadding: Theme.spacingLg

        ColumnLayout {
            Layout.fillWidth: true
            spacing: Theme.spacingMd

            Label {
                text: qsTr("Giọng đọc")
                color: Theme.textMuted
                font.family: Theme.fontFamily
                font.pixelSize: Theme.fontSizeSm
                font.weight: Theme.fontWeightHeading
            }

            RowLayout {
                Layout.fillWidth: true
                spacing: Theme.spacingMd

                Rectangle {
                    objectName: "inspectorVoiceAvatar"
                    Layout.preferredWidth: 48
                    Layout.preferredHeight: 48
                    radius: 24
                    color: Theme.accentSubtle
                    visible: avatarText.text !== ""

                    Label {
                        id: avatarText

                        anchors.centerIn: parent
                        text: root.initials(root.voiceInfo.name)
                        color: Theme.accent
                        font.family: Theme.fontFamily
                        font.pixelSize: Theme.fontSizeMd
                        font.weight: Theme.fontWeightBold
                    }
                }

                ColumnLayout {
                    Layout.fillWidth: true
                    Layout.minimumWidth: 0
                    spacing: Theme.spacingXxs

                    Label {
                        objectName: "inspectorVoiceName"
                        Layout.fillWidth: true
                        text: root.unavailableReason !== "" ? qsTr("Chưa có giọng đọc")
                            : (root.voiceInfo.name || qsTr("Chưa chọn giọng"))
                        color: Theme.text
                        font.family: Theme.fontFamily
                        font.pixelSize: Theme.fontSizeLg
                        font.weight: Theme.fontWeightBold
                        elide: Text.ElideRight
                    }

                    Label {
                        objectName: "inspectorVoicePersona"
                        Layout.fillWidth: true
                        text: root.unavailableReason !== "" ? root.unavailableReason
                            : root.personaLine(root.voiceInfo)
                        visible: text !== ""
                        color: Theme.textMuted
                        font.family: Theme.fontFamily
                        font.pixelSize: Theme.fontSizeSm
                        wrapMode: Text.Wrap
                    }
                }
            }

            RowLayout {
                Layout.fillWidth: true
                spacing: Theme.spacingSm

                AppButton {
                    objectName: "inspectorAuditionButton"
                    Layout.fillWidth: true
                    variant: "secondary"
                    iconKind: root.isAuditioning(root.voiceId) ? "stop" : "play"
                    text: root.isAuditioning(root.voiceId) ? qsTr("Dừng") : qsTr("Nghe thử")
                    accessibleLabel: root.isAuditioning(root.voiceId)
                        ? qsTr("Dừng nghe thử") : qsTr("Nghe thử %1").arg(root.voiceInfo.name)
                    busy: root.hasController && controller.auditionVoiceId === root.voiceId
                        && controller.auditionState === "loading"
                    enabled: root.voiceId !== "" && root.unavailableReason === ""
                        && root.hasController && !controller.busy
                    disabledReason: root.unavailableReason !== "" ? root.unavailableReason
                        : qsTr("Không thể nghe thử khi đang tạo âm thanh.")
                    onClicked: root.audition(root.voiceId)
                }

                // Task 3.5 may route this to the Giọng đọc destination; the
                // host owns the decision through changeVoiceRequested().
                AppButton {
                    objectName: "inspectorChangeVoiceButton"
                    Layout.fillWidth: true
                    variant: "ghost"
                    text: qsTr("Đổi giọng…")
                    enabled: root.picker !== null && root.picker.enabled
                    disabledReason: root.unavailableReason
                    onClicked: root.changeVoiceRequested()
                }
            }

            Rectangle {
                Layout.fillWidth: true
                Layout.preferredHeight: 1
                color: Theme.borderSubtle
            }

            ColumnLayout {
                objectName: "inspectorRecentVoices"
                Layout.fillWidth: true
                spacing: Theme.spacingXxs

                Label {
                    text: qsTr("Gần đây")
                    color: Theme.textSubtle
                    font.family: Theme.fontFamily
                    font.pixelSize: Theme.fontSizeXs
                    font.weight: Theme.fontWeightMedium
                }

                Label {
                    objectName: "inspectorRecentEmpty"
                    Layout.fillWidth: true
                    visible: root.recentRows.length === 0
                    text: qsTr("Chưa có giọng nào gần đây.")
                    color: Theme.textMuted
                    font.family: Theme.fontFamily
                    font.pixelSize: Theme.fontSizeSm
                    wrapMode: Text.Wrap
                }

                Repeater {
                    model: root.recentRows

                    // A whole-row selection target; the audition button on the
                    // right takes its own clicks.
                    AbstractButton {
                        id: recentRow

                        required property var modelData
                        required property int index
                        readonly property string voiceId: modelData.id || ""
                        readonly property bool current: voiceId === root.voiceId

                        objectName: "inspectorRecentRow"
                        Layout.fillWidth: true
                        implicitHeight: Theme.controlHitTarget
                        hoverEnabled: true
                        enabled: root.hasController && !controller.busy
                        Accessible.name: qsTr("Chọn giọng %1").arg(nameLabel.text)
                        onClicked: root.selectVoice(voiceId)

                        background: Rectangle {
                            radius: Theme.radiusMd
                            color: recentRow.current ? Theme.accentSubtle
                                : (recentRow.hovered ? Theme.surfaceHover : "transparent")
                            border.width: recentRow.activeFocus ? Theme.focusRingWidth : 0
                            border.color: Theme.accent
                        }

                        contentItem: RowLayout {
                            spacing: Theme.spacingSm

                            Rectangle {
                                Layout.leftMargin: Theme.spacingXs
                                Layout.preferredWidth: 30
                                Layout.preferredHeight: 30
                                radius: 15
                                color: Theme.surfaceAlt

                                Label {
                                    anchors.centerIn: parent
                                    text: root.initials(nameLabel.text)
                                    color: Theme.textMuted
                                    font.family: Theme.fontFamily
                                    font.pixelSize: Theme.fontSizeXs
                                    font.weight: Theme.fontWeightHeading
                                }
                            }

                            ColumnLayout {
                                Layout.fillWidth: true
                                Layout.minimumWidth: 0
                                spacing: 0

                                Label {
                                    id: nameLabel

                                    objectName: "inspectorRecentName"
                                    Layout.fillWidth: true
                                    text: recentRow.modelData.name || recentRow.voiceId
                                    color: Theme.text
                                    font.family: Theme.fontFamily
                                    font.pixelSize: Theme.fontSizeBase
                                    font.weight: Theme.fontWeightMedium
                                    elide: Text.ElideRight
                                }

                                Label {
                                    objectName: "inspectorRecentPersona"
                                    Layout.fillWidth: true
                                    text: root.personaLine(recentRow.modelData)
                                    visible: text !== ""
                                    color: Theme.textSubtle
                                    font.family: Theme.fontFamily
                                    font.pixelSize: Theme.fontSizeXs
                                    elide: Text.ElideRight
                                }
                            }

                            AppIconButton {
                                objectName: "inspectorRecentAudition"
                                size: "sm"
                                iconKind: root.isAuditioning(recentRow.voiceId) ? "stop" : "play"
                                accessibleLabel: root.isAuditioning(recentRow.voiceId)
                                    ? qsTr("Dừng nghe thử")
                                    : qsTr("Nghe thử %1").arg(nameLabel.text)
                                tooltipText: accessibleLabel
                                busy: root.hasController
                                    && controller.auditionVoiceId === recentRow.voiceId
                                    && controller.auditionState === "loading"
                                enabled: root.hasController && !controller.busy
                                onClicked: root.audition(recentRow.voiceId)
                            }
                        }
                    }
                }
            }
        }
    }

    // ── Lần tạo này: quick access to the generation settings ──────────────
    // One row per setting (label · slider · value) so the whole column fits
    // beside the editor at the default window height.
    AppCard {
        objectName: "inspectorRunCard"
        Layout.fillWidth: true
        cardPadding: Theme.spacingLg

        ColumnLayout {
            Layout.fillWidth: true
            spacing: Theme.spacingXs

            Label {
                Layout.bottomMargin: Theme.spacingXs
                text: qsTr("Lần tạo này")
                color: Theme.textMuted
                font.family: Theme.fontFamily
                font.pixelSize: Theme.fontSizeSm
                font.weight: Theme.fontWeightHeading
            }

            GridLayout {
                Layout.fillWidth: true
                columns: 3
                columnSpacing: Theme.spacingSm
                rowSpacing: 0

                Label {
                    text: qsTr("Tốc độ")
                    color: Theme.text
                    font.family: Theme.fontFamily
                    font.pixelSize: Theme.fontSizeBase
                }

                // Bounds mirror Settings.speed [0.5, 2.0]; a user move writes
                // the setting and the binding follows it back (edits made on
                // the Settings page land here too).
                AppSlider {
                    id: speedSlider

                    objectName: "inspectorSpeedSlider"
                    Layout.fillWidth: true
                    Layout.minimumWidth: 80
                    from: 0.5
                    to: 2.0
                    stepSize: 0.05
                    snapMode: Slider.SnapAlways
                    value: root.hasController ? controller.speed : 1.0
                    enabled: root.hasController && EngineState.generationControl("speed")
                    accessibleLabel: qsTr("Tốc độ đọc")
                    onMoved: controller.speed = Number(value.toFixed(2))
                }

                Label {
                    objectName: "inspectorSpeedValue"
                    Layout.preferredWidth: valueMetrics.advanceWidth
                    horizontalAlignment: Text.AlignRight
                    text: "%1×".arg(Number(speedSlider.value).toFixed(2).replace(/0$/, ""))
                    color: Theme.textMuted
                    font.family: Theme.fontFamily
                    font.pixelSize: Theme.fontSizeSm
                    font.weight: Theme.fontWeightMedium
                }

                Label {
                    text: qsTr("Ngắt giữa câu")
                    color: Theme.text
                    font.family: Theme.fontFamily
                    font.pixelSize: Theme.fontSizeBase
                }

                // Bounds mirror Settings.silence_p [0.0, 2.0].
                AppSlider {
                    id: pauseSlider

                    objectName: "inspectorPauseSlider"
                    Layout.fillWidth: true
                    Layout.minimumWidth: 80
                    from: 0.0
                    to: 2.0
                    stepSize: 0.05
                    snapMode: Slider.SnapAlways
                    value: root.hasController ? controller.silenceP : 0.15
                    enabled: root.hasController && EngineState.generationControl("silence_p")
                    accessibleLabel: qsTr("Khoảng lặng ngắt câu")
                    onMoved: controller.silenceP = Number(value.toFixed(2))
                }

                Label {
                    objectName: "inspectorPauseValue"
                    Layout.preferredWidth: valueMetrics.advanceWidth
                    horizontalAlignment: Text.AlignRight
                    text: "%1 s".arg(Number(pauseSlider.value).toFixed(2))
                    color: Theme.textMuted
                    font.family: Theme.fontFamily
                    font.pixelSize: Theme.fontSizeSm
                    font.weight: Theme.fontWeightMedium
                }
            }

            // Widest value either column shows: the slider tracks never
            // shift while a value is dragged.
            TextMetrics {
                id: valueMetrics

                font.family: Theme.fontFamily
                font.pixelSize: Theme.fontSizeSm
                font.weight: Theme.fontWeightMedium
                text: "0.00 s"
            }

            // Live vs generate-then-replay: ONE global setting
            // (controller.livePreview), strict binding + write-back.
            ColumnLayout {
                Layout.fillWidth: true
                spacing: 0
                visible: root.showLivePreview

                AppToggle {
                    id: livePreviewToggle

                    objectName: "livePreviewToggle"
                    text: qsTr("Phát trực tiếp")
                    checked: root.hasController && controller.livePreview === true
                    enabled: root.hasController && !controller.busy
                    accessibleLabel: qsTr("Phát trực tiếp khi đang tạo")
                    ToolTip.text: qsTr("Tắt: tạo xong tự phát lại từ đầu")
                    ToolTip.visible: hovered
                    onToggled: controller.livePreview = checked
                }

                Label {
                    Layout.fillWidth: true
                    // Under the toggle's label, past its indicator.
                    Layout.leftMargin: livePreviewToggle.leftPadding
                        + livePreviewToggle.indicator.width + livePreviewToggle.spacing
                    text: qsTr("Nghe ngay khi đang tạo")
                    color: Theme.textSubtle
                    font.family: Theme.fontFamily
                    font.pixelSize: Theme.fontSizeXs
                    wrapMode: Text.Wrap
                }
            }
        }
    }
}
