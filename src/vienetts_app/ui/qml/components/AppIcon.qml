import QtQuick
import ".."

// Single source of vector iconography: a 20×20 grid, 1.5px rounded strokes,
// drawn on Canvas so every icon inherits the surrounding color instantly
// (no per-icon color variants, no icon font, no emoji). Kinds mirror the tab
// ids plus a few utility glyphs — extend the switch, never fork a copy.
Canvas {
    id: root

    property string kind: "text"
    property color iconColor: Theme.textMuted
    property real strokeWidth: 1.5

    width: 20
    height: 20
    antialiasing: true
    renderTarget: Canvas.FramebufferObject

    onKindChanged: requestPaint()
    onIconColorChanged: requestPaint()
    onWidthChanged: requestPaint()
    onHeightChanged: requestPaint()
    Component.onCompleted: requestPaint()

    onPaint: {
        const ctx = getContext("2d");
        ctx.reset();
        if (root.width <= 0 || root.height <= 0) return;
        ctx.save();
        ctx.scale(root.width / 20.0, root.height / 20.0);
        ctx.strokeStyle = String(root.iconColor);
        ctx.fillStyle = String(root.iconColor);
        ctx.lineWidth = root.strokeWidth;
        ctx.lineCap = "round";
        ctx.lineJoin = "round";

        switch (root.kind) {
        case "text":
            // Document with text lines
            ctx.beginPath();
            ctx.rect(4, 2, 12, 16);
            ctx.stroke();
            ctx.beginPath();
            ctx.moveTo(7, 6); ctx.lineTo(13, 6);
            ctx.moveTo(7, 10); ctx.lineTo(13, 10);
            ctx.moveTo(7, 14); ctx.lineTo(10.5, 14);
            ctx.stroke();
            break;
        case "paragraph":
            // Open book / facing pages
            ctx.beginPath();
            ctx.rect(2.5, 3.5, 6.5, 13);
            ctx.rect(11, 3.5, 6.5, 13);
            ctx.stroke();
            ctx.beginPath();
            ctx.moveTo(4.5, 7); ctx.lineTo(7, 7);
            ctx.moveTo(4.5, 10.5); ctx.lineTo(7, 10.5);
            ctx.moveTo(13, 7); ctx.lineTo(15.5, 7);
            ctx.moveTo(13, 10.5); ctx.lineTo(15.5, 10.5);
            ctx.stroke();
            break;
        case "audiobook":
            // Headphones over a book — the listening studio
            ctx.beginPath();
            ctx.arc(10, 9, 6, Math.PI, 0);          // headband arc
            ctx.stroke();
            ctx.beginPath();
            ctx.rect(2.5, 9, 3.5, 6);               // left ear cup
            ctx.rect(14, 9, 3.5, 6);                // right ear cup
            ctx.stroke();
            ctx.beginPath();
            ctx.moveTo(7, 17.5); ctx.lineTo(13, 17.5);  // open book base
            ctx.moveTo(10, 15.5); ctx.lineTo(10, 17.5);
            ctx.stroke();
            break;
        case "cloning":
        case "wave":
            // Soundwave bars (cloning reuses the wave motif)
            ctx.beginPath();
            ctx.moveTo(4, 7); ctx.lineTo(4, 13);
            ctx.moveTo(8, 4); ctx.lineTo(8, 16);
            ctx.moveTo(12, 2); ctx.lineTo(12, 18);
            ctx.moveTo(16, 6); ctx.lineTo(16, 14);
            ctx.stroke();
            break;
        case "settings":
            // Sliders
            ctx.beginPath();
            ctx.moveTo(3, 6); ctx.lineTo(17, 6);
            ctx.moveTo(3, 14); ctx.lineTo(17, 14);
            ctx.stroke();
            ctx.beginPath();
            ctx.arc(7, 6, 2.2, 0, Math.PI * 2);
            ctx.arc(13, 14, 2.2, 0, Math.PI * 2);
            ctx.fill();
            break;
        case "upload":
            // Tray with up arrow
            ctx.beginPath();
            ctx.moveTo(4, 13); ctx.lineTo(4, 16.5); ctx.lineTo(16, 16.5); ctx.lineTo(16, 13);
            ctx.stroke();
            ctx.beginPath();
            ctx.moveTo(10, 3.5); ctx.lineTo(10, 12.5);
            ctx.moveTo(6.5, 7); ctx.lineTo(10, 3.5); ctx.lineTo(13.5, 7);
            ctx.stroke();
            break;
        case "file":
            // Sheet with folded corner
            ctx.beginPath();
            ctx.moveTo(5, 2.5); ctx.lineTo(12.5, 2.5); ctx.lineTo(15.5, 5.5);
            ctx.lineTo(15.5, 17.5); ctx.lineTo(5, 17.5); ctx.closePath();
            ctx.stroke();
            ctx.beginPath();
            ctx.moveTo(12.5, 2.5); ctx.lineTo(12.5, 5.5); ctx.lineTo(15.5, 5.5);
            ctx.stroke();
            break;
        case "play":
            ctx.beginPath();
            ctx.moveTo(6.5, 4.5); ctx.lineTo(15.5, 10); ctx.lineTo(6.5, 15.5);
            ctx.closePath();
            ctx.fill();
            break;
        case "pause":
            ctx.fillRect(6, 4.5, 2.8, 11);
            ctx.fillRect(11.2, 4.5, 2.8, 11);
            break;
        case "stop":
            ctx.fillRect(6, 6, 8, 8);
            break;
        case "previous":
            ctx.beginPath();
            ctx.moveTo(5, 4.5); ctx.lineTo(5, 15.5);
            ctx.moveTo(15.5, 4.5); ctx.lineTo(8, 10); ctx.lineTo(15.5, 15.5);
            ctx.stroke();
            break;
        case "next":
            ctx.beginPath();
            ctx.moveTo(15, 4.5); ctx.lineTo(15, 15.5);
            ctx.moveTo(4.5, 4.5); ctx.lineTo(12, 10); ctx.lineTo(4.5, 15.5);
            ctx.stroke();
            break;
        case "download":
            ctx.beginPath();
            ctx.moveTo(4, 15.5); ctx.lineTo(4, 17); ctx.lineTo(16, 17); ctx.lineTo(16, 15.5);
            ctx.moveTo(10, 3); ctx.lineTo(10, 13);
            ctx.moveTo(6.5, 9.5); ctx.lineTo(10, 13); ctx.lineTo(13.5, 9.5);
            ctx.stroke();
            break;
        case "close":
            ctx.beginPath();
            ctx.moveTo(5, 5); ctx.lineTo(15, 15);
            ctx.moveTo(15, 5); ctx.lineTo(5, 15);
            ctx.stroke();
            break;
        case "refresh":
            ctx.beginPath();
            ctx.arc(10, 10, 6, Math.PI * 0.2, Math.PI * 1.7);
            ctx.stroke();
            ctx.beginPath();
            ctx.moveTo(15.8, 5.2); ctx.lineTo(15.7, 9); ctx.lineTo(12.1, 7.8);
            ctx.stroke();
            break;
        case "chevronDown":
            ctx.beginPath();
            ctx.moveTo(5.5, 7.5); ctx.lineTo(10, 12); ctx.lineTo(14.5, 7.5);
            ctx.stroke();
            break;
        case "chevronUp":
            ctx.beginPath();
            ctx.moveTo(5.5, 12.5); ctx.lineTo(10, 8); ctx.lineTo(14.5, 12.5);
            ctx.stroke();
            break;
        case "check":
            ctx.beginPath();
            ctx.moveTo(4.5, 10); ctx.lineTo(8.2, 13.5); ctx.lineTo(15.5, 6);
            ctx.stroke();
            break;
        case "folder":
            ctx.beginPath();
            ctx.moveTo(2.5, 6); ctx.lineTo(8, 6); ctx.lineTo(9.5, 8);
            ctx.lineTo(17.5, 8); ctx.lineTo(16, 16.5); ctx.lineTo(3.5, 16.5);
            ctx.closePath();
            ctx.stroke();
            break;
        case "reset":
            ctx.beginPath();
            ctx.arc(10.5, 10, 5.5, Math.PI * 0.2, Math.PI * 1.75);
            ctx.stroke();
            ctx.beginPath();
            ctx.moveTo(5.2, 5.8); ctx.lineTo(5.2, 9.5); ctx.lineTo(8.7, 8.2);
            ctx.stroke();
            break;
        case "spinner":
            ctx.beginPath();
            ctx.arc(10, 10, 6, 0, Math.PI * 1.5);
            ctx.stroke();
            break;
        case "externalLink":
            ctx.beginPath();
            ctx.moveTo(11, 4.5); ctx.lineTo(15.5, 4.5); ctx.lineTo(15.5, 9);
            ctx.moveTo(15.5, 4.5); ctx.lineTo(9, 11);
            ctx.moveTo(13.5, 11.5); ctx.lineTo(13.5, 15.5); ctx.lineTo(4.5, 15.5); ctx.lineTo(4.5, 6.5); ctx.lineTo(8.5, 6.5);
            ctx.stroke();
            break;
        case "copy":
            // Two overlapping sheets (copy to clipboard)
            ctx.beginPath();
            ctx.rect(7.5, 2.5, 9, 12);
            ctx.stroke();
            ctx.beginPath();
            ctx.rect(3.5, 5.5, 9, 12);
            ctx.stroke();
            break;
        case "search":
            ctx.beginPath();
            ctx.arc(8.5, 8.5, 4.8, 0, Math.PI * 2);
            ctx.stroke();
            ctx.beginPath();
            ctx.moveTo(12.0, 12.0); ctx.lineTo(16.5, 16.5);
            ctx.stroke();
            break;
        }
        ctx.restore();
    }
}
