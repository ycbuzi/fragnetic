#!/usr/bin/env bash
# ============================================================================
#  install.sh -- make Fragnetic a NATIVE Linux app (app-menu entry, icon, own
#  chromeless window). Run this once after extracting the tarball:
#      tar -xzf Fragnetic-linux-x86_64.tar.gz && cd Fragnetic && ./install.sh
#  No root needed -- everything installs under ~/.local.
# ============================================================================
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"
SRC_BIN="$HERE/Fragnetic"
SRC_ICON="$HERE/fragnetic.png"

if [ ! -f "$SRC_BIN" ]; then echo "[X] Fragnetic binary not found next to install.sh"; exit 1; fi

BIN_DIR="$HOME/.local/bin"
APPS="$HOME/.local/share/applications"
ICONS="$HOME/.local/share/icons/hicolor/256x256/apps"
mkdir -p "$BIN_DIR" "$APPS" "$ICONS"

# stable install location (survives moving/deleting the extracted folder)
install -m 755 "$SRC_BIN" "$BIN_DIR/Fragnetic"
[ -f "$SRC_ICON" ] && cp -f "$SRC_ICON" "$ICONS/fragnetic.png"

cat > "$APPS/fragnetic.desktop" <<DESKTOP
[Desktop Entry]
Type=Application
Name=Fragnetic
GenericName=FragPunk companion
Comment=Region ping, private AI coach, and match tools for FragPunk
Exec=$BIN_DIR/Fragnetic
Icon=fragnetic
Terminal=false
Categories=Game;Utility;Network;
StartupWMClass=Fragnetic
DESKTOP
chmod +x "$APPS/fragnetic.desktop"
update-desktop-database "$APPS" 2>/dev/null || true

echo "[OK] Installed. Launch 'Fragnetic' from your app menu, or run: $BIN_DIR/Fragnetic"
echo "     (For a native chromeless window, have a Chromium-based browser installed:"
echo "      chromium / google-chrome / brave / microsoft-edge. Otherwise it opens a tab.)"
echo "     Uninstall: rm ~/.local/bin/Fragnetic ~/.local/share/applications/fragnetic.desktop ~/.local/share/icons/hicolor/256x256/apps/fragnetic.png"
