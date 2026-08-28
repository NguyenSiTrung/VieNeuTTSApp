"""Generate multi-resolution icons (.png, .icns, .ico) for VieNeuTTS."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication, QImage


def generate_icons(source_path: Path, output_dir: Path) -> None:
    _app = QGuiApplication.instance() or QGuiApplication(sys.argv)
    output_dir.mkdir(parents=True, exist_ok=True)
    icons_dir = output_dir / "icons"
    icons_dir.mkdir(parents=True, exist_ok=True)

    img = QImage(str(source_path))
    if img.isNull():
        raise RuntimeError(f"Failed to load master image from {source_path}")

    # Standard resolutions
    sizes = [16, 32, 48, 64, 128, 256, 512, 1024]
    for size in sizes:
        scaled = img.scaled(
            size,
            size,
            Qt.AspectRatioMode.IgnoreAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        dest_png = icons_dir / f"icon_{size}x{size}.png"
        scaled.save(str(dest_png), "PNG")
        print(f"Saved {dest_png}")

    # Save primary 512x512 icon as icon.png
    shutil.copyfile(icons_dir / "icon_512x512.png", output_dir / "icon.png")

    # Generate macOS .iconset -> .icns if iconutil is available
    if sys.platform == "darwin" and shutil.which("iconutil"):
        iconset_dir = output_dir / "icon.iconset"
        iconset_dir.mkdir(parents=True, exist_ok=True)

        iconset_mapping = [
            (16, "icon_16x16.png"),
            (32, "icon_16x16@2x.png"),
            (32, "icon_32x32.png"),
            (64, "icon_32x32@2x.png"),
            (128, "icon_128x128.png"),
            (256, "icon_128x128@2x.png"),
            (256, "icon_256x256.png"),
            (512, "icon_256x256@2x.png"),
            (512, "icon_512x512.png"),
            (1024, "icon_512x512@2x.png"),
        ]

        for sz, fname in iconset_mapping:
            scaled = img.scaled(
                sz,
                sz,
                Qt.AspectRatioMode.IgnoreAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            scaled.save(str(iconset_dir / fname), "PNG")

        icns_path = output_dir / "icon.icns"
        subprocess.run(
            ["iconutil", "-c", "icns", str(iconset_dir), "-o", str(icns_path)],
            check=True,
        )
        shutil.rmtree(iconset_dir)
        print(f"Generated {icns_path}")

    # Generate .ico (Windows)
    ico_img = img.scaled(
        256,
        256,
        Qt.AspectRatioMode.IgnoreAspectRatio,
        Qt.TransformationMode.SmoothTransformation,
    )
    ico_path = output_dir / "icon.ico"
    ico_img.save(str(ico_path), "ICO")
    print(f"Generated {ico_path}")


if __name__ == "__main__":
    brain_dir = Path.home() / ".gemini" / "antigravity-cli" / "brain"
    conv_id = "41c1071a-16aa-42c5-8806-78e4870ee1b0"
    img_name = "vieneutts_app_icon_1787928512592.jpg"
    src = brain_dir / conv_id / img_name
    out = Path(__file__).resolve().parent.parent / "src" / "vienetts_app" / "ui" / "assets"
    generate_icons(src, out)
