import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import ".."

// Shared voice catalog selector (Pro-Audio Studio edition). Group headers are
// informational; selectable rows display rich persona badges (gender, region,
// style chips) and a tactile per-row audition button.
ComboBox {
    id: root

    property string purpose: "select"
    property string fieldLabel: qsTr("Giọng đọc")
    property string popupTitle: qsTr("Chọn giọng đọc")
    property string selectedVoice: ""
    property string filterText: ""
    readonly property bool popupOpen: voicePopup.visible
    // Set by an audition button before its click bubbles to the row/trigger:
    // consumed + cleared so comparing/auditioning never collapses the picker.
    property bool auditionClickGuard: false
    readonly property bool popupDim: voicePopup.dim
    readonly property string selectedVoiceLabel: {
        const row = rowForId(selectedVoice);
        return row ? displayLabel(row.label) : "";
    }
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

    objectName: "voicePicker"
    textRole: "label"
    model: flatModel
    implicitHeight: 48
    implicitWidth: 280
    Accessible.name: fieldLabel
    Accessible.description: selectedVoiceLabel

    function displayLabel(label) {
        return label ? label.replace(/^[▸—]\s*/, "") : "";
    }

    function parseVoiceInfo(rawLabel) {
        if (!rawLabel)
            return { name: "", gender: "", region: "", style: "" };
        const clean = displayLabel(rawLabel);
        if (!clean.includes(" — "))
            return { name: clean, gender: "", region: "", style: "" };
        const parts = clean.split(" — ");
        const name = parts[0].trim();
        const rest = parts.slice(1).join(" — ").trim();
        const tokens = rest.split(" · ").map(s => s.trim());
        let gender = "";
        let region = "";
        let style = "";
        if (tokens.length >= 1) gender = tokens[0];
        if (tokens.length >= 2) region = tokens[1];
        if (tokens.length >= 3) {
            style = tokens.slice(2).join(" · ")
                .replace(/^(Phong cách|Giọng đọc)\s*/i, "")
                .trim();
        }
        return { name: name, gender: gender, region: region, style: style };
    }

    readonly property var currentVoiceInfo: parseVoiceInfo(selectedVoiceLabel)

    readonly property bool isAuditioningSelected: {
        return typeof controller !== "undefined" && controller
            && controller.auditionVoiceId === root.selectedVoice
            && controller.auditionState !== "idle";
    }

    function rowForId(id) {
        for (let i = 0; i < flatModel.length; i++)
            if (flatModel[i].id === id)
                return flatModel[i];
        return null;
    }

    function indexFor(id) {
        for (let i = 0; i < flatModel.length; i++)
            if (flatModel[i].id !== "" && flatModel[i].id === id)
                return i;
        return 0;
    }

    function rowMatches(row) {
        const needle = filterText.trim().toLocaleLowerCase();
        return needle === "" || (row && row.label
            && row.label.toLocaleLowerCase().includes(needle));
    }

    function groupHasMatchingVoice(groupIndex) {
        if (filterText.trim() === "")
            return true;
        for (let i = groupIndex + 1; i < flatModel.length; i++) {
            const row = flatModel[i];
            if (row.id === "")
                return false;
            if (rowMatches(row))
                return true;
        }
        return false;
    }

    function openPopup() {
        voicePopup.open();
    }

    function closePopup() {
        voicePopup.close();
    }

    onCurrentIndexChanged: {
        const row = currentIndex >= 0 && currentIndex < flatModel.length ? flatModel[currentIndex] : null;
        selectedVoice = row && row.id !== "" ? row.id : "";
    }

    onActivated: function(index) {
        const row = index >= 0 && index < flatModel.length ? flatModel[index] : null;
        if (!row || row.id === "")
            return;
        if (purpose === "default")
            controller.defaultVoice = row.id;
        else
            selectedVoice = row.id;
    }

    Component.onCompleted: {
        const target = (typeof controller !== "undefined" && controller) ? controller.defaultVoice : "";
        const index = indexFor(target);
        currentIndex = index;
        if (flatModel[index] && flatModel[index].id !== "")
            selectedVoice = flatModel[index].id;
    }

    Connections {
        target: (typeof controller !== "undefined" && controller) ? controller : null

        function onDefaultVoiceChanged() {
            if (root.purpose !== "default")
                return;
            const index = root.indexFor(controller.defaultVoice);
            if (root.currentIndex !== index)
                root.currentIndex = index;
        }
    }

    indicator: Item {
        visible: false
        width: 0
        height: 0
    }

    // ── Studio Trigger Bar (Closed State) ──────────────────────────────
    contentItem: Item {
        implicitHeight: root.implicitHeight

        RowLayout {
            anchors.fill: parent
            anchors.leftMargin: Theme.spacingMd
            anchors.rightMargin: Theme.spacingMd
            spacing: Theme.spacingSm

            // Left Voice Avatar / Wave Badge
            Rectangle {
                Layout.preferredWidth: 32
                Layout.preferredHeight: 32
                radius: Theme.radiusSm
                color: root.popupOpen || root.isAuditioningSelected
                    ? Theme.accentSubtle
                    : (root.hovered ? Theme.surfaceAlt : Theme.surfaceCardAlt)
                border.width: 1
                border.color: root.popupOpen || root.isAuditioningSelected
                    ? Theme.accent
                    : (root.hovered ? Theme.border : Theme.borderSubtle)

                AppIcon {
                    anchors.centerIn: parent
                    width: 16
                    height: 16
                    kind: "wave"
                    iconColor: root.popupOpen || root.hovered || root.isAuditioningSelected
                        ? Theme.accent : Theme.textMuted
                }

                Behavior on color { ColorAnimation { duration: Theme.durationFast } }
                Behavior on border.color { ColorAnimation { duration: Theme.durationFast } }
            }

            // Middle: Voice Persona Details
            ColumnLayout {
                Layout.fillWidth: true
                spacing: 1
                clip: true
                RowLayout {
                    Layout.fillWidth: true
                    spacing: Theme.spacingXs

                    Label {
                        text: root.currentVoiceInfo.name !== ""
                            ? root.currentVoiceInfo.name
                            : (root.selectedVoiceLabel || qsTr("Chọn giọng đọc…"))
                        color: root.selectedVoice !== "" ? Theme.text : Theme.textMuted
                        font.family: Theme.fontFamily
                        font.pixelSize: Theme.fontSizeBase
                        font.weight: Theme.fontWeightHeading
                        verticalAlignment: Text.AlignVCenter
                        elide: Text.ElideRight
                    }

                    // Region Pill
                    Rectangle {
                        visible: root.currentVoiceInfo.region !== ""
                        radius: Theme.radiusPill
                        color: Theme.accentSubtle
                        implicitHeight: 18
                        implicitWidth: regLbl.implicitWidth + 10

                        Label {
                            id: regLbl
                            anchors.centerIn: parent
                            text: root.currentVoiceInfo.region
                            color: Theme.accent
                            font.family: Theme.fontFamily
                            font.pixelSize: Theme.fontSizeXs
                            font.weight: Theme.fontWeightMedium
                        }
                    }

                    // Gender Pill
                    Rectangle {
                        visible: root.currentVoiceInfo.gender !== "" && root.width >= 330
                        radius: Theme.radiusPill
                        color: Theme.isDark ? "#232836" : "#e2e8f0"
                        implicitHeight: 18
                        implicitWidth: genLbl.implicitWidth + 10

                        Label {
                            id: genLbl
                            anchors.centerIn: parent
                            text: root.currentVoiceInfo.gender
                            color: Theme.textMuted
                            font.family: Theme.fontFamily
                            font.pixelSize: Theme.fontSizeXs
                            font.weight: Theme.fontWeightMedium
                        }
                    }

                    // Style Pill
                    Rectangle {
                        visible: root.currentVoiceInfo.style !== "" && root.width >= 380
                        radius: Theme.radiusPill
                        color: Theme.isDark ? "#1b1f2b" : "#f1f5f9"
                        implicitHeight: 18
                        implicitWidth: styLbl.implicitWidth + 10

                        Label {
                            id: styLbl
                            anchors.centerIn: parent
                            text: root.currentVoiceInfo.style
                            color: Theme.textSubtle
                            font.family: Theme.fontFamily
                            font.pixelSize: Theme.fontSizeXs
                        }
                    }

                    Item { Layout.fillWidth: true }
                }
            }

            // Right: Inline Audition Button (Quick-listen without opening popup)
            AppIconButton {
                id: triggerAuditionBtn
                visible: root.selectedVoice !== ""
                size: "sm"
                iconKind: root.isAuditioningSelected ? "stop" : "play"
                busy: typeof controller !== "undefined" && controller
                    && controller.auditionVoiceId === root.selectedVoice
                    && controller.auditionState === "loading"
                enabled: (typeof controller !== "undefined" && controller)
                    ? !controller.busy : true
                tooltipText: root.isAuditioningSelected
                    ? qsTr("Dừng nghe thử") : qsTr("Nghe thử giọng đang chọn")

                onClicked: {
                    root.auditionClickGuard = true;
                    if (typeof controller !== "undefined" && controller)
                        controller.auditionVoice(root.selectedVoice);
                }
            }

            // Divider
            Rectangle {
                Layout.preferredWidth: 1
                Layout.preferredHeight: 18
                color: Theme.borderSubtle
            }

            // Chevron Icon
            AppIcon {
                width: 16
                height: 16
                kind: "chevronDown"
                iconColor: root.popupOpen || root.hovered ? Theme.accent : Theme.textMuted
                rotation: root.popupOpen ? 180 : 0

                Behavior on rotation {
                    NumberAnimation {
                        duration: Theme.durationFast
                        easing.type: Easing.OutCubic
                    }
                }
            }
        }
    }

    HoverHandler {
        id: hoverHandler
        cursorShape: root.enabled ? Qt.PointingHandCursor : Qt.ArrowCursor
    }

    background: Rectangle {
        radius: Theme.radiusMd
        color: root.popupOpen ? Theme.surfaceAlt : (root.hovered ? Theme.surfaceHover : Theme.surface)
        border.width: root.activeFocus || root.popupOpen ? Theme.focusRingWidth : 1
        border.color: root.activeFocus || root.popupOpen ? Theme.accent : (root.hovered ? Theme.border : Theme.borderSubtle)

        Behavior on color { ColorAnimation { duration: Theme.durationFast } }
        Behavior on border.color { ColorAnimation { duration: Theme.durationFast } }
    }

    // ── Studio Voice Catalog (Dropdown Popup) ──────────────────────────
    popup: Popup {
        id: voicePopup

        x: root.width < width ? root.width - width : 0
        y: root.height + Theme.spacingXs
        // Pro-Audio layout: clamp width so ultra-wide layouts don't overstretch
        width: Math.max(380, Math.min(root.width, 520))
        padding: Theme.spacingSm
        modal: false
        dim: false
        focus: true
        closePolicy: Popup.CloseOnEscape | Popup.CloseOnPressOutsideParent
        onClosed: {
            // An audition click bubbles through the row's guard AND the
            // popup's press-outside handling: reopen so back-to-back
            // compares never collapse the picker.
            if (root.auditionClickGuard) {
                root.auditionClickGuard = false;
                voicePopup.open();
                return;
            }
            voicePickerFilter.text = "";
            if (typeof controller !== "undefined" && controller)
                controller.stopAudition();
        }
        implicitHeight: Math.min(
            Theme.popupMaxHeight + 40,
            pickerContent.implicitHeight + Theme.spacingSm * 2
        )

        background: Rectangle {
            radius: Theme.radiusLg
            color: Theme.surfacePopup
            border.color: Theme.borderPopup
            border.width: 1

            // Soft shadow depth
            Rectangle {
                anchors.fill: parent
                anchors.margins: -1
                radius: parent.radius + 1
                color: "transparent"
                border.color: Theme.shadowPopup
                border.width: 1
                z: -1
            }
        }

        contentItem: ColumnLayout {
            id: pickerContent

            spacing: Theme.spacingSm

            // Popup Title Bar
            RowLayout {
                Layout.fillWidth: true
                Layout.leftMargin: Theme.spacingXs
                Layout.rightMargin: Theme.spacingXs
                Layout.topMargin: Theme.spacingXs

                Label {
                    text: root.popupTitle
                    color: Theme.textMuted
                    font.family: Theme.fontFamily
                    font.pixelSize: Theme.fontSizeXs
                    font.weight: Theme.fontWeightBold
                    font.letterSpacing: Theme.trackingWide
                }

                Item { Layout.fillWidth: true }

                Label {
                    text: qsTr("%n giọng", "", root.flatModel.filter(r => r.id !== "").length)
                    color: Theme.textSubtle
                    font.family: Theme.fontFamily
                    font.pixelSize: Theme.fontSizeXs
                }
            }

            // Search / Filter Field
            Rectangle {
                Layout.fillWidth: true
                implicitHeight: 38
                radius: Theme.radiusSm
                color: Theme.surfaceAlt
                border.width: voicePickerFilter.activeFocus ? Theme.focusRingWidth : 1
                border.color: voicePickerFilter.activeFocus ? Theme.accent : Theme.borderSubtle

                RowLayout {
                    anchors.fill: parent
                    anchors.leftMargin: Theme.spacingMd
                    anchors.rightMargin: Theme.spacingMd
                    spacing: Theme.spacingSm

                    AppIcon {
                        width: 14
                        height: 14
                        kind: "search"
                        iconColor: voicePickerFilter.activeFocus ? Theme.accent : Theme.textSubtle
                    }

                    TextField {
                        id: voicePickerFilter
                        objectName: "voicePickerFilter"

                        Layout.fillWidth: true
                        placeholderText: qsTr("Tìm giọng đọc…")
                        placeholderTextColor: Theme.textSubtle
                        color: Theme.text
                        selectedTextColor: Theme.accentText
                        selectionColor: Theme.accent
                        selectByMouse: true
                        font.family: Theme.fontFamily
                        font.pixelSize: Theme.fontSizeBase
                        padding: 0
                        background: null
                        onTextChanged: root.filterText = text
                    }

                    AppIconButton {
                        visible: voicePickerFilter.text !== ""
                        size: "sm"
                        iconKind: "close"
                        tooltipText: qsTr("Xóa bộ lọc")
                        onClicked: voicePickerFilter.text = ""
                    }
                }
            }

            // Voice List
            ListView {
                id: voiceList

                objectName: "voicePickerList"
                Layout.fillWidth: true
                Layout.preferredHeight: Math.min(260, contentHeight)
                clip: true
                model: root.flatModel
                currentIndex: root.highlightedIndex
                spacing: 3

                ScrollBar.vertical: ScrollBar {
                    implicitWidth: 6
                    contentItem: Rectangle {
                        radius: 3
                        color: Theme.border
                    }
                }

                delegate: ItemDelegate {
                    id: voiceRow

                    required property var modelData
                    required property int index
                    readonly property bool isGroup: modelData && modelData.id === ""
                    readonly property bool isSelected: !isGroup && modelData.id === root.selectedVoice
                    readonly property bool filterActive: root.filterText.trim() !== ""
                    readonly property bool rowMatches: root.rowMatches(modelData)
                    readonly property string rowLabel: modelData ? modelData.label : ""
                    readonly property var rowVoiceInfo: root.parseVoiceInfo(rowLabel)
                    readonly property bool isAuditioningThis: {
                        return typeof controller !== "undefined" && controller
                            && controller.auditionVoiceId === (modelData ? modelData.id : "")
                            && controller.auditionState !== "idle";
                    }

                    objectName: "voicePickerRow"
                    width: voiceList.width
                    visible: isGroup
                        ? root.groupHasMatchingVoice(index) : rowMatches
                    height: visible ? (isGroup ? 30 : 50) : 0
                    enabled: !isGroup
                    highlighted: !isGroup && root.highlightedIndex === index
                    hoverEnabled: !isGroup
                    onClicked: {
                        if (root.auditionClickGuard)
                            return;
                        root.currentIndex = index;
                        root.activated(index);
                        voicePopup.close();
                    }

                    contentItem: RowLayout {
                        spacing: Theme.spacingSm
                        // Alias rowLabel on the contentItem so target.parent().property("rowLabel")
                        // matches reliably regardless of child visual hierarchy.
                        property alias rowLabel: voiceRow.rowLabel

                        // --- Group Section Header ---
                        RowLayout {
                            visible: voiceRow.isGroup
                            Layout.fillWidth: true
                            spacing: Theme.spacingSm

                            Label {
                                text: root.displayLabel(voiceRow.rowLabel).toUpperCase()
                                color: Theme.accent
                                font.family: Theme.fontFamily
                                font.pixelSize: Theme.fontSizeXs
                                font.weight: Theme.fontWeightBold
                                font.letterSpacing: Theme.trackingWide
                            }

                            Rectangle {
                                Layout.fillWidth: true
                                height: 1
                                color: Theme.borderSubtle
                            }
                        }

                        // --- Individual Voice Row Content ---
                        RowLayout {
                            visible: !voiceRow.isGroup
                            Layout.fillWidth: true
                            spacing: Theme.spacingSm

                            // Voice Avatar Badge
                            Rectangle {
                                Layout.preferredWidth: 32
                                Layout.preferredHeight: 32
                                radius: Theme.radiusSm
                                color: voiceRow.isSelected
                                    ? Theme.accentSubtle
                                    : (voiceRow.hovered ? Theme.surfaceHover : Theme.surfaceAlt)
                                border.width: 1
                                border.color: voiceRow.isSelected
                                    ? Theme.accent
                                    : (voiceRow.hovered ? Theme.border : "transparent")

                                AppIcon {
                                    anchors.centerIn: parent
                                    width: 14
                                    height: 14
                                    kind: "wave"
                                    iconColor: voiceRow.isSelected || voiceRow.isAuditioningThis
                                        ? Theme.accent : Theme.textMuted
                                }
                            }

                            // Middle Info: Name + Tag Chips
                            ColumnLayout {
                                Layout.fillWidth: true
                                spacing: 2

                                Label {
                                    Layout.fillWidth: true
                                    text: voiceRow.rowVoiceInfo.name !== ""
                                        ? voiceRow.rowVoiceInfo.name
                                        : root.displayLabel(voiceRow.rowLabel)
                                    color: voiceRow.isSelected
                                        ? Theme.accent : Theme.text
                                    font.family: Theme.fontFamily
                                    font.pixelSize: Theme.fontSizeBase
                                    font.weight: voiceRow.isSelected
                                        ? Theme.fontWeightBold : Theme.fontWeightMedium
                                    elide: Text.ElideRight
                                }

                                RowLayout {
                                    Layout.fillWidth: true
                                    spacing: 4

                                    // Region Tag
                                    Rectangle {
                                        visible: voiceRow.rowVoiceInfo.region !== ""
                                        radius: Theme.radiusPill
                                        color: Theme.accentSubtle
                                        implicitHeight: 16
                                        implicitWidth: rRegLbl.implicitWidth + 8

                                        Label {
                                            id: rRegLbl
                                            anchors.centerIn: parent
                                            text: voiceRow.rowVoiceInfo.region
                                            color: Theme.accent
                                            font.family: Theme.fontFamily
                                            font.pixelSize: Theme.fontSizeXs - 1
                                            font.weight: Theme.fontWeightMedium
                                        }
                                    }

                                    // Gender Tag
                                    Rectangle {
                                        visible: voiceRow.rowVoiceInfo.gender !== ""
                                        radius: Theme.radiusPill
                                        color: Theme.isDark ? "#232836" : "#e2e8f0"
                                        implicitHeight: 16
                                        implicitWidth: rGenLbl.implicitWidth + 8

                                        Label {
                                            id: rGenLbl
                                            anchors.centerIn: parent
                                            text: voiceRow.rowVoiceInfo.gender
                                            color: Theme.textMuted
                                            font.family: Theme.fontFamily
                                            font.pixelSize: Theme.fontSizeXs - 1
                                            font.weight: Theme.fontWeightMedium
                                        }
                                    }

                                    // Style Tag
                                    Rectangle {
                                        visible: voiceRow.rowVoiceInfo.style !== ""
                                        radius: Theme.radiusPill
                                        color: Theme.isDark ? "#1b1f2b" : "#f1f5f9"
                                        implicitHeight: 16
                                        implicitWidth: rStyLbl.implicitWidth + 8

                                        Label {
                                            id: rStyLbl
                                            anchors.centerIn: parent
                                            text: voiceRow.rowVoiceInfo.style
                                            color: Theme.textSubtle
                                            font.family: Theme.fontFamily
                                            font.pixelSize: Theme.fontSizeXs - 1
                                        }
                                    }

                                    Item { Layout.fillWidth: true }
                                }
                            }

                            // Audition Play Button (objectName: voiceAuditionButton)
                            AppIconButton {
                                objectName: "voiceAuditionButton"
                                visible: !voiceRow.isGroup
                                    && (voiceRow.hovered || voiceRow.isSelected
                                        || voiceRow.isAuditioningThis)
                                size: "sm"
                                iconKind: voiceRow.isAuditioningThis ? "stop" : "play"
                                busy: typeof controller !== "undefined" && controller
                                    && controller.auditionVoiceId === voiceRow.modelData.id
                                    && controller.auditionState === "loading"
                                enabled: (typeof controller !== "undefined" && controller)
                                    ? !controller.busy : true
                                tooltipText: voiceRow.isAuditioningThis
                                    ? qsTr("Dừng nghe thử") : qsTr("Nghe thử giọng này")

                                onClicked: {
                                    root.auditionClickGuard = true;
                                    if (typeof controller !== "undefined" && controller)
                                        controller.auditionVoice(voiceRow.modelData.id);
                                }
                            }

                            // Selected Checkmark
                            AppIcon {
                                objectName: "voicePickerSelectedMark"
                                visible: voiceRow.isSelected
                                width: 16
                                height: 16
                                kind: "check"
                                iconColor: Theme.accent
                            }
                        }
                    }

                    background: Rectangle {
                        radius: Theme.radiusSm
                        color: voiceRow.isGroup ? "transparent"
                            : (voiceRow.isSelected
                                ? Theme.accentSubtle
                                : (voiceRow.hovered || voiceRow.highlighted
                                    ? Theme.surfaceHover : "transparent"))
                        border.width: voiceRow.isSelected ? 1 : 0
                        border.color: voiceRow.isSelected ? Theme.accent : "transparent"

                        Behavior on color { ColorAnimation { duration: Theme.durationFast } }
                    }
                }
            }
        }
    }
}
