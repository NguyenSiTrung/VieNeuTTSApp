import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import ".."

// Shared voice catalog selector. Group headers are informational; selectable
// rows make the active voice easy to scan before and after the menu opens.
ComboBox {
    id: root

    property string purpose: "select"
    property string fieldLabel: qsTr("Giọng đọc")
    property string popupTitle: qsTr("Chọn giọng đọc")
    property string selectedVoice: ""
    property string filterText: ""
    readonly property bool popupOpen: voicePopup.visible
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
    implicitHeight: Theme.controlHeightLg
    implicitWidth: 260
    Accessible.name: fieldLabel
    Accessible.description: selectedVoiceLabel

    function displayLabel(label) {
        return label.replace(/^[▸—]\s*/, "");
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

    contentItem: Item {
        implicitHeight: root.implicitHeight

        RowLayout {
            anchors.fill: parent
            anchors.leftMargin: Theme.spacingMd
            anchors.rightMargin: Theme.spacingMd
            spacing: Theme.spacingSm

            Rectangle {
                Layout.preferredWidth: 26
                Layout.preferredHeight: 26
                radius: Theme.radiusSm
                color: root.popupOpen ? Theme.accentSubtle : (root.hovered ? Theme.accentSubtle : Theme.surfaceAlt)

                AppIcon {
                    anchors.centerIn: parent
                    width: 14
                    height: 14
                    kind: "wave"
                    iconColor: root.popupOpen || root.hovered ? Theme.accent : Theme.textMuted
                }

                Behavior on color { ColorAnimation { duration: Theme.durationFast } }
            }

            Label {
                Layout.fillWidth: true
                text: root.selectedVoiceLabel || qsTr("Chọn giọng đọc")
                color: root.selectedVoiceLabel !== "" ? Theme.text : Theme.textMuted
                font.family: Theme.fontFamily
                font.pixelSize: Theme.fontSizeBase
                font.weight: Theme.fontWeightMedium
                verticalAlignment: Text.AlignVCenter
                elide: Text.ElideRight
            }

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

    popup: Popup {
        id: voicePopup

        y: root.height + Theme.spacingXs
        width: root.width
        padding: Theme.spacingSm
        modal: false
        dim: false
        focus: true
        closePolicy: Popup.CloseOnEscape | Popup.CloseOnPressOutsideParent
        onClosed: voicePickerFilter.text = ""
        implicitHeight: Math.min(
            Theme.popupMaxHeight,
            pickerContent.implicitHeight + Theme.spacingSm * 2
        )

        background: Rectangle {
            radius: Theme.radiusMd
            color: Theme.surfacePopup
            border.color: Theme.borderPopup
            border.width: 1

            // Drop shadow / elevation outline
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

            spacing: Theme.spacingXs

            Label {
                Layout.fillWidth: true
                text: root.popupTitle
                color: Theme.textMuted
                font.family: Theme.fontFamily
                font.pixelSize: Theme.fontSizeXs
                font.weight: Theme.fontWeightBold
                font.letterSpacing: Theme.trackingWide
                topPadding: Theme.spacingXs
                bottomPadding: Theme.spacingXs
            }

            TextField {
                id: voicePickerFilter
                objectName: "voicePickerFilter"

                Layout.fillWidth: true
                visible: root.flatModel.length > 12
                placeholderText: qsTr("Tìm giọng đọc…")
                placeholderTextColor: Theme.textSubtle
                color: Theme.text
                selectedTextColor: Theme.accentText
                selectionColor: Theme.accent
                selectByMouse: true
                font.family: Theme.fontFamily
                font.pixelSize: Theme.fontSizeBase
                leftPadding: Theme.spacingMd
                rightPadding: Theme.spacingMd
                topPadding: Theme.spacingSm
                bottomPadding: Theme.spacingSm
                onTextChanged: root.filterText = text

                background: Rectangle {
                    radius: Theme.radiusSm
                    color: Theme.surfaceAlt
                    border.width: voicePickerFilter.activeFocus
                        ? Theme.focusRingWidth : 1
                    border.color: voicePickerFilter.activeFocus
                        ? Theme.accent : Theme.borderSubtle
                }
            }

            ListView {
                id: voiceList

                objectName: "voicePickerList"
                Layout.fillWidth: true
                Layout.preferredHeight: Math.min(240, contentHeight)
                clip: true
                model: root.flatModel
                currentIndex: root.highlightedIndex
                spacing: 2

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

                    objectName: "voicePickerRow"
                    width: voiceList.width
                    visible: isGroup
                        ? root.groupHasMatchingVoice(index) : rowMatches
                    height: visible ? (isGroup ? 28 : 44) : 0
                    enabled: !isGroup
                    highlighted: !isGroup && root.highlightedIndex === index
                    hoverEnabled: !isGroup

                    HoverHandler {
                        enabled: !voiceRow.isGroup
                        cursorShape: Qt.PointingHandCursor
                    }
                    onClicked: {
                        root.currentIndex = index;
                        root.activated(index);
                        voicePopup.close();
                    }

                    contentItem: RowLayout {
                        spacing: Theme.spacingSm

                        Item {
                            Layout.preferredWidth: voiceRow.isGroup ? 0 : 20
                            Layout.preferredHeight: 20
                        }

                        ColumnLayout {
                            Layout.fillWidth: true
                            spacing: 0

                            Label {
                                Layout.fillWidth: true
                                text: root.displayLabel(voiceRow.modelData.label)
                                color: voiceRow.isGroup
                                    ? Theme.textSubtle
                                    : (voiceRow.highlighted || voiceRow.isSelected
                                        ? Theme.accent : Theme.text)
                                font.family: Theme.fontFamily
                                font.pixelSize: voiceRow.isGroup
                                    ? Theme.fontSizeXs : Theme.fontSizeBase
                                font.weight: voiceRow.isGroup
                                    ? Theme.fontWeightBold : Theme.fontWeightMedium
                                font.letterSpacing: voiceRow.isGroup ? Theme.trackingWide : 0
                                elide: Text.ElideRight
                            }

                            Label {
                                Layout.fillWidth: true
                                visible: !voiceRow.isGroup
                                    && root.displayLabel(voiceRow.modelData.label).includes(" · ")
                                text: root.displayLabel(voiceRow.modelData.label).split(" — ").slice(1).join(" — ")
                                color: Theme.textMuted
                                font.family: Theme.fontFamily
                                font.pixelSize: Theme.fontSizeXs
                                elide: Text.ElideRight
                            }
                        }

                        AppIcon {
                            objectName: "voicePickerSelectedMark"
                            visible: voiceRow.isSelected
                            width: 16
                            height: 16
                            kind: "check"
                            iconColor: Theme.accent
                        }
                    }

                    background: Rectangle {
                        radius: Theme.radiusSm
                        color: voiceRow.isGroup ? "transparent"
                            : (voiceRow.highlighted || voiceRow.isSelected
                                ? Theme.accentSubtle
                                : (voiceRow.hovered ? Theme.surfaceHover : "transparent"))
                        Behavior on color { ColorAnimation { duration: Theme.durationFast } }
                    }
                }
            }
        }
    }
}
