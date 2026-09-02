#!/bin/sh
# Installs the portable Linux build for the current user (no root needed):
#   - ~/.local/bin/vienetts         symlink to the frozen binary
#   - ~/.local/share/applications   VieNeuTTS menu entry
#   - ~/.local/share/icons/hicolor  launcher icons (16..1024px)
# Uninstall: rm ~/.local/bin/vienetts
#            ~/.local/share/applications/vienetts-app.desktop
#            ~/.local/share/icons/hicolor/*/apps/vienetts-app.png
set -eu

here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
app_root=$(CDPATH= cd -- "$here/../.." && pwd)
binary="$app_root/VieNeuTTS"
[ -x "$binary" ] || { echo "No executable at $binary — run this from the extracted zip's share/linux folder." >&2; exit 1; }

bin_dir=${HOME}/.local/bin
data_dir=${XDG_DATA_HOME:-${HOME}/.local/share}

mkdir -p "$bin_dir" "${data_dir}/applications"
ln -sf "$binary" "$bin_dir/vienetts"
cp -f "$here/vienetts-app.desktop" "${data_dir}/applications/vienetts-app.desktop"

for png in "$here"/icons/hicolor/*/apps/vienetts-app.png; do
  [ -f "$png" ] || continue
  size_dir=$(dirname "$(dirname "$png")")
  mkdir -p "${data_dir}/icons/hicolor/$(basename "$size_dir")/apps"
  cp -f "$png" "${data_dir}/icons/hicolor/$(basename "$size_dir")/apps/vienetts-app.png"
done

update-desktop-database "${data_dir}/applications" 2>/dev/null || true
echo "Installed: run 'vienetts' (new terminals) or find VieNeuTTS in your application menu."
