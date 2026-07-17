#!/usr/bin/env bash
# ============================================================================
#  build_linux.sh -- build a Linux binary of Fragnetic.
#
#  Entry point is fragroute.py (server + browser UI) -- NOT fragroute_app.py,
#  which is the Windows-only WebView2/pywebview host. On Linux the app just opens
#  your default browser (webbrowser.open), so no GTK/Qt webview dependency.
#
#  What works in this binary: UI, region ping, VPN routing (wg-quick), coach via
#  Ollama (or a native llama-server you drop in). Windows-only features (screen
#  recording, WASAPI audio, firewall region-lock, image-gen/voice sidecars) are
#  absent and degrade gracefully. No personal data is bundled (privacy-clean by
#  construction: CI builds fresh, and only a sanitized reference icons file ships).
#
#  Deps:  python3 -m pip install pyinstaller pillow cryptography maxminddb
#  Run :  bash build_linux.sh   ->   dist/Fragnetic
# ============================================================================
set -e
cd "$(dirname "$0")"

# clean reference icons (rank/type/preset emblems only; empty in a fresh CI checkout -- fine)
python3 sanitize_ship_assets.py 2>/dev/null || true
IC_ADD=""
[ -f ship_assets/fragroute_icons.json ] && IC_ADD="--add-data=ship_assets/fragroute_icons.json:."
PNG_ADD=""
[ -f assets/fragroute.png ] && PNG_ADD="--add-data=assets/fragroute.png:assets"

python3 -m PyInstaller --noconfirm --onefile --clean --name Fragnetic \
  --add-data "fragroute_ui.html:." \
  --add-data "fragroute_lancers.json:." \
  --add-data "fragroute_weapons.json:." \
  --add-data "fragroute_cards.json:." \
  $PNG_ADD \
  $IC_ADD \
  --hidden-import fragroute_ai --hidden-import fragroute_capture --hidden-import fragroute_modes \
  --hidden-import fragroute_learning --hidden-import fragroute_knowledge --hidden-import fragroute_llm \
  --hidden-import fragroute_imagegen --hidden-import fragroute_voice --hidden-import fragroute_yolo \
  --hidden-import fragroute_dataset --hidden-import fragroute_embed --hidden-import fragroute_video \
  --hidden-import fragroute_setup --hidden-import fragroute_license --hidden-import fragroute_auth \
  --hidden-import fragroute_hardware --hidden-import fragroute_tts --hidden-import fragroute_persona \
  --hidden-import fragroute_audio --hidden-import fragroute_regionlock --hidden-import fragroute_proc \
  --hidden-import fragroute_procaudio --hidden-import fragroute_wgc --hidden-import fragroute_live \
  --collect-all cryptography --collect-all PIL --collect-all maxminddb \
  --exclude-module matplotlib --exclude-module pandas --exclude-module scipy \
  --exclude-module tkinter --exclude-module pytest --exclude-module IPython \
  --exclude-module pywebview --exclude-module webview --exclude-module clr_loader \
  --exclude-module pyaudiowpatch \
  fragroute.py

echo "[OK] Built dist/Fragnetic (Linux). Run it, then open the printed 127.0.0.1 URL."
