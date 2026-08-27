import QtQuick
import QtQuick.Controls
import ".."

// Tracked uppercase micro-label for section separators ("CHỨC NĂNG", "BIỂU CẢM"…).
// All-caps without letter-spacing looks like a rendering bug; with tracking it
// reads as an intentional editorial device. Assign `text` like any Label.
Label {
    color: Theme.textSubtle
    font.family: Theme.fontFamily
    font.pixelSize: Theme.fontSizeXs
    font.weight: Theme.fontWeightBold
    font.letterSpacing: Theme.trackingWide
}
