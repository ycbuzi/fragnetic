@echo off
REM ============================================================
REM  Build Fragnetic.exe  --  just double-click this file.
REM  Produces a single-file, windowed, admin app: dist\Fragnetic.exe
REM  All-in-one: bundles wireguard.exe so nothing external is needed.
REM ============================================================
setlocal enabledelayedexpansion
cd /d "%~dp0"

echo ============================================================
echo  Building Fragnetic.exe  (version set by APP_BUILD in fragroute.py)
echo ============================================================
echo.

REM --- 1) Find a Python (py launcher, then python, then known path) ---
set "PY="
py -3 --version >nul 2>nul && set "PY=py -3"
if not defined PY (
  where python >nul 2>nul && set "PY=python"
)
if not defined PY (
  if exist "C:\Program Files\Python313\python.exe" set "PY=""C:\Program Files\Python313\python.exe"""
)
if not defined PY (
  echo [X] Could not find Python.
  echo     Install it from https://www.python.org/downloads/ and tick "Add to PATH".
  pause
  exit /b 1
)
echo Using Python: %PY%
%PY% --version
echo.

REM --- 2) Install build + runtime deps (PyInstaller, native window, tray, icon) ---
REM  pywebview + pythonnet give the FRAMELESS native window (custom titlebar).
REM  Without them the exe falls back to an Edge app-window that CAN'T be frameless.
echo Installing dependencies...
%PY% -m pip install --upgrade pip >nul 2>nul
%PY% -m pip install pyinstaller pystray pillow pywebview pythonnet onnxruntime-directml numpy cryptography maxminddb pyaudiowpatch
if errorlevel 1 (
  echo [X] pip install failed. Check your internet connection and try again.
  pause
  exit /b 1
)

REM --- 3) Draw the app icon ---
echo Generating icon...
%PY% make_icon.py
if errorlevel 1 (
  echo [X] Icon generation failed.
  pause
  exit /b 1
)

REM --- 3b) UI SMOKE CHECK: refuse to build a broken login/UI ----------------
REM  A broken <script> in fragroute_ui.html silently kills the whole UI (no login,
REM  no buttons) and 'python compiles' never catches it. Abort the build loudly.
echo Checking UI (login/disclaimer path + JS syntax)...
%PY% check_ui.py
if errorlevel 1 (
  echo [X] UI smoke check FAILED - refusing to build a broken app. Fix fragroute_ui.html.
  pause
  exit /b 1
)

REM --- 4) Acquire + bundle WireGuard (all-in-one) -------------------------
REM  If wireguard.exe isn't already in this folder, copy it from a system
REM  WireGuard for Windows install so the built exe is fully self-contained.
set "WG_ADD="
if not exist "wireguard.exe" (
  if exist "C:\Program Files\WireGuard\wireguard.exe" (
    copy /y "C:\Program Files\WireGuard\wireguard.exe" "wireguard.exe" >nul
  ) else if exist "C:\Program Files (x86)\WireGuard\wireguard.exe" (
    copy /y "C:\Program Files (x86)\WireGuard\wireguard.exe" "wireguard.exe" >nul
  )
)
REM  Bundle the current weapon-skins snapshot (from dist) so a bare exe carries
REM  the user's skins to another computer (seeded on first run if none present).
set "WS_ADD="
if exist "dist\fragroute_weapon_skins.json" (
  set "WS_ADD=--add-data dist\fragroute_weapon_skins.json;."
  echo Bundling weapon-skins snapshot ^(portable to another PC^).
)
set "IC_ADD="
if exist "dist\fragroute_icons.json" (
  set "IC_ADD=--add-data dist\fragroute_icons.json;."
  echo Bundling icons ^(rank emblems etc.^).
)
if exist "wireguard.exe" (
  set "WG_ADD=--add-data wireguard.exe;."
  echo Bundling WireGuard ^(all-in-one^).
) else (
  echo [!] wireguard.exe not found - building WITHOUT a bundled WireGuard.
  echo     The app will still use a system WireGuard install if one exists.
  echo     For a fully all-in-one exe: install WireGuard once from
  echo     https://www.wireguard.com/install/  then run this again,
  echo     or drop wireguard.exe next to this script.
)

REM --- 5) Build the exe ---
echo Building exe ^(about a minute^)...
%PY% -m PyInstaller --noconfirm --onefile --windowed --clean ^
  --name Fragnetic ^
  --icon assets\fragroute.ico ^
  --uac-admin ^
  --add-data "fragroute_ui.html;." ^
  --add-data "fragroute_lancers.json;." ^
  --add-data "fragroute_weapons.json;." ^
  --add-data "fragroute_cards.json;." ^
  --add-data "assets\fragroute.ico;assets" ^
  --add-data "assets\fragroute.png;assets" ^
  !WS_ADD! ^
  !IC_ADD! ^
  !WG_ADD! ^
  --hidden-import fragroute ^
  --hidden-import fragroute_ai ^
  --hidden-import fragroute_capture ^
  --hidden-import fragroute_modes ^
  --hidden-import fragroute_learning ^
  --hidden-import fragroute_knowledge ^
  --hidden-import fragroute_llm ^
  --hidden-import fragroute_imagegen ^
  --hidden-import fragroute_voice ^
  --hidden-import fragroute_yolo ^
  --hidden-import fragroute_dataset ^
  --hidden-import fragroute_embed ^
  --hidden-import fragroute_video ^
  --hidden-import fragroute_setup ^
  --hidden-import fragroute_license ^
  --hidden-import fragroute_auth ^
  --hidden-import fragroute_hardware ^
  --hidden-import fragroute_tts ^
  --hidden-import fragroute_persona ^
  --hidden-import fragroute_audio ^
  --hidden-import fragroute_regionlock ^
  --hidden-import pyaudiowpatch ^
  --collect-all pyaudiowpatch ^
  --hidden-import maxminddb ^
  --collect-all maxminddb ^
  --hidden-import fragroute_live ^
  --collect-all pystray ^
  --collect-all onnxruntime ^
  --collect-all cryptography ^
  --collect-all PIL ^
  --collect-all webview ^
  --collect-all clr_loader ^
  --exclude-module matplotlib ^
  --exclude-module pandas ^
  --exclude-module scipy ^
  --exclude-module tkinter ^
  --exclude-module pytest ^
  --exclude-module IPython ^
  --exclude-module notebook ^
  --hidden-import clr ^
  fragroute_app.py
if errorlevel 1 (
  echo [X] PyInstaller build failed. See the messages above.
  pause
  exit /b 1
)

REM --- 6) Put configs + readme + wireguard next to the exe ---
REM  NOTE: never overwrite the user's live data files in dist\ (queue log, rank,
REM  servers, settings, etc.) -- those accumulate as they play. Only seed the
REM  queue log if dist\ doesn't already have one.
if exist configs xcopy /e /i /y configs "dist\configs" >nul
if exist fragroute_queue_log.json if not exist "dist\fragroute_queue_log.json" copy /y fragroute_queue_log.json "dist\" >nul
REM Shipped skin catalog -- the reference gallery next to the exe (read by machine_id-based path)
if exist fragroute_skins_catalog.json copy /y fragroute_skins_catalog.json "dist\" >nul
if exist README.md copy /y README.md "dist\" >nul
if exist EULA.md copy /y EULA.md "dist\" >nul
if exist PRIVACY.md copy /y PRIVACY.md "dist\" >nul
if exist REFUND.md copy /y REFUND.md "dist\" >nul
if exist DISCLAIMER.md copy /y DISCLAIMER.md "dist\" >nul
if exist THIRD_PARTY_NOTICES.txt copy /y THIRD_PARTY_NOTICES.txt "dist\" >nul
if exist wireguard.exe copy /y wireguard.exe "dist\" >nul
REM  ffmpeg (NVENC + ddagrab) powers the low-impact match recorder. It is a
REM  SIDECAR next to the exe -- intentionally NOT --add-data'd into the onefile
REM  (~137 MB would slow every elevated launch). Seed it to dist\ if present;
REM  dist\ffmpeg.exe also survives --clean rebuilds on its own.
REM  Always refresh ffmpeg.exe so dist gets the LGPL build (a stale GPL build here
REM  would be a licensing problem for a sold app).
if exist ffmpeg.exe copy /y ffmpeg.exe "dist\" >nul
if exist "dist\ffmpeg.exe" (echo Recorder: ffmpeg present ^(capture enabled^).) else (echo [!] No dist\ffmpeg.exe - recorder shows "needs ffmpeg" until added.)
REM  Local AI: llama.cpp (cpu/vk) + the GGUF model live in an 'llm' SIDECAR folder
REM  next to the exe (NOT in the onefile -- the ~1.8 GB model would bloat startup).
REM  Seed it to dist\ once; it survives --clean rebuilds on its own afterwards.
if exist "llm" xcopy /e /i /y /d llm "dist\llm" >nul
REM  Image generator: stable-diffusion.cpp (sd) sidecar folder (binary + model).
if exist "sd" xcopy /e /i /y /d sd "dist\sd" >nul
if exist "sd\vk" xcopy /e /i /y "sd\vk" "dist\sd\vk" >nul
if exist "sd\cpu" xcopy /e /i /y "sd\cpu" "dist\sd\cpu" >nul
if exist "dist\sd\vk\sd-cli.exe" (echo Image gen: sd binary present.) else (echo [!] No dist\sd binary - image gen off until added.)
REM  Voice commands: whisper.cpp STT sidecar (binary + model) in the 'stt' folder.
if exist "stt" xcopy /e /i /y /d stt "dist\stt" >nul
if exist "dist\stt" (echo Voice: stt folder present.) else (echo [!] No dist\stt - voice commands off until added.)
REM  Offline detector: CPU-only onnxruntime is bundled (--collect-all onnxruntime
REM  above). We use CPU (NOT DirectML) -- DirectML crashed on this machine's AMD
REM  iGPU. The YOLOX .onnx model lives in a 'yolo' SIDECAR (kept out of the onefile).
if exist "yolo" xcopy /e /i /y /d yolo "dist\yolo" >nul
if exist "dist\yolo\yolox_tiny.onnx" (echo Detector: YOLOX model present.) else (echo [!] No dist\yolo model - offline detector off until a YOLOX .onnx is added.)
REM  CLIP encoder (clip\clip_vitb32.onnx) powers the labeler's few-shot class
REM  suggestions -- sidecar (it's ~350MB, not in the onefile).
if exist "clip" xcopy /e /i /y /d clip "dist\clip" >nul
if exist "dist\clip\clip_vitb32.onnx" (echo Suggester: CLIP encoder present.) else (echo [!] No dist\clip - label suggestions off until clip_vitb32.onnx is added.)
REM  Server locator: the offline GeoIP DB (geo\*.mmdb) names ANY server in the
REM  Live Game tab, including off-VPN raw match IPs. Downloaded via Setup; seed to dist.
if exist "geo" xcopy /e /i /y /d geo "dist\geo" >nul
if exist "dist\geo\dbip-city-lite.mmdb" (echo Server locator: GeoIP DB present.) else (echo [!] No dist\geo - off-VPN server names need the GeoIP DB from Setup.)
REM  Coach VOICE: Piper neural TTS (binary + voice model) in the 'tts' sidecar.
if exist "tts" xcopy /e /i /y /d tts "dist\tts" >nul
if exist "dist\tts\piper\piper.exe" (echo Coach voice: Piper neural TTS present.) else (echo [!] No dist\tts - coach falls back to Windows SAPI voice.)
if exist "dist\llm" (echo Local AI: llm folder present ^(coach LLM enabled^).) else (echo [!] No dist\llm - coach stays router-only until the model is added.)

echo.
echo ============================================================
echo  DONE.
echo  Your app:     dist\Fragnetic.exe
echo  Its configs:  dist\configs\
if exist wireguard.exe echo  WireGuard:    bundled (all-in-one)
echo.
echo  Double-click Fragnetic.exe, click YES on the UAC prompt.
echo  The header shows the BUILD number so you know the new code is running.
echo ============================================================
echo.
pause
