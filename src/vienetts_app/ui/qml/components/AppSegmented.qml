import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "."
import ".."

// Segmented single-choice control (FR-1.6, ui_shell_redesign): a sunken
// track with one raised segment for the current value. Used for Settings'
// Color mode now, and later by mode switches and device pickers.
//
// API
//   model         array of {value, label[, enabled]} rows, or plain strings
//                 (a string is both value and label)
//   currentValue  the selected value (read/write; NOTIFY = currentValueChanged)
//   currentIndex  read-only index of currentValue in the model (-1 if none)
//   activated(value)  fires on USER selection only (click, arrow keys) —
//                 never on programmatic currentValue writes, so owners can
//                 keep a strict binding (`currentValue: bridge.pref`) and
//                 write back in `onActivated` (CheckBox write-back pattern).
//                 When the owner does NOT write back, the control moves the
//                 selection itself (uncontrolled use).
//
// Each segment is a focusable AbstractButton at least Theme.controlHitTarget
// tall, named `<root objectName>_<value>`, announced as a radio button with
// its label. Left/Up and Right/Down move the selection (wrapping, skipping
// disabled segments); Home/End jump to the first/last enabled segment.
Item {
    id: root

    property var model: []
    property string currentValue: ""
    readonly property int currentIndex: _indexOf(currentValue)
    property string accessibleLabel: ""

    signal activated(string value)

    readonly property var _items: {
        const rows = [];
        const source = root.model || [];
        for (let i = 0; i < source.length; ++i) {
            const row = source[i];
            if (typeof row === "string")
                rows.push({ "value": row, "label": row, "enabled": true });
            else
                rows.push({
                    "value": String(row.value),
                    "label": row.label !== undefined ? String(row.label) : String(row.value),
                    "enabled": row.enabled !== false
                });
        }
        return rows;
    }

    readonly property int _trackPadding: 3

    implicitWidth: segmentRow.implicitWidth + _trackPadding * 2
    implicitHeight: Theme.controlHitTarget + _trackPadding * 2

    Accessible.role: Accessible.Grouping
    Accessible.name: accessibleLabel

    function _indexOf(value) {
        for (let i = 0; i < _items.length; ++i) {
            if (_items[i].value === value)
                return i;
        }
        return -1;
    }

    // User selection: tell the owner first; if it did not write the value
    // back through its own binding, move the selection here.
    function _select(index) {
        if (index < 0 || index >= _items.length || !_items[index].enabled || !root.enabled)
            return;
        const value = _items[index].value;
        if (value !== root.currentValue) {
            root.activated(value);
            if (root.currentValue !== value)
                root.currentValue = value;
        }
        const segment = segmentRepeater.itemAt(index);
        if (segment)
            segment.forceActiveFocus(Qt.TabFocusReason);
    }

    function _step(from, delta) {
        const count = _items.length;
        for (let n = 1; n <= count; ++n) {
            const i = ((from + delta * n) % count + count) % count;
            if (_items[i].enabled)
                return i;
        }
        return -1;
    }

    function _edge(fromEnd) {
        const count = _items.length;
        for (let n = 0; n < count; ++n) {
            const i = fromEnd ? count - 1 - n : n;
            if (_items[i].enabled)
                return i;
        }
        return -1;
    }

    Rectangle {
        anchors.fill: parent
        radius: Theme.radiusMd
        color: Theme.bg
        border.width: 1
        border.color: Theme.border

        Behavior on color { ColorAnimation { duration: Theme.durationBase } }
    }

    RowLayout {
        id: segmentRow

        anchors.fill: parent
        anchors.margins: root._trackPadding
        spacing: 0

        Repeater {
            id: segmentRepeater

            model: root._items

            delegate: AbstractButton {
                id: segment

                required property var modelData
                required property int index

                readonly property bool isCurrent: modelData.value === root.currentValue

                objectName: root.objectName !== "" ? root.objectName + "_" + modelData.value : ""
                Layout.fillWidth: true
                Layout.fillHeight: true
                implicitWidth: Math.max(Theme.controlHitTarget,
                    segmentLabel.implicitWidth + leftPadding + rightPadding)
                implicitHeight: Theme.controlHitTarget
                leftPadding: Theme.spacingMd
                rightPadding: Theme.spacingMd
                text: modelData.label
                enabled: root.enabled && modelData.enabled
                // Only the current segment is a Tab stop (radio-group rule);
                // arrows move within the group, clicks still focus any segment.
                focusPolicy: isCurrent || (root.currentIndex < 0 && index === 0)
                    ? Qt.StrongFocus : Qt.ClickFocus

                Accessible.role: Accessible.RadioButton
                Accessible.name: modelData.label
                Accessible.checkable: true
                Accessible.checked: isCurrent

                onClicked: root._select(index)

                Keys.onPressed: (event) => {
                    let target = -1;
                    if (event.key === Qt.Key_Left || event.key === Qt.Key_Up)
                        target = root._step(index, -1);
                    else if (event.key === Qt.Key_Right || event.key === Qt.Key_Down)
                        target = root._step(index, 1);
                    else if (event.key === Qt.Key_Home)
                        target = root._edge(false);
                    else if (event.key === Qt.Key_End)
                        target = root._edge(true);
                    else
                        return;
                    event.accepted = true;
                    root._select(target);
                }

                contentItem: Label {
                    id: segmentLabel

                    text: segment.text
                    horizontalAlignment: Text.AlignHCenter
                    verticalAlignment: Text.AlignVCenter
                    elide: Text.ElideRight
                    font.family: Theme.fontFamily
                    font.pixelSize: Theme.fontSizeBase
                    font.weight: segment.isCurrent ? Theme.fontWeightHeading : Theme.fontWeightMedium
                    color: !segment.enabled ? Theme.controlDisabledText
                        : (segment.isCurrent || segment.hovered ? Theme.text : Theme.textMuted)
                }

                // Raised current segment: surfaceAlt on the dark track (design
                // token map); light mode lifts it to surfaceCard + border so it
                // stays visible on the near-white track. Disabled segments are
                // never filled — a disabled current value keeps only an outline.
                background: Rectangle {
                    radius: Theme.radiusMd - root._trackPadding
                    color: {
                        if (!segment.enabled)
                            return "transparent";
                        if (segment.isCurrent)
                            return Theme.isDark ? Theme.surfaceAlt : Theme.surfaceCard;
                        return segment.hovered ? Theme.surfaceHover : "transparent";
                    }
                    border.width: segment.visualFocus ? Theme.focusRingWidth
                        : (segment.isCurrent && (!Theme.isDark || !segment.enabled) ? 1 : 0)
                    border.color: segment.visualFocus ? Theme.borderFocus
                        : (segment.enabled ? Theme.border : Theme.controlDisabledBorder)

                    Behavior on color { ColorAnimation { duration: Theme.durationFast } }
                }
            }
        }
    }
}
