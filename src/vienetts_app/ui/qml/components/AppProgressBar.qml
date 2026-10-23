import QtQuick
import QtQuick.Controls
import ".."

// Shared 6 px track: render/export progress in the subtitle dock and the AI
// progress row. The indeterminate sweep is one XAnimator on a clipped bar, so
// a long ffmpeg burn-in costs no more than a short render.
//
// API
//   value            0..1
//   indeterminate    sweeping bar when the total is unknown
//   accessibleLabel  spoken name ("" is not acceptable at a call site)
ProgressBar {
    id: root

    property string accessibleLabel: ""

    implicitHeight: 6
    from: 0
    to: 1

    Accessible.role: Accessible.ProgressBar
    Accessible.name: root.accessibleLabel

    background: Rectangle {
        implicitHeight: 6
        radius: 3
        color: Theme.surfaceAlt
    }

    contentItem: Item {
        id: progressTrack

        clip: true

        Rectangle {
            visible: !root.indeterminate
            width: root.visualPosition * parent.width
            height: parent.height
            radius: 3
            color: Theme.accent
        }

        Rectangle {
            id: indetBar

            visible: root.indeterminate
            width: parent.width * 0.3
            height: parent.height
            radius: 3
            color: Theme.accent

            XAnimator on x {
                from: -indetBar.width
                to: progressTrack.width
                duration: 900
                loops: Animation.Infinite
                running: root.indeterminate
            }
        }
    }
}
