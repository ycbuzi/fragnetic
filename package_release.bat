@echo off
REM ============================================================
REM  package_release.bat -- build a CLEAN customer distributable.
REM
REM  The app accumulates PERSONAL data next to the exe as you use it
REM  (accounts + password hashes, license, trial marker, queue/match history,
REM  replays, recordings, map screenshots, the training dataset, wallpaper +
REM  settings, harvested server intel...). Zipping dist\ would ship ALL of that
REM  to a buyer. This script instead copies ONLY a WHITELIST of safe files into
REM  release\Fragnetic\, so personal data cannot leak by construction.
REM
REM  Run build_exe.bat FIRST, then run this. Ship the release\ folder (or zip it).
REM ============================================================
setlocal enabledelayedexpansion
cd /d "%~dp0"

set "SRC=dist"
set "OUT=release\Fragnetic"

if not exist "%SRC%\Fragnetic.exe" (
  echo [X] No dist\Fragnetic.exe -- run build_exe.bat first.
  exit /b 1
)

echo Building CLEAN release from %SRC%  -^>  %OUT%
if exist "release" rmdir /s /q "release"
mkdir "%OUT%"

REM --- exe + runtime binaries (safe) ---
copy /y "%SRC%\Fragnetic.exe" "%OUT%\" >nul
if exist "%SRC%\ffmpeg.exe"    copy /y "%SRC%\ffmpeg.exe"    "%OUT%\" >nul
if exist "%SRC%\wireguard.exe" copy /y "%SRC%\wireguard.exe" "%OUT%\" >nul

REM --- model / asset folders (safe: no personal data) ---
REM  Pass "slim" to ship only the small runtime models (detector + geo, ~0.2GB); the
REM  app then downloads the fit-matched big models (LLM/SD/STT/TTS) on first run via
REM  Setup. Default (no arg) = FULL, bundling every model (~30GB -- external hosting).
set "MODELDIRS=llm sd stt yolo clip geo tts"
if /I "%~1"=="slim" (set "MODELDIRS=yolo geo" & echo    [SLIM build -- big models download in-app on first run])
for %%D in (%MODELDIRS%) do (
  if exist "%SRC%\%%D" xcopy /e /i /y "%SRC%\%%D" "%OUT%\%%D" >nul
)

REM --- reference asset + legal docs (safe) ---
if exist "%SRC%\fragroute_icons.json" copy /y "%SRC%\fragroute_icons.json" "%OUT%\" >nul
REM Shipped skin CATALOG -- the reference gallery a customer browses + marks owned.
REM Public game-skin images, NOT personal data (no accounts/license/history), so safe.
if exist "%SRC%\fragroute_skins_catalog.json" copy /y "%SRC%\fragroute_skins_catalog.json" "%OUT%\" >nul
for %%F in (README.md EULA.md PRIVACY.md REFUND.md DISCLAIMER.md THIRD_PARTY_NOTICES.txt) do (
  if exist "%SRC%\%%F" copy /y "%SRC%\%%F" "%OUT%\" >nul
)

REM  Deliberately NOT copied (PERSONAL -- must never ship):
REM   fragroute_accounts.json  fragroute_license.json  .fragroute_trial
REM   fragroute_queue_log.json fragroute_replays.json  fragroute_players.json
REM   fragroute_rank.json      fragroute_settings.json fragroute_throttle.json
REM   fragroute_maps.json fragroute_maps\ fragroute_captures\ dataset\ edited\
REM   fragroute_servers*.json  fragroute_serverpings.json fragroute_weapon_skins.json
REM   fragroute_diag.log  configs\

REM --- SAFETY SCAN: abort if any personal artifact slipped into the release ---
set "LEAK="
for %%P in (
  fragroute_accounts.json fragroute_license.json .fragroute_trial
  fragroute_queue_log.json fragroute_replays.json fragroute_players.json
  fragroute_rank.json fragroute_settings.json fragroute_throttle.json
  fragroute_maps.json fragroute_servers.json fragroute_servers.backup.json
  fragroute_serverpings.json fragroute_weapon_skins.json fragroute_diag.log
) do (
  if exist "%OUT%\%%P" set "LEAK=!LEAK! %%P"
)
for %%D in (dataset edited configs fragroute_captures fragroute_maps) do (
  if exist "%OUT%\%%D" set "LEAK=!LEAK! %%D\"
)
if defined LEAK (
  echo [X] PERSONAL DATA LEAK into release:!LEAK!
  echo     Aborting -- do NOT ship this folder.
  exit /b 2
)

echo.
echo [OK] Clean release ready: %OUT%\
echo      Contents:
dir /b "%OUT%"
echo.
echo  Ship the release\Fragnetic\ folder (or zip it). It contains NO personal data.
endlocal
