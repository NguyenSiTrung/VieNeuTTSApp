import QtQuick
import ".."

// Single source of vector iconography: a 20×20 grid, 1.5px rounded strokes,
// drawn on Canvas so every icon inherits the surrounding color instantly
// (no per-icon color variants, no icon font, no emoji). Kinds mirror the tab
// ids plus a few utility glyphs — extend the switch, never fork a copy.
Canvas {
    id: root

    property string kind: "text"    // text|paragraph|audiobook|cloning|settings|upload|file|wave
    property color iconColor: Theme.textMuted
    property real strokeWidth: 1.5

    width: 20
    height: 20
    antialiasing: true
    renderTarget: Canvas.FramebufferObject

    onKindChanged: requestPaint()
    onIconColorChanged: requestPaint()
    Component.onCompleted: requestPaint()

    onPaint: {
        const ctx = getContext("2d");
        ctx.reset();
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
        }
    }
}
