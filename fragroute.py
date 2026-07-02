#!/usr/bin/env python3
"""
FRAGROUTE  --  Fragpunk VPN route optimizer (ProtonVPN / WireGuard backend)
===========================================================================
A LOCAL app. Runs on YOUR machine, outside the browser, with admin/root so it
can actually switch your VPN tunnel and read network state. Stdlib only --
no pip install required.

WHAT IT DOES
  - Discovers ProtonVPN WireGuard .conf files in ./configs and maps each to a
    Fragpunk region by country/state code in the filename.
  - Measures real round-trip latency to each region's VPN endpoint.
  - Brings a region's WireGuard tunnel up / down (this is the route switch).
  - Reads current connection state (active tunnel, default route, exit IP).
  - Persists your queue-time log so the UI can build personal recommendations.

HOW PROTONVPN CONTROL WORKS
  Proton has no public control API. You generate WireGuard configs from your
  account dashboard (Downloads -> WireGuard configuration), drop the .conf
  files in the ./configs folder next to this script, and this app uses the
  WireGuard tool to raise/lower those tunnels. WireGuard rewrites the routes
  automatically when a tunnel comes up.

REQUIREMENTS
  1. Python 3.8+
  2. WireGuard installed
       Windows: https://www.wireguard.com/install/  (gives wireguard.exe)
       Linux:   apt/dnf install wireguard-tools      (gives wg-quick)
       macOS:   brew install wireguard-tools
  3. ProtonVPN WireGuard .conf files in ./configs
  4. Run from an ADMIN / sudo terminal (tunnel changes need privilege)

USAGE
  python fragroute.py                 # run, opens browser to the UI
  python fragroute.py --dry-run       # never executes tunnel commands, just
                                       # logs what it WOULD run (safe to test)
  python fragroute.py --port 8787
  python fragroute.py --configs /path/to/configs
"""

import argparse
import collections
import ctypes
import datetime
import hashlib
import json
import traceback as _traceback
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import urllib.error
import urllib.parse
import urllib.request
import ssl
import webbrowser
import html as _html
import time
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

try:
    import fragroute_ai  # AI coach: LLM-first + action/live-data dispatch
except Exception:
    fragroute_ai = None

try:
    import fragroute_capture  # low-impact NVENC capture for AI review (Phase 2)
except Exception:
    fragroute_capture = None

try:
    import fragroute_modes  # FragPunk game-mode profiles (mode-aware events)
except Exception:
    fragroute_modes = None

try:
    import fragroute_learning  # self-learning mode knowledge store (Phase 4)
except Exception:
    fragroute_learning = None

try:
    import fragroute_knowledge  # FragPunk-only online knowledge fetcher (Phase 4)
except Exception:
    fragroute_knowledge = None

try:
    import fragroute_llm  # local FragPunk-grounded LLM via llama.cpp (Phase 5)
except Exception:
    fragroute_llm = None

try:
    import fragroute_imagegen  # local image generation via stable-diffusion.cpp (Phase 8)
except Exception:
    fragroute_imagegen = None

try:
    import fragroute_voice  # local voice commands via whisper.cpp STT (Phase 10)
except Exception:
    fragroute_voice = None

try:
    import fragroute_yolo  # offline YOLOX object detector for VOD/UI review (ONNX)
except Exception:
    fragroute_yolo = None

try:
    import fragroute_dataset  # training-data pipeline: harvest frames, label, export
except Exception:
    fragroute_dataset = None

try:
    import fragroute_live  # live practice detector (bot/solo modes ONLY -- gated)
except Exception:
    fragroute_live = None

try:
    import fragroute_embed  # CLIP few-shot class suggester for the labeler
except Exception:
    fragroute_embed = None

try:
    import fragroute_video  # AI video editor: trim/montage/caption over clips (ffmpeg)
except Exception:
    fragroute_video = None

try:
    import fragroute_setup  # first-run model installer (downloads the big sidecars)
except Exception:
    fragroute_setup = None

try:
    import fragroute_license  # signed-key licensing + per-feature entitlements
except Exception:
    fragroute_license = None

try:
    import fragroute_auth  # local (+optional cloud) accounts, login gate
except Exception:
    fragroute_auth = None

try:
    import fragroute_hardware  # GPU/CPU/RAM probe + per-feature compatibility verdicts
except Exception:
    fragroute_hardware = None

try:
    import fragroute_tts  # neural TTS (Piper) -- the coach's soothing voice
except Exception:
    fragroute_tts = None

try:
    import fragroute_persona  # per-user adaptive coach personality
except Exception:
    fragroute_persona = None

try:
    import fragroute_regionlock  # switch region WITHOUT a VPN (firewall region lock)
except Exception:
    fragroute_regionlock = None


def _captures_dir():
    """Base folder for the capture ring + saved clips (next to other app data)."""
    try:
        return STATE["configs_dir"].parent / "fragroute_captures"
    except Exception:
        return Path.cwd() / "fragroute_captures"


def _harvest_folders():
    """Folders the auto-harvester scans for new recordings to feed YOLO: the
    user's configured folders (OBS output, etc.) PLUS the app's own clips. Any
    recording dropped here is auto-imported as training frames."""
    folders = [str(f) for f in (get_setting("harvestFolders", []) or []) if f]
    try:
        folders.append(str(_captures_dir() / "clips"))
        folders.append(str(_captures_dir()))
    except Exception:
        pass
    return folders


def _auto_harvest_loop():
    """Background watcher: every ~90s, if auto-harvest is on and we're NOT in a
    match (so it never costs in-game FPS), import any new recordings into the YOLO
    dataset. Best-effort; failures never disturb the app."""
    while True:
        try:
            time.sleep(90)
            if not get_setting("autoHarvest", True) or fragroute_dataset is None:
                continue
            # DEV/OWNER ONLY: labeling + training the detector is the owner's job;
            # a consumer never harvests. Gated on the admin entitlement ("train").
            if fragroute_license is not None and not fragroute_license.is_enabled("train"):
                continue
            # only when idle/menu -- harvesting does ffmpeg + CPU detection work
            if AUTODETECT.get("phase") == "match":
                continue
            r = fragroute_dataset.auto_harvest(folders=_harvest_folders())
            if r.get("newFrames"):
                diag("ai", True, msg="auto-harvest +%d frames from %d clip(s)"
                     % (r.get("newFrames", 0), r.get("newVideos", 0)))
        except Exception as e:
            try:
                diag("ai", False, msg="auto-harvest", exc=e)
            except Exception:
                pass


# --- AI Coach footage automation (Phase 3) ---------------------------------
# These wire the recorder to the app's existing (network-based, reliable) match
# detection and the global save-clip hotkey. All best-effort: a capture hiccup
# must never disturb gameplay or the autodetector.
def capture_auto_start(reason="match"):
    """Arm the rolling recorder at match start (only if autoRecord is enabled)."""
    if fragroute_capture is None:
        return
    try:
        if not get_setting("autoRecord", False):
            diag("capture", True, msg="auto-record off; skipped %s" % reason)
            return
        if fragroute_capture.is_recording():
            return
        _prune_recordings()   # free disk headroom BEFORE a (potentially large) full-match record
        # full-match recording => unlimited segments (ring_segments=0, no rolling
        # overwrite) so the WHOLE game is kept; else the rolling highlight buffer.
        full = get_setting("fullMatchRecording", True)
        opts = {"ring_segments": 0} if full else {}
        r = fragroute_capture.start(_captures_dir(), opts)
        diag("capture", bool(r.get("ok")), msg="auto-start: %s" % r.get("message", ""))
        if r.get("ok"):
            _notify("Recording started", ("Recording this full %s." if full else "Rolling buffer for this %s.") % reason)
    except Exception as e:
        diag("capture", False, msg="auto-start", exc=e)


def _clip_seconds():
    """Saved-clip length, never below 60s (the user's floor)."""
    try:
        return max(60, int(get_setting("clipSeconds", 60)))
    except Exception:
        return 60


def _prune_recordings():
    """Disk-sensitive auto-cleanup: keep recordings under the GB cap (default 40)
    and leave the disk some headroom, deleting OLDEST first. Best effort; logged."""
    if fragroute_capture is None:
        return
    try:
        max_gb = float(get_setting("recordingsMaxGB", 40))
        min_free = float(get_setting("recordingsMinFreeGB", 5))
        r = fragroute_capture.prune_recordings(_captures_dir(), max_gb=max_gb, min_free_gb=min_free)
        if r.get("deleted"):
            diag("capture", True, msg="auto-cleanup: removed %d old recording(s), freed %dMB "
                 "(now %.1fGB / %.0fGB cap)" % (r["deleted"], r.get("freedMB", 0), r.get("usedGB", 0), max_gb))
    except Exception:
        pass


def _label_pool_add(image_path, prefix="scan"):
    """ADMIN/OWNER only: also drop a screen capture into the YOLO labeling pool so
    you can label it later. Consumers never train, so this is gated on admin."""
    if fragroute_dataset is None or not image_path:
        return
    try:
        if fragroute_license is not None and not fragroute_license.is_enabled("train"):
            return
        fragroute_dataset.add_image(image_path, prefix)
    except Exception:
        pass


def capture_match_end(match_dur=None):
    """At match end: save the recording (gated). In FULL-MATCH mode we stop the
    recorder and save the WHOLE game; in highlight mode we keep the rolling buffer
    running and save the last 60s.

    Logs the outcome (success AND failure) so a missed recording is never silent,
    and skips sub-45s 'matches' (a login/menu blip) so the login screen is never
    saved as a recording (and a flap never stops a real ongoing full recording)."""
    if fragroute_capture is None:
        return
    try:
        full = get_setting("fullMatchRecording", True)
        recording = fragroute_capture.is_recording()
        # FULL-MATCH: even if the recorder process died mid-match, SALVAGE whatever
        # segments were written (don't lose the game). Highlight mode needs a live
        # buffer, so it still bails if not recording.
        if not recording and not (full and fragroute_capture.has_footage(_captures_dir())):
            diag("capture", True, msg="match-end: not recording; nothing to save")
            return
        if not get_setting("autoClipMatchEnd", True):
            diag("capture", True, msg="match-end: auto-save OFF; skipped")
            return
        if match_dur is not None and match_dur < 45:
            diag("capture", True, msg="match-end: %ss too short (flap/login) -- keep recording" % match_dur)
            return
        if full:
            if recording:
                fragroute_capture.stop()                   # finalize the segments first
            r = fragroute_capture.save_full(_captures_dir(), "match")
            kind = "full match"
        else:
            r = fragroute_capture.save_clip(_captures_dir(), _clip_seconds(), "matchend")
            kind = "match clip"
        if r.get("ok"):
            diag("capture", True, msg="%s SAVED: %s" % (kind, r.get("name", "")))
            _notify("Match recording saved", r.get("message") or r.get("name", ""))
            _prune_recordings()   # keep under the GB cap after a new recording lands
        else:
            diag("capture", False, msg="match-end save FAILED: %s" % r.get("message", "?"))
    except Exception as e:
        diag("capture", False, msg="match-end", exc=e)


def capture_game_closed():
    """Stop the recorder once FragPunk has actually exited (not on match flaps)."""
    if fragroute_capture is None:
        return
    try:
        if fragroute_capture.is_recording():
            # Save BEFORE stopping. Covers the common "I played then closed FragPunk
            # straight from a match" case, where no clean match->menu transition fired.
            if get_setting("autoClipMatchEnd", True):
                try:
                    if get_setting("fullMatchRecording", True):
                        fragroute_capture.stop()           # finalize segments first
                        r = fragroute_capture.save_full(_captures_dir(), "match")
                    else:
                        r = fragroute_capture.save_clip(_captures_dir(), _clip_seconds(), "gameclose")
                    diag("capture", bool(r.get("ok")),
                         msg="game-close save: %s" % (r.get("name") or r.get("message", "")))
                except Exception:
                    pass
            fragroute_capture.stop()
            diag("capture", True, msg="stopped (game closed)")
    except Exception:
        pass


def hotkey_save_clip():
    """Save-clip hotkey: clip the last N seconds if recording, else arm first."""
    if fragroute_capture is None:
        return
    try:
        if fragroute_capture.is_recording():
            r = fragroute_capture.save_clip(_captures_dir(), _clip_seconds(), "hotkey")
            _notify("Clip saved" if r.get("ok") else "Clip not saved",
                    r.get("name") or r.get("message", ""))
        else:
            r = fragroute_capture.start(_captures_dir(), {})
            _notify("Recording armed" if r.get("ok") else "Recorder error",
                    "Press again after a fight to save a clip." if r.get("ok")
                    else r.get("message", ""))
    except Exception:
        pass


# --- AI VISION: recognition + map captures (uses the VL model + your catalogs) ---
def _maps_dir():
    try:
        return STATE["configs_dir"].parent / "fragroute_maps"
    except Exception:
        return Path.cwd() / "fragroute_maps"


def _maps_store():
    p = _maps_dir().parent / "fragroute_maps.json"
    try:
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {"captures": []}


def _save_maps(d):
    p = _maps_dir().parent / "fragroute_maps.json"
    try:
        tmp = str(p) + ".tmp"
        Path(tmp).write_text(json.dumps(d, indent=2), encoding="utf-8")
        Path(tmp).replace(p)
    except Exception:
        pass


def _known_names():
    """Weapon + Lancer names from the app's catalogs, to GROUND the vision model
    (so it picks from the real FragPunk set instead of guessing)."""
    weapons, lancers = [], []
    try:
        for _cat, names in (load_catalog("weapons").get("weapons", {}) or {}).items():
            weapons += list(names)
    except Exception:
        pass
    try:
        lancers = list((load_catalog("lancers").get("lancers", {}) or {}).keys())
    except Exception:
        pass
    return weapons, lancers


# Tuned against real FragPunk frames: the model can't match FragPunk-specific
# WEAPON NAMES (never seen them), so we ask for weapon TYPE (judged by shape). And
# we teach it the HUD LAYOUT so it stops confusing currency for health and minimap
# callouts for enemies. Anti-hallucination: only report what's clearly visible.
_RECOG_PROMPT = (
    "This is a FragPunk (5v5 hero shooter) gameplay frame. Report ONLY what you can "
    "CLEARLY see -- never invent names, players, or labels. FragPunk HUD layout:\n"
    "- TOP-CENTER: the two team round scores and the round timer.\n"
    "- TOP-LEFT: a minimap. Any text there (e.g. 'A Long', 'B Short') are MAP CALLOUTS, "
    "NOT players or enemies.\n"
    "- TOP-RIGHT: the team rosters (Lancer hero portraits).\n"
    "- RIGHT side: your HEALTH (a large number, usually up to 150).\n"
    "- BOTTOM-RIGHT: your AMMO, shown as current/reserve (e.g. 5/10).\n"
    "- BOTTOM-LEFT: your rank emblem and shard currency (a small number with a '+').\n"
    "- BOTTOM-CENTER: your ability icons; your weapon is held in your hands (center-low).\n"
    "STRICT RULE: NEVER report minimap labels, callouts (like 'A Long', 'B Short', "
    "'A Site'), usernames, or kill-feed names as enemies. An ENEMY is ONLY a clearly "
    "rendered 3D character model standing in the game world in front of you.\n"
    "Report briefly:\n"
    "1. WEAPON TYPE in your hands -- pistol, SMG, assault rifle, marksman, sniper "
    "(has a long barrel/scope), shotgun, LMG, or melee -- judged by its SHAPE only.\n"
    "2. Is a real enemy character visible in the play area? (yes + roughly where, or no).\n"
    "3. Health and ammo, only if clearly readable.\n"
    "4. The map's visual style (e.g. industrial, temple, urban).\n"
    "5. Any kill-feed text or notifications.\n"
    "If unsure about something, say 'unclear' rather than guessing.")


def recognize_screen(image_path=None):
    """Capture the screen (or use a given image) and have the vision model identify
    FragPunk weapon type / enemies / map / HUD. The grab is saved into the Maps
    gallery so the user always SEES the capture, even if the vision model is slow/
    unavailable; the model's error (if any) is surfaced instead of a silent blank."""
    if fragroute_llm is None:
        return {"ok": False, "message": "vision model unavailable"}
    saved_name = None
    if image_path is None:
        d = _maps_dir()
        d.mkdir(parents=True, exist_ok=True)
        shot = d / ("scan_%s.png" % time.strftime("%Y%m%d_%H%M%S", time.localtime()))
        if fragroute_capture is None or not fragroute_capture.capture_screenshot(str(shot)):
            return {"ok": False, "message": "couldn't capture the screen (fullscreen-exclusive? try Borderless)"}
        image_path = str(shot)
        saved_name = shot.name
        _label_pool_add(image_path, "scan")   # admin-only: feed the labeling pool
    # speed: 640px + a tight token cap keeps a "what's on screen" read fast (~1-2s
    # warm). The VLM is a snapshot analyst, not a per-frame real-time tracker.
    txt = fragroute_llm.chat_vision(_RECOG_PROMPT, image_path, maxdim=640, max_tokens=300)
    if saved_name:                     # show the capture in the gallery either way
        try:
            store = _maps_store()
            store.setdefault("captures", []).insert(0, {
                "image": saved_name, "notes": txt or "", "ts": int(time.time() * 1000)})
            store["captures"] = store["captures"][:80]
            _save_maps(store)
        except Exception:
            pass
    err = None
    if not txt:
        try:
            err = (fragroute_llm.vision_status() or {}).get("error")
        except Exception:
            err = None
    diag("ai", bool(txt), msg="recognize: %s" % ("ok" if txt else "no model output: %s" % (err or "?")))
    return {"ok": bool(txt), "image": saved_name, "visionError": err,
            "reply": txt or ("Captured your screen, but the vision model returned nothing"
                             "%s." % (" (%s)" % err if err else ""))}


def _gameplay_capture_ready():
    """Can we actually capture GAMEPLAY right now -- not the Windows desktop, the
    taskbar, or this app? Returns (ok, reason).

    A recording IS ready: the ring buffer holds real game frames. A LIVE grab only
    sees the game when FragPunk is the FOREGROUND window -- a fullscreen-exclusive
    game is invisible from any other window, so grabbing while you're alt-tabbed to
    Fragnetic just captures your desktop (the reported bug). In that case we bail
    with guidance instead of saving a junk desktop shot to the gallery/label pool."""
    try:
        if fragroute_capture is not None and fragroute_capture.is_recording():
            return True, "buffer"
    except Exception:
        pass
    try:
        gp = game_proc_status()
    except Exception:
        gp = {"running": False, "foreground": False}
    if not gp.get("running"):
        return False, "FragPunk isn't running, so there's no gameplay to look at yet."
    if not gp.get("foreground"):
        return False, ("FragPunk is running but not in focus — from here I'd only capture your "
                       "desktop. Use the in-game Scout hotkey (it works while you're in the match), "
                       "or turn on Auto-record and I'll grab a live gameplay frame from the buffer.")
    return True, "foreground"


def capture_map():
    """Snap the current screen and have the vision model describe the map area --
    sightlines, angles, cover/corners, and a position to hold. Stored in a gallery
    so you build a picture of every map over time."""
    if fragroute_llm is None or fragroute_capture is None:
        return {"ok": False, "message": "vision/capture unavailable"}
    ready, why = _gameplay_capture_ready()
    if not ready:
        return {"ok": False, "message": why}
    d = _maps_dir()
    d.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S", time.localtime())
    shot = d / ("map_%s.png" % stamp)
    if not fragroute_capture.capture_screenshot(str(shot)):
        return {"ok": False, "message": "couldn't capture the screen"}
    _label_pool_add(str(shot), "map")   # admin-only: feed the labeling pool
    prompt = ("This is a FragPunk match screenshot. If a map name is visible, state it "
              "first. Then describe this area of the map: key sightlines, angles, "
              "cover/corners, and one strong position or peek to hold here. Be concise.")
    txt = fragroute_llm.chat_vision(prompt, str(shot))
    store = _maps_store()
    store.setdefault("captures", []).insert(0, {
        "image": shot.name, "notes": txt or "", "ts": int(time.time() * 1000)})
    store["captures"] = store["captures"][:80]
    _save_maps(store)
    diag("ai", bool(txt), msg="map-capture")
    return {"ok": bool(txt), "reply": txt or "captured (vision couldn't describe it)",
            "image": shot.name}


SCOUT_STATE = {"text": "", "ts": 0}


def _speak(text):
    """Speak text from the BACKEND (works in-game when the UI is paused/hidden).
    Prefers the neural Piper voice (soothing/natural); falls back to Windows SAPI.
    Fire-and-forget."""
    if OS != "Windows" or not text:
        return

    def _go():
        # 1) neural Piper voice (the good one)
        try:
            if fragroute_tts is not None and fragroute_tts.available():
                rate = None
                try:
                    rate = float(get_setting("ttsRate", 1.0)) or None
                except Exception:
                    rate = None
                if fragroute_tts.speak(str(text)[:600], get_setting("ttsVoice", None), rate):
                    return
        except Exception:
            pass
        # 2) fallback: Windows SAPI (David/Zira)
        try:
            safe = str(text).replace('"', " ").replace("'", " ").replace("`", " ")[:400]
            ps = ("Add-Type -AssemblyName System.Speech; "
                  "(New-Object System.Speech.Synthesis.SpeechSynthesizer).Speak('%s')" % safe.replace("'", " "))
            subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                           timeout=30, **_NO_WINDOW_KW)
        except Exception:
            pass
    threading.Thread(target=_go, daemon=True).start()


def scout_voice():
    """Hotkey scout: grab the current frame, get a SHORT vision read, and speak it.
    Honest limits: ~10s latency (vision), and it only sees what's on screen -- it
    reads visible enemies + your weapon + the map position/angle, not through walls."""
    if fragroute_llm is None or fragroute_capture is None:
        _speak("Scout isn't available.")
        return
    img = str(Path(tempfile.gettempdir()) / "fragroute_scout.png")
    if not fragroute_capture.capture_screenshot(img):
        _speak("Could not capture the screen.")
        return
    prompt = ("This is a FragPunk gameplay frame. Give ONE short spoken voice callout "
              "(under 22 words). Say: my weapon TYPE (pistol/SMG/rifle/sniper/shotgun/"
              "LMG/melee, by shape), whether an enemy is visible on screen, and which "
              "angle or area to watch. Only mention what you clearly see; no guessing.")
    try:
        # small image = ~0.5s warm callout (scout favors speed over exact text)
        txt = fragroute_llm.chat_vision(prompt, img, max_tokens=70, maxdim=512)
    except Exception:
        txt = None
    SCOUT_STATE.update(text=txt or "", ts=int(time.time() * 1000))
    if txt:
        _speak(txt)
        diag("ai", True, msg="scout")
    else:
        _speak("Couldn't read the screen.")


def _auto_map_shot():
    """Auto: grab ONE screenshot of actual GAMEPLAY (not the shard-card pick) so the
    Maps gallery fills in over time. FragPunk opens every round on the card-select
    screen, so we wait past it and -- if the detector is available -- only KEEP a
    frame that looks like gameplay (HUD visible), retrying if we still see cards."""
    if fragroute_capture is None:
        return
    try:
        d = _maps_dir()
        d.mkdir(parents=True, exist_ok=True)
        gameplay_cues = {"minimap", "crosshair", "ammo counter", "health bar", "killfeed"}
        have_det = (fragroute_yolo is not None and fragroute_yolo.available())
        last = None
        for attempt in range(6):
            time.sleep(20 if attempt == 0 else 12)   # ~20s past the card pick, then +12s per retry
            # don't grab the desktop/taskbar: only shoot when we can actually see
            # gameplay (recording buffer, or FragPunk is the foreground window).
            ready, _why = _gameplay_capture_ready()
            if not ready:
                continue
            shot = d / ("map_%s.png" % time.strftime("%Y%m%d_%H%M%S", time.localtime()))
            if not fragroute_capture.capture_screenshot(str(shot)):
                continue
            last = shot
            is_cards = False
            if have_det:
                try:
                    labels = {x.get("label") for x in fragroute_yolo.detect_image(str(shot), conf_thr=0.35)}
                    # clearly the card screen: shard/ability picks and NO gameplay HUD
                    is_cards = bool(labels & {"shard perk", "ability icon"}) and not (labels & gameplay_cues)
                except Exception:
                    is_cards = False
            if is_cards and attempt < 5:
                try:
                    shot.unlink()          # still on cards -> discard and wait for gameplay
                except Exception:
                    pass
                last = None
                continue
            store = _maps_store()          # keep it (gameplay / ambiguous / final try)
            store.setdefault("captures", []).insert(0, {
                "image": shot.name, "notes": "", "ts": int(time.time() * 1000)})
            store["captures"] = store["captures"][:80]
            _save_maps(store)
            _label_pool_add(str(shot), "map")   # admin-only: feed the labeling pool
            diag("ai", True, msg="auto map shot (attempt %d)" % (attempt + 1))
            return
        diag("ai", bool(last), msg="auto map shot: best-effort" if last else "no gameplay frame")
    except Exception as e:
        diag("ai", False, msg="auto map shot", exc=e)


# --- live practice detector plumbing (frame + fail-safe mode source) ----------
_LIVE_MODE_CACHE = {"text": "", "ts": 0.0}


def _live_frame():
    """Capture ONE frame for the live detector. Returns a path or None. Uses the
    same DXGI capture as the recorder (no game hook); pulls from the ring buffer
    when recording so it never spins a second capture client."""
    if fragroute_capture is None:
        return None
    p = str(Path(tempfile.gettempdir()) / "fragroute_live.png")
    try:
        return p if fragroute_capture.capture_screenshot(p) else None
    except Exception:
        return None


def _live_mode_str():
    """Best-effort CURRENT mode string for the live detector's safety gate. Combines
    the engine's training flag + known mode key + a cached OCR read of the mode pill
    (refreshed every 3s). Garbled/empty -> the gate treats it as BLOCKED (fail-safe),
    so the detector simply won't run rather than risk running in real PvP."""
    parts = []
    try:
        if AUTODETECT.get("matchIsTraining"):
            parts.append("training")
        mm = AUTODETECT.get("matchMode")
        if mm and mm != "unknown":
            parts.append(str(mm).replace("_", " "))
    except Exception:
        pass
    now = time.time()
    if now - _LIVE_MODE_CACHE["ts"] > 3:
        _LIVE_MODE_CACHE["ts"] = now
        try:
            gm = read_game_mode() or {}
            _LIVE_MODE_CACHE["text"] = gm.get("raw") or ""
        except Exception:
            _LIVE_MODE_CACHE["text"] = ""
    if _LIVE_MODE_CACHE["text"]:
        parts.append(_LIVE_MODE_CACHE["text"])
    return " | ".join(parts)


def _build_ai_ctx():
    """Assemble the coach context (data accessors + LLM + agentic action executors).
    Shared by the chat endpoint AND voice commands so both have identical powers."""
    ctx = {
        "regions": REGIONS,
        "region_best_latency": region_best_latency,
        "load_log": load_log,
        "game_status": game_status,
        "replay_library": replay_library,
        "session_start_ts": lambda: AUTODETECT.get("sessionStartTs"),
        "cards": load_catalog("cards"),     # shard-card catalog for grounded card advice
    }
    if fragroute_learning is not None:
        ctx["mode_profile"] = fragroute_learning.profile
        ctx["learning_summary"] = fragroute_learning.summary
        ctx["search_facts"] = fragroute_learning.search_facts
    if fragroute_llm is not None:
        try:
            # Base the model choice on whether FragPunk is RUNNING, not "foreground".
            # A fullscreen-exclusive game stops being "foreground" the instant you
            # alt-tab to chat -- so the old check loaded the big 14B on the 4070 (your
            # game GPU) and tanked FPS when you tabbed back. While the game runs at all,
            # use the small model on the 1650S and keep the 4070 free.
            gp = game_proc_status()
            running = bool(gp.get("running"))
            fragroute_llm.set_prefer_fast(running)
            if running:
                try:
                    fragroute_llm.release_for_game()   # unload any 14B still on the 4070
                except Exception:
                    pass
        except Exception:
            pass
        ctx["llm"] = fragroute_llm.chat
        ctx["llm_available"] = fragroute_llm.available

    def _connect_best():
        rows = [(region_best_latency(r["id"]), r["id"]) for r in REGIONS]
        rows = [(ms, rid) for ms, rid in rows if ms is not None]
        if not rows:
            return {"ok": False, "message": "no ping data yet -- open the main tab first"}
        rows.sort()
        return connect_region(rows[0][1])
    acts = {"connect_best": _connect_best, "disconnect": disconnect}
    if fragroute_capture is not None:
        acts["start_recording"] = lambda: fragroute_capture.start(_captures_dir(), {})
        acts["stop_recording"] = fragroute_capture.stop
        acts["save_clip"] = lambda: fragroute_capture.save_clip(_captures_dir(), _clip_seconds(), "ai")
    if fragroute_knowledge is not None:
        acts["refresh_knowledge"] = lambda: fragroute_knowledge.refresh(force=True)
    if fragroute_imagegen is not None:
        def _gen_image(prompt):
            if not fragroute_imagegen.available():
                return {"ok": False, "message": "the image model isn't installed yet"}
            threading.Thread(target=lambda: fragroute_imagegen.generate(prompt), daemon=True).start()
            return {"ok": True}
        ctx["gen_image"] = _gen_image

    def _live_state():
        s = dict(LIVE_STATE)
        if not s.get("inMatch"):
            return {"ok": True, "message": "you're not in a match right now."}
        mode = s.get("mode") if s.get("mode") not in (None, "unknown") else "a match"
        el = int(time.time() - s["since"]) if s.get("since") else 0
        return {"ok": True, "message": "you're in %s, ~%dm in." % (mode, el // 60)}
    acts["live_state"] = _live_state

    def _analyze_clip():
        if fragroute_llm is None or fragroute_capture is None:
            return {"ok": False, "message": "vision/recorder unavailable"}
        clips = (fragroute_capture.list_clips(_captures_dir()) or {}).get("items", [])
        if not clips:
            return {"ok": False, "message": "no clips recorded yet -- play a match first"}
        frames = fragroute_capture.extract_frames(clips[0].get("path"), tempfile.gettempdir())
        if not frames:
            return {"ok": False, "message": "couldn't read frames from the clip"}
        prompt = ("These are frames across a FragPunk clip in order. Briefly review the "
                  "player's crosshair placement and positioning, with one tip.")
        txt = (fragroute_llm.chat_vision_multi(prompt, frames) if len(frames) > 1
               else fragroute_llm.chat_vision(prompt, frames[0]))
        return {"ok": bool(txt), "message": txt or "the vision model couldn't read that clip."}
    acts["analyze_clip"] = _analyze_clip

    def _detect_clip():
        """OFFLINE object detection over the latest recorded clip (YOLOX/ONNX).
        Never touches a live match -- it reads a saved clip's frames."""
        if fragroute_yolo is None or fragroute_capture is None:
            return {"ok": False, "message": "offline detector/recorder unavailable"}
        if not fragroute_yolo.available():
            return {"ok": False, "message": "offline detector not set up yet -- add a "
                    "YOLOX .onnx to the 'yolo' folder and install onnxruntime."}
        clips = (fragroute_capture.list_clips(_captures_dir()) or {}).get("items", [])
        if not clips:
            return {"ok": False, "message": "no clips recorded yet -- play a match first"}
        frames = fragroute_capture.extract_frames(clips[0].get("path"), tempfile.gettempdir())
        if not frames:
            return {"ok": False, "message": "couldn't read frames from the clip"}
        r = fragroute_yolo.analyze_frames(frames)
        if not r.get("ok"):
            return {"ok": False, "message": r.get("message", "detection failed")}
        counts = r.get("labelCounts") or {}
        if counts:
            top = sorted(counts.items(), key=lambda kv: kv[1], reverse=True)[:6]
            detail = ", ".join("%s x%d" % (k, v) for k, v in top)
            msg = ("Reviewed %d frames (%s). Detected: %s. Peak %d objects in a frame."
                   % (r.get("frames", 0), r.get("provider", "cpu"), detail, r.get("peakObjects", 0)))
        else:
            msg = ("Reviewed %d frames but the model detected nothing -- it likely "
                   "needs a FragPunk-trained YOLOX model (the default is generic)."
                   % r.get("frames", 0))
        return {"ok": True, "message": msg}
    acts["detect_clip"] = _detect_clip

    def _make_montage():
        """Stitch your recent clips into a highlight montage (video-only clips get a
        silent track; clips that have audio keep it)."""
        if fragroute_video is None or fragroute_capture is None:
            return {"ok": False, "message": "video editor/recorder unavailable"}
        clips = (fragroute_capture.list_clips(_captures_dir()) or {}).get("items", [])
        paths = [c.get("path") for c in clips[:8] if c.get("path")]
        if len(paths) < 2:
            return {"ok": False, "message": "need at least 2 clips to make a montage -- record/clip a few first"}
        r = fragroute_video.montage(list(reversed(paths)), title=f"{APP_NAME} Highlights")
        if r.get("ok"):
            return {"ok": True, "message": "Montage ready: %s (%d clips) -- in the Footage/Edited gallery." % (r.get("name"), r.get("clips", 0))}
        return {"ok": False, "message": r.get("message", "montage failed")}
    acts["make_montage"] = _make_montage

    def _make_highlights():
        """Auto-highlight: scan the latest recording with the detector for action
        moments (kills/enemies), cut a clip around each, and montage them. Offline
        (post-match) so no in-game cost. Best-effort -- quality scales with the model."""
        if not (fragroute_video and fragroute_yolo and fragroute_capture):
            return {"ok": False, "message": "video/detector/recorder unavailable"}
        if not fragroute_yolo.available():
            return {"ok": False, "message": "auto-highlights need the trained detector (no model yet)"}
        clips = (fragroute_capture.list_clips(_captures_dir()) or {}).get("items", [])
        if not clips:
            return {"ok": False, "message": "no recordings to scan -- record a match first"}
        src = clips[0].get("path")
        frames = fragroute_video.frames_timed(src, fps=1.0, max_frames=240)
        if not frames:
            return {"ok": False, "message": "couldn't read the recording"}
        # score each sampled second by action. Weight the clear action cues, but
        # give ANY detection some weight so a still-improving model isn't useless.
        _w = {"enemy": 2.0, "enemy head": 2.0, "downed enemy": 3.0,
              "killfeed": 4.0, "dropped weapon": 1.0}
        scored = []
        for t, fp in frames:
            try:
                dets = fragroute_yolo.detect_image(fp, conf_thr=0.35)
            except Exception:
                dets = []
            score = sum(_w.get(d.get("label"), 0.4) for d in dets)
            scored.append((t, score))
        # pick peak moments (merge ones within 5s, cap 8)
        moments = []
        for t, s in sorted(scored, key=lambda x: -x[1]):
            if s < 1.5:
                break
            if all(abs(t - m) >= 5 for m in moments):
                moments.append(t)
            if len(moments) >= 8:
                break
        sampled = False
        # FALLBACK: the detector found little/no action -> evenly sample the
        # recording so you ALWAYS get a highlight reel instead of an error.
        if len(moments) < 2 and frames:
            ts = [t for t, _ in frames]
            n = min(6, max(2, len(ts) // 8))
            step = max(1, len(ts) // n)
            moments = sorted({ts[i] for i in range(0, len(ts), step)})[:n]
            sampled = True
        moments.sort()
        if not moments:
            return {"ok": False, "message": "couldn't read the recording to make highlights"}
        cuts = []
        for t in moments:
            r = fragroute_video.trim(src, max(0.0, t - 3.0), 6.0)
            if r.get("ok"):
                cuts.append(r["file"])
        if len(cuts) < 1:
            return {"ok": False, "message": "couldn't cut highlight clips"}
        if len(cuts) == 1:
            return {"ok": True, "message": "1 action moment -> %s (in Edited)." % os.path.basename(cuts[0])}
        r = fragroute_video.montage(cuts, title=f"{APP_NAME} Highlights")
        if r.get("ok"):
            note = " (evenly sampled -- the detector found little action; label more to improve)" if sampled else ""
            return {"ok": True, "message": "Auto-highlights: %d moments stitched -> %s%s (Video tab)." % (len(cuts), r.get("name"), note)}
        return {"ok": False, "message": r.get("message", "montage failed")}
    acts["make_highlights"] = _make_highlights

    def _aim_review():
        """Aim analysis from your latest clip: in an FPS the crosshair is always at
        screen CENTER, so we measure -- when an enemy is on screen -- how often the
        center is ON the enemy (on-target %) and the average center->enemy distance.
        Offline; needs the trained detector."""
        if not (fragroute_video and fragroute_yolo and fragroute_capture):
            return {"ok": False, "message": "video/detector/recorder unavailable"}
        if not fragroute_yolo.available():
            return {"ok": False, "message": "aim review needs the trained detector (no model yet)"}
        clips = (fragroute_capture.list_clips(_captures_dir()) or {}).get("items", [])
        if not clips:
            return {"ok": False, "message": "no clips to analyze -- record/clip a match first"}
        frames = fragroute_video.frames_timed(clips[0].get("path"), fps=2.0, max_frames=300)
        if not frames:
            return {"ok": False, "message": "couldn't read the clip"}
        try:
            from PIL import Image
            with Image.open(frames[0][1]) as im:
                W, H = im.size
        except Exception:
            W, H = 1920, 1080
        cx, cy = W / 2.0, H / 2.0
        enemy_frames = on_target = 0
        dists = []
        for _t, fp in frames:
            try:
                dets = fragroute_yolo.detect_image(fp, conf_thr=0.35)
            except Exception:
                dets = []
            enemies = [d for d in dets if d.get("label") in ("enemy", "enemy head")]
            if not enemies:
                continue
            enemy_frames += 1
            hit, best = False, 1e9
            for e in enemies:
                x1, y1, x2, y2 = e["box"]
                if x1 <= cx <= x2 and y1 <= cy <= y2:
                    hit = True
                ex, ey = (x1 + x2) / 2.0, (y1 + y2) / 2.0
                best = min(best, ((cx - ex) ** 2 + (cy - ey) ** 2) ** 0.5)
            if hit:
                on_target += 1
            dists.append(best)
        if enemy_frames < 3:
            return {"ok": False, "message": "not enough frames with a visible enemy in that clip to analyze aim"}
        pct = 100.0 * on_target / enemy_frames
        avg_off = 100.0 * (sum(dists) / len(dists)) / W if dists else 0
        base = ("Aim review (latest clip): enemy on screen in %d sampled frames. Crosshair ON an "
                "enemy %.0f%% of those; avg %.0f%% of screen-width off-target."
                % (enemy_frames, pct, avg_off))
        tip = ""
        if fragroute_llm is not None and fragroute_llm.available():
            try:
                tip = (fragroute_llm.chat([
                    {"role": "system", "content": "You are a concise FragPunk aim coach."},
                    {"role": "user", "content": "Crosshair on an enemy %.0f%% of frames an enemy was visible, avg %.0f%% "
                     "of screen-width off. ONE short crosshair-placement tip." % (pct, avg_off)}], max_tokens=70) or "").strip()
            except Exception:
                tip = ""
        return {"ok": True, "onTargetPct": round(pct, 1), "enemyFrames": enemy_frames,
                "message": base + (("  Tip: " + tip) if tip else "")}
    acts["aim_review"] = _aim_review

    def _match_report():
        """Post-match recap: last match + today's W/L + recent rank movement + an LLM
        coaching tip. Built from the queue log, rank history, and learning store."""
        try:
            log = load_log() or []
        except Exception:
            log = []
        if not log:
            return {"ok": False, "message": "no matches logged yet -- play a match with the app open"}
        last = log[0]
        dur = int(last.get("duration", 0))
        rid = last.get("regionId")
        rname = (REGION_BY_ID.get(rid) or {}).get("name") or rid or "unknown"
        lines = ["Last match: %s, queued %d:%02d, %s." % (rname, dur // 60, dur % 60, last.get("outcome", "matched"))]
        # today's session W/L
        try:
            import datetime
            start = int(datetime.datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000)
            today = [e for e in log if int(e.get("ts", 0)) >= start]
            w = sum(1 for e in today if str(e.get("outcome", "")).lower() in ("win", "won"))
            l = sum(1 for e in today if str(e.get("outcome", "")).lower() in ("loss", "lost", "lose"))
            if today:
                rec = (" (%dW/%dL)" % (w, l)) if (w or l) else ""
                lines.append("Today: %d matches%s." % (len(today), rec))
        except Exception:
            pass
        # rank movement
        try:
            rk = rank_status()
            if rk.get("ok") and rk.get("tier"):
                rp = rk.get("rp")
                lines.append("Rank: %s%s." % (rk["tier"], (" %s RP" % rp) if rp is not None else ""))
        except Exception:
            pass
        # learning context (mode tempo / lancer)
        mode = AUTODETECT.get("matchMode")
        if mode and mode != "unknown":
            lines.append("Mode: %s." % mode.replace("_", " ").title())
        tip = ""
        if fragroute_llm is not None and fragroute_llm.available():
            try:
                tip = (fragroute_llm.chat([
                    {"role": "system", "content": "You are a concise FragPunk coach. One actionable sentence."},
                    {"role": "user", "content": "Post-match recap: " + " ".join(lines) + " Give ONE short tip for the next match."}],
                    max_tokens=70) or "").strip()
            except Exception:
                tip = ""
        return {"ok": True, "message": "\n".join(lines) + (("\nTip: " + tip) if tip else "")}
    acts["match_report"] = _match_report

    def _live_start():
        """Start the LIVE practice detector -- runs ONLY in bot/solo modes; the
        module gate refuses (and self-stops) in anything with real opponents."""
        if fragroute_live is None or fragroute_yolo is None:
            return {"ok": False, "message": "live detector unavailable"}
        if not fragroute_yolo.available():
            return {"ok": False, "message": "detector not ready -- add a YOLOX .onnx to "
                    "the 'yolo' folder (a FragPunk-trained one detects Lancers/weapons)."}
        optin = lambda: bool(get_setting("liveDetectOptIn", False))
        r = fragroute_live.start(_live_frame, _live_mode_str, optin)
        if r.get("ok") and (r.get("started") or r.get("already")):
            return {"ok": True, "message": "Live practice detector ON (mode: %s / %s). "
                    "It self-stops if the mode changes to real PvP."
                    % (r.get("mode") or "?", r.get("tier"))}
        return {"ok": False, "message": "Won't run here -- %s. Live detection is only "
                "allowed in bot/solo practice modes." % r.get("reason", "mode not allowed")}

    def _live_stop():
        if fragroute_live is None:
            return {"ok": False, "message": "live detector unavailable"}
        fragroute_live.stop("user")
        return {"ok": True, "message": "Live practice detector off."}
    acts["live_practice"] = _live_start
    acts["stop_live"] = _live_stop
    acts["recognize"] = lambda: (lambda r: {"ok": r.get("ok"), "message": r.get("reply")})(recognize_screen())
    acts["capture_map"] = lambda: (lambda r: {"ok": r.get("ok"), "message": r.get("reply")})(capture_map())
    ctx["actions"] = acts
    return ctx


def _prewarm_coach_model():
    """Warm the coach's text model BEFORE we transcribe so the load overlaps your
    recording+transcription. VOICE ALWAYS uses the FAST model (Phi on the 1650S):
    a spoken reply wants to be quick, and the 14B 'smart' model on the 4070 takes
    ~30s to load + generates slowly (that was the 'took forever'). The 14B stays for
    the typed Chat tab where depth matters and latency is fine."""
    # warm the whisper SERVER too so transcription is decode-only (fast), not a
    # model reload every turn -- key for smooth back-and-forth voice.
    if fragroute_voice is not None:
        try:
            fragroute_voice.prewarm_whisper()
        except Exception:
            pass
    if fragroute_llm is None:
        return
    try:
        fragroute_llm.set_prefer_fast(True)   # voice = fast model, always
        fragroute_llm.prewarm_text()
    except Exception:
        pass


def _voice_record(max_secs):
    """Record the mic for a turn. Prefers VAD (auto-stops the moment you finish
    talking -> a short reply comes back in ~1-2s) and falls back to a fixed window."""
    if fragroute_voice is None:
        return None
    try:                                  # honor the user's selected mic (or default)
        fragroute_voice.PREFERRED_MIC = get_setting("voiceMic", None) or None
    except Exception:
        pass
    # DEFINITIVE (live-proven on this rig): ffmpeg's dshow recorder returns NOTHING
    # in the elevated app (wav=none) even with a loud mic, while pyaudio capture WORKS
    # there (mic_probe reads a real level). So pyaudio VAD is the PRIMARY recorder --
    # it captures via pyaudio, then filters the WAV FILE through ffmpeg (file I/O
    # works; only ffmpeg's live dshow device capture fails). ffmpeg record() stays as
    # a fallback for rigs where dshow works or pyaudio is unavailable.
    if not bool(get_setting("voiceForceFfmpeg", False)):
        try:
            if getattr(fragroute_voice, "vad_available", lambda: False)():
                wav = fragroute_voice.record_vad(max_seconds=max(6, int(max_secs)))
                if wav:
                    return wav                # else fall through to the ffmpeg path
        except Exception:
            pass
    return fragroute_voice.record(max(4, int(max_secs)))


_VOICE_BUSY = {"on": False}


def voice_command():
    """Hotkey: record the mic, transcribe (whisper), run it through the coach, and
    SPEAK the answer. Lets you talk to the AI hands-free while in-game."""
    if fragroute_voice is None or fragroute_ai is None:
        _speak("Voice commands aren't set up.")
        return
    # re-entrancy guard: a held/repeated hotkey fired voice_command many times a
    # second, stacking overlapping mic captures that all failed on device contention.
    if _VOICE_BUSY["on"]:
        return
    _VOICE_BUSY["on"] = True
    try:
        _voice_command_impl()
    finally:
        _VOICE_BUSY["on"] = False


def _voice_command_impl():
    def _beep(freq, ms):
        try:
            import winsound
            winsound.Beep(freq, ms)
        except Exception:
            pass
    _beep(1000, 90); _beep(1300, 110)     # rising two-tone = "listening, talk now"
    # warm the local model NOW so its ~15s cold load overlaps your recording +
    # transcription instead of leaving you with "the model isn't loaded" silence.
    _prewarm_coach_model()
    secs = int(get_setting("voiceCmdSeconds", 5))
    wav = None
    try:
        wav = _voice_record(secs)
        text = fragroute_voice.transcribe(wav) if wav else None
    except Exception:
        text = None
    if not text:
        # CLEAR feedback so you always know it heard the key -- distinguishes a
        # mic/record fail from a "didn't catch speech" so you're never left guessing.
        _beep(400, 250)
        got_audio = bool(wav and os.path.exists(wav) and os.path.getsize(wav) > 20000)
        diag("ai", False, msg="voice: no transcript (wav=%s)" % (os.path.getsize(wav) if wav and os.path.exists(wav) else "none"))
        _speak("I didn't catch that. Press the key, wait for the two beeps, then talk."
               if got_audio else "I couldn't hear your mic. Check it's set as the default input.")
        return
    _beep(1200, 90)            # high beep = got it, thinking
    SCOUT_STATE.update(text="you said: " + text, ts=int(time.time() * 1000))
    diag("ai", True, msg="voice: " + text[:60])
    user = ((fragroute_auth.current() if fragroute_auth else {}) or {}).get("username") or "default"
    # UNIFIED CONVERSATION: this spoken turn shares history with the typed chat, and
    # shows up in the chat log.
    _convo_add("user", text, "voice")
    reply = _coach_respond(text, user, _convo_history()) or "I'm not sure."
    _convo_add("assistant", reply, "voice")
    _speak(reply)


def _coach_respond(text, user="default", history=None, fast=True):
    """Run the coach on a message + this player's adaptive persona. Returns the reply
    text or None. Shared by voice command, voice-to-voice, and chat.

    fast=True (the default, used by all VOICE paths): force the quick model and a
    short, spoken-length answer so replies come back fast. The typed Chat tab passes
    fast=False to keep the deeper 14B and longer answers."""
    if fragroute_ai is None:
        return None
    ctx = _build_ai_ctx()
    if fragroute_persona is not None:
        try:
            fragroute_persona.observe(user, text)
            ctx["persona"] = fragroute_persona.persona_prompt(user)
        except Exception:
            pass
    if fast and fragroute_llm is not None:
        try:
            fragroute_llm.set_prefer_fast(True)   # override _build_ai_ctx's game-based pick
        except Exception:
            pass
        # SPOKEN brevity (applied AFTER persona so it isn't overwritten): a short reply
        # generates faster AND is quicker to speak, so the back-and-forth feels snappy.
        # Cap tokens low and tell the coach to be terse + conversational (1-2 sentences).
        ctx["max_tokens"] = 90
        brief = ("SPOKEN MODE: reply in 1-2 short, natural sentences a coach would say "
                 "out loud. Be direct and conversational -- no lists, no headings, no "
                 "preamble. Get to the point fast.")
        ctx["persona"] = ((ctx.get("persona") + "\n\n") if ctx.get("persona") else "") + brief
    try:
        out = fragroute_ai.ai_chat(text, history, ctx)
        return (out or {}).get("reply")
    except Exception as e:
        diag("ai", False, msg="coach_respond", exc=e)
        return None


def _speak_sync(text):
    """BLOCKING speak (waits until finished) so the voice-chat loop doesn't record
    itself talking. Neural Piper voice preferred, SAPI fallback."""
    if not text:
        return
    try:
        if fragroute_tts is not None and fragroute_tts.available():
            rate = None
            try:
                rate = float(get_setting("ttsRate", 1.0)) or None
            except Exception:
                rate = None
            if fragroute_tts.speak(str(text)[:600], get_setting("ttsVoice", None), rate):
                return
    except Exception:
        pass
    try:
        safe = str(text).replace("'", " ").replace('"', " ").replace("`", " ")[:400]
        ps = ("Add-Type -AssemblyName System.Speech; "
              "(New-Object System.Speech.Synthesis.SpeechSynthesizer).Speak('%s')" % safe)
        subprocess.run(["powershell", "-NoProfile", "-Command", ps], timeout=45, **_NO_WINDOW_KW)
    except Exception:
        pass


_CONVERSE = {"on": False, "history": []}
_CONVERSE_STOP = ("stop voice", "stop chat", "stop listening", "that's all", "thats all",
                  "end chat", "end conversation", "goodbye coach", "we're done", "were done", "stop talking")

# UNIFIED CONVERSATION: one shared history for BOTH typed chat and spoken voice, so
# the coach remembers across modalities (ask by voice, follow up by typing, etc.) and
# voice turns show up in the chat log. Each turn: {role, content, via:'text'|'voice', ts}.
_CONVO = {"turns": []}
_CONVO_LOCK = threading.Lock()
_CONVO_MAX = 40


def _convo_add(role, content, via):
    if not content:
        return
    with _CONVO_LOCK:
        _CONVO["turns"].append({"role": role, "content": content, "via": via,
                                "ts": int(time.time() * 1000)})
        if len(_CONVO["turns"]) > _CONVO_MAX:
            _CONVO["turns"] = _CONVO["turns"][-_CONVO_MAX:]


def _convo_history(n=12):
    """The recent shared turns as [{role, content}] for the LLM (both modalities)."""
    with _CONVO_LOCK:
        return [{"role": t["role"], "content": t["content"]} for t in _CONVO["turns"][-n:]]


def _converse_loop():
    """VOICE-TO-VOICE: listen -> transcribe -> coach -> SPEAK (wait) -> repeat, until
    stopped. Records the MIC (not the speaker) and waits for the coach to finish
    talking before listening again, so it never hears itself."""
    user = ((fragroute_auth.current() if fragroute_auth else {}) or {}).get("username") or "default"
    _prewarm_coach_model()                    # warm the model before the first turn
    _speak_sync("Voice chat on. Talk to me whenever. Say stop when you're done.")
    idle = 0
    while _CONVERSE["on"]:
        try:
            _prewarm_coach_model()            # keep it warm (no-op once loaded)
            secs = int(get_setting("voiceCmdSeconds", 6))
            wav = _voice_record(secs) if fragroute_voice else None
            if not _CONVERSE["on"]:
                break
            text = fragroute_voice.transcribe(wav) if wav else None
            if not text or len(text.strip()) < 2:
                idle += 1
                if idle >= 20:                 # ~2 min of silence -> auto-stop
                    _speak_sync("I'll stop listening for now.")
                    break
                continue
            idle = 0
            low = text.lower().strip()
            if any(w in low for w in _CONVERSE_STOP):
                _speak_sync("Alright, voice chat off.")
                break
            SCOUT_STATE.update(text="you: " + text, ts=int(time.time() * 1000))
            diag("ai", True, msg="converse: " + text[:50])
            _convo_add("user", text, "voice")          # shared with typed chat
            reply = _coach_respond(text, user, _convo_history())
            if reply:
                _convo_add("assistant", reply, "voice")
                SCOUT_STATE.update(text="coach: " + reply[:90], ts=int(time.time() * 1000))
                _speak_sync(reply)             # blocks until spoken, THEN listen again
        except Exception as e:
            diag("ai", False, msg="converse", exc=e)
            time.sleep(1)
    _CONVERSE["on"] = False


def converse_start():
    if _CONVERSE["on"]:
        return {"ok": True, "message": "Voice chat already on.", "on": True}
    if fragroute_voice is None or not fragroute_voice.available():
        return {"ok": False, "message": "Voice needs a mic + the whisper model (see Setup).", "on": False}
    _CONVERSE.update(on=True, history=[])
    threading.Thread(target=_converse_loop, daemon=True).start()
    return {"ok": True, "message": "Voice chat on -- just talk.", "on": True}


def converse_stop():
    _CONVERSE["on"] = False
    return {"ok": True, "message": "Voice chat off.", "on": False}


APP_BUILD = "17.7"    # bump on every change; shown in the UI header so you can see what's running
APP_NAME = "Fragnetic"  # product/display name (internal files stay fragroute_* for compat)

# ===========================================================================
# DIAGNOSTICS  -- so "the app wasn't working" stops being invisible.
# Every subsystem reports OK/ERR here; errors are also appended to a capped log
# file (fragroute_diag.log) with a traceback. The Health tab + /api/health read
# this so you can SEE what's working, what failed, and whether we're stepping on
# the game. Uncaught exceptions (even in daemon threads) are funneled here too.
# ===========================================================================
DIAG_LOCK = threading.Lock()
# component -> rolling health record
DIAG = {}
DIAG_EVENTS = collections.deque(maxlen=500)   # recent (ts, lvl, comp, msg)
DIAG_START = time.time()
_DIAG_PATH = [None]

# Friendly names for the components we deliberately track (others may appear
# dynamically from API errors). Order = display order in the Health tab.
DIAG_COMPONENTS = [
    ("web",        "Web server / UI"),
    ("vpn",        "VPN Â· WireGuard"),
    ("game",       "Game detection"),
    ("autodetect", "Auto-capture (OCR)"),
    ("rank",       "Rank reader (OCR)"),
    ("serverping", "Server pings (OCR)"),
    ("route",      "Route optimizer"),
    ("news",       "News fetch"),
    ("locker",     "Locker"),
    ("browser",    "Private browser"),
    ("ai",         "AI coach / LLM"),
    ("capture",    "Footage recorder"),
    ("learning",   "Mode learning"),
    ("knowledge",  "Online knowledge"),
    ("live",       "Live match watch"),
]


def _diag_path():
    if _DIAG_PATH[0] is None:
        try:
            _DIAG_PATH[0] = STATE["configs_dir"].parent / "fragroute_diag.log"
        except Exception:
            _DIAG_PATH[0] = Path("fragroute_diag.log")
    return _DIAG_PATH[0]


def _diag_write(ts, lvl, comp, msg):
    try:
        p = _diag_path()
        try:    # keep the log from growing forever: keep ~the last 200 KB
            if p.exists() and p.stat().st_size > 512 * 1024:
                tail = p.read_text(encoding="utf-8", errors="replace")[-200_000:]
                p.write_text("...rotated...\n" + tail, encoding="utf-8")
        except Exception:
            pass
        stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))
        with open(p, "a", encoding="utf-8") as f:
            f.write(f"{stamp} [{lvl:5}] {comp}: {msg}\n")
    except Exception:
        pass


def diag(component, ok=True, msg="", exc=None):
    """Record a subsystem heartbeat. ok=True is a success ping; ok=False logs a
    failure (with traceback if `exc` is given) to memory AND the diag file."""
    ts = time.time()
    detail = str(msg or "")
    if exc is not None:
        detail = (detail + " :: " if detail else "") + f"{type(exc).__name__}: {exc}"
    try:
        with DIAG_LOCK:
            d = DIAG.get(component)
            if d is None:
                d = {"ok": True, "okCount": 0, "errCount": 0,
                     "lastOk": 0.0, "lastErr": 0.0, "lastErrMsg": "", "lastMsg": ""}
                DIAG[component] = d
            d["ok"] = bool(ok)
            if detail:
                d["lastMsg"] = detail
            if ok:
                d["okCount"] += 1
                d["lastOk"] = ts
            else:
                d["errCount"] += 1
                d["lastErr"] = ts
                d["lastErrMsg"] = detail or "error"
            DIAG_EVENTS.append({"ts": ts, "lvl": "ok" if ok else "err",
                                "comp": component, "msg": detail})
    except Exception:
        pass
    # Persist failures (and any explicitly-messaged event) to the file.
    if not ok:
        line = detail or "error"
        if exc is not None:
            try:
                line += "\n" + "".join(_traceback.format_exception(
                    type(exc), exc, exc.__traceback__)).strip()
            except Exception:
                pass
        _diag_write(ts, "ERR", component, line)
    elif msg:
        _diag_write(ts, "OK", component, detail)


def install_excepthooks():
    """Funnel otherwise-invisible crashes (main + daemon threads) into diag, so a
    background thread dying no longer just silently breaks a feature."""
    try:
        _prev = sys.excepthook
        def _hook(et, ev, tb):
            try:
                diag("uncaught", False, exc=ev)
            except Exception:
                pass
            try:
                _prev(et, ev, tb)
            except Exception:
                pass
        sys.excepthook = _hook
    except Exception:
        pass
    try:
        def _thook(args):
            try:
                diag(f"thread:{getattr(args, 'thread', None) and args.thread.name}",
                     False, exc=args.exc_value)
            except Exception:
                pass
        threading.excepthook = _thook
    except Exception:
        pass


def setup_status():
    """First-run readiness: a checklist of every component + GPU/audio checks +
    one-line guidance for anything missing. Powers the Setup tab so a new user (or
    a buyer) can see what's installed and how to fix gaps."""
    items = []

    def add(key, label, ok, detail="", fix=""):
        items.append({"key": key, "label": label, "ok": bool(ok), "detail": detail, "fix": fix})

    # GPU (NVIDIA) -- needed for fast recording (NVENC) + training
    nv = False
    try:
        r = subprocess.run(["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
                           capture_output=True, text=True, timeout=8, **_NO_WINDOW_KW)
        names = [x.strip() for x in (r.stdout or "").splitlines() if x.strip()]
        nv = bool(names)
        add("gpu", "NVIDIA GPU", nv, ", ".join(names) if nv else "",
            "" if nv else "An NVIDIA GPU enables NVENC recording + local AI on GPU.")
    except Exception:
        add("gpu", "NVIDIA GPU", False, "", "nvidia-smi not found.")

    # ffmpeg recorder (NVENC + ddagrab)
    if fragroute_capture is not None:
        try:
            pr = fragroute_capture.probe()
            add("recorder", "Recorder (ffmpeg NVENC + ddagrab)", pr.get("ok"),
                pr.get("message", ""), "" if pr.get("ok") else "Put an NVENC+ddagrab ffmpeg.exe next to the app.")
        except Exception as e:
            add("recorder", "Recorder (ffmpeg)", False, str(e))
        try:
            lb = fragroute_capture.detect_audio_loopback()
            add("audio", "Game-audio capture (loopback)", bool(lb), lb or "",
                "" if lb else "Enable 'Stereo Mix' (Sound > Recording) or install a virtual audio cable for clip audio.")
        except Exception:
            add("audio", "Game-audio capture", False)

    # local LLM (smart + fast) + vision
    if fragroute_llm is not None:
        try:
            m = fragroute_llm.find_models()
            add("llm", "Coach LLM (text models)", bool(m.get("smart") or m.get("fast")),
                "smart=%s fast=%s" % (bool(m.get("smart")), bool(m.get("fast"))),
                "" if (m.get("smart") or m.get("fast")) else "Add a GGUF text model to the llm folder.")
            add("vision", "Vision model (screen reading)", fragroute_llm.vision_available(),
                "", "" if fragroute_llm.vision_available() else "Add a *-vl-*.gguf + mmproj to the llm folder.")
        except Exception as e:
            add("llm", "Coach LLM", False, str(e))

    # image gen, detector, suggester, voice, video
    if fragroute_imagegen is not None:
        ok = fragroute_imagegen.available()
        add("imagegen", "Image generation (SDXL/SD)", ok, "",
            "" if ok else "Add a diffusion model + sd-cli to the sd folder.")
    if fragroute_yolo is not None:
        st = fragroute_yolo.status()
        add("detector", "Object detector (YOLOX)", st.get("available"), st.get("model") or "",
            "" if st.get("available") else "Add a YOLOX .onnx to the yolo folder (or train one).")
    if fragroute_embed is not None:
        add("suggester", "Label suggester (CLIP)", fragroute_embed.available(), "",
            "" if fragroute_embed.available() else "Add clip_vitb32.onnx to the clip folder.")

    # GeoIP database -- lets the Live Game tab name ANY server (incl. off-VPN /
    # non-Alibaba-LB raw match IPs), not just ones learned during play.
    try:
        geo_db = _geo_db_path()
        add("geoip", "Server locator (GeoIP DB)", bool(geo_db),
            "loaded" if geo_db else "",
            "" if geo_db else "Download 'Server locator' in Model downloads to name servers off-VPN.")
    except Exception:
        add("geoip", "Server locator (GeoIP DB)", False)
    if fragroute_voice is not None:
        try:
            # self-heal the ffmpeg path (same as the video check) so a stale/unset
            # FFMPEG never shows a false "whisper missing" when whisper is installed.
            _ff = fragroute_capture.find_ffmpeg() if fragroute_capture else None
            if _ff and not getattr(fragroute_voice, "FFMPEG", None):
                fragroute_voice.FFMPEG = _ff
            add("voice", "Voice commands (whisper)", fragroute_voice.available())
        except Exception:
            add("voice", "Voice commands", False)
    if fragroute_video is not None:
        # mirror the recorder's ffmpeg -- and self-heal fragroute_video.FFMPEG if it
        # somehow wasn't wired, so this never shows a false "needs ffmpeg".
        _ff = fragroute_capture.find_ffmpeg() if fragroute_capture else None
        if _ff and not getattr(fragroute_video, "FFMPEG", None):
            fragroute_video.FFMPEG = _ff
        vok = fragroute_video.available() or bool(_ff)
        add("video", "Video editor", vok, "",
            "" if vok else "Needs the same ffmpeg as the recorder.")

    ready = sum(1 for i in items if i["ok"])
    return {"items": items, "ready": ready, "total": len(items), "build": APP_BUILD}


def app_health_snapshot():
    """Everything the Health tab needs: per-component status, recent events, the
    app's own resource use, and whether FragPunk is running/foreground.
    (Named app_* to avoid colliding with the VPN health_snapshot.)"""
    with DIAG_LOCK:
        comps = {k: dict(v) for k, v in DIAG.items()}
        events = list(DIAG_EVENTS)[-150:]
    known = []
    for key, label in DIAG_COMPONENTS:
        rec = comps.pop(key, None)
        known.append({"key": key, "label": label, **(rec or {
            "ok": None, "okCount": 0, "errCount": 0,
            "lastOk": 0.0, "lastErr": 0.0, "lastErrMsg": "", "lastMsg": ""})})
    # any extra components that showed up dynamically (e.g. a failing API route)
    extra = [{"key": k, "label": k, **v} for k, v in sorted(comps.items())]
    overall = all((c["ok"] is not False) for c in known + extra)
    return {
        "ok": overall,
        "build": APP_BUILD,
        "uptimeSec": round(time.time() - DIAG_START, 1),
        "components": known + extra,
        "events": events,
        "proc": proc_stats(),
        "game": game_proc_status(),
        "subsystems": _subsystems_health(),
        "logPath": str(_diag_path()),
        "now": time.time(),
    }


def _subsystems_health():
    """Live status of the newer AI subsystems (queried directly, not heartbeats),
    so the Health tab shows: is the local model loaded? is the recorder ready?
    how much has it learned? Best-effort -- any failure becomes a recorded error."""
    out = {}
    if fragroute_llm is not None:
        try:
            out["ai"] = fragroute_llm.status()
        except Exception as e:
            out["ai"] = {"error": str(e)}
            diag("ai", False, msg="status", exc=e)
        try:
            out["vision"] = fragroute_llm.vision_status()
        except Exception:
            pass
    if fragroute_imagegen is not None:
        try:
            out["imagegen"] = fragroute_imagegen.status()
        except Exception:
            pass
    if fragroute_yolo is not None:
        try:
            out["yolo"] = fragroute_yolo.status()
        except Exception:
            pass
    if fragroute_dataset is not None:
        try:
            out["dataset"] = fragroute_dataset.status()
        except Exception:
            pass
    if fragroute_embed is not None:
        try:
            out["embed"] = fragroute_embed.status()
        except Exception:
            pass
    if fragroute_video is not None:
        try:
            out["video"] = fragroute_video.status()
        except Exception:
            pass
    if fragroute_live is not None:
        try:
            out["live"] = fragroute_live.status()
        except Exception:
            pass
    if fragroute_capture is not None:
        try:
            out["capture"] = fragroute_capture.status(_captures_dir())
        except Exception as e:
            out["capture"] = {"error": str(e)}
            diag("capture", False, msg="status", exc=e)
    if fragroute_learning is not None:
        try:
            s = fragroute_learning.summary()
            out["learning"] = {"totalMatches": s.get("totalMatches", 0),
                               "modes": len(s.get("modes", {})),
                               "onlineFacts": sum(m.get("onlineFacts", 0)
                                                  for m in s.get("modes", {}).values())}
        except Exception as e:
            out["learning"] = {"error": str(e)}
            diag("learning", False, msg="summary", exc=e)
    try:
        live = dict(LIVE_STATE)
        if live.get("inMatch") and live.get("since"):
            live["elapsed"] = int(time.time() - live["since"])
        out["live"] = live
    except Exception:
        pass
    return out


# ----- lightweight self / game resource probes (stdlib ctypes; no psutil) -----
_PROC_CPU = {"t": 0.0, "kernel": 0, "user": 0, "pct": None}


def _filetime_pair(ft_low_high):
    return (ft_low_high[1] << 32) | ft_low_high[0]


def proc_stats():
    """Our own CPU% (since last call) + memory + thread count. Best-effort; if
    anything is unavailable we return what we can so the panel still renders."""
    out = {"cpuPct": None, "memMB": None, "threads": None, "pid": os.getpid()}
    if OS != "Windows":
        try:
            out["threads"] = threading.active_count()
        except Exception:
            pass
        return out
    try:
        import ctypes.wintypes as wt
        k32 = ctypes.windll.kernel32
        k32.GetCurrentProcess.restype = ctypes.c_void_p
        k32.GetProcessTimes.argtypes = [ctypes.c_void_p,
                                        ctypes.POINTER(wt.FILETIME), ctypes.POINTER(wt.FILETIME),
                                        ctypes.POINTER(wt.FILETIME), ctypes.POINTER(wt.FILETIME)]
        h = k32.GetCurrentProcess()
        creation = wt.FILETIME(); exit_ = wt.FILETIME()
        kernel = wt.FILETIME(); user = wt.FILETIME()
        if k32.GetProcessTimes(h, ctypes.byref(creation), ctypes.byref(exit_),
                               ctypes.byref(kernel), ctypes.byref(user)):
            kt = (kernel.dwHighDateTime << 32) | kernel.dwLowDateTime
            ut = (user.dwHighDateTime << 32) | user.dwLowDateTime
            now = time.time()
            prev = _PROC_CPU
            if prev["t"]:
                dt = now - prev["t"]
                dcpu = ((kt - prev["kernel"]) + (ut - prev["user"])) / 1e7  # 100ns -> s
                ncpu = max(1, os.cpu_count() or 1)
                if dt > 0:
                    out["cpuPct"] = round(min(100.0, 100.0 * dcpu / (dt * ncpu)), 1)
            prev.update({"t": now, "kernel": kt, "user": ut})
    except Exception:
        pass
    try:
        class _PMC(ctypes.Structure):
            _fields_ = [("cb", ctypes.c_uint32), ("PageFaultCount", ctypes.c_uint32),
                        ("PeakWorkingSetSize", ctypes.c_size_t),
                        ("WorkingSetSize", ctypes.c_size_t),
                        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                        ("PagefileUsage", ctypes.c_size_t),
                        ("PeakPagefileUsage", ctypes.c_size_t)]
        psapi = ctypes.windll.psapi
        k32m = ctypes.windll.kernel32
        k32m.GetCurrentProcess.restype = ctypes.c_void_p
        psapi.GetProcessMemoryInfo.argtypes = [ctypes.c_void_p, ctypes.POINTER(_PMC), ctypes.c_uint32]
        pmc = _PMC(); pmc.cb = ctypes.sizeof(_PMC)
        if psapi.GetProcessMemoryInfo(k32m.GetCurrentProcess(), ctypes.byref(pmc), pmc.cb):
            out["memMB"] = round(pmc.WorkingSetSize / (1024 * 1024), 1)
    except Exception:
        pass
    try:
        out["threads"] = threading.active_count()
    except Exception:
        pass
    return out


_GAME_PROC_CACHE = {"ts": 0.0, "val": None}


def game_proc_status():
    """Is FragPunk running, and is it the FOREGROUND window? Used to confirm the
    app backs off while you're playing. Cached a few seconds because the
    process-list scan is the heaviest part of /api/health -- keep it cheap."""
    now = time.time()
    c = _GAME_PROC_CACHE
    if c["val"] is not None and (now - c["ts"]) < 4.0:
        return c["val"]
    res = {"running": False, "foreground": False, "name": None}
    if OS != "Windows":
        _GAME_PROC_CACHE.update({"ts": now, "val": res})
        return res
    try:
        import ctypes.wintypes as wt
        TH32CS_SNAPPROCESS = 0x00000002
        k32 = ctypes.windll.kernel32

        class PROCESSENTRY32(ctypes.Structure):
            _fields_ = [("dwSize", ctypes.c_uint32),
                        ("cntUsage", ctypes.c_uint32),
                        ("th32ProcessID", ctypes.c_uint32),
                        ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
                        ("th32ModuleID", ctypes.c_uint32),
                        ("cntThreads", ctypes.c_uint32),
                        ("th32ParentProcessID", ctypes.c_uint32),
                        ("pcPriClassBase", ctypes.c_long),
                        ("dwFlags", ctypes.c_uint32),
                        ("szExeFile", ctypes.c_char * 260)]
        snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        game_pids = set()
        if snap and snap != -1:
            entry = PROCESSENTRY32(); entry.dwSize = ctypes.sizeof(PROCESSENTRY32)
            ok = k32.Process32First(snap, ctypes.byref(entry))
            while ok:
                try:
                    name = entry.szExeFile.decode(errors="ignore")
                except Exception:
                    name = ""
                if name.lower().startswith("fragpunk"):
                    res["running"] = True
                    res["name"] = name
                    game_pids.add(entry.th32ProcessID)
                ok = k32.Process32Next(snap, ctypes.byref(entry))
            k32.CloseHandle(snap)
        if res["running"]:
            try:
                hwnd = ctypes.windll.user32.GetForegroundWindow()
                pid = wt.DWORD()
                ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                res["foreground"] = pid.value in game_pids
            except Exception:
                pass
    except Exception:
        pass
    _GAME_PROC_CACHE.update({"ts": now, "val": res})
    return res


# ===========================================================================
# REGION DEFINITIONS  (mirrors the browser tracker)
# ===========================================================================
# NOTE: "utc" is an approximate standard-time UTC offset per region, used only by
# the time-of-day heat heuristic (Scout). It ignores DST on purpose -- a heuristic
# doesn't need the hour pinned exactly, and this keeps us stdlib-only (zoneinfo has
# no tz database on Windows without the pip `tzdata` package, so we don't use it).
REGIONS = [
    {"id": "us-east",    "name": "US East",       "code": "NA-E",   "tz": "America/New_York",    "utc": -4,  "pool": "deep",     "hint": "US East (NY/NJ/VA)"},
    {"id": "us-central", "name": "US Central",    "code": "NA-C",   "tz": "America/Chicago",     "utc": -5,  "pool": "standard", "hint": "US Central (IL/TX)"},
    {"id": "us-west",    "name": "US West",       "code": "NA-W",   "tz": "America/Los_Angeles", "utc": -7,  "pool": "standard", "hint": "US West (CA/WA)"},
    {"id": "eu",         "name": "Europe",        "code": "EU",     "tz": "Europe/Berlin",       "utc": 2,   "pool": "deep",     "hint": "Europe (Frankfurt/NL/UK)"},
    {"id": "asia-east",  "name": "Asia East",     "code": "ASIA-E", "tz": "Asia/Tokyo",          "utc": 9,   "pool": "standard", "hint": "Asia East (JP/KR)"},
    {"id": "asia-se",    "name": "Asia SE",       "code": "ASIA-S", "tz": "Asia/Singapore",      "utc": 8,   "pool": "thin",     "hint": "Asia SE (SG/HK)"},
    {"id": "oceania",    "name": "Oceania",       "code": "OCE",    "tz": "Australia/Sydney",    "utc": 10,  "pool": "thin",     "hint": "Oceania (AU)"},
    {"id": "sa",         "name": "South America", "code": "SA",     "tz": "America/Sao_Paulo",   "utc": -3,  "pool": "thin",     "hint": "South America (BR)"},
]
REGION_BY_ID = {r["id"]: r for r in REGIONS}

# US state codes -> Fragpunk sub-region
US_STATE_REGION = {
    # East
    "NY": "us-east", "NJ": "us-east", "VA": "us-east", "GA": "us-east", "FL": "us-east",
    "MA": "us-east", "PA": "us-east", "NC": "us-east", "MI": "us-east", "OH": "us-east",
    "DC": "us-east", "MD": "us-east", "CT": "us-east",
    # Central
    "IL": "us-central", "TX": "us-central", "MO": "us-central", "MN": "us-central",
    "CO": "us-central", "KS": "us-central", "TN": "us-central", "WI": "us-central",
    # West
    "CA": "us-west", "WA": "us-west", "OR": "us-west", "NV": "us-west", "AZ": "us-west",
    "UT": "us-west", "ID": "us-west",
}
# Country codes -> Fragpunk region
COUNTRY_REGION = {
    # ---- Europe -> Fragpunk EU pool ----
    "DE": "eu", "NL": "eu", "FR": "eu", "GB": "eu", "UK": "eu", "CH": "eu", "SE": "eu",
    "ES": "eu", "IT": "eu", "PL": "eu", "IE": "eu", "FI": "eu", "AT": "eu", "BE": "eu",
    "NO": "eu", "DK": "eu", "CZ": "eu", "PT": "eu", "RO": "eu", "HU": "eu", "GR": "eu",
    "BG": "eu", "HR": "eu", "RS": "eu", "SK": "eu", "SI": "eu", "EE": "eu", "LV": "eu",
    "LT": "eu", "IS": "eu", "LU": "eu", "CY": "eu", "MT": "eu", "UA": "eu", "MD": "eu",
    "AL": "eu", "MK": "eu", "BA": "eu", "GE": "eu", "AM": "eu", "AZ": "eu",
    # Russia, Turkey, Middle East and Africa have no dedicated Fragpunk region;
    # EU is the nearest populated matchmaking pool, so route them there.
    "RU": "eu", "TR": "eu", "IL": "eu", "AE": "eu", "EG": "eu", "ZA": "eu", "NG": "eu",
    # ---- Asia East -> JP/KR/TW pool ----
    "JP": "asia-east", "KR": "asia-east", "TW": "asia-east", "CN": "asia-east",
    # ---- South/Southeast Asia -> Singapore hub (UTC+8 cluster incl. HK/Macao) ----
    "SG": "asia-se", "MY": "asia-se", "TH": "asia-se", "ID": "asia-se", "PH": "asia-se",
    "VN": "asia-se", "IN": "asia-se", "BD": "asia-se", "PK": "asia-se", "LK": "asia-se",
    "KH": "asia-se", "HK": "asia-se", "MO": "asia-se",
    # ---- Oceania ----
    "AU": "oceania", "NZ": "oceania",
    # ---- South America ----
    "BR": "sa", "AR": "sa", "CL": "sa", "CO": "sa", "PE": "sa", "UY": "sa", "EC": "sa",
    "BO": "sa", "PY": "sa", "VE": "sa",
    # ---- North America (country-level; US states handled separately above) ----
    "CA": "us-east", "MX": "us-central",
}
# Last-ditch keyword fallback for hand-named configs
KEYWORD_REGION = {
    "frankfurt": "eu", "amsterdam": "eu", "london": "eu", "paris": "eu", "berlin": "eu",
    "tokyo": "asia-east", "seoul": "asia-east",
    "singapore": "asia-se", "hongkong": "asia-se",
    "sydney": "oceania", "melbourne": "oceania",
    "saopaulo": "sa", "brazil": "sa",
    "newyork": "us-east", "virginia": "us-east", "miami": "us-east",
    "chicago": "us-central", "dallas": "us-central",
    "losangeles": "us-west", "seattle": "us-west", "sanjose": "us-west",
}

# ===========================================================================
# RUNTIME STATE
# ===========================================================================
STATE = {
    "configs_dir": None,
    "dry_run": False,
    "active_tunnel": None,     # tunnel name we brought up
    "active_region": None,     # region id of active tunnel
    "configs": {},             # region_id -> {name, path, endpoint_host, endpoint}
    "unmapped": [],            # list of {name, path} we couldn't map
    "latency": {},             # region_id -> ms (float) or None
}
LOG_PATH = None       # set in main()
SETTINGS_PATH = None  # set in main()
SERVERS_PATH = None   # set in main() -- harvested Fragpunk game-server intel
PLAYERS_PATH = None   # set in main() -- rolling Steam player-count history
RANK_PATH = None      # set in main() -- competitive rank / RP history
REPLAYS_PATH = None   # set in main() -- per-replay review notes/tags
SERVERPINGS_PATH = None  # set in main() -- OCR'd in-game per-region ping table
WEAPONSKINS_PATH = None  # set in main() -- user's weapon skin screenshots (portable, base64)
_WEAPONSKINS_LOCK = threading.Lock()
_WEAPONSKINS_CACHE = {"loaded": False, "data": {"weapons": {}}}
_LOG_LOCK = threading.Lock()  # serialize queue-log writes (monitor + HTTP)
_RANK_LOCK = threading.Lock()
_REPLAY_LOCK = threading.Lock()
_SRVPING_LOCK = threading.Lock()
_SERVERS_LOCK = threading.Lock()  # serialize server-intel writes

# Live health of the active tunnel (latency, jitter, packet loss, liveness).
# Updated by the background health monitor; read by status_snapshot().
TUNNEL_HEALTH = {
    "tunnel": None,           # which tunnel these stats describe
    "alive": None,            # True / False / None (unknown)
    "lastMs": None,           # most recent ping (ms)
    "avgMs": None,            # rolling average over the recent window
    "jitterMs": None,         # mean absolute deviation of recent pings
    "lossPct": None,          # % of recent pings that timed out
    "consecutiveFails": 0,    # ping cycles in a row with zero responses
    "history": [],            # recent ping samples (None = timeout), last ~30
    "checkedTs": None,        # ms ts of the last check
}
_HEALTH_LOCK = threading.Lock()
_HEALTH_RECONNECT = {"tried": False}  # so auto-reconnect fires once per outage

OS = platform.system()  # 'Windows' | 'Linux' | 'Darwin'

# ---------------------------------------------------------------------------
# Windows: stop every subprocess (ping, wireguard, route) from flashing a black
# console window on screen. On Windows we pass CREATE_NO_WINDOW + a hidden
# STARTUPINFO; on other OSes these are no-ops. Use _NO_WINDOW_KW everywhere we
# call subprocess.run / Popen.
# ---------------------------------------------------------------------------
if OS == "Windows":
    _CREATE_NO_WINDOW = 0x08000000  # subprocess.CREATE_NO_WINDOW (Py3.7+)
    _startupinfo = subprocess.STARTUPINFO()
    _startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    _startupinfo.wShowWindow = subprocess.SW_HIDE
    _NO_WINDOW_KW = {"creationflags": _CREATE_NO_WINDOW, "startupinfo": _startupinfo}
else:
    _NO_WINDOW_KW = {}


# ===========================================================================
# PRIVILEGE + BINARY DISCOVERY
# ===========================================================================
def is_admin():
    try:
        if OS == "Windows":
            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        return os.geteuid() == 0
    except Exception:
        return False


def lower_process_priority():
    """Drop our scheduling priority so the GAME always gets CPU first.

    This is the single biggest thing we can do to keep FRAGROUTE from costing
    in-game FPS: at BELOW_NORMAL the OS only hands us cycles the game isn't
    using, so our background pings/netstat/recompute can never preempt frame
    work. Best effort, never raises."""
    try:
        if OS == "Windows":
            # BELOW_NORMAL_PRIORITY_CLASS = 0x00004000.
            # Set argtypes/restype explicitly: without them ctypes passes the
            # GetCurrentProcess() pseudo-handle (-1) as a truncated 32-bit value
            # on 64-bit Python, so the handle is invalid and the call silently
            # fails (returns 0). c_void_p keeps the full 64-bit handle.
            k32 = ctypes.windll.kernel32
            k32.GetCurrentProcess.restype = ctypes.c_void_p
            k32.SetPriorityClass.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
            k32.SetPriorityClass.restype = ctypes.c_int
            k32.SetPriorityClass(k32.GetCurrentProcess(), 0x00004000)
        else:
            os.nice(5)
    except Exception:
        pass


def _in_match():
    """True while we're in a LIVE match -- the window when we must touch the
    network/CPU as little as possible. Reads the auto-detect phase; safe to
    call before AUTODETECT exists."""
    try:
        return AUTODETECT.get("phase") == "match"
    except Exception:
        return False


def relaunch_elevated(already_relaunched=False):
    """Re-launch this script with admin/root.

    Windows -> ShellExecuteW 'runas' verb, which pops a UAC prompt and opens a
               new elevated console; the current (non-elevated) process exits.
    Linux/macOS -> re-exec under sudo, which prompts for a password in the
               terminal and replaces the current process.

    Returns True if the caller should exit (a hand-off happened / was attempted),
    False if we're already elevated or couldn't elevate and should keep running.
    """
    if is_admin():
        return False
    if already_relaunched:
        # We asked once and still aren't elevated. Don't loop forever -- just run
        # without admin (the UI will show NO ADMIN and tunnel switching is off).
        print("Still not elevated; continuing without admin "
              "(tunnel switching disabled).")
        return False

    script = os.path.abspath(__file__)
    child_args = list(sys.argv[1:]) + ["--elevated"]

    if OS == "Windows":
        print("\nFRAGROUTE needs administrator access to switch VPN tunnels.")
        print("A Windows UAC prompt will appear -- click Yes.")
        print("(A new elevated window opens; this window will close.)")
        params = subprocess.list2cmdline([script] + child_args)
        try:
            ret = ctypes.windll.shell32.ShellExecuteW(
                None, "runas", sys.executable, params, str(SCRIPT_DIR), 1)
        except Exception as e:
            print("Could not request elevation:", e)
            return False
        if ret <= 32:  # ShellExecuteW returns <=32 on failure / user declined
            print("\nElevation was declined or failed (UAC said no).")
            print("Options: click Yes on the prompt next time, run this from an")
            print("Administrator terminal, or use --dry-run to test without admin.")
        return True
    else:
        sudo = shutil.which("sudo")
        if not sudo:
            print("Need root but 'sudo' isn't available. Re-run as root, "
                  "or use --dry-run to test without root.")
            return False
        print("\nFRAGROUTE needs root to switch VPN tunnels -- elevating with sudo.")
        try:
            os.execvp("sudo", ["sudo", sys.executable, script] + child_args)
        except Exception as e:
            print("Could not elevate via sudo:", e)
            return False
        return True  # execvp replaces this process on success; unreachable then


def _wg_stable_path():
    base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    return Path(base) / "FRAGROUTE" / "wireguard.exe"


def find_wireguard():
    """Return (kind, path). All-in-one friendly: a system install wins if
    present; otherwise a wireguard.exe shipped with the app is used (one bundled
    inside the .exe is copied to a stable per-user folder so the installed
    tunnel service keeps working across restarts)."""
    if OS != "Windows":
        wq = shutil.which("wg-quick")
        return ("wg-quick", wq) if wq else (None, None)

    # 1) canonical system install
    for c in (shutil.which("wireguard"),
              r"C:\Program Files\WireGuard\wireguard.exe",
              r"C:\Program Files (x86)\WireGuard\wireguard.exe"):
        if c and Path(c).exists():
            return ("wireguard-exe", c)

    # 2) a copy we already extracted on a previous run
    stable = _wg_stable_path()
    if stable.exists():
        return ("wireguard-exe", str(stable))

    # 3) shipped right next to the .exe (already a stable path)
    if getattr(sys, "frozen", False):
        beside = Path(sys.executable).resolve().parent / "wireguard.exe"
        if beside.exists():
            return ("wireguard-exe", str(beside))

    # 4) shipped inside the .exe (temp dir) or next to the script -> copy it to
    #    the stable folder so the installed tunnel service has a permanent path
    shipped = SCRIPT_DIR / "wireguard.exe"
    if shipped.exists():
        try:
            stable.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(shipped, stable)
            return ("wireguard-exe", str(stable))
        except Exception:
            return ("wireguard-exe", str(shipped))

    return (None, None)


def auto_install_wireguard_async():
    """Make WireGuard available without blocking startup (all-in-one).

    If a wireguard.exe is shipped with the app (bundled inside the .exe or next
    to it), copy it to a stable per-user folder so the tunnel service has a
    permanent path. No download and no system installer -- the bundled binary
    self-installs its driver the first time a tunnel comes up via
    /installtunnelservice. Runs on a daemon thread; failures are non-fatal.
    """
    def _work():
        try:
            kind, path = find_wireguard()
            STATE["wireguard_kind"] = kind
            STATE["wireguard_path"] = path
        except Exception:
            pass
    threading.Thread(target=_work, daemon=True).start()


# ===========================================================================
# CONFIG DISCOVERY + REGION MAPPING
# ===========================================================================
def map_filename_to_region(stem):
    """Infer a region id from a WireGuard config filename stem."""
    up = stem.upper()
    low = re.sub(r"[^a-z]", "", stem.lower())

    # 1) City/keyword match first -- most specific, beats bare 2-letter codes
    #    (so "my-seattle-server" -> us-west, not Malaysia)
    for kw, rid in KEYWORD_REGION.items():
        if kw in low:
            return rid
    # 2) US-XX state pattern
    m = re.search(r"\bUS[-_]?([A-Z]{2})\b", up)
    if m and m.group(1) in US_STATE_REGION:
        return US_STATE_REGION[m.group(1)]
    # 3) Country code as a token (split on any non-alphanumeric, e.g. '#')
    for token in re.split(r"[^A-Z0-9]+", up):
        if token in COUNTRY_REGION:
            return COUNTRY_REGION[token]
        if token == "US":
            return "us-east"  # plain US, no state -> deep-pool default
    return None


def parse_wg_config(path):
    """Pull Endpoint host:port out of a WireGuard .conf (best effort)."""
    endpoint = None
    try:
        for line in Path(path).read_text(errors="ignore").splitlines():
            line = line.strip()
            if line.lower().startswith("endpoint"):
                # Endpoint = 1.2.3.4:51820
                val = line.split("=", 1)[1].strip()
                endpoint = val
                break
    except Exception:
        pass
    host = endpoint.rsplit(":", 1)[0] if endpoint else None
    return endpoint, host


def discover_configs(configs_dir):
    """Scan the configs folder, group .conf files by region (multiple allowed)."""
    configs = {}          # region_id -> [entry, ...]
    unmapped = []
    d = Path(configs_dir)
    if not d.exists():
        d.mkdir(parents=True, exist_ok=True)
    for path in sorted(d.glob("*.conf")):
        stem = path.stem
        endpoint, host = parse_wg_config(path)
        rid = map_filename_to_region(stem)
        entry = {"name": stem, "path": str(path), "endpoint": endpoint, "endpoint_host": host}
        if rid:
            configs.setdefault(rid, []).append(entry)   # keep ALL, never drop
        else:
            unmapped.append(entry)
    # keep each region's servers in a stable, name-sorted order
    for rid in configs:
        configs[rid].sort(key=lambda e: e["name"].lower())
    STATE["configs"] = configs
    STATE["unmapped"] = unmapped
    return configs, unmapped


# ---- helpers for the region -> [servers] model ----
def all_config_entries():
    """Flat list of every mapped config entry across all regions."""
    out = []
    for entries in STATE["configs"].values():
        out.extend(entries)
    return out


def find_config_by_name(name):
    """Return (region_id, entry) for a config name, or (None, None)."""
    for rid, entries in STATE["configs"].items():
        for e in entries:
            if e["name"] == name:
                return rid, e
    return None, None


def region_best_config(region_id):
    """Pick the lowest-ping config in a region (fallback: first by name)."""
    entries = STATE["configs"].get(region_id) or []
    if not entries:
        return None
    def lat(e):
        ms = STATE["latency"].get(e["name"])
        return ms if ms is not None else float("inf")
    ranked = sorted(entries, key=lat)
    return ranked[0]


def region_best_latency(region_id):
    """Lowest measured latency among a region's configs (or None)."""
    entries = STATE["configs"].get(region_id) or []
    vals = [STATE["latency"].get(e["name"]) for e in entries]
    vals = [v for v in vals if v is not None]
    return min(vals) if vals else None


# ===========================================================================
# LATENCY
# ===========================================================================
# Cap how many ping subprocesses run at once. Firing ~24 pings simultaneously
# (one per config) is a CPU/network spike that can briefly steal cycles from
# the game; 6 in flight keeps a full sweep quick without the spike.
_PING_SEM = threading.Semaphore(6)


def ping_host(host, timeout_ms=1500):
    """Single ping, returns latency in ms (float) or None."""
    if not host:
        return None
    try:
        if OS == "Windows":
            cmd = ["ping", "-n", "1", "-w", str(timeout_ms), host]
        elif OS == "Darwin":
            cmd = ["ping", "-c", "1", "-W", str(timeout_ms), host]
        else:  # Linux: -W is seconds
            cmd = ["ping", "-c", "1", "-W", str(max(1, timeout_ms // 1000)), host]
        out = subprocess.run(cmd, capture_output=True, text=True,
                             timeout=(timeout_ms / 1000) + 2, **_NO_WINDOW_KW)
        text = out.stdout + out.stderr
        # Match "time=42.1 ms" / "time=42ms" / "time<1ms" / "Average = 42ms"
        m = re.search(r"time[=<]\s*([\d.]+)\s*ms", text, re.IGNORECASE)
        if m:
            return float(m.group(1))
        m = re.search(r"Average\s*=\s*([\d.]+)\s*ms", text, re.IGNORECASE)
        if m:
            return float(m.group(1))
    except Exception:
        return None
    return None


def refresh_latency():
    """Ping every mapped region's endpoint. Runs them in threads to stay quick."""
    results = {}
    threads = []
    lock = threading.Lock()

    def worker(name, host):
        with _PING_SEM:           # throttle concurrent ping subprocesses
            ms = ping_host(host)
        with lock:
            results[name] = ms

    for entry in all_config_entries():
        t = threading.Thread(target=worker,
                             args=(entry["name"], entry.get("endpoint_host")))
        t.start()
        threads.append(t)
    for t in threads:
        t.join(timeout=6)
    STATE["latency"] = results
    return results


# ===========================================================================
# VPN CONTROL  (the actual route switching)
# ===========================================================================
def _wg_command(action, cfg, tunnel_name, kind, wg):
    """Build the platform-specific tunnel command. Falls back to a
    representative binary path when WireGuard isn't found (for dry-run)."""
    if kind == "wireguard-exe":
        binary = wg
    elif kind == "wg-quick":
        binary = wg
    else:
        # not installed -- show a representative command so dry-run is useful
        binary = (r"C:\Program Files\WireGuard\wireguard.exe"
                  if OS == "Windows" else "wg-quick")
        kind = "wireguard-exe" if OS == "Windows" else "wg-quick"

    if kind == "wireguard-exe":
        if action == "up":
            return [binary, "/installtunnelservice", cfg["path"]]
        return [binary, "/uninstalltunnelservice", tunnel_name]
    else:
        if action == "up":
            return [binary, "up", cfg["path"]]
        path = cfg["path"] if cfg else tunnel_name
        return [binary, "down", path]


def _run(cmd):
    """Run a command and return (ok, combined_output).

    Used for the real tunnel and route operations (ping has its own
    subprocess call). ok is True only on a zero exit code; any failure
    to even launch the command is reported as (False, error_text).
    """
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=30,
                           **_NO_WINDOW_KW)
        out = ((p.stdout or "") + (p.stderr or "")).strip()
        return p.returncode == 0, out
    except Exception as e:
        return False, str(e)


def _mark_route_change():
    """Record that we just changed the VPN route. The game-state detector uses
    this to ride out the brief match-server connection drop a route switch causes
    mid-match, so it doesn't read it as the match ending + a new one starting."""
    STATE["routeChangeTs"] = time.time()


def _installed_tunnel_services():
    """Names (without the WireGuardTunnel$ prefix) of installed WG tunnel
    services. WireGuard installs every tunnel as an AUTO-START Windows service;
    a leftover one silently routes you through an old exit on boot/launch."""
    if OS != "Windows":
        return []
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-Service 'WireGuardTunnel$*' -ErrorAction SilentlyContinue | "
             "Select-Object -ExpandProperty Name"],
            capture_output=True, text=True, timeout=10, **_NO_WINDOW_KW).stdout
        names = []
        for line in out.splitlines():
            line = line.strip()
            if line.startswith("WireGuardTunnel$"):
                names.append(line.split("$", 1)[1])
        return names
    except Exception:
        return []


def cleanup_stray_tunnels(keep=None):
    """Uninstall any leftover WireGuard tunnel services for OUR configs (except
    `keep`). Called at startup so a tunnel left installed by a crash or the route
    optimizer can't auto-connect you through an old exit on launch. Needs admin;
    no-ops in dry-run / when not elevated / when WireGuard isn't the exe backend."""
    if OS != "Windows" or STATE.get("dry_run") or not is_admin():
        return []
    kind, wg = find_wireguard()
    if kind != "wireguard-exe" or not wg:
        return []
    ours = {e["name"] for e in all_config_entries()}
    removed = []
    for name in _installed_tunnel_services():
        if name == keep:
            continue
        if ours and name not in ours:
            continue                       # don't touch the user's other tunnels
        ok, _ = _run([wg, "/uninstalltunnelservice", name])
        if ok:
            removed.append(name)
    if removed and STATE.get("active_tunnel") in removed:
        STATE["active_tunnel"] = None
        STATE["active_region"] = None
    return removed


def disconnect(_kind=None, _wg=None):
    """Bring down the currently active tunnel."""
    name = STATE["active_tunnel"]
    if not name:
        return {"ok": True, "message": "No active tunnel.", "active": None}
    _mark_route_change()
    kind, wg = (_kind, _wg) if _kind else find_wireguard()
    _, cfg = find_config_by_name(name)
    cmd = _wg_command("down", cfg, name, kind, wg)

    if STATE["dry_run"]:
        STATE["active_tunnel"] = None
        STATE["active_region"] = None
        return {"ok": True, "dryRun": True, "command": " ".join(cmd),
                "message": "[dry-run] would disconnect", "active": None}
    if not kind:
        return {"ok": False, "message": "WireGuard not found.", "active": name}
    if not is_admin():
        return {"ok": False, "message": "Need admin/root to change tunnels.",
                "command": " ".join(cmd), "active": name}
    ok, out = _run(cmd)
    if ok:
        STATE["active_tunnel"] = None
        STATE["active_region"] = None
    diag("vpn", bool(ok), msg=("disconnected " + str(name)) if ok else f"disconnect failed: {out}")
    return {"ok": ok, "message": out or "disconnected", "command": " ".join(cmd),
            "active": STATE["active_tunnel"]}


def _connect_entry(region_id, cfg):
    """Bring up a specific config entry (auto-drops the old tunnel first)."""
    prev_tunnel = STATE.get("active_tunnel")   # the route to fall back to if bad
    _mark_route_change()
    # drop existing tunnel first for a clean switch
    if STATE["active_tunnel"]:
        disconnect()

    kind, wg = find_wireguard()
    name = cfg["name"]
    cmd = _wg_command("up", cfg, name, kind, wg)

    if STATE["dry_run"]:
        STATE["active_tunnel"] = name
        STATE["active_region"] = region_id
        return {"ok": True, "dryRun": True, "command": " ".join(cmd),
                "message": f"[dry-run] would connect {name} ({region_id})",
                "active": name, "activeRegion": region_id, "activeConfig": name}
    if not kind:
        return {"ok": False, "message": "WireGuard not installed / not found on PATH.",
                "command": " ".join(cmd)}
    if not is_admin():
        return {"ok": False, "message": "Need admin/root to change tunnels.",
                "command": " ".join(cmd)}
    ok, out = _run(cmd)
    if not ok and "already" in (out or "").lower():
        # a stray service for this tunnel exists -> remove it and retry once
        _run([wg, "/uninstalltunnelservice", name])
        time.sleep(1)
        ok, out = _run(cmd)
    if ok:
        STATE["active_tunnel"] = name
        STATE["active_region"] = region_id
        # if this was a mid-match switch, arm the safety net to undo it if bad
        try:
            _arm_auto_revert(prev_tunnel)
        except Exception:
            pass
    return {"ok": ok, "message": out or f"connected {name}",
            "command": " ".join(cmd), "active": STATE["active_tunnel"],
            "activeRegion": STATE["active_region"], "activeConfig": STATE["active_tunnel"]}


def connect_region(region_id):
    """Connect the best (lowest-ping) config in a region."""
    if region_id not in STATE["configs"] or not STATE["configs"][region_id]:
        return {"ok": False, "message": f"No config mapped to {region_id}. "
                f"Drop a ProtonVPN .conf for this region in the configs folder."}
    cfg = region_best_config(region_id)
    return _connect_entry(region_id, cfg)


def connect_config(name):
    """Connect one specific config by name."""
    region_id, cfg = find_config_by_name(name)
    if not cfg:
        diag("vpn", False, msg=f"No config named '{name}'")
        return {"ok": False, "message": f"No config named '{name}'."}
    try:
        res = _connect_entry(region_id, cfg)
        diag("vpn", bool(res and res.get("ok")),
             msg=("connected " + str(name)) if (res and res.get("ok"))
                 else f"connect failed: {res and res.get('message')}")
        return res
    except Exception as e:
        diag("vpn", False, msg=f"connect {name}", exc=e)
        raise


def read_default_route():
    """Best-effort read of the current default route for display."""
    try:
        if OS == "Windows":
            ok, out = _run(["route", "print", "-4", "0.0.0.0"])
            if not ok:
                return None
            for line in out.splitlines():
                parts = line.split()
                if len(parts) >= 4 and parts[0] == "0.0.0.0":
                    return f"gw {parts[2]} if {parts[3]}"
        else:
            ok, out = _run(["ip", "route", "show", "default"])
            if ok and out:
                return out.splitlines()[0]
    except Exception:
        pass
    return None


def public_ip():
    """Best-effort exit IP lookup so you can confirm the switch took."""
    for url in ("https://api.ipify.org", "https://ifconfig.me/ip"):
        try:
            with urllib.request.urlopen(url, timeout=3) as r:
                return r.read().decode().strip()
        except Exception:
            continue
    return None


def active_server_info():
    """Detailed info about the server you're CURRENTLY connected to -- the
    thing you'd otherwise dig out of Resource Monitor. Returns None when no
    tunnel is up. Includes the config name, its region, the real endpoint
    host:port the tunnel is talking to, and the measured ping to it."""
    name = STATE["active_tunnel"]
    if not name:
        return None
    rid, cfg = find_config_by_name(name)
    region = REGION_BY_ID.get(rid or STATE.get("active_region"))
    endpoint = cfg.get("endpoint") if cfg else None
    host = cfg.get("endpoint_host") if cfg else None
    return {
        "config": name,
        "regionId": rid or STATE.get("active_region"),
        "regionName": region["name"] if region else None,
        "regionCode": region["code"] if region else None,
        "endpoint": endpoint,            # e.g. 203.0.113.7:51820
        "endpointHost": host,            # e.g. 203.0.113.7
        "pingMs": STATE["latency"].get(name),
    }


def status_snapshot(include_ip=False):
    return {
        "os": OS,
        "admin": is_admin(),
        "dryRun": STATE["dry_run"],
        "build": APP_BUILD,
        "appName": APP_NAME,
        "activeTunnel": STATE["active_tunnel"],
        "activeRegion": STATE["active_region"],
        "activeServer": active_server_info(),
        "tunnelHealth": health_snapshot(),   # live latency/jitter/loss of the tunnel
        "defaultRoute": read_default_route(),
        "publicIp": public_ip() if include_ip else None,
        "wireguard": find_wireguard()[0],
    }


# ===========================================================================
# GAME DETECTION  (read the LIVE Fragpunk server from the game's own socket)
# ---------------------------------------------------------------------------
# This is the route-independent "what server am I on / am I queuing" feature.
# It does NOT read game memory (that's what anti-cheats ban for) and needs no
# public API. Instead it does exactly what Resource Monitor's network view
# does: find the Fragpunk process, list its live network connections, and pick
# out the remote match-server IP the game is actually talking to. Then it
# geolocates that IP to a human-readable region.
# ===========================================================================
FRAGPUNK_PROC_NAMES = ("fragpunk.exe", "fragpunk-win64-shipping.exe",
                       "fragpunk_launcher.exe")
# names that contain 'fragpunk' but are NOT the game (our own app, configs, etc)
_NOT_THE_GAME = ("fragroute", "python", "pythonw", "cmd.exe", "conhost")

# small cache so we don't hammer the geo service: ip -> (info_dict, ts)
_GEO_CACHE = {}
_GEO_TTL = 3600  # an IP's location doesn't change within an hour

# Offline fallback: very rough IP-block -> region. Only used when the online
# lookup fails (no internet / blocked). Cloud game servers cluster in known
# provider ranges; this is approximate by design and clearly labelled as such.
_OFFLINE_IP_HINTS = [
    # (first octet ranges are too broad to be exact; we keep this conservative
    #  and lean on the online lookup for accuracy)
]


def _is_private_ip(ip):
    """True for LAN / loopback / link-local / CGNAT addresses we should ignore
    (the game server is a public IP)."""
    try:
        parts = [int(x) for x in ip.split(".")]
    except Exception:
        return True
    if len(parts) != 4:
        return True
    a, b = parts[0], parts[1]
    if a == 10: return True
    if a == 127: return True
    if a == 0: return True
    if a == 169 and b == 254: return True            # link-local
    if a == 172 and 16 <= b <= 31: return True       # 172.16/12
    if a == 192 and b == 168: return True            # 192.168/16
    if a == 100 and 64 <= b <= 127: return True      # CGNAT 100.64/10
    if a >= 224: return True                         # multicast / reserved
    return False


def _find_game_pids():
    """Return a set of PIDs for the running Fragpunk GAME process(es), or empty.
    Matches the executable name exactly -- not a loose substring -- so it never
    mistakes FRAGROUTE itself (or this script's path) for the game."""
    pids = set()
    try:
        if OS == "Windows":
            out = subprocess.run(["tasklist", "/fo", "csv", "/nh"],
                                 capture_output=True, text=True, timeout=6,
                                 **_NO_WINDOW_KW).stdout
            for line in out.splitlines():
                cols = [c.strip('" ') for c in line.split('","')]
                if len(cols) < 2 or not cols[1].isdigit():
                    continue
                exe = cols[0].strip('" ').lower()
                # exact executable-name match against the known game binaries
                if exe in FRAGPUNK_PROC_NAMES:
                    pids.add(cols[1])
        else:
            # Linux/mac: match the process NAME (comm), not the full cmdline, and
            # skip anything that's clearly us or an interpreter.
            out = subprocess.run(["ps", "-eo", "pid,comm"],
                                 capture_output=True, text=True, timeout=6).stdout
            for line in out.splitlines()[1:]:
                parts = line.split(None, 1)
                if len(parts) != 2 or not parts[0].isdigit():
                    continue
                comm = parts[1].strip().lower()
                if "fragpunk" in comm and not any(x in comm for x in _NOT_THE_GAME):
                    pids.add(parts[0])
    except Exception:
        pass
    return pids


def _game_connections(pids):
    """List remote (ip, port, proto, state) the given PIDs are connected to,
    public IPs only. Uses netstat -ano on Windows so we can match by PID.
    `state` is the TCP state (e.g. ESTABLISHED, CLOSE_WAIT) or "" for UDP."""
    conns = []
    if not pids:
        return conns
    try:
        if OS == "Windows":
            out = subprocess.run(["netstat", "-ano"], capture_output=True,
                                 text=True, timeout=8, **_NO_WINDOW_KW).stdout
            for line in out.splitlines():
                p = line.split()
                # TCP  local  remote  STATE  PID   |  UDP  local  remote(*)  PID
                if len(p) >= 4 and p[0] in ("TCP", "UDP"):
                    proto = p[0]
                    pid = p[-1]
                    if pid not in pids:
                        continue
                    state = p[3] if (proto == "TCP" and len(p) >= 5) else ""
                    remote = p[2] if proto == "TCP" else (p[2] if len(p) >= 4 else "")
                    if not remote or remote.startswith("*"):
                        continue
                    # split host:port from the right (IPv4 only for now)
                    if ":" not in remote:
                        continue
                    host, _, port = remote.rpartition(":")
                    if "." not in host:        # skip IPv6 for the readout
                        continue
                    if _is_private_ip(host):
                        continue
                    conns.append((host, port, proto, state))
        else:
            # Linux/mac best effort via ss; matching by pid is OS-specific
            out = subprocess.run(["ss", "-tunp"], capture_output=True,
                                 text=True, timeout=8).stdout
            for line in out.splitlines():
                if not any(("pid=" + pid) in line for pid in pids):
                    continue
                st = "ESTABLISHED" if "ESTAB" in line else ""
                m = re.search(r"\s(\d+\.\d+\.\d+\.\d+):(\d+)\s+users", line)
                if m and not _is_private_ip(m.group(1)):
                    proto = "TCP" if line.lstrip().lower().startswith("tcp") else "UDP"
                    conns.append((m.group(1), m.group(2), proto, st))
    except Exception:
        pass
    return conns


# Ports that are never a game server (web/API/CDN). The game keeps a persistent
# HTTPS backend connection open the whole session; it must never be mistaken for
# the match server.
_WEB_PORTS = {"80", "443", "8080", "8443"}
# First-seen wall-clock time per Established endpoint (NOT a call counter, so it
# is immune to how often game_status() is polled). The persistent lobby has the
# oldest first-seen; a per-match server appears later. A match-server candidate
# must stay Established for at least _MATCH_MIN_AGE_S before we trust it as a
# real match -- this rejects the brief Established window a dying match socket
# shows during the match-end -> requeue transition (which otherwise produced a
# false 'match_found' and a bogus auto-logged queue time).
_CONN_FIRST = {}
_CONN_FIRST_LOCK = threading.Lock()
_MATCH_MIN_AGE_S = 8
_INFRA_LEARN_S = 18      # established this long while in the menu => infrastructure
# Endpoints (ip:port) learned to be INFRASTRUCTURE, not match servers. FragPunk
# keeps MULTIPLE persistent connections -- the lobby/matchmaking gateway AND a
# Frankfurt home-backend (e.g. 8.211.x:18110) -- both open the whole session.
# The old "oldest = lobby, any other = match" rule mistook the second infra
# connection for a match server (especially after a VPN switch churned the
# connections), firing phantom 'match_found's and logging bogus EU queues while
# the user was only sitting in the menu. We learn infra endpoints during the
# menu (by ip:port identity, so it survives VPN reconnects) and never treat
# them as a match server.
_INFRA = set()
_INFRA_LOCK = threading.Lock()
# Known FragPunk INFRASTRUCTURE ports. The lobby/matchmaking gateway is always
# :11000 and the Frankfurt home-backend always :18110, even though their IPs
# rotate (8.221.58.95 <-> 8.221.59.131, 8.211.97.35 <-> 8.211.93.64). Match
# servers use other ports (:90xx, :18090). Treating these ports as infra
# regardless of IP fixes the false match when a rotated lobby IP is the only
# connection. Match commits at 8s (< the 18s infra-learn window), so a real
# match server's port can never be mislearned as infra.
_INFRA_PORTS = {"11000", "18110"}
# After the game process first appears, ignore a "match" classification for this
# long. At launch FragPunk opens several short-lived backend/gateway connections
# (on non-lobby ports) that can linger past the 8s match-age gate and otherwise
# flap a phantom "match found -> match ended" right at startup. A real match
# needs menu-load + queue, so it can never legitimately begin this soon after
# the process starts. Tracked by the running-edge in game_status().
_LAUNCH_SETTLE_S = 30
_GAME_RUN_SINCE = {"ts": None}


def _is_public_ip(host):
    """True for a routable public IPv4 (rejects loopback/private/link-local), so a
    UDP gameplay remote is a real server and not localhost/LAN chatter."""
    try:
        import ipaddress
        ip = ipaddress.ip_address(host)
        return not (ip.is_private or ip.is_loopback or ip.is_link_local
                    or ip.is_multicast or ip.is_reserved or ip.is_unspecified)
    except Exception:
        return False


def _classify_game(conns):
    """Decide menu vs match from the live connection set, and pick the match
    server. Returns (phase, match_server, lobby), each (host,port) or None.

    Keep only Established, public, non-web TCP. Endpoints that stay up while
    we're in the menu are INFRASTRUCTURE (lobby + home backend); a match server
    is a stable Established connection that is NOT known infrastructure. This
    survives long matches and VPN-switch connection churn, and stops the EU
    home-backend from being mistaken for a match."""
    now = time.time()
    game = []
    udp_game = []
    for host, port, proto, state in conns:
        if port in _WEB_PORTS:
            continue                              # never a game server
        if proto == "UDP":
            # the actual GAMEPLAY runs over UDP (e.g. :7786/:7798). netstat DOES
            # list these remotes; use them as a fallback so the live host shows even
            # when the TCP session isn't Established (or is being firewall-blocked).
            if port not in _INFRA_PORTS and _is_public_ip(host):
                udp_game.append((host, port))
            continue
        if proto != "TCP":
            continue
        if state and state.upper() != "ESTABLISHED":
            continue                              # ignore CLOSE_WAIT / TIME_WAIT etc.
        game.append((host, port))

    # track first-seen for BOTH tcp + udp game endpoints so age gating works for each
    keys = {f"{h}:{p}" for h, p in (game + udp_game)}
    with _CONN_FIRST_LOCK:
        for k in list(_CONN_FIRST):
            if k not in keys:
                del _CONN_FIRST[k]                # connection gone -> forget it
        for k in keys:
            _CONN_FIRST.setdefault(k, now)        # stamp first-seen once
        first = dict(_CONN_FIRST)

    def age(hp):
        return now - first.get(f"{hp[0]}:{hp[1]}", now)

    def key(hp):
        return f"{hp[0]}:{hp[1]}"

    if not game and not udp_game:
        return "menu", None, None                 # running but no game conn yet

    # Infrastructure = a connection on a known infra PORT (lobby :11000, EU
    # backend :18110) -- stable across IP rotation, and never a match server.
    # A match server is anything else that's been Established >= 8s. We do NOT
    # dynamically "learn" infra by ip:port anymore: that could swallow a real
    # match server if detection lagged, and the port rule covers every case.
    infra_now = {hp for hp in game if hp[1] in _INFRA_PORTS}
    others = [hp for hp in game
              if hp not in infra_now and age(hp) >= _MATCH_MIN_AGE_S]
    # pick a lobby to report (prefer a known-infra endpoint, else the oldest TCP)
    lobby = (max(infra_now, key=age) if infra_now
             else (max(game, key=age) if game else None))
    if others:
        match_srv = max(others, key=age)          # the most stable non-infra TCP conn
        return "match", match_srv, lobby
    # FALLBACK: no TCP match server, but a persistent UDP GAMEPLAY server means you're
    # in a match (the TCP session may be brief / blocked). Report it so the live host
    # shows for non-VPN games too, instead of a blank "in menu".
    udp_others = [hp for hp in udp_game if age(hp) >= _MATCH_MIN_AGE_S]
    if udp_others:
        return "match", max(udp_others, key=age), lobby
    return "menu", None, lobby


# Offline GeoIP database reader (lazy). Drop a commercial-safe .mmdb into the
# app's `geo` folder -- DB-IP Lite (CC-BY-4.0) or MaxMind GeoLite2 (free, EULA) --
# and we read it locally with NO third-party API call. This is the commercial
# path: ip-api.com's free tier is NON-COMMERCIAL only, so it is OFF by default
# in a shipped build and only used if the user opts in (geoOnlineLookup) for
# their own personal use.
_GEO_DB = {"reader": None, "tried": False}


def _geo_db_path():
    base = (Path(sys.executable).parent if getattr(sys, "frozen", False)
            else Path(__file__).parent)
    d = base / "geo"
    if d.exists():
        for p in sorted(d.glob("*.mmdb")):
            return str(p)
    return None


def _geo_mmdb(ip):
    """Local offline lookup via a bundled .mmdb (commercial-safe). None if no DB
    or reader. Never raises."""
    if not _GEO_DB["tried"]:
        _GEO_DB["tried"] = True
        try:
            import maxminddb           # MIT-licensed reader
            p = _geo_db_path()
            if p:
                _GEO_DB["reader"] = maxminddb.open_database(p)
        except Exception:
            _GEO_DB["reader"] = None
    reader = _GEO_DB["reader"]
    if not reader:
        return None
    try:
        rec = reader.get(ip) or {}
        country = (rec.get("country") or {})
        city = (rec.get("city") or {})
        subs = (rec.get("subdivisions") or [{}])
        sub0 = subs[0] if subs else {}
        names = lambda d: (d.get("names") or {}).get("en")
        return {
            "country": names(country),
            "countryCode": country.get("iso_code"),
            "state": sub0.get("iso_code"),
            "region": names(sub0),
            "city": names(city),
            "isp": None, "asn": None,
            "source": "offline-db",
        }
    except Exception:
        return None


def _geo_lookup(ip):
    """Return {'country','region','city','isp','source'} for an IP.
    Commercial-safe: prefers a bundled offline DB; the non-commercial online API
    is opt-in only. Cached for an hour. Never raises."""
    now = time.time()
    hit = _GEO_CACHE.get(ip)
    if hit and now - hit[1] < _GEO_TTL:
        return hit[0]

    # --- offline DB first (commercial-safe, no network) ---
    info = _geo_mmdb(ip)

    # --- optional online: ip-api.com -- NON-COMMERCIAL free tier, OFF by default.
    #     Only used if the user explicitly enables it for personal use. Do NOT
    #     enable this in a build you sell. ---
    if info is None and get_setting("geoOnlineLookup", False):
        try:
            url = ("http://ip-api.com/json/" + urllib.parse.quote(ip) +
                   "?fields=status,country,countryCode,region,regionName,city,isp,as,query")
            with urllib.request.urlopen(url, timeout=4) as r:
                data = json.loads(r.read().decode())
            if data.get("status") == "success":
                info = {
                    "country": data.get("country"),
                    "countryCode": data.get("countryCode"),
                    "state": data.get("region"),          # short code, e.g. VA / CA
                    "region": data.get("regionName"),
                    "city": data.get("city"),
                    "isp": data.get("isp"),
                    "asn": data.get("as"),
                    "source": "online",
                }
        except Exception:
            info = None

    # --- offline fallback: rely on DNS-derived region + ping (geo is cosmetic) ---
    if info is None:
        info = {"country": None, "countryCode": None, "state": None,
                "region": None, "city": None, "isp": None, "asn": None,
                "source": "offline"}

    _GEO_CACHE[ip] = (info, now)
    return info


def _country_to_region(cc):
    """Map an ISO country code to our Fragpunk region id (best effort)."""
    if not cc:
        return None
    cc = cc.upper()
    if cc == "US":
        return "us-east"   # can't tell coast from country code alone
    return COUNTRY_REGION.get(cc)


def _geo_region(geo):
    """Region id from a geo result, using the US state code to pick the coast
    (Virginia -> us-east, California -> us-west) instead of defaulting all US to
    us-east. Falls back to country-level mapping."""
    cc = (geo.get("countryCode") or "").upper()
    if cc == "US":
        st = (geo.get("state") or "").upper()
        if st in US_STATE_REGION:
            return US_STATE_REGION[st]
    return _country_to_region(cc)


# ---------------------------------------------------------------------------
# DNS-derived region (authoritative, beats GeoIP on cloud IPs)
# ---------------------------------------------------------------------------
# Fragpunk runs on Alibaba Cloud; its load-balancer hostnames embed the real
# cloud region code, e.g.
#   home.fragpunk.com -> alb-....eu-central-1.alb.aliyuncsslbintl.com
# GeoIP mislabels these IPs (an Alibaba LB IP can resolve to the wrong city),
# but the region code in the CNAME is ground truth. We read the OS DNS cache,
# extract any cloud region code, and map the resolved IPs (and their /24) to our
# region. Used as the FIRST choice for region tagging, with GeoIP as fallback.
_CLOUD_REGION = {
    "eu-central": "eu", "eu-west": "eu", "eu-north": "eu", "eu-south": "eu",
    "us-east": "us-east", "us-west": "us-west",
    "ap-southeast-2": "oceania",                 # Sydney
    "ap-southeast": "asia-se", "ap-south": "asia-se", "me-": "asia-se",
    "ap-northeast": "asia-east", "cn-": "asia-east",
    "sa-east": "sa",
}
_DNS_REGION = {}          # ip -> region_id  AND  "a.b.c" (/24) -> region_id
_DNS_REGION_TS = 0.0
_DNS_REGION_TTL = 300     # re-read the DNS cache at most every 5 min
_DNS_REGION_LOCK = threading.Lock()
_REGION_CODE_RE = re.compile(r"((?:eu|us|ap|cn|sa|me)-[a-z]+-?\d?)", re.IGNORECASE)
_IPV4_RE = re.compile(r"\b(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})\b")


def _code_to_region(code):
    code = code.lower()
    if code in _CLOUD_REGION:
        return _CLOUD_REGION[code]
    for prefix, rid in _CLOUD_REGION.items():     # prefix match (e.g. ap-southeast-1)
        if code.startswith(prefix):
            return rid
    return None


def _refresh_dns_region_hints():
    """Parse the OS DNS cache; map IPs that resolved through a cloud-region
    CNAME to our region. Windows only (ipconfig /displaydns); best effort."""
    global _DNS_REGION, _DNS_REGION_TS
    if OS != "Windows":
        return
    now = time.time()
    if now - _DNS_REGION_TS < _DNS_REGION_TTL:
        return
    _DNS_REGION_TS = now
    try:
        out = subprocess.run(["ipconfig", "/displaydns"], capture_output=True,
                             text=True, timeout=8, **_NO_WINDOW_KW).stdout
    except Exception:
        return
    mapping = {}
    # Scan line by line. Each DNS record begins with a "Record Name" line; the
    # cloud region code shows up in that line (for the ALB A-record blocks) or in
    # a CNAME line within the block. Carry the current block's region code and
    # apply it to A-record IPs in the same block; reset at each record boundary
    # so codes never bleed into unrelated records.
    current_rid = None
    for line in out.splitlines():
        low = line.lower()
        if "record name" in low:
            m = _REGION_CODE_RE.search(line)          # alb A-record blocks carry it here
            current_rid = _code_to_region(m.group(1)) if m else None
            continue
        m = _REGION_CODE_RE.search(line)              # e.g. a CNAME line
        if m:
            rid2 = _code_to_region(m.group(1))
            if rid2:
                current_rid = rid2
        if current_rid:
            for ip in _IPV4_RE.findall(line):
                if _is_private_ip(ip):
                    continue
                mapping[ip] = current_rid
                a, b, c, _ = ip.split(".")
                mapping[f"{a}.{b}.{c}"] = current_rid  # tag the whole /24
    if mapping:
        with _DNS_REGION_LOCK:
            _DNS_REGION = mapping


def _dns_region_for_ip(ip):
    """Authoritative region for an IP from the DNS cache, or None."""
    _refresh_dns_region_hints()
    with _DNS_REGION_LOCK:
        if ip in _DNS_REGION:
            return _DNS_REGION[ip]
        try:
            a, b, c, _ = ip.split(".")
            return _DNS_REGION.get(f"{a}.{b}.{c}")
        except Exception:
            return None


def _learned_server_match(ip):
    """Name an arbitrary game-server IP from the servers we've ALREADY recorded
    during play (record_server). This is how an off-VPN / non-Alibaba-LB raw
    match IP gets a region + city: we matched it (exact, else same /24) against
    the FragPunk servers we learned while on a VPN where the region was known.
    Returns {'regionId','city','country','exact'} or None."""
    if not ip:
        return None
    try:
        regions = (load_servers().get("regions", {}) or {})
    except Exception:
        return None
    try:
        a, b, c, _ = ip.split(".")
        slash24 = f"{a}.{b}.{c}."
    except Exception:
        slash24 = None
    near = None
    for rid, bucket in regions.items():
        for sip, rec in (bucket or {}).items():
            if sip == ip:
                return {"regionId": rid, "city": rec.get("city"),
                        "country": rec.get("country"), "exact": True}
            if slash24 and near is None and sip.startswith(slash24):
                near = {"regionId": rid, "city": rec.get("city"),
                        "country": rec.get("country"), "exact": False}
    return near


def game_status():
    """The route-independent game readout. Tells you:
      - whether Fragpunk is running
      - the live server IP:port the game is talking to (from its own socket)
      - that server's geolocated region/city
      - an INFERRED activity state (in-match vs idle/menu) based on whether a
        stable public game connection exists -- this is inference from network
        behaviour, NOT a read of the game's internal queue flag.
    """
    pids = _find_game_pids()
    running = bool(pids)
    if not running:
        _GAME_RUN_SINCE["ts"] = None          # reset launch timer for next start
        return {"running": False, "server": None, "state": "not running"}
    if _GAME_RUN_SINCE["ts"] is None:
        _GAME_RUN_SINCE["ts"] = time.time()   # stamp first moment we see it running

    conns = _game_connections(pids)
    phase, match_srv, lobby = _classify_game(conns)

    # LAUNCH SETTLING: suppress a phantom match during the startup window. The
    # transient backend connections the game opens at launch can briefly look
    # like a match server (Established > 8s on a non-infra port); ignoring
    # "match" for the first _LAUNCH_SETTLE_S stops the found->ended flap. A real
    # match (menu-load + queue) can never start this fast, so nothing is missed.
    if phase == "match":
        since = _GAME_RUN_SINCE["ts"]
        if since and (time.time() - since) < _LAUNCH_SETTLE_S:
            phase, match_srv = "menu", None

    def _describe(hp):
        host, port = hp
        geo = _geo_lookup(host)
        # Region sources, best-first, so an OFF-VPN raw match IP still gets named:
        #   1) DNS cloud-region CNAME (authoritative for Alibaba LBs)
        #   2) a server we LEARNED during play (exact IP, else same /24)
        #   3) GeoIP (state-aware US coast), works for any IP once a DB is present
        dns_rid = _dns_region_for_ip(host)
        learned = _learned_server_match(host)
        learned_rid = learned.get("regionId") if learned else None
        geo_rid = _geo_region(geo)
        # GeoIP now uses a real offline DB, so it's RELIABLE for the actual server IP.
        # The DNS-CNAME hint can bleed FragPunk's Frankfurt "home backend" region onto
        # a US match (the false "Europe match found"). So when GeoIP and DNS disagree,
        # TRUST GeoIP -- it reflects where the IP actually is. Otherwise DNS > learned > geo.
        if dns_rid and geo_rid and dns_rid != geo_rid:
            rid = geo_rid
            src = "geoip(vs-dns)"
        else:
            rid = dns_rid or learned_rid or geo_rid
            src = ("dns" if dns_rid else "learned" if learned_rid else "geoip" if geo_rid else None)
        region = REGION_BY_ID.get(rid)
        # city/country: GeoIP first, fall back to whatever we learned for this IP
        city = geo.get("city") or (learned.get("city") if learned else None)
        country = geo.get("country") or (learned.get("country") if learned else None)
        where = ", ".join([x for x in (city, country) if x]) or None
        return {
            "ip": host, "port": port, "proto": "TCP",
            "city": city, "country": country,
            "countryCode": geo.get("countryCode"), "isp": geo.get("isp"),
            "where": where, "regionId": rid,
            "regionName": region["name"] if region else None,
            "regionCode": region["code"] if region else None,
            "geoSource": geo.get("source"),
            "regionSource": src,
            # always something to show the user, even with no GeoIP DB on a fresh
            # off-VPN server: location -> region name -> the raw endpoint.
            "serverName": where or (region["name"] if region else None) or ("%s:%s" % (host, port)),
            "named": bool(where or region),
        }

    # context shown in both states: which region's matchmaking you're on
    lobby_info = _describe(lobby) if lobby else None

    if phase == "match" and match_srv:
        server = _describe(match_srv)
        server["connections"] = len([c for c in conns if c[2] == "TCP"])
        return {"running": True, "state": "in match",
                "server": server, "lobby": lobby_info}

    # menu/queuing: report NO match server (so the auto-capture monitor sees
    # 'menu' and runs the queue clock), but expose the lobby region as context.
    return {"running": True, "state": "in menu / queuing",
            "server": None, "lobby": lobby_info}


# ===========================================================================
# GAME INFO  (installed version + update detection from the local install)
# ---------------------------------------------------------------------------
# The game's paks are encrypted, but FragPunk\Binaries\Win64\version.txt is
# plaintext. We read it to show the installed version and flag when an update
# has landed since last launch (patches change queues/maintenance windows).
# ===========================================================================
_GAMEINFO_CACHE = {"version": None, "path": None, "ts": 0.0}


def _steam_library_roots():
    """Steam library folder roots (default + extras from libraryfolders.vdf)."""
    roots = []
    for base in (os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
                 os.environ.get("ProgramFiles", r"C:\Program Files")):
        if base:
            roots.append(Path(base) / "Steam")
    # parse libraryfolders.vdf for libraries on other drives
    for r in list(roots):
        vdf = r / "steamapps" / "libraryfolders.vdf"
        try:
            txt = vdf.read_text(errors="ignore")
            for m in re.finditer(r'"path"\s*"([^"]+)"', txt):
                roots.append(Path(m.group(1).replace("\\\\", "\\")))
        except Exception:
            pass
    # de-dup preserving order
    seen, out = set(), []
    for r in roots:
        s = str(r).lower()
        if s not in seen:
            seen.add(s); out.append(r)
    return out


def find_game_version_file():
    """Locate FragPunk's plaintext version.txt across Steam libraries."""
    rel = Path("steamapps") / "common" / "FragPunk" / "FragPunk" / "Binaries" / "Win64" / "version.txt"
    for root in _steam_library_roots():
        p = root / rel
        try:
            if p.exists():
                return p
        except Exception:
            pass
    return None


def game_info():
    """Installed game version + whether it changed since last launch."""
    now = time.time()
    if _GAMEINFO_CACHE["version"] and now - _GAMEINFO_CACHE["ts"] < 60:
        version, path = _GAMEINFO_CACHE["version"], _GAMEINFO_CACHE["path"]
    else:
        version, path = None, None
        vf = find_game_version_file()
        if vf:
            try:
                version = vf.read_text(errors="ignore").replace("\x00", "").strip() or None
                path = str(vf)
            except Exception:
                pass
        _GAMEINFO_CACHE.update({"version": version, "path": path, "ts": now})

    last = get_setting("lastSeenGameVersion") or ""
    updated = bool(version and last and version != last)
    return {"version": version, "previousVersion": last or None,
            "updated": updated, "path": path,
            "gameRunning": bool(_find_game_pids())}


def note_game_version_seen():
    """Persist the current version as 'seen' (call after surfacing an update)."""
    info = game_info()
    if info["version"]:
        save_settings({"lastSeenGameVersion": info["version"]})
    return info


# ===========================================================================
# QUEUE LOG  (owned by the backend, JSON on disk)
# ===========================================================================
def load_log():
    try:
        return json.loads(Path(LOG_PATH).read_text())
    except Exception:
        return []


def save_log(entries):
    try:
        Path(LOG_PATH).write_text(json.dumps(entries, indent=2))
        return True
    except Exception:
        return False


def append_log(entry):
    with _LOG_LOCK:
        entries = load_log()
        # DE-DUPE: a single match was sometimes logged twice within a few
        # seconds (a detector phase-flap, or a second engine instance briefly
        # overlapping). Reject an AUTO entry that's near-identical to a very
        # recent one -- same region + outcome, within 25s, ~same duration.
        # (Two real matches can't finish that close together, so this is safe.)
        if entry.get("auto") and entries:
            for prev in entries[:3]:
                if (prev.get("regionId") == entry.get("regionId")
                        and prev.get("outcome") == entry.get("outcome")
                        and abs(int(entry.get("ts", 0)) - int(prev.get("ts", 0))) < 25000
                        and abs(int(prev.get("duration", 0)) - int(entry.get("duration", 0))) <= 20):
                    return entries  # duplicate -> ignore
        entries.insert(0, entry)
        entries = entries[:200]
        save_log(entries)
    return entries


# ===========================================================================
# SERVER INTEL  (harvested real Fragpunk game-server IPs, per region)
# ---------------------------------------------------------------------------
# Every match the detector reads the real game-server IP off Fragpunk's own
# socket (see game_status()). We persist those IPs here so the Scout can ping
# the ACTUAL datacenters -- not just the VPN endpoint -- and rank regions by
# true routing quality. Anti-cheat-safe: this is the same public IP Resource
# Monitor would show, never a memory read.
#
# Shape on disk (fragroute_servers.json):
#   { "regions": { "<rid>": { "<ip>": {firstSeen,lastSeen,count,port,
#                                       city,country,lastPingMs,pingTs} } },
#     "updated": <ms> }
# ===========================================================================
_SERVERS_MAX_PER_REGION = 24   # keep the freshest N IPs per region (evict oldest)


def load_servers():
    try:
        data = json.loads(Path(SERVERS_PATH).read_text())
        if isinstance(data, dict) and isinstance(data.get("regions"), dict):
            return data
    except Exception:
        pass
    return {"regions": {}, "updated": None}


# ===========================================================================
# PRIVATE BROWSER  (ephemeral, wiped when FRAGROUTE closes)
# ---------------------------------------------------------------------------
# Opens the system browser (Edge/Chrome) on a THROWAWAY profile under TEMP, so
# cookies/logins work normally during the session, but the whole profile --
# cookies, history, cache, every URL -- is DELETED when FRAGROUTE exits and the
# browser windows close. Nothing persists, nothing is recoverable.
# ===========================================================================
_BROWSER = {"dir": None, "procs": []}
_BROWSER_LOCK = threading.Lock()


def _find_browser():
    cands = [
        os.path.expandvars(r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"),
        os.path.expandvars(r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"),
        os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"),
        shutil.which("msedge"), shutil.which("chrome"),
    ]
    for c in cands:
        if c and Path(c).exists():
            return c
    return None


def _browser_normalize(url):
    """User input -> safe http(s) URL, or a web search if it isn't a URL. Only
    http/https is ever launched (never file:/javascript:/etc)."""
    u = (url or "").strip()
    if not u:
        return "about:blank"
    low = u.lower()
    if low.startswith(("http://", "https://")):
        return u
    if low.startswith(("javascript:", "file:", "data:", "about:", "chrome:", "edge:")):
        return "about:blank"
    if "." in u and " " not in u:
        return "https://" + u
    return "https://www.google.com/search?q=" + urllib.parse.quote(u)


def _browser_spawn_deelevated(exe, prof, url):
    """Launch the browser at the user's NORMAL integrity from our elevated
    process. An admin-launched Chrome/Edge won't show a window, so we drop to a
    basic-user token with `runas /trustlevel` (passes the full command line and
    de-elevates reliably). Returns True on success."""
    try:
        cmd = '"%s" --user-data-dir="%s" --no-first-run --no-default-browser-check --new-window "%s"' % (
            exe, prof, url)
        # pass the whole command as ONE arg so runas parses it (no shell quoting)
        r = subprocess.run(["runas", "/trustlevel:0x20000", cmd],
                           capture_output=True, text=True, timeout=12, **_NO_WINDOW_KW)
        return r.returncode == 0
    except Exception:
        return False


def browser_open(url):
    """Open a URL in the ephemeral private browser. Returns {ok, url, browser}."""
    exe = _find_browser()
    if not exe:
        diag("browser", False, msg="no Edge/Chrome found")
        return {"ok": False, "message": "No Edge or Chrome found to browse with."}
    target = _browser_normalize(url)
    with _BROWSER_LOCK:
        if not (_BROWSER["dir"] and Path(_BROWSER["dir"]).exists()):
            _BROWSER["dir"] = tempfile.mkdtemp(prefix="fragroute_browser_")
            try:
                import atexit
                atexit.register(browser_wipe)   # wipe the profile on exit
            except Exception:
                pass
        prof = _BROWSER["dir"]
        name = "Edge" if "edge" in exe.lower() else "Chrome"
        # The app runs ELEVATED (--uac-admin); a directly-spawned browser then
        # won't open a window. De-elevate (runas /trustlevel) so a real,
        # connecting window appears. Fall back to a direct launch if not elevated
        # (or if the de-elevation fails).
        try:
            elevated = is_admin()
        except Exception:
            elevated = False
        if elevated and _browser_spawn_deelevated(exe, prof, target):
            diag("browser", True, msg=f"opened ({name}, de-elevated)")
            return {"ok": True, "url": target, "browser": name}
        try:
            p = subprocess.Popen([exe, f"--user-data-dir={prof}", "--no-first-run",
                                  "--no-default-browser-check", "--new-window", target],
                                 **_NO_WINDOW_KW)
            _BROWSER["procs"].append(p)
            diag("browser", True, msg=f"opened ({name})")
            return {"ok": True, "url": target, "browser": name}
        except Exception as e:
            diag("browser", False, msg="spawn browser", exc=e)
            return {"ok": False, "message": str(e)}


def browser_wipe():
    """Close the private windows and delete the throwaway profile -- cookies,
    history, cache, URLs gone for good. Best effort, never raises."""
    with _BROWSER_LOCK:
        for p in _BROWSER["procs"]:
            try:
                p.terminate()
            except Exception:
                pass
        _BROWSER["procs"] = []
        d = _BROWSER.get("dir")
    # also close any DE-ELEVATED browser windows -- they aren't tracked Popens,
    # so kill only the browser processes using THIS throwaway profile (matched
    # by its unique temp-folder name, so the user's normal browser is untouched).
    if d and OS == "Windows":
        tok = Path(d).name
        try:
            ps = ("Get-CimInstance Win32_Process -Filter \"Name='chrome.exe' or Name='msedge.exe'\" "
                  "| Where-Object { $_.CommandLine -like '*%s*' } "
                  "| ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }" % tok)
            subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                           capture_output=True, timeout=10, **_NO_WINDOW_KW)
        except Exception:
            pass
    if d:
        for _ in range(4):
            shutil.rmtree(d, ignore_errors=True)
            if not Path(d).exists():
                break
            time.sleep(0.5)   # let the browser release file locks, then retry
        _BROWSER["dir"] = None
    return {"ok": True}


def browser_status():
    with _BROWSER_LOCK:
        alive = sum(1 for p in _BROWSER["procs"] if p.poll() is None)
        return {"open": alive, "active": bool(_BROWSER.get("dir")),
                "available": bool(_find_browser())}


# ===========================================================================
# REPLAY LIBRARY  (FragPunk saves an encrypted replay per match)
# ---------------------------------------------------------------------------
# FragPunk writes one Replay.demo per match under
#   %LOCALAPPDATA%/FragPunk/<profile>/Saved/Demos/<id>/Replay.demo
# The .demo is ENCRYPTED (see fragpunk-encrypts-saved) so we can't read stats
# from it -- but we index each match by its file time and ENRICH it with what
# we already track (rank RP change, region, queue) by timestamp, so you know
# which matches are worth re-watching. Watching happens in FragPunk's own
# replay browser; this just helps you pick + take review notes.
# ===========================================================================
def _replay_dirs():
    base = Path(os.environ.get("LOCALAPPDATA") or str(Path.home())) / "FragPunk"
    dirs = []
    if base.exists():
        try:
            for prof in base.iterdir():
                d = prof / "Saved" / "Demos"
                if d.is_dir():
                    dirs.append((prof.name, d))
        except Exception:
            pass
    return dirs


def _replay_id(path):
    return hashlib.md5(str(path).encode("utf-8", "ignore")).hexdigest()[:12]


def load_replay_meta():
    if not REPLAYS_PATH:
        return {}
    try:
        if Path(REPLAYS_PATH).exists():
            d = json.loads(Path(REPLAYS_PATH).read_text())
            return d if isinstance(d, dict) else {}
    except Exception:
        pass
    return {}


def _save_replay_meta(meta):
    try:
        Path(REPLAYS_PATH).write_text(json.dumps(meta, indent=2))
    except Exception:
        pass


def _nearest_rp(ts_ms, hist, window_ms=45 * 60 * 1000):
    """RP delta of the rank reading that happened just AFTER a match ended
    (rank updates in the menu post-match). None if nothing within the window."""
    best = None
    for h in hist:
        ht = h.get("ts")
        if ht is None or "delta" not in h:
            continue
        if ts_ms <= ht <= ts_ms + window_ms:
            if best is None or ht < best[0]:
                best = (ht, h["delta"])
    return best[1] if best else None


def _nearest_queue(ts_ms, log, window_ms=45 * 60 * 1000):
    """Region of the queue-log entry whose match started just BEFORE this
    replay's end time. None if nothing within the window."""
    best = None
    for e in log:
        et = e.get("ts")
        if et is None:
            continue
        if ts_ms - window_ms <= et <= ts_ms + 5 * 60 * 1000:
            d = abs(et - ts_ms)
            if best is None or d < best[0]:
                best = (d, e.get("regionId"))
    return best[1] if best else None


def replay_library():
    """Index every saved replay, newest first, enriched with rank RP change +
    region by timestamp where our logs overlap. Includes review notes/tags."""
    meta = load_replay_meta()
    try:
        hist = (load_rank() or {}).get("history") or []
    except Exception:
        hist = []
    try:
        log = load_log() or []
    except Exception:
        log = []
    items = []
    for profile, d in _replay_dirs():
        try:
            demos = list(d.rglob("Replay.demo"))
        except Exception:
            demos = []
        for p in demos:
            try:
                st = p.stat()
            except Exception:
                continue
            iid = _replay_id(p)
            ts_ms = int(st.st_mtime * 1000)
            m = meta.get(iid, {})
            rp = _nearest_rp(ts_ms, hist)
            region = _nearest_queue(ts_ms, log)
            items.append({
                "id": iid,
                "profile": profile,
                "folder": str(p.parent),
                "ts": ts_ms,
                "sizeMB": round(st.st_size / 1048576.0, 1),
                "rpDelta": rp,
                "regionId": region,
                "note": m.get("note", ""),
                "review": bool(m.get("review")),
                "reviewed": bool(m.get("reviewed")),
            })
    items.sort(key=lambda x: x["ts"], reverse=True)
    return {"items": items, "total": len(items),
            "dirs": [str(d) for _, d in _replay_dirs()]}


def replay_set_note(iid, note=None, review=None, reviewed=None):
    with _REPLAY_LOCK:
        meta = load_replay_meta()
        rec = meta.setdefault(iid, {})
        if note is not None:
            rec["note"] = str(note)[:300]
        if review is not None:
            rec["review"] = bool(review)
        if reviewed is not None:
            rec["reviewed"] = bool(reviewed)
        _save_replay_meta(meta)
    return {"ok": True, "id": iid}


def replay_open_folder(iid):
    """Open the replay's folder in Explorer so the user can find/manage it."""
    for _profile, d in _replay_dirs():
        try:
            for p in d.rglob("Replay.demo"):
                if _replay_id(p) == iid:
                    if OS == "Windows":
                        subprocess.run(["explorer", "/select,", str(p)], **_NO_WINDOW_KW)
                    return {"ok": True}
        except Exception:
            pass
    return {"ok": False, "message": "replay not found"}


def save_servers(data):
    try:
        Path(SERVERS_PATH).write_text(json.dumps(data, indent=2))
        return True
    except Exception:
        return False


def record_server(region_id, server):
    """Upsert the game-server IP we just saw for a region. Called from the
    auto-capture loop when a match is detected. Best effort, never raises."""
    if not region_id or not isinstance(server, dict):
        return
    ip = server.get("ip")
    if not ip:
        return
    now_ms = int(time.time() * 1000)
    with _SERVERS_LOCK:
        data = load_servers()
        regions = data.setdefault("regions", {})
        bucket = regions.setdefault(region_id, {})
        rec = bucket.get(ip)
        if rec:
            rec["lastSeen"] = now_ms
            rec["count"] = int(rec.get("count", 0)) + 1
        else:
            rec = {"firstSeen": now_ms, "lastSeen": now_ms, "count": 1}
            bucket[ip] = rec
        rec["port"] = server.get("port") or rec.get("port")
        rec["city"] = server.get("city") or rec.get("city")
        rec["country"] = server.get("country") or rec.get("country")
        # evict oldest-seen IPs if a region's pool grows too large
        if len(bucket) > _SERVERS_MAX_PER_REGION:
            oldest = sorted(bucket.items(),
                            key=lambda kv: kv[1].get("lastSeen", 0))
            for ip_del, _ in oldest[:len(bucket) - _SERVERS_MAX_PER_REGION]:
                bucket.pop(ip_del, None)
        data["updated"] = now_ms
        save_servers(data)


def region_server_ips(region_id):
    """List of harvested IPs for a region (freshest first)."""
    bucket = (load_servers().get("regions", {}) or {}).get(region_id, {}) or {}
    return [ip for ip, _ in sorted(bucket.items(),
                                   key=lambda kv: kv[1].get("lastSeen", 0),
                                   reverse=True)]


# Curated FragPunk server ranges per region (Alibaba/GCP), seeded from live capture so
# the region lock has something to block before the learned map fills. 8.221 is SPLIT
# across us-east (8.221.51) and asia-east (8.221.146), so it's only ever seeded at /24
# -- never blanket the whole 8.221/16. Learned /24s (record_server) are merged on top.
# COVERAGE NOTE: FragPunk does NOT switch region when only SOME of a region's servers
# are blocked -- it just picks another server in the SAME region (confirmed live: with
# 8.221.51/59 blocked it hopped to 8.221.49). So forcing a region change needs the
# away-regions covered COMPREHENSIVELY. The us-east session+gameplay cluster spans
# 8.221.48-63 (seen .49/.51/.59) -> block the whole 8.221.48.0/20; Tokyo is 8.221.146
# (safely OUTSIDE it) and the lobby 8.221.58.x is inside but protected by the :11000
# port whitelist. UDP gameplay also uses Alibaba 47.77 + GCP 136.119 (Iowa) -> /16.
_REGION_SEED_CIDRS = {
    "us-east":   ["8.221.48.0/20", "47.77.0.0/16", "47.246.0.0/16", "136.119.0.0/16"],
    "us-west":   [],
    "eu":        ["8.211.0.0/16"],
    "asia-se":   ["47.84.0.0/16", "8.219.0.0/16"],
    "asia-east": ["8.221.146.0/24"],
}


def _ip_to_24(ip):
    a, b, c, _d = ip.split(".")
    return "%s.%s.%s.0/24" % (a, b, c)


def region_block_map(target_region):
    """Build {region_id: [cidr,...]} for every region EXCEPT `target_region`, so
    applying it forces matchmaking onto the target. Merges the curated seed table
    with the /24s we've LEARNED from real matches (tightens the more you play).
    The lobby :11000 / backend :18110 / web :443 are protected by PORT in the lock
    module, so it's safe even when a blocked region shares a prefix with the lobby."""
    target = (target_region or "").strip()
    regions = (load_servers().get("regions", {}) or {})
    all_rids = set(regions.keys()) | set(_REGION_SEED_CIDRS.keys())
    block = {}
    for rid in all_rids:
        if not rid or rid == target:
            continue
        cidrs = set(_REGION_SEED_CIDRS.get(rid, []))
        for ip in (regions.get(rid, {}) or {}).keys():
            try:
                cidrs.add(_ip_to_24(ip))
            except Exception:
                pass
        if cidrs:
            block[rid] = sorted(cidrs)
    return block


def _region_real_ping(region_id):
    """Best (lowest) recent real game-server ping for a region, or None.
    Only trusts pings measured in the last 30 min so a stale value from a
    different route doesn't mislead the ranking."""
    bucket = (load_servers().get("regions", {}) or {}).get(region_id, {}) or {}
    now_ms = int(time.time() * 1000)
    vals = []
    for rec in bucket.values():
        ms = rec.get("lastPingMs")
        ts = rec.get("pingTs", 0)
        if ms is not None and (now_ms - ts) < 30 * 60 * 1000:
            vals.append(ms)
    return min(vals) if vals else None


def scout_ping_servers(region_ids=None):
    """Ping the harvested game-server IPs (threaded) and store lastPingMs on
    each. Best effort -- many cloud hosts drop ICMP echo, so silent IPs simply
    keep no ping and the ranking falls back to the VPN endpoint ping.

    Pings are measured from your CURRENT route, so this answers 'from where I
    sit now, which Fragpunk datacenter is closest/best-routed'."""
    data = load_servers()
    regions = data.get("regions", {}) or {}
    targets = []  # (rid, ip)
    for rid, bucket in regions.items():
        if region_ids and rid not in region_ids:
            continue
        # ping at most a few freshest IPs per region to stay cheap
        for ip in (region_server_ips(rid)[:4]):
            targets.append((rid, ip))
    if not targets:
        return {}

    results = {}
    lock = threading.Lock()

    def worker(rid, ip):
        with _PING_SEM:           # share the global ping throttle
            ms = ping_host(ip, timeout_ms=1200)
        with lock:
            results.setdefault(rid, {})[ip] = ms

    threads = [threading.Thread(target=worker, args=(rid, ip))
               for rid, ip in targets]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=4)

    # write the measured pings back into the store
    now_ms = int(time.time() * 1000)
    with _SERVERS_LOCK:
        data = load_servers()
        regions = data.setdefault("regions", {})
        for rid, ipmap in results.items():
            bucket = regions.get(rid, {})
            for ip, ms in ipmap.items():
                if ip in bucket and ms is not None:
                    bucket[ip]["lastPingMs"] = round(ms, 1)
                    bucket[ip]["pingTs"] = now_ms
        save_servers(data)
    return results


# ===========================================================================
# HEAT + QUEUE STATS + RECOMMENDATION  (the Scout's brain)
# ---------------------------------------------------------------------------
# HONEST LIMITATION: Fragpunk publishes no population API and we can't run a
# scout client (anti-cheat). So "how busy is this region" is INFERRED, not
# observed. Two inputs feed the estimate:
#   * heat   -- a time-of-day x pool-depth heuristic (more people on in the
#               local evening; US-East/EU pools are deepest).
#   * history-- the median of YOUR own logged queue times for the region,
#               which is real ground truth. The more samples we have, the more
#               the estimate leans on history instead of the heuristic.
# Real game-server ping then biases ties toward the better-routed region.
# ===========================================================================
_POOL_BASE = {"deep": 1.0, "standard": 0.62, "thin": 0.34}


def _region_local_hour(region_id):
    r = REGION_BY_ID.get(region_id) or {}
    utc_h = (time.time() / 3600.0) % 24
    return (utc_h + r.get("utc", 0)) % 24


def _time_weight(h):
    """Rough share of a region's player base online at local hour h (0..1)."""
    if 18 <= h < 24:
        return 1.0          # prime time
    if 16 <= h < 18:
        return 0.85
    if 13 <= h < 16:
        return 0.62
    if 10 <= h < 13:
        return 0.48
    if 7 <= h < 10:
        return 0.30
    if 2 <= h < 7:
        return 0.18         # dead hours
    return 0.42             # 0:00-2:00, winding down


def region_heat(region_id):
    """0..1 estimate of how populated a region is right now."""
    r = REGION_BY_ID.get(region_id) or {}
    base = _POOL_BASE.get(r.get("pool"), 0.5)
    return round(base * _time_weight(_region_local_hour(region_id)), 3)


def _heat_expected_queue(heat):
    """Map heat (0..1) to an expected queue in seconds: busier -> faster."""
    return round(18 + (1 - heat) * 210, 1)   # ~18s when packed, ~228s when dead


def region_queue_stats(region_id, hour_window=3):
    """Your logged queue stats for a region. Returns median seconds + sample
    count. If enough same-time-of-day samples exist, prefers those (queue
    behaviour at 2am differs from 8pm)."""
    rows = [e for e in load_log()
            if e.get("regionId") == region_id
            and isinstance(e.get("duration"), (int, float))
            and e.get("duration", 0) > 0]
    matched = [e for e in rows if e.get("outcome") == "matched"] or rows
    if not matched:
        return {"count": 0, "medianSec": None, "timeBucketed": False}

    def _median(seq):
        s = sorted(seq)
        n = len(s)
        return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2.0

    now_h = _region_local_hour(region_id)

    def _near(ts):
        local = ((ts / 1000.0 / 3600.0) + (REGION_BY_ID.get(region_id, {}).get("utc", 0))) % 24
        d = abs(local - now_h)
        return min(d, 24 - d) <= hour_window

    same_time = [e["duration"] for e in matched if _near(e.get("ts", 0))]
    if len(same_time) >= 3:
        return {"count": len(same_time),
                "medianSec": round(_median(same_time), 1),
                "timeBucketed": True}
    return {"count": len(matched),
            "medianSec": round(_median([e["duration"] for e in matched]), 1),
            "timeBucketed": False}


# ===========================================================================
# LIVE POPULATION  (real global player count from Steam, anchors the heat model)
# ---------------------------------------------------------------------------
# The heat estimate above is a pure time-of-day heuristic. Steam publishes
# FragPunk's live concurrent player count (public, no key), which is REAL data.
# We poll it, keep a rolling history to learn the recent peak, and derive a
# gentle multiplier on expected queue times: fewer players than peak -> longer
# queues. Global + Steam-only (FragPunk is also on Epic), so it's a trend
# anchor, not an exact per-region count -- but far better than guessing.
# ===========================================================================
STEAM_APPID = "2943650"
_PLAYERS_CACHE = {"count": None, "ts": 0.0}
_PLAYERS_FETCH_LOCK = threading.Lock()
_POPULATION = {"mult": 1.0, "current": None, "recentPeak": None,
               "factor": None, "samples": 0, "ts": None}
_POPULATION_LOCK = threading.Lock()


def load_players():
    try:
        return json.loads(Path(PLAYERS_PATH).read_text())
    except Exception:
        return []


def _record_players(count):
    """Append to the rolling player-count history (coalesce to ~8-min buckets)."""
    try:
        data = load_players()
        now_ms = int(time.time() * 1000)
        if data and now_ms - data[-1].get("ts", 0) < 8 * 60 * 1000:
            data[-1] = {"ts": now_ms, "count": count}
        else:
            data.append({"ts": now_ms, "count": count})
        data = data[-600:]                         # ~days of 8-min samples
        Path(PLAYERS_PATH).write_text(json.dumps(data))
    except Exception:
        pass


def steam_players(force=False):
    """Live FragPunk concurrent players from Steam (cached ~2 min). None on
    failure (keeps the last value). Never raises."""
    now = time.time()
    with _PLAYERS_FETCH_LOCK:
        if (not force and _PLAYERS_CACHE["count"] is not None
                and now - _PLAYERS_CACHE["ts"] < 120):
            return _PLAYERS_CACHE["count"]
    try:
        url = ("https://api.steampowered.com/ISteamUserStats/"
               "GetNumberOfCurrentPlayers/v1/?appid=" + STEAM_APPID)
        with urllib.request.urlopen(url, timeout=6) as r:
            d = json.loads(r.read())
        count = int(d.get("response", {}).get("player_count"))
        if count <= 0:
            return _PLAYERS_CACHE["count"]
    except Exception:
        return _PLAYERS_CACHE["count"]
    with _PLAYERS_FETCH_LOCK:
        _PLAYERS_CACHE["count"] = count
        _PLAYERS_CACHE["ts"] = now
    _record_players(count)
    return count


def refresh_population():
    """Recompute the population snapshot + queue multiplier. Called from the
    Scout loop so compute_recommendation can stay a cheap pure read."""
    count = steam_players()
    hist = [x["count"] for x in load_players()
            if isinstance(x.get("count"), (int, float))]
    peak = max(hist) if hist else None
    factor = None
    mult = 1.0
    if count and peak:
        factor = round(max(0.1, min(1.0, count / peak)), 2)
        # gentle: only act with enough history; at 50% of peak ~1.5x queues
        if len(hist) >= 6:
            mult = round(max(0.85, min(2.2, (1.0 / factor) ** 0.6)), 2)
    snap = {"mult": mult, "current": count, "recentPeak": peak,
            "factor": factor, "samples": len(hist),
            "ts": int(time.time() * 1000)}
    with _POPULATION_LOCK:
        _POPULATION.update(snap)
    return snap


def population_snapshot():
    with _POPULATION_LOCK:
        return dict(_POPULATION)


def _median(xs):
    s = sorted(xs)
    n = len(s)
    if not s:
        return None
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2.0


def play_insights():
    """Analytics from your queue log + population history: queue time by region,
    by local hour-of-day (-> best times to play), and players-online by hour.
    All from data we already collect -- no external source."""
    matched = [e for e in load_log()
               if e.get("outcome") == "matched"
               and isinstance(e.get("duration"), (int, float))
               and e.get("duration", 0) > 0]

    by_r = {}
    for e in matched:
        by_r.setdefault(e.get("regionId"), []).append(e["duration"])
    by_region = [{
        "regionId": r, "name": (REGION_BY_ID.get(r) or {}).get("name") or r,
        "count": len(v), "medianSec": round(_median(v), 1),
        "avgSec": round(sum(v) / len(v), 1),
    } for r, v in by_r.items() if r]
    by_region.sort(key=lambda x: x["medianSec"])

    by_h = {}
    for e in matched:
        try:
            h = time.localtime(e["ts"] / 1000).tm_hour
        except Exception:
            continue
        by_h.setdefault(h, []).append(e["duration"])
    by_hour = [{"hour": h, "count": len(by_h[h]),
                "medianSec": round(_median(by_h[h]), 1)} for h in sorted(by_h)]
    best_hours = sorted([x for x in by_hour if x["count"] >= 2],
                        key=lambda x: x["medianSec"])[:3]

    pop_h = {}
    for p in load_players():
        try:
            h = time.localtime(p["ts"] / 1000).tm_hour
        except Exception:
            continue
        if isinstance(p.get("count"), (int, float)):
            pop_h.setdefault(h, []).append(p["count"])
    pop_by_hour = [{"hour": h, "avg": round(sum(pop_h[h]) / len(pop_h[h]))}
                   for h in sorted(pop_h)]

    return {"totalMatched": len(matched), "byRegion": by_region,
            "byHour": by_hour, "bestHours": best_hours, "popByHour": pop_by_hour}


def compute_recommendation(max_ping=None, preferred=None):
    """Rank mapped regions by expected time-to-match. Returns a sorted list of
    rows (best first) plus the chosen best region id. Pure function of current
    pings + heat + your queue log + cached population -- safe to call often."""
    if max_ping is None:
        try:
            max_ping = float(get_setting("maxPing", 0)) or None
        except Exception:
            max_ping = None
    if preferred is None:
        preferred = get_setting("preferredRegion") or None

    pop_mult = population_snapshot().get("mult") or 1.0

    rows = []
    for r in REGIONS:
        rid = r["id"]
        if not (STATE["configs"].get(rid)):
            continue   # only regions you actually have a config for are routable
        vpn_ping = region_best_latency(rid)
        real_ping = _region_real_ping(rid)
        ping = real_ping if real_ping is not None else vpn_ping
        heat = region_heat(rid)
        stats = region_queue_stats(rid)
        heat_exp = _heat_expected_queue(heat)
        if stats["count"] > 0 and stats["medianSec"] is not None:
            # trust history more as samples accumulate (caps out ~0.7 weight)
            n = min(stats["count"], 8)
            w = n / (n + 3.0)
            expected = w * stats["medianSec"] + (1 - w) * heat_exp
        else:
            expected = heat_exp
        # anchor to real live population: fewer players than peak -> longer queues
        expected = round(expected * pop_mult, 1)
        filtered = (max_ping is not None and ping is not None and ping > max_ping)
        # score: expected queue dominates; ping is a mild quality bias; the
        # preferred region gets a small handicap so it wins close calls.
        score = expected + (ping if ping is not None else 140) * 0.25
        if preferred and rid == preferred:
            score -= 25
        rows.append({
            "regionId": rid,
            "name": r["name"],
            "code": r["code"],
            "pool": r["pool"],
            "localHour": round(_region_local_hour(rid), 1),
            "heat": heat,
            "vpnPingMs": round(vpn_ping, 1) if vpn_ping is not None else None,
            "realPingMs": round(real_ping, 1) if real_ping is not None else None,
            "pingMs": round(ping, 1) if ping is not None else None,
            "samples": stats["count"],
            "timeBucketed": stats["timeBucketed"],
            "medianSec": stats["medianSec"],
            "expectedQueueSec": expected,
            "serverIps": len(region_server_ips(rid)),
            "filtered": bool(filtered),
            "score": round(score, 1),
            "isPreferred": bool(preferred and rid == preferred),
        })

    # rank: unfiltered first, then by score ascending
    rows.sort(key=lambda x: (x["filtered"], x["score"]))
    best = next((x["regionId"] for x in rows if not x["filtered"]), None)
    if best is None and rows:
        best = rows[0]["regionId"]
    return {"ranking": rows, "best": best,
            "maxPing": max_ping, "preferred": preferred}


# ===========================================================================
# SETTINGS  (persisted to disk; both the UI and the backend read these)
# ---------------------------------------------------------------------------
# One source of truth on disk so prefs survive restarts AND the backend can act
# on the ones it needs (auto-capture, poll rate, auto-connect on launch).
# ===========================================================================
DEFAULT_SETTINGS = {
    # --- routing & VPN ---
    "autoConnectOnLaunch": False,   # bring up a tunnel when Fragpunk launches
    "autoConnectRegion": "",        # "" = best overall ping, else a region id
    "confirmTunnelSwitch": False,   # ask before switching an active tunnel
    "pingRefreshSeconds": 0,        # 0 = manual only (UI honors)
    "preferredRegion": "",          # bias the recommendation toward this region
    "maxPing": 120,                 # ping cap (mirrors the inline slider)
    # --- YOLO training data ---
    "autoHarvest": True,            # auto-import match recordings into the YOLO dataset (ADMIN-only at runtime)
    "harvestFolders": [],           # extra folders to watch (OBS output, etc.); clips/ is always included
    # --- live game / auto-capture ---
    "autoCapture": True,            # auto-log queue/match times from detection
    "gamePollSeconds": 6,           # backend monitor poll interval (2-60)
    "pauseDetectionWhenIdle": False,# stop polling while the game isn't running
    "suggestBetterRoute": True,     # nudge if a lower-ping region is available
    # --- scout (probe agent) ---
    "scoutEnabled": True,           # background pre-queue recon + ranking
    "scoutIntervalSeconds": 20,     # how often the scout re-ranks (8-120)
    "slowQueueNudge": True,         # nudge to hop when a queue runs long
    "slowQueueFactorPct": 150,      # nudge when queue > this % of predicted median
    "notifySlowQueue": False,       # desktop toast for the slow-queue nudge
    "queueMarkHotkeyEnabled": False,# global hotkey to mark the exact queue-start
    "queueMarkHotkey": "ctrl+alt+q",# the combo (OS RegisterHotKey, not a hook)
    # ---- AI Coach footage recorder (Phase 3) ----
    "autoRecord": True,             # auto start/stop the recorder during matches
    "fullMatchRecording": True,     # True = record the WHOLE match; False = rolling 60s highlight clips
    "recordingsMaxGB": 40,          # disk-sensitive auto-cleanup: cap the recordings folder (delete oldest)
    "recordingsMinFreeGB": 5,       # also keep at least this much free on the disk
    "autoClipMatchEnd": True,       # auto-save the recording when a match ends
    "clipHotkeyEnabled": False,     # global hotkey to save the last N seconds as a clip
    "clipHotkey": "ctrl+alt+c",     # the save-clip combo (OS RegisterHotKey, not a hook)
    "clipSeconds": 60,              # how many seconds back a saved clip captures (>=60s)
    "scoutHotkeyEnabled": False,    # global hotkey: vision-scout the screen + speak it aloud
    "scoutHotkey": "ctrl+alt+x",    # the scout combo (in-game voice readout)
    "voiceCmdHotkeyEnabled": False, # global hotkey: talk to the AI (mic -> whisper -> answer)
    "voiceCmdHotkey": "ctrl+alt+v", # the voice-command combo
    "voiceCmdSeconds": 5,           # how long the mic records per voice command
    "coachSpeak": True,             # coach speaks its replies/callouts aloud (neural voice)
    "ttsVoice": "",                 # selected Piper voice file ("" = first/default)
    "ttsRate": 1.0,                 # speech length_scale (1.0 normal; >1 slower/calmer)
    "onlineLearning": False,        # let the coach fetch FragPunk-ONLY facts (official+wiki)
    "autoMapCapture": True,         # auto-snap one map screenshot a few seconds into each match
    "autoRevertOnSwitch": True,     # undo a mid-match VPN switch that makes ping worse/dead
    "autoRevertGraceSec": 5,        # wait this long for the new tunnel before judging
    "autoRevertWorseMs": 80,        # revert if match ping is worse than baseline by this
    "notifyConnSpike": False,       # toast on in-match ping spike / packet loss
    "connSpikeMs": 60,              # spike = match ping > running avg + this
    "overlayOcr": True,            # read the game's own net overlay via OCR (real ping/loss)
    "rankTracker": True,           # OCR the lobby rank card -> rank/RP over time (menu only)
    "queueTimerOcr": True,         # OCR the queue pill -> precise queue time (no menu-dwell inflation)
    # --- news ---
    "newsAutoRefreshSeconds": 0,    # 0 = off (UI honors)
    "newsSources": "both",          # both | steam | youtube
    "newsOpenIn": "inapp",          # inapp | browser
    "newsCount": 12,                # how many stories to pull
    # --- timer / log ---
    "autoStartTimerOnConnect": True,
    "minLogSeconds": 3,             # ignore queues shorter than this
    # --- connection health / notifications ---
    "autoReconnect": False,         # auto-reconnect if the active tunnel drops
    "notifyMatchFound": False,      # desktop toast when a match is found
    "notifyTunnelDrop": False,      # desktop toast if the tunnel goes down
    "notifyBetterRoute": False,     # desktop toast if a lower-ping route exists
    # --- appearance ---
    "theme": "pink",                # accent: pink|cyan|amber|green|purple|red
    "compact": False,               # denser cards
    "wallpaperDim": 50,             # % darken on the user's chosen background image
    "lite": True,                   # freeze animations (GPU saver)
    "clock24h": True,               # 24h vs 12h header clock
    "defaultTab": "routing",        # routing | news | status
    # --- game info / updates ---
    "lastSeenGameVersion": "",      # for update detection across launches
    "notifyGameUpdate": True,       # toast when a new game version is detected
    # --- route presets (saved region+config combos) ---
    "routePresets": [],             # [{name, regionId, config}]
}
_SETTINGS = dict(DEFAULT_SETTINGS)
_SETTINGS_LOCK = threading.Lock()


def load_settings():
    """Load settings from disk, filling any missing keys with defaults."""
    global _SETTINGS
    merged = dict(DEFAULT_SETTINGS)
    try:
        data = json.loads(Path(SETTINGS_PATH).read_text())
        if isinstance(data, dict):
            for k, v in data.items():
                if k in DEFAULT_SETTINGS:
                    merged[k] = v
    except Exception:
        pass
    _SETTINGS = merged
    return dict(_SETTINGS)


def save_settings(updates):
    """Merge updates into settings and persist. Returns the full settings dict."""
    global _SETTINGS
    with _SETTINGS_LOCK:
        for k, v in (updates or {}).items():
            if k in DEFAULT_SETTINGS:
                _SETTINGS[k] = v
        try:
            Path(SETTINGS_PATH).write_text(json.dumps(_SETTINGS, indent=2))
        except Exception:
            pass
        return dict(_SETTINGS)


def get_setting(key, default=None):
    return _SETTINGS.get(key, DEFAULT_SETTINGS.get(key, default))


def _best_overall_region():
    """Region id with the lowest measured latency among mapped regions, or None."""
    best, best_ms = None, float("inf")
    for r in REGIONS:
        ms = region_best_latency(r["id"])
        if ms is not None and ms < best_ms:
            best, best_ms = r["id"], ms
    return best


# ===========================================================================
# AUTO-CAPTURE  (catch queue-start / match-found / match-end from detection)
# ---------------------------------------------------------------------------
# Runs on its own background thread so it keeps working even while the UI window
# is hidden (which is exactly when you're in a match). It watches the game-state
# transitions and auto-fills the queue log:
#
#   not running -> menu      : GAME LAUNCHED   (start session; optional auto-connect)
#   menu        -> in match  : MATCH FOUND     (stop queue clock, log the duration)
#   in match    -> menu      : MATCH ENDED     (record match length, start next clock)
#   anything    -> not running: GAME CLOSED    (end session)
#
# HONEST LIMITATION: the exact instant you press "queue" isn't on the wire, so
# the queue clock measures menu->match (includes any menu dwell). It's accurate
# for back-to-back requeues; the first match after launch includes menu setup,
# so that one is shown but NOT auto-logged. A short debounce (two consistent
# reads) keeps transient matchmaking blips from registering as a real match.
# ===========================================================================
AUTODETECT = {
    "phase": "idle",            # idle | menu | match
    "since": None,              # ms ts of current committed phase
    "queueStartTs": None,       # ms ts the current queue clock started
    "queueManual": False,       # True when the user tapped "Mark Queue" (precise start)
    "queueOcrStartTs": None,    # ms ts the queue truly started, from OCR'd on-screen timer
    "queueOcrTs": None,         # ms ts of that OCR reading (freshness guard)
    "queueOcrMode": None,       # mode read off the queue pill (RANKED/CASUAL/...)
    "matchStartTs": None,       # ms ts the current match started
    "currentServer": None,      # server dict while in a match
    "matchIsTraining": False,   # True when the current "match" is Training Base/warm-up (not logged)
    "sessionStartTs": None,     # ms ts the game launched this session
    "matchesThisSession": 0,
    "events": [],               # recent events (newest first) for the UI feed
    # debounce bookkeeping
    "_pendingRaw": None,
    "_pendingIp": None,
    "_pendingSince": None,
    "_pendingCount": 0,
    "_slowNudgeFor": None,      # queueStartTs we've already fired a slow-queue nudge for
}
_AUTODETECT_LOCK = threading.Lock()
_AUTOCONNECT_ONCE = {"done": False}  # so we only auto-connect once per launch


def _ad_event(kind, **extra):
    ev = {"kind": kind, "ts": int(time.time() * 1000)}
    ev.update(extra)
    AUTODETECT["events"].insert(0, ev)
    AUTODETECT["events"] = AUTODETECT["events"][:20]


def _maybe_auto_connect_on_launch():
    """If enabled and nothing is connected, bring up a tunnel when the game
    starts. Best overall ping by default, or a chosen region."""
    if not get_setting("autoConnectOnLaunch"):
        return
    if STATE.get("active_tunnel"):
        return
    if _AUTOCONNECT_ONCE["done"]:
        return
    target = get_setting("autoConnectRegion") or _best_overall_region()
    if not target:
        return
    _AUTOCONNECT_ONCE["done"] = True
    try:
        res = connect_region(target)
        _ad_event("auto_connected", regionId=target, ok=bool(res.get("ok")))
    except Exception:
        pass


# LIVE GAME WATCH -- the AI "sees" the match in real time (screen OCR, anti-cheat
# safe). Runs only while in a match, throttled, and STOPS OCRing the mode once it
# resolves (so it costs almost nothing). Fixes matches logging as mode "unknown",
# and exposes a live snapshot (/api/live) the coach + UI can read.
LIVE_STATE = {"inMatch": False, "mode": None, "map": None, "ts": 0.0, "since": 0.0}


def _live_match_watch():
    LIVE_STATE["inMatch"] = True
    if not LIVE_STATE.get("since"):
        LIVE_STATE["since"] = time.time()
    now = time.time()
    if now - LIVE_STATE.get("ts", 0) < 8:        # throttle screen reads to ~8s
        return
    LIVE_STATE["ts"] = now
    mk = AUTODETECT.get("matchMode")
    if mk and mk != "unknown":
        LIVE_STATE["mode"] = mk                  # already known -> no OCR cost
        return
    # only retry the mode OCR for the first ~75s of the match (it's shown early);
    # after that stop, so we never repeatedly run OCR / hold the lock mid-match.
    if now - LIVE_STATE.get("since", now) > 75:
        return
    if not get_setting("overlayOcr", True) or fragroute_modes is None:
        return
    try:                                          # keep retrying the mode OCR live
        gm = read_game_mode()
        k = fragroute_modes.classify(gm.get("raw", ""))[0]
        if k != "unknown":
            LIVE_STATE["mode"] = k
            AUTODETECT["matchMode"] = k           # so match-end logs the real mode
            diag("live", True, msg="resolved mode=%s mid-match" % k)
    except Exception as e:
        diag("live", False, msg="live watch", exc=e)


def _autodetect_tick():
    """One poll: read game state, debounce, detect transitions, auto-log."""
    # optional: skip the work entirely while idle if the user asked us to
    st = game_status()
    running = bool(st.get("running"))
    server = st.get("server")
    server_ip = (server or {}).get("ip")
    now = time.time()

    # ROUTE-CHANGE STICKINESS: a VPN switch mid-match briefly drops the game's
    # connection to the match server (your IP/path changed) before it reconnects
    # to the SAME server. For a grace window after a switch, if we were in a
    # match but momentarily see no server, hold the last known server so the
    # blip isn't read as match-end + a brand-new match (which would log a bogus
    # queue time). Once the connection re-establishes, normal detection resumes.
    if (running and not server_ip
            and AUTODETECT.get("phase") == "match"
            and AUTODETECT.get("currentServer")
            and (now - STATE.get("routeChangeTs", 0)) < 25):
        server = AUTODETECT["currentServer"]
        server_ip = (server or {}).get("ip")

    with _AUTODETECT_LOCK:
        # raw phase from this single read
        if not running:
            raw = "idle"
        elif server_ip:
            raw = "match"
        else:
            raw = "menu"

        # keep the live server fresh even before a commit
        if raw == "match":
            AUTODETECT["currentServer"] = server

        # ----- debounce: need two consistent reads before committing -----
        same = (raw == AUTODETECT["_pendingRaw"] and
                (raw != "match" or server_ip == AUTODETECT["_pendingIp"]))
        if not same:
            AUTODETECT["_pendingRaw"] = raw
            AUTODETECT["_pendingIp"] = server_ip
            AUTODETECT["_pendingSince"] = now
            AUTODETECT["_pendingCount"] = 1
            return
        AUTODETECT["_pendingCount"] += 1
        if AUTODETECT["_pendingCount"] < 2:
            return

        committed = raw
        prev = AUTODETECT["phase"]
        # use the FIRST time we saw this phase, so timings aren't skewed by
        # the extra confirmation tick
        t_ms = int(AUTODETECT.get("_pendingSince", now) * 1000)

        if committed == prev:
            if committed == "match":
                AUTODETECT["currentServer"] = server
                _live_match_watch()      # AI watches the live match (OCR mode/state)
            else:
                LIVE_STATE["inMatch"] = False
            return

        # =================== transition prev -> committed ===================
        # GAME LAUNCHED
        if prev == "idle" and committed != "idle":
            AUTODETECT["sessionStartTs"] = t_ms
            AUTODETECT["matchesThisSession"] = 0
            _AUTOCONNECT_ONCE["done"] = False
            _ad_event("game_launched")
            _maybe_auto_connect_on_launch()

        # GAME CLOSED -> now it's safe to stop the rolling recorder
        if committed == "idle" and prev != "idle":
            capture_game_closed()
            LIVE_STATE.update(inMatch=False, since=0.0)
            if fragroute_live is not None:
                fragroute_live.stop("game closed")

        # ENTERING MENU (from launch or after a match)
        if committed == "menu":
            if prev == "match":
                # record the match that just ended
                ms = AUTODETECT["matchStartTs"]
                match_dur = max(0, int((t_ms - ms) / 1000)) if ms else 0
                match_mode = AUTODETECT.get("matchMode") or "unknown"
                if ms:
                    _ad_event("match_ended", durationS=match_dur)
                capture_match_end(match_dur)   # auto-save final clip (gated; skips short flaps)
                AUTODETECT["matchStartTs"] = None
                AUTODETECT["currentServer"] = None
                was_training = AUTODETECT.get("matchIsTraining")
                AUTODETECT["matchIsTraining"] = False
                # LEARN from this match (skip Training Base / warm-up sessions)
                if ms and not was_training and fragroute_learning is not None:
                    try:
                        fragroute_learning.observe_match(match_mode, durationS=match_dur)
                        diag("learning", True, msg="observed %s (%ds)" % (match_mode, match_dur))
                    except Exception as e:
                        diag("learning", False, msg="observe", exc=e)
                LIVE_STATE.update(inMatch=False, since=0.0)
                # capture this match's RP result right away -- the 55s menu poll
                # can miss it on a fast requeue (common after a LOSS), which made
                # losses look untracked. A real match end forces a prompt read.
                if not was_training:
                    _schedule_post_match_rank_read()
            # start a fresh queue clock (auto-timed until the user marks it)
            AUTODETECT["queueStartTs"] = t_ms
            AUTODETECT["queueManual"] = False
            AUTODETECT["queueOcrStartTs"] = None
            AUTODETECT["queueOcrTs"] = None

        # ENTERING MATCH
        if committed == "match":
            AUTODETECT["matchStartTs"] = t_ms
            AUTODETECT["currentServer"] = server
            # MODE CHECK: Training Base / warm-up open a game connection just like
            # a real match. OCR the top strip to tell them apart so they never log
            # as real matches (which would pollute queue history + lancer stats).
            training = False
            mode_key = "unknown"
            if get_setting("overlayOcr", True):
                try:
                    gm = read_game_mode(tries=3)   # retry empty reads so warm-up isn't logged as a match
                    training = bool(gm.get("training"))
                    if fragroute_modes is not None:
                        mode_key = fragroute_modes.classify(gm.get("raw", ""))[0]
                except Exception:
                    training = False
            AUTODETECT["matchIsTraining"] = training
            AUTODETECT["matchMode"] = mode_key
            if training:
                _ad_event("training_session")
            else:
                # REAL match starting -> kill any live detector instantly. The
                # loop self-stops too, but this closes the OCR-cache lag window so
                # the detector can never run into real PvP.
                if fragroute_live is not None:
                    fragroute_live.stop("real match started")
                AUTODETECT["matchesThisSession"] += 1
                if fragroute_llm is not None:
                    try:
                        fragroute_llm.set_prefer_fast(True)
                        fragroute_llm.release_for_game()  # free the 4070 for the match
                    except Exception:
                        pass
                # free the offline detector + CLIP RAM during the match (they're
                # only used for labeling/VOD review, never in-match) -- zero footprint.
                for _m in (fragroute_yolo, fragroute_embed):
                    if _m is not None:
                        try:
                            _m.release()
                        except Exception:
                            pass
                capture_auto_start("match")   # arm rolling recorder (gated by autoRecord)
                if get_setting("autoMapCapture", True):
                    threading.Thread(target=_auto_map_shot, daemon=True).start()
            first_of_session = (AUTODETECT["matchesThisSession"] <= 1)
            # Region attribution: the VPN exit region (when connected) IS the
            # matchmaking region you're playing -- and it's RELIABLE, unlike GeoIP
            # on these Alibaba match IPs (which mislabels e.g. an Asia East server
            # as Singapore/asia-se). So prefer the active route's region; fall back
            # to the server's GeoIP/DNS region only when not on a tunnel.
            geo_rid = (server or {}).get("regionId")
            on_vpn = bool(STATE.get("active_region"))
            rid = STATE.get("active_region") or geo_rid
            # harvest the real game-server IP under the region you actually queued
            # in, so the Route Optimizer registers it (skip in Training Base).
            if rid and not training:
                try:
                    record_server(rid, server)
                    # OFF-VPN tracking: with no tunnel, GeoIP names the raw match IP
                    # (verified: 8.211->eu, 47.246->us-east, etc.). Measure the REAL
                    # ping to the live server we just harvested so the Live Game /
                    # Route Optimizer show truth (ping beats GeoIP guessing), and log
                    # it so off-VPN match tracking is observable in the diag.
                    if not on_vpn:
                        diag("scout", True, msg="tracked off-VPN match: %s in %s (%s)"
                             % (server.get("ip"), rid, server.get("where") or "?"))
                        threading.Thread(target=lambda: scout_ping_servers([rid]),
                                         daemon=True).start()
                except Exception:
                    pass
            elif not training and not on_vpn and (server or {}).get("ip"):
                # off-VPN but GeoIP couldn't map a region -> don't silently drop it;
                # note the raw server so the user can still see what they connected to.
                diag("scout", False, msg="off-VPN match on %s (region unresolved)"
                     % (server.get("ip")))
            qs = AUTODETECT["queueStartTs"]
            manual = bool(AUTODETECT.get("queueManual"))
            if qs and not training:
                dur = max(0, int((t_ms - qs) / 1000))
                # prefer the OCR'd on-screen queue timer when we have a recent
                # reading: it counts ONLY real queue time, not menu/warm-up dwell,
                # which fixes the inflated first-of-session estimate.
                ocr_start = AUTODETECT.get("queueOcrStartTs")
                ocr_ts = AUTODETECT.get("queueOcrTs")
                ocr_used = False
                if ocr_start and ocr_ts and (t_ms - ocr_ts) <= 90000:
                    dur = max(0, int((t_ms - ocr_start) / 1000))
                    ocr_used = True
                _ad_event("match_found", durationS=dur, regionId=rid,
                          includesMenu=(first_of_session and not manual and not ocr_used),
                          manual=manual, ocr=ocr_used)
                # auto-log a trustworthy queue measurement: enabled, has a region,
                # at least minLogSeconds, not an absurd outlier. A MANUAL mark or an
                # OCR-derived duration is precise, so it logs even on the first match
                # of a session (which we'd otherwise skip as menu-inflated).
                if (get_setting("autoCapture")
                        and (manual or ocr_used or not first_of_session)
                        and dur >= int(get_setting("minLogSeconds", 3))
                        and dur <= 600):
                    # count the match even with NO VPN / unresolved region, so
                    # non-VPN matches still land in stats + the learning store.
                    append_log({"regionId": rid or "unknown", "duration": dur,
                                "outcome": "matched", "ts": t_ms, "auto": True,
                                **({"mode": mode_key} if (mode_key and mode_key != "unknown") else {}),
                                **({"manual": True} if manual else {}),
                                **({"ocr": True} if ocr_used else {})})
                # desktop toast when a match is found (useful while the window
                # is hidden in-game) -- best effort, gated by the setting
                if get_setting("notifyMatchFound"):
                    rname = (REGION_BY_ID.get(rid) or {}).get("name") or rid or "server"
                    _notify("Match found", f"{rname} \u00b7 queued {dur // 60}:{dur % 60:02d}")
            AUTODETECT["queueStartTs"] = None
            AUTODETECT["queueManual"] = False
            AUTODETECT["queueOcrStartTs"] = None
            AUTODETECT["queueOcrTs"] = None

            # one-shot better-route nudge when entering a match: compare the
            # match server's region VPN ping to the best overall region
            if get_setting("notifyBetterRoute") and rid:
                try:
                    cur_best = region_best_latency(rid)
                    bo = _best_overall_region()
                    bo_ms = region_best_latency(bo) if bo else None
                    if (bo and bo != rid and bo_ms is not None
                            and (cur_best is None or bo_ms + 15 < cur_best)):
                        bname = (REGION_BY_ID.get(bo) or {}).get("name") or bo
                        _notify("Better route available",
                                f"{bname} ~{round(bo_ms)}ms \u2014 switch between matches")
                except Exception:
                    pass

        # GAME CLOSED
        if committed == "idle" and prev != "idle":
            if prev == "match":
                ms = AUTODETECT["matchStartTs"]
                if ms:
                    _ad_event("match_ended",
                              durationS=max(0, int((t_ms - ms) / 1000)))
            _ad_event("game_closed")
            AUTODETECT["queueStartTs"] = None
            AUTODETECT["queueManual"] = False
            AUTODETECT["queueOcrStartTs"] = None
            AUTODETECT["queueOcrTs"] = None
            AUTODETECT["matchStartTs"] = None
            AUTODETECT["matchIsTraining"] = False
            AUTODETECT["currentServer"] = None

        AUTODETECT["phase"] = committed
        AUTODETECT["since"] = t_ms


def autodetect_status():
    """Snapshot for the UI: current phase, live elapsed clocks, session info,
    and the recent event feed."""
    with _AUTODETECT_LOCK:
        now = time.time()
        q = AUTODETECT["queueStartTs"]
        m = AUTODETECT["matchStartTs"]
        return {
            "phase": AUTODETECT["phase"],
            "queueElapsed": (int(now - q / 1000.0) if q else None),
            "queueManual": bool(AUTODETECT.get("queueManual")),
            "matchElapsed": (int(now - m / 1000.0) if m else None),
            "matchIsTraining": bool(AUTODETECT.get("matchIsTraining")),
            "queueIsFirst": (AUTODETECT["matchesThisSession"] == 0),
            "currentServer": AUTODETECT["currentServer"],
            "sessionStartTs": AUTODETECT["sessionStartTs"],
            "matchesThisSession": AUTODETECT["matchesThisSession"],
            "events": AUTODETECT["events"][:12],
            "autoCapture": bool(get_setting("autoCapture")),
        }


def mark_queue_start():
    """Set the queue clock to NOW -- the precise instant the user pressed Queue
    in-game. Overrides the menu->match auto-estimate for this queue, and makes
    the next match_found log even if it's the first of the session. Works only
    while the game is up (menu or match); harmless otherwise."""
    now_ms = int(time.time() * 1000)
    with _AUTODETECT_LOCK:
        phase = AUTODETECT.get("phase")
        if phase == "idle":
            return {"ok": False, "message": "game isn't running", "phase": phase}
        AUTODETECT["queueStartTs"] = now_ms
        AUTODETECT["queueManual"] = True
        AUTODETECT["_slowNudgeFor"] = None      # let the slow-queue nudge re-arm
    _ad_event("queue_marked")
    return {"ok": True, "phase": phase, "queueStartTs": now_ms}


# ---------------------------------------------------------------------------
# GLOBAL HOTKEY for "Mark Queue"  (so you can tap it without alt-tabbing)
# ---------------------------------------------------------------------------
# Uses Win32 RegisterHotKey -- a benign OS-level hotkey registration, NOT a
# keyboard hook or injection, so it's safe alongside anti-cheat (the same API
# Discord/OBS/Steam use). Off by default; the loop re-registers when the
# setting changes, so it can be toggled without a restart.
_HOTKEY_MODS = {"alt": 0x0001, "ctrl": 0x0002, "control": 0x0002,
                "shift": 0x0004, "win": 0x0008, "windows": 0x0008, "cmd": 0x0008}


def _parse_hotkey(combo):
    """'ctrl+alt+q' -> (modifier_flags, virtual_key) or (0, None)."""
    mods, vk = 0, None
    for tok in str(combo or "").lower().replace(" ", "").split("+"):
        if not tok:
            continue
        if tok in _HOTKEY_MODS:
            mods |= _HOTKEY_MODS[tok]
        elif len(tok) == 1:
            vk = ord(tok.upper())                       # letters/digits
        elif tok[0] == "f" and tok[1:].isdigit():
            vk = 0x70 + int(tok[1:]) - 1                # F1=0x70 ...
    return mods, vk


def _hotkey_loop():
    if OS != "Windows":
        return
    try:
        import ctypes
        from ctypes import wintypes
    except Exception:
        return
    user32 = ctypes.windll.user32
    WM_HOTKEY, MOD_NOREPEAT, PM_REMOVE = 0x0312, 0x4000, 0x0001
    ID_QUEUE, ID_CLIP, ID_SCOUT, ID_VOICE = 0xB0B, 0xB0C, 0xB0D, 0xB0E
    msg = wintypes.MSG()
    reg = {ID_QUEUE: None, ID_CLIP: None, ID_SCOUT: None, ID_VOICE: None}

    def _sync(hid, enabled_key, combo_key, default_combo):
        want = bool(get_setting(enabled_key, False))
        mods, vk = _parse_hotkey(get_setting(combo_key, default_combo))
        target = (mods, vk) if (want and vk) else None
        if target != reg[hid]:
            if reg[hid] is not None:
                user32.UnregisterHotKey(None, hid)
                reg[hid] = None
            if target is not None and user32.RegisterHotKey(
                    None, hid, target[0] | MOD_NOREPEAT, target[1]):
                reg[hid] = target

    while True:
        try:
            _sync(ID_QUEUE, "queueMarkHotkeyEnabled", "queueMarkHotkey", "ctrl+alt+q")
            _sync(ID_CLIP, "clipHotkeyEnabled", "clipHotkey", "ctrl+alt+c")
            _sync(ID_SCOUT, "scoutHotkeyEnabled", "scoutHotkey", "ctrl+alt+x")
            _sync(ID_VOICE, "voiceCmdHotkeyEnabled", "voiceCmdHotkey", "ctrl+alt+v")
            while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, PM_REMOVE):
                if msg.message == WM_HOTKEY:
                    hid = int(msg.wParam)
                    try:
                        if hid == ID_QUEUE:
                            mark_queue_start()
                        elif hid == ID_CLIP:
                            hotkey_save_clip()
                        elif hid == ID_SCOUT:
                            # vision scout takes ~10s -> run off the hotkey thread
                            threading.Thread(target=scout_voice, daemon=True).start()
                        elif hid == ID_VOICE:
                            # record + transcribe + answer (several s) -> off-thread
                            threading.Thread(target=voice_command, daemon=True).start()
                    except Exception:
                        pass
        except Exception:
            pass
        time.sleep(0.2)


def reset_autocapture_session():
    """Clear the live session counters/clocks (does NOT touch the saved log)."""
    with _AUTODETECT_LOCK:
        AUTODETECT.update({
            "queueStartTs": None, "queueManual": False, "matchStartTs": None,
            "currentServer": None, "matchIsTraining": False,
            "sessionStartTs": None, "matchesThisSession": 0, "events": [],
            "_pendingRaw": None, "_pendingIp": None, "_pendingSince": None,
            "_pendingCount": 0, "_slowNudgeFor": None,
        })
    return autodetect_status()


def _autodetect_loop():
    """Daemon loop. Polls on the user's interval; backs off while idle if asked."""
    while True:
        try:
            if not (get_setting("pauseDetectionWhenIdle")
                    and AUTODETECT["phase"] == "idle"):
                _autodetect_tick()
            else:
                # still do a light check so we notice the game launching
                _autodetect_tick()
            diag("autodetect", True)
        except Exception as e:
            diag("autodetect", False, msg="auto-capture tick", exc=e)
        try:
            interval = int(get_setting("gamePollSeconds", 6))
        except Exception:
            interval = 6
        interval = max(2, min(60, interval))
        # in a live match we only need to catch match-END, so poll slower --
        # fewer netstat/tasklist subprocess spawns while you're playing
        if _in_match():
            interval = max(interval, 12)
        time.sleep(interval)


# ===========================================================================
# SCOUT  (the probe agent: pre-queue reconnaissance + slow-queue bailout)
# ---------------------------------------------------------------------------
# A background thread that does the legwork BEFORE you queue so the moment you
# hit play you're already in the statistically-fastest, best-routed region:
#   * pings the harvested real game servers (when you're NOT in a match, so it
#     never disturbs live game traffic),
#   * keeps a warm "fastest-fill" ranking (heat + your history + real ping),
#   * watches the live queue clock and nudges you to hop if a queue runs long.
#
# It CANNOT see live player counts (no API, anti-cheat) -- the ranking is an
# inference, clearly labelled as such in the UI. What it removes is the manual
# guesswork of "which region should I queue in right now".
# ===========================================================================
SCOUT = {
    "ranking": [],      # list of region rows, best first (from compute_recommendation)
    "best": None,       # chosen best region id
    "updated": None,    # ms ts of last recompute
    "pingedAt": None,   # ms ts of last real-server ping sweep
    "nudge": None,      # {fromRegion,toRegion,elapsedSec,expectedSec,savesSec,ts} or None
    "lobby": None,      # live matchmaking-region quality while in menu:
                        # {ip,regionId,regionName,where,pingMs,ts}
    "matchPing": None,  # live "your real ping to the current match server":
                        # {ip,ms,avg,jitter,lossPct,ts}
    "overlay": None,    # the game's OWN in-HUD net overlay, read via OCR:
                        # {ok,ping,loss,choke,routeFlapMs,fps,ts}
}
_SCOUT_LOCK = threading.Lock()
_MATCH_PING_HIST = []   # recent samples (ms or None) for the live match-ping readout
_CONNALERT = {"ts": 0.0}  # debounce for connection-spike toasts


# ===========================================================================
# IN-GAME OVERLAY OCR  (read FragPunk's OWN net overlay: the REAL numbers)
# ---------------------------------------------------------------------------
# FragPunk's performance overlay prints the true Ping / Loss / Choke / Route
# Flapping / FPS along the top of the screen. We screenshot just that top strip
# and run Windows' built-in OCR (no dependencies) to read the real values --
# far better than our external ICMP estimate, and Loss/Choke/Route-Flapping are
# unobtainable any other way. Anti-cheat-safe (screen read, like OBS), and cheap
# (a thin top strip, OCR'd every ~9s, only in a match). Requires the user to
# have the overlay enabled and the game in borderless/windowed-fullscreen.
# ===========================================================================
_OVERLAY_PS_PATH = None
_OVERLAY_PS = r"""
$ErrorActionPreference='SilentlyContinue'
Add-Type -AssemblyName System.Drawing
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$asTask=([System.WindowsRuntimeSystemExtensions].GetMethods()|?{$_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1'})[0]
function Await($op,$t){$m=$asTask.MakeGenericMethod($t);$tk=$m.Invoke($null,@($op));$tk.Wait();$tk.Result}
[Windows.Storage.StorageFile,Windows.Foundation,ContentType=WindowsRuntime]|Out-Null
[Windows.Media.Ocr.OcrEngine,Windows.Foundation,ContentType=WindowsRuntime]|Out-Null
[Windows.Graphics.Imaging.BitmapDecoder,Windows.Foundation,ContentType=WindowsRuntime]|Out-Null
$b=[System.Windows.Forms.Screen]::PrimaryScreen.Bounds
# the perf overlay sits TOP-LEFT in small text -> capture just that corner and
# upscale 3x so OCR can read the Ping / Route Flapping numbers reliably
$cw=[int]($b.Width*0.46); $ch=[int]($b.Height*0.04); if($ch -lt 18){$ch=18}
$bmp=New-Object System.Drawing.Bitmap $cw,$ch
$g=[System.Drawing.Graphics]::FromImage($bmp)
$g.CopyFromScreen($b.X,$b.Y,0,0,(New-Object System.Drawing.Size($cw,$ch)))
$up=New-Object System.Drawing.Bitmap($bmp,(New-Object System.Drawing.Size(($cw*3),($ch*3))))
$tmp=[System.IO.Path]::Combine($env:TEMP,'fragroute_ovl.png')
$up.Save($tmp,[System.Drawing.Imaging.ImageFormat]::Png); $g.Dispose(); $bmp.Dispose(); $up.Dispose()
$eng=[Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages()
if(-not $eng){ return }
$sf=Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync($tmp)) ([Windows.Storage.StorageFile])
$st=Await ($sf.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
$dec=Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($st)) ([Windows.Graphics.Imaging.BitmapDecoder])
$sb=Await ($dec.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
$r=Await ($eng.RecognizeAsync($sb)) ([Windows.Media.Ocr.OcrResult])
$st.Dispose()
Write-Output ($r.Text -replace '\s+',' ')
"""


def _ensure_overlay_script():
    global _OVERLAY_PS_PATH
    if _OVERLAY_PS_PATH and Path(_OVERLAY_PS_PATH).exists():
        return _OVERLAY_PS_PATH
    try:
        p = Path(tempfile.gettempdir()) / "fragroute_overlay_ocr.ps1"
        p.write_text(_OVERLAY_PS)
        _OVERLAY_PS_PATH = str(p)
        return _OVERLAY_PS_PATH
    except Exception:
        return None


def read_game_overlay():
    """Screenshot the top strip + OCR it; parse the game's net overlay numbers.
    Returns {ok, ping, loss, choke, routeFlapMs, fps}. Best effort; ok=False if
    the overlay isn't visible / capture is black (exclusive fullscreen)."""
    if OS != "Windows":
        return {"ok": False}
    sp = _ensure_overlay_script()
    if not sp:
        return {"ok": False}
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", sp],
            capture_output=True, text=True, timeout=12, **_NO_WINDOW_KW).stdout or ""
    except Exception:
        return {"ok": False}

    def num(pat):
        m = re.search(pat, out, re.IGNORECASE)
        return int(m.group(1)) if m else None

    ping = num(r"Ping[:\s]+(\d+)\s*m")
    loss = num(r"Loss[:\s]+(\d+)\s*%")
    choke = num(r"Choke[:\s]+(\d+)\s*%")
    flap = num(r"Flapping[:\s]+(\d+)")
    fps = num(r"FPS[:\s]*(\d+)")
    ok = (ping is not None) or (fps is not None)
    return {"ok": ok, "ping": ping, "loss": loss, "choke": choke,
            "routeFlapMs": flap, "fps": fps, "ts": int(time.time() * 1000)}


# Training Base and warm-up modes open a game-server connection just like a real
# match, so the connection-based detector flags them as "match" -- which would
# log fake queues and pollute per-lancer stats. We OCR the top strip (which
# shows "Training Base" / the mode) to tell a REAL match from training/warm-up.
_MODE_PS_PATH = None
_MODE_PS = r"""
$ErrorActionPreference='SilentlyContinue'
Add-Type -AssemblyName System.Drawing; Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$asTask=([System.WindowsRuntimeSystemExtensions].GetMethods()|?{$_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1'})[0]
function Await($op,$t){$m=$asTask.MakeGenericMethod($t);$tk=$m.Invoke($null,@($op));$tk.Wait();$tk.Result}
[Windows.Storage.StorageFile,Windows.Foundation,ContentType=WindowsRuntime]|Out-Null
[Windows.Media.Ocr.OcrEngine,Windows.Foundation,ContentType=WindowsRuntime]|Out-Null
[Windows.Graphics.Imaging.BitmapDecoder,Windows.Foundation,ContentType=WindowsRuntime]|Out-Null
$b=[System.Windows.Forms.Screen]::PrimaryScreen.Bounds
$h=[int]($b.Height*0.06); if($h -lt 24){$h=24}
$bmp=New-Object System.Drawing.Bitmap $b.Width,$h
$g=[System.Drawing.Graphics]::FromImage($bmp)
$g.CopyFromScreen($b.X,$b.Y,0,0,(New-Object System.Drawing.Size($b.Width,$h)))
$up=New-Object System.Drawing.Bitmap($bmp,(New-Object System.Drawing.Size(($b.Width),($h*2))))
$tmp=[System.IO.Path]::Combine($env:TEMP,'fragroute_mode.png')
$up.Save($tmp,[System.Drawing.Imaging.ImageFormat]::Png); $g.Dispose(); $bmp.Dispose(); $up.Dispose()
$eng=[Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages(); if(-not $eng){return}
$sf=Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync($tmp)) ([Windows.Storage.StorageFile])
$st=Await ($sf.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
$dec=Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($st)) ([Windows.Graphics.Imaging.BitmapDecoder])
$sb=Await ($dec.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
$r=Await ($eng.RecognizeAsync($sb)) ([Windows.Media.Ocr.OcrResult])
$st.Dispose()
Write-Output ($r.Text -replace '\s+',' ')
"""


def read_game_mode(tries=1):
    """OCR the top strip to classify the current session. Returns
    {ok, training, raw}. training=True for Training Base / Warm-up / Practice.

    With tries>1, an EMPTY read (OCR caught nothing -- a blip or mid-transition
    screen) is retried before giving up, so a warm-up/Training-Base session isn't
    mistaken for a real match just because one OCR pass missed the mode pill. A
    confident read (any text) returns immediately, so real-match detection stays
    fast and the live-detector stop is never delayed."""
    global _MODE_PS_PATH
    if OS != "Windows":
        return {"ok": False, "training": False}
    for attempt in range(max(1, tries)):
        try:
            if not (_MODE_PS_PATH and Path(_MODE_PS_PATH).exists()):
                p = Path(tempfile.gettempdir()) / "fragroute_mode_ocr.ps1"
                p.write_text(_MODE_PS)
                _MODE_PS_PATH = str(p)
            out = subprocess.run(
                ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", _MODE_PS_PATH],
                capture_output=True, text=True, timeout=12, **_NO_WINDOW_KW).stdout or ""
        except Exception:
            out = ""
        low = out.lower()
        ok = bool(out.strip())
        training = any(w in low for w in ("training", "warm", "practice", "warm-up", "warmup",
                                           "firing range", "shooting range", "range", "tutorial"))
        if ok or attempt >= tries - 1:
            return {"ok": ok, "training": training, "raw": out[:120]}
        time.sleep(1.0)                       # only when the read was empty
    return {"ok": False, "training": False}


# ---- generic region OCR (rank card, queue timer) --------------------------
# A parameterized screen-region OCR: capture a fractional rectangle of the
# primary screen, upscale, and read it. Used for the lobby rank card and the
# "finding a match" queue-timer pill. Anti-cheat-safe (screen read only).
_REGION_PS_PATH = None
_REGION_PS = r"""
param([double]$fx,[double]$fy,[double]$fw,[double]$fh,[int]$scale)
$ErrorActionPreference='SilentlyContinue'
Add-Type -AssemblyName System.Drawing; Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$asTask=([System.WindowsRuntimeSystemExtensions].GetMethods()|?{$_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1'})[0]
function Await($op,$t){$m=$asTask.MakeGenericMethod($t);$tk=$m.Invoke($null,@($op));$tk.Wait();$tk.Result}
[Windows.Storage.StorageFile,Windows.Foundation,ContentType=WindowsRuntime]|Out-Null
[Windows.Media.Ocr.OcrEngine,Windows.Foundation,ContentType=WindowsRuntime]|Out-Null
[Windows.Graphics.Imaging.BitmapDecoder,Windows.Foundation,ContentType=WindowsRuntime]|Out-Null
$b=[System.Windows.Forms.Screen]::PrimaryScreen.Bounds
$x=[int]($b.Width*$fx); $y=[int]($b.Height*$fy)
$cw=[int]($b.Width*$fw); $ch=[int]($b.Height*$fh)
if($cw -lt 8){$cw=8}; if($ch -lt 8){$ch=8}; if($scale -lt 1){$scale=1}
$bmp=New-Object System.Drawing.Bitmap $cw,$ch
$g=[System.Drawing.Graphics]::FromImage($bmp)
$g.CopyFromScreen($b.X+$x,$b.Y+$y,0,0,(New-Object System.Drawing.Size($cw,$ch)))
$up=New-Object System.Drawing.Bitmap($bmp,(New-Object System.Drawing.Size(($cw*$scale),($ch*$scale))))
$tmp=[System.IO.Path]::Combine($env:TEMP,'fragroute_region.png')
$up.Save($tmp,[System.Drawing.Imaging.ImageFormat]::Png); $g.Dispose(); $bmp.Dispose(); $up.Dispose()
$eng=[Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages(); if(-not $eng){return}
$sf=Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync($tmp)) ([Windows.Storage.StorageFile])
$st=Await ($sf.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
$dec=Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($st)) ([Windows.Graphics.Imaging.BitmapDecoder])
$sb=Await ($dec.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
$r=Await ($eng.RecognizeAsync($sb)) ([Windows.Media.Ocr.OcrResult])
$st.Dispose()
Write-Output ($r.Text -replace '\s+',' ')
"""


def _region_ocr(fx, fy, fw, fh, scale=2, timeout=12):
    """OCR a fractional screen rectangle. Returns the recognized text (or '')."""
    global _REGION_PS_PATH
    if OS != "Windows":
        return ""
    try:
        if not (_REGION_PS_PATH and Path(_REGION_PS_PATH).exists()):
            p = Path(tempfile.gettempdir()) / "fragroute_region_ocr.ps1"
            p.write_text(_REGION_PS)
            _REGION_PS_PATH = str(p)
        out = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", _REGION_PS_PATH,
             "-fx", str(fx), "-fy", str(fy), "-fw", str(fw), "-fh", str(fh), "-scale", str(scale)],
            capture_output=True, text=True, timeout=timeout, **_NO_WINDOW_KW).stdout or ""
        return out.strip()
    except Exception:
        return ""


# FragPunk ranked tiers (low -> high). Division is the number/roman after it.
_RANK_TIERS = ["BRONZE", "SILVER", "GOLD", "PLATINUM", "DIAMOND", "MASTER",
               "ACE", "PUNKMASTER"]
_ROMAN = {"I": 1, "II": 2, "III": 3, "IV": 4, "V": 5}


def parse_rank(text):
    """Parse the lobby rank card text -> {ok, tier, div, rp, rpMax, raw}."""
    up = (text or "").upper()
    tier = None
    for t in _RANK_TIERS:
        if t in up:
            tier = t
            break
    if tier is None:
        # OCR can mangle the tier word (MASTER->MASTEX); fuzzy-match each token
        for tok in re.findall(r"[A-Z]{4,}", up):
            for t in _RANK_TIERS:
                if _edit_distance(tok, t) <= max(1, len(t) // 5):
                    tier = t
                    break
            if tier:
                break
    # division: a small number or roman numeral right after the tier word
    div = None
    if tier:
        m = re.search(re.escape(tier) + r"\s*([1-5]|IV|III|II|I|V)\b", up)
        if m:
            g = m.group(1)
            div = int(g) if g.isdigit() else _ROMAN.get(g)
    rp = rp_max = None
    m = re.search(r"(\d{1,3})\s*/\s*(\d{2,4})", up)
    if m:
        rp, rp_max = int(m.group(1)), int(m.group(2))
    ok = bool(tier and (rp is not None))
    return {"ok": ok, "tier": tier, "div": div, "rp": rp, "rpMax": rp_max,
            "raw": (text or "")[:120]}


def parse_queue_timer(text):
    """Parse the 'finding a match' pill -> {ok, seconds, mode, estMin, raw}."""
    up = (text or "").upper()
    secs = None
    m = re.search(r"(\d{1,2}):(\d{2})", up)
    if m:
        secs = int(m.group(1)) * 60 + int(m.group(2))
    mode = None
    mm = re.search(r"(RANKED|CASUAL|UNRANKED|CUSTOM|QUICK)", up)
    if mm:
        mode = mm.group(1)
    est = None
    me = re.search(r"(\d+)\s*MIN", up)
    if me:
        est = int(me.group(1))
    ok = secs is not None
    return {"ok": ok, "seconds": secs, "mode": mode, "estMin": est,
            "raw": (text or "")[:120]}


def read_rank():
    """OCR the lobby rank card (left side) -> parsed rank. Best effort."""
    return parse_rank(_region_ocr(0.01, 0.60, 0.30, 0.20, scale=2))


def read_queue_timer():
    """OCR the 'finding a match' queue-timer pill (bottom centre) -> parsed."""
    return parse_queue_timer(_region_ocr(0.40, 0.94, 0.26, 0.055, scale=3))


# ---- in-game SERVER ping table (OCR the region picker) --------------------
# FragPunk's in-game Server menu lists regions + ping, but it's a clunky modal
# with no history. We OCR it so the app keeps your real per-region ping (both
# DIRECT and through whatever VPN is active) -- the ping half of the
# queue-speed-vs-ping tradeoff (the user VPNs to busier regions to queue faster,
# knowingly trading ping). FragPunk's own region names map to our region IDs.
_FP_REGION_MAP = {
    "central us": "us-central", "west us": "us-west",
    "east us": "us-east", "east us - 2": "us-east", "east us 2": "us-east",
    "europe": "eu", "europe - 2": "eu", "europe 2": "eu",
    "tokyo, japan": "asia-east", "tokyo japan": "asia-east",
    "australia": "oceania", "singapore": "asia-se", "south america": "sa",
    "central me": None, "auto": None,
}
_FP_KNOWN = ["South America", "Tokyo, Japan", "East US - 2", "Central US",
             "Europe - 2", "Central ME", "East US", "West US", "Europe",
             "Australia", "Singapore", "Auto"]


def _fp_norm(s):
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).split()


_FP_KN = sorted([(_fp_norm(n), n) for n in _FP_KNOWN], key=lambda x: -len(x[0]))


def parse_server_list(text):
    """Parse the OCR'd Server modal -> {ok, selected, servers:[{name,regionId,
    pingMs}]}. Handles OCR reading the name column then the ping column."""
    parts = re.split(r"\bPING\b", text or "", flags=re.I, maxsplit=1)
    names_blob = re.sub(r"^\s*SERVER\b", "", parts[0], flags=re.I)
    toks = _fp_norm(names_blob)
    names = []
    i = 0
    while i < len(toks):
        hit = None
        for kn, canon in _FP_KN:
            if toks[i:i + len(kn)] == kn:
                hit = (kn, canon)
                break
        if hit:
            names.append(hit[1])
            i += len(hit[0])
        else:
            i += 1
    pings = []
    for t in re.findall(r"([0-9OoSlIB]{2,4})\s*ms", parts[1] if len(parts) > 1 else ""):
        try:
            pings.append(int(t.translate(str.maketrans("OoSlIB", "005118"))))
        except Exception:
            pass
    selected = None
    nm = names[:]
    if nm and nm[0].lower() == "auto":
        selected = "Auto"
        nm = nm[1:]
    elif len(nm) == len(pings) + 1:
        selected = nm[0]
        nm = nm[1:]
    rows = [{"name": n, "regionId": _FP_REGION_MAP.get(n.lower()),
             "pingMs": (pings[i] if i < len(pings) else None)}
            for i, n in enumerate(nm)]
    return {"ok": bool(rows), "selected": selected, "servers": rows}


def read_server_list():
    """OCR FragPunk's in-game Server region/ping modal (centered). Only succeeds
    while that modal is open. Tags the reading with the active VPN, if any."""
    res = parse_server_list(_region_ocr(0.27, 0.34, 0.47, 0.30, scale=2, timeout=14))
    res["vpnTunnel"] = STATE.get("active_tunnel")
    res["vpnRegion"] = STATE.get("active_region")
    return res


def load_server_pings():
    if not SERVERPINGS_PATH:
        return {"regions": {}}
    try:
        if Path(SERVERPINGS_PATH).exists():
            d = json.loads(Path(SERVERPINGS_PATH).read_text())
            return d if isinstance(d, dict) else {"regions": {}}
    except Exception:
        pass
    return {"regions": {}}


def _merge_server_pings(reading):
    """Fold an OCR reading into the stored table. We keep the latest DIRECT and
    latest VPN ping per region separately (they differ a lot), each timestamped,
    so the app shows e.g. 'Central US 65ms direct / 432ms via DE route'."""
    rows = (reading or {}).get("servers") or []
    if not rows:
        return load_server_pings()
    on_vpn = bool(reading.get("vpnTunnel"))
    now_ms = int(time.time() * 1000)
    with _SRVPING_LOCK:
        data = load_server_pings()
        regions = data.setdefault("regions", {})
        for r in rows:
            if r.get("pingMs") is None:
                continue
            rec = regions.setdefault(r["name"], {})
            rec["regionId"] = r.get("regionId")
            if on_vpn:
                rec["vpnPing"] = r["pingMs"]
                rec["vpnTs"] = now_ms
                rec["vpnTunnel"] = reading.get("vpnTunnel")
                rec["vpnRegion"] = reading.get("vpnRegion")
            else:
                rec["directPing"] = r["pingMs"]
                rec["directTs"] = now_ms
        data["updated"] = now_ms
        data["lastSelected"] = reading.get("selected") or data.get("lastSelected")
        try:
            Path(SERVERPINGS_PATH).write_text(json.dumps(data, indent=2))
        except Exception:
            pass
    return data


def server_pings(refresh=False):
    """Stored in-game per-region ping table (+ app's VPN-route best ping for the
    same region, for comparison). refresh=True OCRs the Server modal now."""
    last = None
    if refresh and not _in_match():
        try:
            reading = read_server_list()
            if reading.get("ok"):
                _merge_server_pings(reading)
                last = reading
                diag("serverping", True, msg="read server modal")
            else:
                diag("serverping", True)   # OCR ran fine, modal just wasn't open -- not a fault
        except Exception as e:
            last = None
            diag("serverping", False, msg="server-ping OCR", exc=e)
    data = load_server_pings()
    out = []
    for name, rec in (data.get("regions") or {}).items():
        rid = rec.get("regionId")
        out.append({
            "name": name, "regionId": rid,
            "directPing": rec.get("directPing"), "directTs": rec.get("directTs"),
            "vpnPing": rec.get("vpnPing"), "vpnTs": rec.get("vpnTs"),
            "vpnTunnel": rec.get("vpnTunnel"),
            # our own optimizer's best VPN-route ping to this region's real servers
            "routeBestMs": (region_best_latency(rid) if rid else None),
        })
    # sort by best available ping
    out.sort(key=lambda r: (r.get("directPing") if r.get("directPing") is not None else 9999))
    return {"servers": out, "updated": data.get("updated"),
            "lastSelected": data.get("lastSelected"),
            "onVpn": bool(STATE.get("active_tunnel")),
            "activeTunnel": STATE.get("active_tunnel"),
            "lastRead": last}


# ---- rank / RP tracker ----------------------------------------------------
# Reads the lobby rank card periodically (menu only, never in a match) and
# stores a snapshot whenever the rank or RP changes -> a clean RP-over-time
# history with per-reading deltas. Anti-cheat-safe (screen OCR).
def _rank_value(snap):
    """A monotonic-ish numeric for RP trend math: tier index*1000 + div*100 + rp.
    Lets us compute deltas across tier/division boundaries sensibly."""
    try:
        ti = _RANK_TIERS.index(snap.get("tier")) if snap.get("tier") in _RANK_TIERS else 0
    except Exception:
        ti = 0
    div = snap.get("div") or 0
    rp = snap.get("rp") or 0
    return ti * 100000 + div * 1000 + rp


def load_rank():
    if not RANK_PATH:
        return {"history": []}
    try:
        if Path(RANK_PATH).exists():
            d = json.loads(Path(RANK_PATH).read_text())
            return d if isinstance(d, dict) else {"history": []}
    except Exception:
        pass
    return {"history": []}


def record_rank(snap):
    """Append a rank snapshot if it's valid AND changed from the last one.
    Returns the stored snapshot (with delta) or None."""
    if not snap or not snap.get("ok"):
        return None
    with _RANK_LOCK:
        data = load_rank()
        hist = data.get("history") or []
        last = hist[-1] if hist else None
        same = (last and last.get("tier") == snap.get("tier")
                and last.get("div") == snap.get("div")
                and last.get("rp") == snap.get("rp"))
        if same:
            return None  # nothing changed -> don't spam history
        entry = {"ts": int(time.time() * 1000), "tier": snap.get("tier"),
                 "div": snap.get("div"), "rp": snap.get("rp"),
                 "rpMax": snap.get("rpMax")}
        if last:
            entry["delta"] = _rank_value(entry) - _rank_value(last)
        hist.append(entry)
        data["history"] = hist[-500:]
        try:
            Path(RANK_PATH).write_text(json.dumps(data, indent=2))
        except Exception:
            pass
        return entry


def rank_status(refresh=False):
    """Current rank + RP history + session RP delta. refresh=True OCRs now
    (only when the game is running and we're NOT in a match)."""
    fresh = None
    if refresh and not _in_match():
        try:
            r = read_rank()
            if r.get("ok"):
                record_rank(r)
                fresh = r
                diag("rank", True, msg="read rank card")
            else:
                diag("rank", True)   # OCR ran fine, rank card just wasn't visible -- not a fault
        except Exception as e:
            fresh = None
            diag("rank", False, msg="rank OCR", exc=e)
    data = load_rank()
    hist = data.get("history") or []
    current = None
    if hist:
        h = hist[-1]
        current = {"tier": h.get("tier"), "div": h.get("div"),
                   "rp": h.get("rp"), "rpMax": h.get("rpMax"), "ts": h.get("ts")}
    elif fresh and fresh.get("ok"):
        current = {"tier": fresh["tier"], "div": fresh["div"],
                   "rp": fresh["rp"], "rpMax": fresh["rpMax"]}
    # session RP delta = sum of deltas since the app started this session
    session_delta = None
    if AUTODETECT.get("sessionStartTs"):
        s0 = AUTODETECT["sessionStartTs"]
        ds = [h.get("delta", 0) for h in hist if h.get("ts", 0) >= s0 and "delta" in h]
        if ds:
            session_delta = sum(ds)
    return {"current": current, "history": hist[-60:],
            "sessionDelta": session_delta, "lastRead": fresh}


def scout_ping_match():
    """One light ping to the CURRENT match server for a live 'your real ping'
    readout. A single packet every loop is trivial next to live game traffic, so
    this stays within the no-disturb rule. Tracks a short rolling window for
    avg/jitter/loss. Clears when not in a match."""
    with _AUTODETECT_LOCK:
        srv = AUTODETECT.get("currentServer") or {}
    ip = srv.get("ip")
    if not ip:
        with _SCOUT_LOCK:
            SCOUT["matchPing"] = None
        _MATCH_PING_HIST.clear()
        return
    ms = ping_host(ip, timeout_ms=1000)
    _MATCH_PING_HIST.append(ms)
    del _MATCH_PING_HIST[:-15]
    ok = [s for s in _MATCH_PING_HIST if s is not None]
    avg = round(sum(ok) / len(ok), 1) if ok else None
    jitter = (round(sum(abs(s - avg) for s in ok) / len(ok), 1)
              if (avg is not None and len(ok) >= 2) else None)
    loss = round(sum(1 for s in _MATCH_PING_HIST if s is None)
                 / len(_MATCH_PING_HIST) * 100) if _MATCH_PING_HIST else None
    with _SCOUT_LOCK:
        SCOUT["matchPing"] = {
            "ip": ip,
            "ms": round(ms, 1) if ms is not None else None,
            "avg": avg, "jitter": jitter, "lossPct": loss,
            "regionId": srv.get("regionId"),
            # rolling samples for the live net-graph (None = dropped ping)
            "history": [round(s, 1) if s is not None else None
                        for s in _MATCH_PING_HIST],
            "ts": int(time.time() * 1000),
        }
    # connection-quality alert: spike vs the running average, or packet loss
    try:
        if (get_setting("notifyConnSpike", False) and avg is not None
                and ms is not None and len(ok) >= 4):
            spike = float(get_setting("connSpikeMs", 60))
            bad = (ms > avg + spike) or (loss is not None and loss >= 25)
            last = _CONNALERT.get("ts", 0)
            if bad and (time.time() - last) > 30:   # debounce 30s
                _CONNALERT["ts"] = time.time()
                why = (f"{round(ms)}ms (avg {round(avg)})" if ms > avg + spike
                       else f"{loss}% packet loss")
                _notify("Connection spike", f"Match ping {why}")
    except Exception:
        pass


def scout_status():
    with _SCOUT_LOCK:
        return {
            "enabled": bool(get_setting("scoutEnabled", True)),
            "ranking": list(SCOUT["ranking"]),
            "best": SCOUT["best"],
            "updated": SCOUT["updated"],
            "pingedAt": SCOUT["pingedAt"],
            "nudge": SCOUT["nudge"],
            "lobby": SCOUT["lobby"],
            "matchPing": SCOUT["matchPing"],
            "overlay": SCOUT["overlay"],
            "population": population_snapshot(),
        }


def scout_ping_lobby():
    """While in the menu, ping FragPunk's persistent lobby/matchmaking gateway so
    we can show your CURRENT matchmaking-region quality before a match even
    starts. Updates SCOUT['lobby']. Best effort; never raises."""
    try:
        st = game_status()
        lob = st.get("lobby")
        if not lob or not lob.get("ip"):
            return
        ms = ping_host(lob["ip"], timeout_ms=1200)
        with _SCOUT_LOCK:
            SCOUT["lobby"] = {
                "ip": lob["ip"],
                "regionId": lob.get("regionId"),
                "regionName": lob.get("regionName"),
                "where": lob.get("where"),
                "regionSource": lob.get("regionSource"),
                "pingMs": round(ms, 1) if ms is not None else None,
                "ts": int(time.time() * 1000),
            }
    except Exception:
        pass


def scout_recompute():
    """Refresh the warm ranking from current pings + heat + history."""
    rec = compute_recommendation()
    with _SCOUT_LOCK:
        SCOUT["ranking"] = rec["ranking"]
        SCOUT["best"] = rec["best"]
        SCOUT["updated"] = int(time.time() * 1000)
    return rec


def _scout_expected_for(region_id):
    """Expected queue seconds for a region from the current ranking (or None)."""
    with _SCOUT_LOCK:
        for row in SCOUT["ranking"]:
            if row["regionId"] == region_id:
                return row["expectedQueueSec"]
    return None


def _check_slow_queue():
    """If the live queue is running long vs. prediction and a meaningfully
    faster region exists, fire a one-shot bailout nudge. Honest about its
    inputs: the prediction is the heat+history estimate, not a live count."""
    if not get_setting("slowQueueNudge", True):
        return
    with _AUTODETECT_LOCK:
        phase = AUTODETECT["phase"]
        q = AUTODETECT["queueStartTs"]
        first = AUTODETECT["matchesThisSession"] <= 1
        already = AUTODETECT.get("_slowNudgeFor")
    # the nudge is only meaningful while queuing; clear it once we leave menus
    if phase != "menu":
        with _SCOUT_LOCK:
            SCOUT["nudge"] = None
    # only while actively queuing in menus, and not the menu-inflated first match
    if phase != "menu" or not q or first or already == q:
        return
    elapsed = time.time() - q / 1000.0
    if elapsed < 30:
        return
    cur = STATE.get("active_region")
    if not cur:
        return
    cur_exp = _scout_expected_for(cur)
    if not cur_exp:
        return
    try:
        factor = float(get_setting("slowQueueFactorPct", 150)) / 100.0
    except Exception:
        factor = 1.5
    if elapsed < cur_exp * factor:
        return
    # find the best alternative region that's predicted meaningfully faster
    with _SCOUT_LOCK:
        ranking = list(SCOUT["ranking"])
    alt = next((row for row in ranking
                if not row["filtered"] and row["regionId"] != cur
                and row["expectedQueueSec"] + 20 < elapsed), None)
    if not alt:
        return
    with _AUTODETECT_LOCK:
        AUTODETECT["_slowNudgeFor"] = q
    saves = int(max(0, elapsed - alt["expectedQueueSec"]))
    nudge = {
        "fromRegion": cur,
        "toRegion": alt["regionId"],
        "toName": alt["name"],
        "elapsedSec": int(elapsed),
        "expectedSec": alt["expectedQueueSec"],
        "savesSec": saves,
        "ts": int(time.time() * 1000),
    }
    with _SCOUT_LOCK:
        SCOUT["nudge"] = nudge
    _ad_event("slow_queue", **nudge)
    if get_setting("notifySlowQueue", False):
        _notify("Queue running long",
                f"{alt['name']} predicted faster â€” hop to save ~{saves}s")


# ===========================================================================
# ROUTE OPTIMIZER  (automates the "toggle the VPN to find a lower-ping route")
# ---------------------------------------------------------------------------
# The match server is fixed, but your PING to it depends on the network path.
# Internet routing isn't latency-optimized, so a VPN exit with better peering to
# the game's datacenter can cut ping a lot (this is what ExitLag/NoPing sell).
# This profiles your real ping to a region's harvested game servers through
# DIRECT and through every ProtonVPN config, then ranks the routes so you can
# connect the lowest-ping one. It SWITCHES your live tunnel repeatedly, so it is
# explicit-trigger only, never runs in a match, and restores your original
# tunnel when done.
# ===========================================================================
ROUTE_PROFILE = {
    "running": False, "region": None, "targetIps": [],
    "total": 0, "done": 0, "current": None,
    "results": [], "best": None,
    "startedTs": None, "doneTs": None, "error": None, "dryRun": False,
}
_ROUTE_LOCK = threading.Lock()
_ROUTE_CANCEL = threading.Event()


def route_profile_status():
    with _ROUTE_LOCK:
        return dict(ROUTE_PROFILE, results=list(ROUTE_PROFILE["results"]),
                    targetIps=list(ROUTE_PROFILE["targetIps"]))


def _ping_best(ip, n=3):
    """Lowest of n pings to ip (best-case path latency), or None."""
    vals = [ping_host(ip, timeout_ms=1200) for _ in range(n)]
    ok = [v for v in vals if v is not None]
    return round(min(ok), 1) if ok else None


def _route_ping_to_targets(target_ips):
    """Best ping to a region across its target server IPs on the CURRENT route."""
    best = None
    for ip in target_ips:
        m = _ping_best(ip)
        if m is not None and (best is None or m < best):
            best = m
    return best


def start_route_profile(region_id):
    """Kick off a route profile for a region on a background thread."""
    if region_id not in REGION_BY_ID:
        return {"ok": False, "message": "unknown region"}
    with _ROUTE_LOCK:
        if ROUTE_PROFILE["running"]:
            return {"ok": False, "message": "a profile is already running"}
    # block if in a match -- use BOTH the debounced phase and a fresh live read
    # (the debounced phase can lag, which let a run start during a match before).
    if _in_match():
        return {"ok": False, "message": "can't optimize during a match â€” finish it first"}
    try:
        live = game_status()
        if live.get("running") and (live.get("server") or {}).get("ip"):
            return {"ok": False, "message":
                    "you're in a match â€” finish it, then optimize from the menu"}
    except Exception:
        pass
    targets = region_server_ips(region_id)[:3]
    if not targets:
        return {"ok": False, "message":
                f"no harvested servers for {region_id} yet â€” play a match there once so I have a real server to ping"}
    _ROUTE_CANCEL.clear()
    with _ROUTE_LOCK:
        ROUTE_PROFILE.update({
            "running": True, "region": region_id, "targetIps": targets,
            "total": 0, "done": 0, "current": None, "results": [], "best": None,
            "startedTs": int(time.time() * 1000), "doneTs": None, "error": None,
            "dryRun": bool(STATE.get("dry_run")),
        })
    threading.Thread(target=_route_profile_worker,
                     args=(region_id, targets), daemon=True).start()
    return {"ok": True, "running": True, "region": region_id, "targetIps": targets}


def cancel_route_profile():
    _ROUTE_CANCEL.set()
    return {"ok": True}


def _route_profile_worker(region_id, targets):
    """Test DIRECT + every config: connect each, let it settle, ping the region's
    servers, record. Restores the original tunnel at the end. Never raises."""
    orig_tunnel = STATE.get("active_tunnel")
    settle = 4.0
    try:
        routes = [("Direct (no VPN)", None, None)]
        for rid, entries in STATE["configs"].items():
            for e in entries:
                routes.append((e["name"], e, rid))
        with _ROUTE_LOCK:
            ROUTE_PROFILE["total"] = len(routes)

        for name, cfg, cfg_rid in routes:
            if _ROUTE_CANCEL.is_set():
                break
            # ABORT if a match has started -- never flip tunnels mid-match (that
            # was the instability). The route restored in `finally` puts you back.
            if _in_match():
                with _ROUTE_LOCK:
                    ROUTE_PROFILE["error"] = "stopped: a match started â€” finish it, then optimize from the menu"
                break
            with _ROUTE_LOCK:
                ROUTE_PROFILE["current"] = name
            # switch route
            try:
                if cfg is None:
                    disconnect()
                else:
                    connect_config(name)
            except Exception:
                pass
            # one more guard: a match can pop during the settle wait below
            if _in_match():
                with _ROUTE_LOCK:
                    ROUTE_PROFILE["error"] = "stopped: a match started â€” finish it, then optimize from the menu"
                break
            # let the tunnel + routes settle (skip the wait in dry-run)
            if not STATE.get("dry_run"):
                for _ in range(int(settle * 2)):
                    if _ROUTE_CANCEL.is_set():
                        break
                    time.sleep(0.5)
            ms = _route_ping_to_targets(targets)
            row = {"route": name, "config": (cfg["name"] if cfg else None),
                   "regionId": cfg_rid, "pingMs": ms,
                   "isDirect": cfg is None}
            with _ROUTE_LOCK:
                ROUTE_PROFILE["results"].append(row)
                ROUTE_PROFILE["done"] += 1
        # rank + pick best (lowest ping wins; None pings sink to the bottom)
        with _ROUTE_LOCK:
            ranked = sorted(ROUTE_PROFILE["results"],
                            key=lambda r: (r["pingMs"] is None,
                                           r["pingMs"] if r["pingMs"] is not None else 9e9))
            ROUTE_PROFILE["results"] = ranked
            ROUTE_PROFILE["best"] = next((r for r in ranked if r["pingMs"] is not None), None)
        diag("route", True, msg=f"profiled {ROUTE_PROFILE.get('done')} routes")
    except Exception as e:
        with _ROUTE_LOCK:
            ROUTE_PROFILE["error"] = str(e)
        diag("route", False, msg="route optimizer", exc=e)
    finally:
        # restore the user's original route
        try:
            if orig_tunnel:
                connect_config(orig_tunnel)
            else:
                disconnect()
        except Exception:
            pass
        with _ROUTE_LOCK:
            ROUTE_PROFILE["running"] = False
            ROUTE_PROFILE["current"] = None
            ROUTE_PROFILE["doneTs"] = int(time.time() * 1000)


# ===========================================================================
# AUTO-REVERT  (undo a mid-match VPN switch that froze / worsened your ping)
# ---------------------------------------------------------------------------
# Switching tunnels mid-match is a gamble: a well-placed exit lowers ping, a
# far one (e.g. Hong Kong for a US-East server) routes your game packets across
# the planet and FREEZES the game. This watches your match-server ping right
# after a switch and, if it goes dead or much worse than before, automatically
# reconnects your previous (working) route -- automating the "disconnect to
# unfreeze" you'd otherwise do by hand.
# ===========================================================================
_AUTOREVERT = {"armed": False, "switchTs": 0.0, "prevTunnel": None,
               "baselineMs": None, "targetIp": None, "reverting": False}
_AUTOREVERT_LOCK = threading.Lock()


def _arm_auto_revert(prev_tunnel):
    """Called right after a tunnel comes up. If we're in a match, capture the
    route to fall back to + the current ping baseline so the watcher can undo a
    bad switch. No-op when not mid-match, in dry-run, or while reverting."""
    if not get_setting("autoRevertOnSwitch", True) or STATE.get("dry_run"):
        return
    with _AUTOREVERT_LOCK:
        if _AUTOREVERT["reverting"]:
            return
    with _AUTODETECT_LOCK:
        if AUTODETECT.get("phase") != "match":
            return
        srv = AUTODETECT.get("currentServer") or {}
    ip = srv.get("ip")
    if not ip:
        return
    with _SCOUT_LOCK:
        mp = SCOUT.get("matchPing") or {}
    baseline = mp.get("avg") if mp.get("avg") is not None else mp.get("ms")
    with _AUTOREVERT_LOCK:
        _AUTOREVERT.update({"armed": True, "switchTs": time.time(),
                            "prevTunnel": prev_tunnel, "baselineMs": baseline,
                            "targetIp": ip})


def _disarm_auto_revert():
    with _AUTOREVERT_LOCK:
        _AUTOREVERT["armed"] = False


def _do_auto_revert(prev_tunnel, base, got, dead):
    with _AUTOREVERT_LOCK:
        _AUTOREVERT["armed"] = False
        _AUTOREVERT["reverting"] = True
    try:
        if prev_tunnel:
            connect_config(prev_tunnel)
            where = f"reconnected {prev_tunnel}"
        else:
            disconnect()
            where = "back to direct"
        reason = ("froze (no response)" if dead
                  else f"ping got worse ({round(got)}ms vs ~{round(base)}ms)")
        _ad_event("route_reverted", to=(prev_tunnel or "direct"), reason=reason)
        _notify("Route auto-reverted", f"That exit {reason} â€” {where}.")
    except Exception:
        pass
    finally:
        with _AUTOREVERT_LOCK:
            _AUTOREVERT["reverting"] = False


def _auto_revert_loop():
    """Daemon: after a mid-match switch, give the new tunnel a moment, then ping
    the match server; revert if it's dead or much worse than the baseline."""
    while True:
        try:
            with _AUTOREVERT_LOCK:
                armed, sw = _AUTOREVERT["armed"], _AUTOREVERT["switchTs"]
                prev, base, ip = (_AUTOREVERT["prevTunnel"],
                                  _AUTOREVERT["baselineMs"], _AUTOREVERT["targetIp"])
            if armed:
                elapsed = time.time() - sw
                phase = AUTODETECT.get("phase")
                grace = float(get_setting("autoRevertGraceSec", 5))
                if phase != "match" or not STATE.get("active_tunnel"):
                    _disarm_auto_revert()          # match ended / user already dropped it
                elif elapsed >= grace:
                    samples = [ping_host(ip, timeout_ms=1000) for _ in range(3)]
                    ok = [s for s in samples if s is not None]
                    worse = float(get_setting("autoRevertWorseMs", 80))
                    dead = not ok
                    much_worse = bool(base and ok and min(ok) > base + worse)
                    if dead or much_worse:
                        _do_auto_revert(prev, base, (min(ok) if ok else None), dead)
                    else:
                        _disarm_auto_revert()      # switch was fine / better -> keep it
                elif elapsed > grace + 12:
                    _disarm_auto_revert()
        except Exception:
            pass
        time.sleep(1.5)


_LAST_RANK_READ = [0.0]   # monotonic ts of the last rank-card OCR (throttle)


def _schedule_post_match_rank_read():
    """After a real match ends, read the lobby rank card promptly so the RP
    change is captured even on a fast requeue (the 55s menu poll would miss it).
    Retries a few times while the card settles; bails if a new match starts."""
    if OS != "Windows" or not get_setting("rankTracker", True):
        return

    def _work():
        waited = 0
        for target in (7, 16, 28):
            time.sleep(max(0, target - waited))
            waited = target
            if _in_match():
                return  # requeued into a new match -- never OCR mid-match
            try:
                r = read_rank()
            except Exception:
                r = None
            if r and r.get("ok"):
                record_rank(r)               # stored only if RP/rank changed
                _LAST_RANK_READ[0] = time.time()
                return
    threading.Thread(target=_work, daemon=True).start()


def _scout_loop():
    """Daemon loop. Re-ranks regions on the Scout interval; pings real game
    servers only while you're not in a match; checks for slow-queue bailout."""
    while True:
        try:
            if get_setting("scoutEnabled", True):
                phase = AUTODETECT.get("phase")
                # ping real datacenters only when it won't fight live game
                # traffic (idle or in menus -- never mid-match)
                if phase != "match":
                    try:
                        scout_ping_servers()
                        scout_ping_lobby()   # live matchmaking-region quality in menu
                        with _SCOUT_LOCK:
                            SCOUT["pingedAt"] = int(time.time() * 1000)
                            SCOUT["matchPing"] = None   # not in a match anymore
                            SCOUT["overlay"] = None
                        _MATCH_PING_HIST.clear()
                    except Exception:
                        pass
                    # RANK tracker: OCR the lobby rank card occasionally (menu
                    # only, never mid-match) and store changes -> RP over time.
                    if get_setting("rankTracker", True) and AUTODETECT.get("phase") == "menu":
                        nowt = time.time()
                        if nowt - _LAST_RANK_READ[0] > 55:
                            _LAST_RANK_READ[0] = nowt
                            try:
                                r = read_rank()
                                if r.get("ok"):
                                    record_rank(r)
                            except Exception:
                                pass
                    # QUEUE timer: OCR the "finding a match" pill (only succeeds
                    # while it's on screen) to calibrate the TRUE queue start, so
                    # the logged queue time excludes menu/warm-up dwell.
                    if get_setting("queueTimerOcr", True) and AUTODETECT.get("queueStartTs"):
                        try:
                            qt = read_queue_timer()
                            if qt.get("ok") and qt.get("seconds") is not None:
                                nowm = int(time.time() * 1000)
                                with _AUTODETECT_LOCK:
                                    AUTODETECT["queueOcrStartTs"] = nowm - qt["seconds"] * 1000
                                    AUTODETECT["queueOcrTs"] = nowm
                                    AUTODETECT["queueOcrMode"] = qt.get("mode")
                        except Exception:
                            pass
                else:
                    # in a match: just ONE light ping to the current server for a
                    # live "your real ping" readout (no datacenter sweep)
                    try:
                        scout_ping_match()
                    except Exception:
                        pass
                    # read the game's OWN net overlay (real ping/loss/route-flap)
                    if get_setting("overlayOcr", True):
                        try:
                            ov = read_game_overlay()
                            # sanity-filter the OCR'd ping against the reliable
                            # ICMP baseline: a value implausibly low (e.g. the
                            # route-flap single-digit misread as ping) is junk.
                            with _SCOUT_LOCK:
                                base = (SCOUT.get("matchPing") or {}).get("avg")
                            p = ov.get("ping")
                            if p is not None and (p < 5 or (base and p < base * 0.5)):
                                ov["ping"] = None
                                ov["pingSuspect"] = True
                            with _SCOUT_LOCK:
                                SCOUT["overlay"] = ov if ov.get("ok") else None
                        except Exception:
                            pass
                try:
                    refresh_population()   # real Steam player count -> queue anchor
                except Exception:
                    pass
                scout_recompute()
                _check_slow_queue()
        except Exception:
            pass
        try:
            interval = int(get_setting("scoutIntervalSeconds", 20))
        except Exception:
            interval = 20
        interval = max(8, min(120, interval))
        # in a match we skip the datacenter sweep, but sample the CURRENT match
        # server every ~9s so the live net-graph + match-ping readout stay fresh
        # (one ping per 9s is negligible next to live game traffic)
        if _in_match():
            interval = 9
        elif AUTODETECT.get("queueStartTs"):
            # ACTIVELY QUEUING: poll fast so the queue-timer OCR reliably catches
            # the on-screen "finding a match" pill -> accurate queue time, and the
            # match logs even on the first/no-VPN match (OCR duration bypasses the
            # menu-inflated first-of-session skip). Cheap: menu, not in a match.
            interval = 4
        time.sleep(interval)


# ===========================================================================
# DESKTOP NOTIFICATIONS  (best-effort Windows toast, no dependencies)
# ---------------------------------------------------------------------------
# Fires a native Windows toast via PowerShell's WinRT API. Runs on a daemon
# thread, never raises, and silently no-ops on non-Windows or if the toast
# can't be shown. Whether it actually appears also depends on the user's
# Windows notification settings (Focus Assist, etc.).
# ===========================================================================
def _notify(title, message):
    if OS != "Windows":
        return

    def _work():
        try:
            t = str(title).replace("'", "''")
            m = str(message).replace("'", "''")
            ps = (
                "$ErrorActionPreference='SilentlyContinue';"
                "[Windows.UI.Notifications.ToastNotificationManager,Windows.UI.Notifications,ContentType=WindowsRuntime]|Out-Null;"
                "$x=[Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02);"
                "$tx=$x.GetElementsByTagName('text');"
                f"$tx.Item(0).AppendChild($x.CreateTextNode('{t}'))|Out-Null;"
                f"$tx.Item(1).AppendChild($x.CreateTextNode('{m}'))|Out-Null;"
                "$toast=[Windows.UI.Notifications.ToastNotification]::new($x);"
                "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('FRAGROUTE').Show($toast);"
            )
            subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                           timeout=8, capture_output=True, **_NO_WINDOW_KW)
        except Exception:
            pass
    threading.Thread(target=_work, daemon=True).start()


# ===========================================================================
# CONNECTION HEALTH  (watch the active tunnel's latency, jitter, liveness)
# ---------------------------------------------------------------------------
# A background thread pings the active tunnel's endpoint every ~12s (a few
# pings per cycle for a jitter estimate). If the tunnel stops responding it can
# notify and, optionally, auto-reconnect the same region. Runs independently of
# the UI so it works while you're in-game with the window hidden.
# ===========================================================================
def health_snapshot():
    """Thread-safe copy of the current tunnel-health stats."""
    with _HEALTH_LOCK:
        snap = dict(TUNNEL_HEALTH)
        snap["history"] = list(TUNNEL_HEALTH["history"])
        return snap


def _reset_health(tunnel=None):
    with _HEALTH_LOCK:
        TUNNEL_HEALTH.update({
            "tunnel": tunnel, "alive": None, "lastMs": None, "avgMs": None,
            "jitterMs": None, "lossPct": None, "consecutiveFails": 0,
            "history": [], "checkedTs": None,
        })
    _HEALTH_RECONNECT["tried"] = False


def _health_tick():
    name = STATE.get("active_tunnel")
    if not name:
        # nothing connected -> clear stats once
        if TUNNEL_HEALTH["tunnel"] is not None:
            _reset_health(None)
        return
    # if the active tunnel changed, start a fresh window
    if TUNNEL_HEALTH["tunnel"] != name:
        _reset_health(name)

    _, cfg = find_config_by_name(name)
    host = (cfg or {}).get("endpoint_host")
    if not host:
        return

    # a few quick pings -> jitter estimate. While you're IN a match, send just
    # one (not three) so we add the least possible traffic to the live tunnel.
    samples = [ping_host(host, timeout_ms=1000) for _ in range(1 if _in_match() else 3)]
    ok = [s for s in samples if s is not None]

    with _HEALTH_LOCK:
        # guard against a tunnel switch mid-cycle
        if STATE.get("active_tunnel") != name:
            return
        hist = TUNNEL_HEALTH["history"]
        hist.extend(samples)
        del hist[:-30]
        recent_ok = [s for s in hist if s is not None]
        avg = (sum(recent_ok) / len(recent_ok)) if recent_ok else None
        jitter = (sum(abs(s - avg) for s in recent_ok) / len(recent_ok)
                  if (avg is not None and len(recent_ok) >= 2) else None)
        loss = (sum(1 for s in hist if s is None) / len(hist) * 100) if hist else None
        TUNNEL_HEALTH["lastMs"] = round(ok[-1], 1) if ok else None
        TUNNEL_HEALTH["avgMs"] = round(avg, 1) if avg is not None else None
        TUNNEL_HEALTH["jitterMs"] = round(jitter, 1) if jitter is not None else None
        TUNNEL_HEALTH["lossPct"] = round(loss) if loss is not None else None
        TUNNEL_HEALTH["alive"] = bool(ok)
        TUNNEL_HEALTH["consecutiveFails"] = (0 if ok
                                             else TUNNEL_HEALTH["consecutiveFails"] + 1)
        TUNNEL_HEALTH["checkedTs"] = int(time.time() * 1000)
        cfails = TUNNEL_HEALTH["consecutiveFails"]
        region = STATE.get("active_region")

    # tunnel looks dead -> notify + optional auto-reconnect (once per outage)
    if cfails >= 3 and not _HEALTH_RECONNECT["tried"]:
        _HEALTH_RECONNECT["tried"] = True
        if get_setting("notifyTunnelDrop"):
            _notify(APP_NAME, "VPN tunnel isn't responding.")
        if get_setting("autoReconnect") and region and not STATE.get("dry_run"):
            try:
                res = connect_region(region)
                if res.get("ok") and get_setting("notifyTunnelDrop"):
                    _notify(APP_NAME, "Reconnected the tunnel automatically.")
            except Exception:
                pass
    elif cfails == 0:
        _HEALTH_RECONNECT["tried"] = False


def _health_loop():
    while True:
        try:
            _health_tick()
        except Exception:
            pass
        # slower cadence during a match (the tick itself also sends fewer pings)
        time.sleep(20 if _in_match() else 12)


# ===========================================================================
# FRAGPUNK NEWS  (official Steam news + YouTube videos; Discord link)
# Discord message history is NOT publicly readable without a bot inside the
# server, so we surface the official sources we *can* read live, plus a link.
# ===========================================================================
FRAGPUNK_STEAM_APPID = "2943650"
FRAGPUNK_YT_CHANNEL = "UC6Y3O2aAJf6orASsPAI9ElA"
FRAGPUNK_DISCORD = "https://discord.com/invite/fragpunk"
FRAGPUNK_SITE = "https://www.fragpunk.com/news"
_STEAM_CLAN_IMG = "https://clan.cloudflare.steamstatic.com/images/"
NEWS_CACHE = {"data": None, "ts": 0.0}
NEWS_TTL = 600       # re-serve a GOOD result for up to 10 minutes
NEWS_FAIL_TTL = 20   # if the last fetch FAILED, allow a fresh try after 20s
_NEWS_LOCK = threading.Lock()  # serialize fetches so retries don't pile up


def _http_get(url, timeout=10):
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) FRAGROUTE/1.0",
               "Accept": "*/*"}
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()
    except urllib.error.URLError as e:
        # Windows Python often can't verify TLS roots -> retry without verification.
        reason = getattr(e, "reason", None)
        if isinstance(reason, ssl.SSLError) or "CERTIFICATE" in str(reason).upper():
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
                return r.read()
        raise


def _strip_bbcode(text):
    text = re.sub(r"\[img\][^\[]*\[/img\]", " ", text, flags=re.I)  # drop image blocks
    text = re.sub(r"<img[^>]*>", " ", text, flags=re.I)
    text = re.sub(r"\[/?[a-zA-Z][^\]]*\]", " ", text)               # other [bbcode]
    text = re.sub(r"<[^>]+>", " ", text)                            # any <html> tags
    text = text.replace("{STEAM_CLAN_IMAGE}", " ")
    text = _html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _first_image(contents):
    """Find the article's lead image. Steam posts host images several ways, so
    we check, in order: an [img] tag, an <img> tag, a bare {STEAM_CLAN_IMAGE}
    placeholder (header images frequently have NO [img] wrapper -- this was the
    main reason images went missing), and finally any raw steam image URL.
    Handles BOTH images with a normal extension AND Steam's extensionless
    /ugc/ images (steamusercontent), which is why some images didn't show."""
    src = None
    m = re.search(r"\[img\]([^\[]+)\[/img\]", contents, re.I)
    if m:
        src = m.group(1).strip()
    if not src:
        m = re.search(r'<img[^>]+src="([^"]+)"', contents, re.I)
        if m:
            src = m.group(1).strip()
    if not src:
        # bare {STEAM_CLAN_IMAGE}/path.ext  (no [img] wrapper, with extension)
        m = re.search(r"\{STEAM_CLAN_IMAGE\}(/?\S+?\.(?:png|jpe?g|gif|webp))", contents, re.I)
        if m:
            src = "{STEAM_CLAN_IMAGE}" + m.group(1)
    if not src:
        # bare {STEAM_CLAN_IMAGE}/path with NO extension (Steam allows this)
        m = re.search(r"\{STEAM_CLAN_IMAGE\}(/?[\w./-]+)", contents, re.I)
        if m:
            src = "{STEAM_CLAN_IMAGE}" + m.group(1)
    if not src:
        # any absolute steam/akamai image URL with an extension (keep ?query)
        m = re.search(r'https?://\S+?\.(?:png|jpe?g|gif|webp)(?:\?\S+)?', contents, re.I)
        if m:
            src = m.group(0)
    if not src:
        # extensionless Steam user-content image URL (e.g. .../ugc/<id>/<hash>/)
        m = re.search(r'https?://[\w.-]*steamuser(?:content|images)[\w.-]*/\S+?(?=[\s\[\]"<]|$)',
                      contents, re.I)
        if m:
            src = m.group(0)
    if not src:
        return None
    # {STEAM_CLAN_IMAGE} already ends in '/', so strip a leading '/' on the path
    # to avoid a double slash (clan.../images//abc -> clan.../images/abc)
    src = src.replace("{STEAM_CLAN_IMAGE}/", _STEAM_CLAN_IMG).replace("{STEAM_CLAN_IMAGE}", _STEAM_CLAN_IMG)
    if src.startswith("//"):
        src = "https:" + src
    # trim trailing punctuation/junk that can cling to a bare URL match
    src = src.rstrip('.,);\'"')
    return src


def fetch_steam_news(count=12):
    # NOTE: no maxlength -> Steam returns the FULL article body. Truncating with
    # maxlength=800 both cut the news text AND often chopped off the article's
    # image (when the first image sits past the cut), which is why images and
    # full news were missing. We keep the whole body and trim only for display.
    url = ("https://api.steampowered.com/ISteamNews/GetNewsForApp/v2/"
           f"?appid={FRAGPUNK_STEAM_APPID}&count={count}&format=json")
    data = json.loads(_http_get(url))
    out = []
    for it in data.get("appnews", {}).get("newsitems", []):
        contents = it.get("contents", "") or ""
        out.append({
            "type": "article",
            "source": "Steam",
            "title": (it.get("title") or "").strip(),
            "url": it.get("url", ""),
            "date": int(it.get("date", 0)),
            "image": _first_image(contents),
            "snippet": _strip_bbcode(contents)[:280],   # display preview only
            "body": _strip_bbcode(contents),            # FULL news text for the reader
            "feed": it.get("feedlabel", ""),
        })
    return out


def fetch_youtube_videos(count=8):
    url = f"https://www.youtube.com/feeds/videos.xml?channel_id={FRAGPUNK_YT_CHANNEL}"
    ns = {"a": "http://www.w3.org/2005/Atom",
          "yt": "http://www.youtube.com/xml/schemas/2015"}
    root = ET.fromstring(_http_get(url))
    out = []
    for e in root.findall("a:entry", ns)[:count]:
        vid = e.findtext("yt:videoId", default="", namespaces=ns)
        title = (e.findtext("a:title", default="", namespaces=ns) or "").strip()
        pub = e.findtext("a:published", default="", namespaces=ns) or ""
        link_el = e.find("a:link", ns)
        link = (link_el.get("href") if link_el is not None
                else f"https://www.youtube.com/watch?v={vid}")
        ts = 0
        try:
            ts = int(datetime.datetime.fromisoformat(
                pub.replace("Z", "+00:00")).timestamp())
        except Exception:
            pass
        out.append({
            "type": "video",
            "source": "YouTube",
            "title": title,
            "url": link,
            "videoId": vid,
            "date": ts,
            "image": (f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg" if vid else None),
            "snippet": "",
        })
    return out


def _official_article_body(url):
    """Fetch a FragPunk news article page and extract its readable body."""
    try:
        s = _http_get(url, timeout=10).decode("utf-8", "ignore")
    except Exception:
        return ""
    # the article text lives in a .content container; fall back to whole-page strip
    m = re.search(r'class="[^"]*\bcontent\b[^"]*"[^>]*>(.*?)</div>\s*</div>', s, re.S)
    chunk = m.group(1) if m else s
    chunk = re.sub(r"<script.*?</script>", " ", chunk, flags=re.S)
    chunk = re.sub(r"<style.*?</style>", " ", chunk, flags=re.S)
    txt = _html.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", chunk))).strip()
    return txt[:6000]


def fetch_official_news(count=10):
    """Official FragPunk news straight from the NetEase-hosted site
    (www.fragpunk.com/news): patch notes, announcements, dev diaries. The list
    is server-rendered HTML; we parse it and pull each article's full body."""
    s = _http_get(FRAGPUNK_SITE, timeout=12).decode("utf-8", "ignore")
    blocks = re.findall(
        r'<a\b[^>]*href="(https://www\.fragpunk\.com/news/(?:official|update|diaries)/\d+/[^"]+\.html)"'
        r'[^>]*class="news-wrapper__item[^"]*"[^>]*>(.*?)</a>', s, re.S)

    def field(inner, cls):
        m = re.search(r'class="' + cls + r'"[^>]*>(.*?)</div>', inner, re.S)
        return _html.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", m.group(1)))).strip() if m else ""

    items = []
    for href, inner in blocks[:count]:
        title = field(inner, "news-title")
        if not title:
            continue
        desc = field(inner, "news-desc")
        tag = field(inner, "tag") or "FragPunk"
        tm = field(inner, "time")  # e.g. 2026/06/10
        ts = 0
        try:
            ts = int(datetime.datetime.strptime(tm, "%Y/%m/%d").timestamp())
        except Exception:
            pass
        im = re.search(r'<img[^>]+src="([^"]+)"', inner)
        img = im.group(1) if im else None
        if img and img.startswith("//"):
            img = "https:" + img
        items.append({"type": "article", "source": "FragPunk", "title": title,
                      "url": href, "date": ts, "image": img,
                      "snippet": desc[:280], "feed": tag, "_needBody": True})

    # pull full article bodies in parallel (best effort; snippet shows regardless)
    def _body(it):
        try:
            it["body"] = _official_article_body(it["url"]) or it.get("snippet", "")
        except Exception:
            it["body"] = it.get("snippet", "")
        it.pop("_needBody", None)
    ths = [threading.Thread(target=_body, args=(it,), daemon=True) for it in items]
    for t in ths:
        t.start()
    for t in ths:
        t.join(timeout=10)
    for it in items:
        it.setdefault("body", it.get("snippet", ""))
        it.pop("_needBody", None)
    return items


def get_news(force=False):
    now = time.time()
    cached = NEWS_CACHE["data"]
    if not force and cached is not None:
        # A good cached result (has items) is reused for the full TTL.
        # A failed one (no items, just errors) is only held briefly so the
        # panel recovers on its own the moment the route starts working.
        had_items = bool(cached.get("items"))
        ttl = NEWS_TTL if had_items else NEWS_FAIL_TTL
        if now - NEWS_CACHE["ts"] < ttl:
            return cached
    # only one fetch at a time; whoever holds the lock refreshes, others reuse
    with _NEWS_LOCK:
        now = time.time()
        cached = NEWS_CACHE["data"]
        if not force and cached is not None and cached.get("items"):
            if now - NEWS_CACHE["ts"] < NEWS_TTL:
                return cached
        return _fetch_news_now(now)


def _fetch_news_now(now):
    results, errors = {}, {}

    def _run(name, fn):
        try:
            results[name] = fn()
        except Exception as e:
            errors[name] = f"{type(e).__name__}: {e}"

    workers = [threading.Thread(target=_run, args=(n, f), daemon=True)
               for n, f in (("official", fetch_official_news),
                            ("steam", fetch_steam_news),
                            ("youtube", fetch_youtube_videos))]
    for w in workers:
        w.start()
    for w in workers:
        w.join(timeout=18)
    items = []
    for n in ("official", "steam", "youtube"):
        items.extend(results.get(n, []))
    items.sort(key=lambda x: x.get("date", 0), reverse=True)
    payload = {"items": items[:24], "errors": errors,
               "discord": FRAGPUNK_DISCORD, "site": FRAGPUNK_SITE,
               "updated": int(now)}
    # Cache either way: a good result for the long TTL, a failure for the short
    # one (so repeated UI retries don't hammer dead sources, but recovery is
    # still quick). get_news() decides which TTL applies by whether items exist.
    NEWS_CACHE["data"] = payload
    NEWS_CACHE["ts"] = now
    if items:
        diag("news", True, msg=f"{len(items)} stories")
    else:
        diag("news", False, msg="no stories; " + "; ".join(
            f"{k}: {v}" for k, v in errors.items()) if errors else "no stories")
    return payload


def news_diagnostics():
    """Probe each news source and report EXACTLY what happens, so the user can
    see why news isn't loading. Tests DNS + HTTP for Steam and YouTube, plus a
    plain-internet check, and reports whether a VPN tunnel is currently up."""
    import socket

    def probe(name, url, host):
        out = {"source": name, "url": url, "ok": False, "detail": ""}
        # 1) DNS
        try:
            ip = socket.gethostbyname(host)
            out["dns"] = ip
        except Exception as e:
            out["dns"] = None
            out["detail"] = f"DNS failed: {type(e).__name__}: {e}"
            return out
        # 2) HTTP GET (small)
        try:
            t0 = time.time()
            data = _http_get(url, timeout=8)
            out["ok"] = True
            out["bytes"] = len(data)
            out["ms"] = int((time.time() - t0) * 1000)
        except Exception as e:
            out["detail"] = f"HTTP failed: {type(e).__name__}: {e}"
        return out

    steam_url = ("https://api.steampowered.com/ISteamNews/GetNewsForApp/v2/"
                 f"?appid={FRAGPUNK_STEAM_APPID}&count=1&maxlength=10&format=json")
    yt_url = ("https://www.youtube.com/feeds/videos.xml"
              f"?channel_id={FRAGPUNK_YT_CHANNEL}")
    results = [
        probe("FragPunk", FRAGPUNK_SITE, "www.fragpunk.com"),
        probe("Steam", steam_url, "api.steampowered.com"),
        probe("YouTube", yt_url, "www.youtube.com"),
    ]
    # plain internet sanity check (a host that's almost never blocked)
    try:
        _http_get("https://api.ipify.org", timeout=5)
        internet = True
    except Exception:
        internet = False

    return {
        "internet": internet,
        "vpnTunnelUp": bool(STATE.get("active_tunnel")),
        "activeTunnel": STATE.get("active_tunnel"),
        "sources": results,
        "hint": ("All sources reachable." if all(r["ok"] for r in results)
                 else ("No internet at all -- check your connection." if not internet
                       else "Internet works but a news source is blocked -- "
                            "most likely your VPN tunnel. Disconnect and retry.")),
    }


# --- Media proxy: serve remote news images from localhost so they always
#     render INSIDE the app window (same-origin), even in a locked-down WebView.
IMG_CACHE = {}          # url -> (content_type, bytes)
IMG_CACHE_MAX = 80
# Steam serves news images from several CDNs; allow all the steamstatic/akamai
# hosts plus YouTube thumbnails. Suffix-matched so any subdomain works (e.g.
# clan.cloudflare.steamstatic.com, shared.akamai.steamstatic.com, etc.)
# steamusercontent.com + steamuserimages = user-uploaded images embedded in
# news posts (these were a common cause of "some images don't load").
_IMG_HOSTS = (".ytimg.com", ".steamstatic.com", ".akamaihd.net",
              ".steamcontent.com", ".akamai.steamstatic.com",
              ".cloudflare.steamstatic.com", ".steampowered.com",
              ".steamusercontent.com", ".steamusercontent.com.",
              ".steamcdn-a.akamaihd.net", ".steamusercontent-a.akamaihd.net",
              # FragPunk official site + NetEase CDN (official news/lancer images)
              ".fragpunk.com", ".easebar.com", ".res.easebar.com", ".nie.easebar.com")


def _img_host_ok(url):
    try:
        h = (urllib.parse.urlparse(url).hostname or "").lower()
    except Exception:
        return False
    return h == "img.youtube.com" or any(h.endswith(s) for s in _IMG_HOSTS)


def fetch_image(url):
    if url in IMG_CACHE:
        return IMG_CACHE[url]
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) FRAGROUTE/1.0",
               "Accept": "image/avif,image/webp,image/*,*/*",
               # Steam sometimes 403s image hotlinks without a referer
               "Referer": "https://store.steampowered.com/"}
    req = urllib.request.Request(url, headers=headers)
    try:
        r = urllib.request.urlopen(req, timeout=10)
    except urllib.error.URLError as e:
        reason = getattr(e, "reason", None)
        if isinstance(reason, ssl.SSLError) or "CERTIFICATE" in str(reason).upper():
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            r = urllib.request.urlopen(req, timeout=10, context=ctx)
        else:
            raise
    try:
        ctype = r.headers.get("Content-Type", "image/jpeg")
        data = r.read()
    finally:
        r.close()
    # If the host handed back an HTML error page instead of an image, don't
    # cache it as one -- surface a clean failure so the UI shows the placeholder.
    if "text/html" in ctype.lower() or data[:15].lstrip().lower().startswith(b"<!doctype") or data[:6].lower() == b"<html>":
        raise ValueError("not an image (got an HTML page)")
    if not ctype.lower().startswith("image/"):
        # some CDNs send octet-stream for valid images; sniff the magic bytes
        if data[:8].startswith(b"\x89PNG\r\n\x1a\n"):
            ctype = "image/png"
        elif data[:3] == b"\xff\xd8\xff":
            ctype = "image/jpeg"
        elif data[:6] in (b"GIF87a", b"GIF89a"):
            ctype = "image/gif"
        elif data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            ctype = "image/webp"
        else:
            raise ValueError(f"not an image (content-type {ctype})")
    if len(IMG_CACHE) >= IMG_CACHE_MAX:
        IMG_CACHE.clear()
    IMG_CACHE[url] = (ctype, data)
    return ctype, data


# ===========================================================================
# HTTP SERVER
# ===========================================================================
SCRIPT_DIR = Path(__file__).resolve().parent

# ---- bundled reference catalogs (Lancers / Weapons) -----------------------
# Shipped via PyInstaller --add-data so they land next to fragroute.py inside
# the onefile temp dir (== SCRIPT_DIR when frozen). Cached after first read.
_CATALOG_CACHE = {}


def load_catalog(name):
    """Load + cache a bundled JSON catalog ('lancers' or 'weapons').
    Returns the parsed object, or {} if the file is missing/corrupt."""
    if name in _CATALOG_CACHE:
        return _CATALOG_CACHE[name]
    data = {}
    try:
        p = SCRIPT_DIR / f"fragroute_{name}.json"
        if p.exists():
            data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        data = {}
    _CATALOG_CACHE[name] = data
    return data


# ===================== WEAPON SKINS (user screenshots) ====================
# Per-weapon gallery the user fills in with their own screenshots. Stored as
# base64 inside ONE portable JSON (fragroute_weapon_skins.json) -- so it's not
# dependent on the original screenshot files and travels to another PC just by
# copying that one file. The manifest endpoint strips the base64 (kept light);
# each image is fetched on demand by id. Clients downscale before upload.
import base64 as _b64


def _weapon_slug(name):
    s = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")
    return s or "weapon"


def _load_weapon_skins():
    try:
        mt = os.path.getmtime(WEAPONSKINS_PATH) if (WEAPONSKINS_PATH and Path(WEAPONSKINS_PATH).exists()) else 0.0
    except Exception:
        mt = 0.0
    # reload if never loaded OR the file changed on disk (external import / a
    # fresh copy dropped in on another machine)
    if _WEAPONSKINS_CACHE["loaded"] and _WEAPONSKINS_CACHE.get("mtime") == mt:
        return _WEAPONSKINS_CACHE["data"]
    data = {"weapons": {}}
    try:
        if WEAPONSKINS_PATH and Path(WEAPONSKINS_PATH).exists():
            loaded = json.loads(Path(WEAPONSKINS_PATH).read_text(encoding="utf-8"))
            if isinstance(loaded, dict) and isinstance(loaded.get("weapons"), dict):
                data = loaded
    except Exception as e:
        diag("weaponskins", False, msg="load", exc=e)
    _WEAPONSKINS_CACHE["data"] = data
    _WEAPONSKINS_CACHE["loaded"] = True
    _WEAPONSKINS_CACHE["mtime"] = mt
    return data


def _save_weapon_skins(data):
    if not WEAPONSKINS_PATH:
        return
    try:
        tmp = str(WEAPONSKINS_PATH) + ".tmp"
        Path(tmp).write_text(json.dumps(data), encoding="utf-8")
        os.replace(tmp, WEAPONSKINS_PATH)
    except Exception as e:
        diag("weaponskins", False, msg="save", exc=e)


def weapon_skins_manifest():
    """All weapons -> their skins, WITHOUT the heavy base64 (id/label/ts/size
    only). The UI fetches each image separately by id."""
    data = _load_weapon_skins()
    out = {}
    total = 0
    for slug, rec in (data.get("weapons") or {}).items():
        skins = []
        for s in (rec.get("skins") or []):
            skins.append({"id": s.get("id"), "label": s.get("label", ""),
                          "ts": s.get("ts"), "w": s.get("w"), "h": s.get("h")})
        total += len(skins)
        out[slug] = {"name": rec.get("name", slug), "skins": skins,
                     "hasStats": bool(rec.get("stats"))}
    return {"weapons": out, "count": total}


# ===================== USER ICONS (rank emblems / type icons) ==============
# Small generic store the user fills with their OWN cropped art (rank emblems,
# weapon-type icons). Keyed by an arbitrary slot string; portable base64 in
# fragroute_icons.json. No shipped game art -- only what the user provides.
ICONS_PATH = None
_ICONS_LOCK = threading.Lock()
_ICONS_CACHE = {"loaded": False, "data": {"slots": {}}, "mtime": 0.0}


def _load_icons():
    try:
        mt = os.path.getmtime(ICONS_PATH) if (ICONS_PATH and Path(ICONS_PATH).exists()) else 0.0
    except Exception:
        mt = 0.0
    if _ICONS_CACHE["loaded"] and _ICONS_CACHE.get("mtime") == mt:
        return _ICONS_CACHE["data"]
    data = {"slots": {}}
    try:
        if ICONS_PATH and Path(ICONS_PATH).exists():
            loaded = json.loads(Path(ICONS_PATH).read_text(encoding="utf-8"))
            if isinstance(loaded, dict) and isinstance(loaded.get("slots"), dict):
                data = loaded
    except Exception as e:
        diag("icons", False, msg="load", exc=e)
    _ICONS_CACHE.update({"loaded": True, "data": data, "mtime": mt})
    return data


def _save_icons(data):
    if not ICONS_PATH:
        return
    try:
        tmp = str(ICONS_PATH) + ".tmp"
        Path(tmp).write_text(json.dumps(data), encoding="utf-8")
        os.replace(tmp, ICONS_PATH)
    except Exception as e:
        diag("icons", False, msg="save", exc=e)


def icons_manifest():
    return {"slots": sorted((_load_icons().get("slots") or {}).keys())}


def icon_set(slot, image):
    slot = (slot or "").strip()
    if not slot or not image:
        return {"ok": False, "message": "missing slot/image"}
    mime = "image/jpeg"; b64 = image
    try:
        if image.startswith("data:"):
            head, b64 = image.split(",", 1)
            m = re.match(r"data:([^;]+)", head)
            if m:
                mime = m.group(1)
        raw = _b64.b64decode(b64)
        if len(raw) > 12 * 1024 * 1024:
            return {"ok": False, "message": "image too large (max ~12MB)"}
    except Exception as e:
        return {"ok": False, "message": f"bad image: {e}"}
    with _ICONS_LOCK:
        data = _load_icons()
        data.setdefault("slots", {})[slot] = {"data": _b64.b64encode(raw).decode("ascii"), "mime": mime}
        _save_icons(data)
    diag("icons", True, msg=f"set {slot}")
    return {"ok": True, "slot": slot}


def icon_get(slot):
    rec = (_load_icons().get("slots") or {}).get(slot)
    if rec and rec.get("data"):
        try:
            return _b64.b64decode(rec["data"]), rec.get("mime", "image/jpeg")
        except Exception:
            return None
    return None


def weapon_stats_get(slug):
    """The stored in-game stats-panel image for a weapon (exact stats, no OCR)."""
    data = _load_weapon_skins()
    rec = (data.get("weapons") or {}).get(slug)
    if rec and rec.get("stats"):
        try:
            return _b64.b64decode(rec["stats"]), rec.get("statsMime", "image/jpeg")
        except Exception:
            return None
    return None


def weapon_skin_add(weapon, name, label, image):
    """Store one image (a data: URL or raw base64) under a weapon. Returns the
    new skin id. The image is whatever the client sends (already downscaled)."""
    slug = _weapon_slug(weapon or name)
    if not image:
        return {"ok": False, "message": "no image"}
    mime = "image/jpeg"
    b64 = image
    try:
        if image.startswith("data:"):
            head, b64 = image.split(",", 1)
            m = re.match(r"data:([^;]+)", head)
            if m:
                mime = m.group(1)
        raw = _b64.b64decode(b64)
        if len(raw) > 12 * 1024 * 1024:   # 12MB hard cap per image
            return {"ok": False, "message": "image too large"}
    except Exception as e:
        return {"ok": False, "message": f"bad image: {e}"}
    with _WEAPONSKINS_LOCK:
        data = _load_weapon_skins()
        rec = data["weapons"].setdefault(slug, {"name": name or weapon or slug, "skins": []})
        if name:
            rec["name"] = name
        sid = "%s-%d" % (slug, int(time.time() * 1000))
        rec["skins"].append({"id": sid, "label": label or "",
                             "ts": int(time.time() * 1000), "mime": mime,
                             "w": 0, "h": 0,
                             "data": _b64.b64encode(raw).decode("ascii")})
        _save_weapon_skins(data)
    diag("weaponskins", True, msg=f"added skin to {slug}")
    return {"ok": True, "id": sid, "weapon": slug}


def weapon_skin_get(sid):
    data = _load_weapon_skins()
    for rec in (data.get("weapons") or {}).values():
        for s in (rec.get("skins") or []):
            if s.get("id") == sid:
                try:
                    return _b64.b64decode(s["data"]), s.get("mime", "image/jpeg")
                except Exception:
                    return None
    return None


def weapon_skin_delete(sid):
    with _WEAPONSKINS_LOCK:
        data = _load_weapon_skins()
        for rec in (data.get("weapons") or {}).values():
            skins = rec.get("skins") or []
            new = [s for s in skins if s.get("id") != sid]
            if len(new) != len(skins):
                rec["skins"] = new
                _save_weapon_skins(data)
                return {"ok": True}
    return {"ok": False, "message": "not found"}


# ======================= LOCKER (skin gallery) =============================
# A visual gallery of the user's in-game cosmetics, built from their FragPunk
# screenshots. Designed for ZERO game impact:
#   * No bundling -- reads screenshots live from disk (Steam + Desktop folders).
#   * Crops are generated LAZILY (only when a thumbnail is requested) and CACHED
#     to %LOCALAPPDATA%/FRAGROUTE/locker_cache, so each image is processed once.
#   * Pure-PIL crop (no numpy) -- light import, ~20ms/img.
#   * MATCH GUARD: while the autodetector says we're in a match, no NEW crop work
#     runs (already-cached thumbs still serve instantly).
_LOCKER_CROP_VER = 2            # bump to invalidate all cached crops
_LOCKER_FRAGPUNK_APPID = "2943650"
_LOCKER_LABELS = None           # {id: {"label":..,"category":..}}
_LOCKER_LABELS_LOCK = threading.Lock()
_LOCKER_EXTS = (".jpg", ".jpeg", ".png")
_LOCKER_SIGS = None             # {id: [ints]} perceptual signatures (disk-cached)
_LOCKER_SIG_THR = 3.7           # high-pass distance below which two skins = "same"


def _locker_root():
    base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    p = Path(base) / "FRAGROUTE"
    return p


def _locker_cache_dir():
    d = _locker_root() / "locker_cache"
    try:
        d.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    return d


def _locker_labels_path():
    return _locker_root() / "locker_labels.json"


def _locker_load_labels():
    global _LOCKER_LABELS
    if _LOCKER_LABELS is not None:
        return _LOCKER_LABELS
    data = {}
    try:
        p = _locker_labels_path()
        if p.exists():
            data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        data = {}
    _LOCKER_LABELS = data if isinstance(data, dict) else {}
    return _LOCKER_LABELS


def _locker_save_labels():
    try:
        _locker_labels_path().write_text(
            json.dumps(_LOCKER_LABELS or {}, indent=2), encoding="utf-8")
    except Exception:
        pass


def _locker_source_dirs():
    """Discover folders that hold FragPunk screenshots. Steam screenshots live
    under userdata/<id>/760/remote/<appid>/screenshots; plus the user's Desktop
    FRAG IMAGES collection."""
    dirs = []
    # Steam (search common install roots, all userdata profiles)
    steam_roots = [
        Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Steam",
        Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Steam",
    ]
    for sr in steam_roots:
        ud = sr / "userdata"
        if not ud.exists():
            continue
        try:
            for prof in ud.iterdir():
                sc = prof / "760" / "remote" / _LOCKER_FRAGPUNK_APPID / "screenshots"
                if sc.is_dir():
                    dirs.append(sc)
        except Exception:
            pass
    # Desktop collections
    home = Path.home()
    for extra in [home / "Desktop" / "FRAG IMAGES"]:
        if extra.exists():
            dirs.append(extra)
    return dirs


def _locker_id(path):
    return hashlib.md5(str(path).encode("utf-8", "ignore")).hexdigest()[:12]


def _locker_scan():
    """Enumerate all source images (recursively for Desktop folders). Returns a
    list of dicts: {id, path, name, mtime}. Cheap -- no image decoding."""
    items = []
    seen = set()
    for d in _locker_source_dirs():
        try:
            walk = d.rglob("*") if d.name == "FRAG IMAGES" else d.iterdir()
            for p in walk:
                if not p.is_file() or p.suffix.lower() not in _LOCKER_EXTS:
                    continue
                rp = str(p)
                if rp in seen:
                    continue
                seen.add(rp)
                try:
                    mt = int(p.stat().st_mtime)
                except Exception:
                    mt = 0
                items.append({"id": _locker_id(p), "path": rp, "name": p.name, "mtime": mt})
        except Exception:
            continue
    items.sort(key=lambda x: x["mtime"], reverse=True)
    return items


def _locker_crop_box(im, top=0.085, bot=0.065, pad_x=0.10, pad_y=0.05, aw=240):
    """Pure-PIL subject crop. Gradient-magnitude -> blur -> percentile mask ->
    erode noise -> bbox. Returns a crop box (l,t,r,b) in full-image coords."""
    from PIL import ImageFilter, ImageChops
    w, h = im.size
    sy0 = int(h * top); sy1 = int(h * (1 - bot))
    stage = im.crop((0, sy0, w, sy1)); W, H = stage.size
    ah = max(1, int(H * aw / W))
    a = stage.resize((aw, ah)).convert("L")
    gx = ImageChops.difference(a, ImageChops.offset(a, 1, 0))
    gy = ImageChops.difference(a, ImageChops.offset(a, 0, 1))
    mag = ImageChops.add(gx, gy).filter(ImageFilter.GaussianBlur(3))
    px = list(mag.getdata()); s = sorted(px)
    thr = max(s[int(len(s) * 0.78)], 7)
    binimg = mag.point(lambda v: 255 if v > thr else 0)
    binimg = binimg.filter(ImageFilter.MinFilter(3)).filter(ImageFilter.MaxFilter(5))
    bbox = binimg.getbbox()
    if not bbox:
        return (0, sy0, W, sy1)
    cx0, ry0, cx1, ry1 = bbox
    sx = W / aw; sy = H / ah
    x0 = cx0 * sx; x1 = cx1 * sx; y0 = ry0 * sy; y1 = ry1 * sy
    bw = x1 - x0; bh = y1 - y0
    x0 = max(0, x0 - bw * pad_x); x1 = min(W, x1 + bw * pad_x)
    y0 = max(0, y0 - bh * pad_y); y1 = min(H, y1 + bh * pad_y)
    return (int(x0), int(sy0 + y0), int(x1), int(sy0 + y1))


def _locker_side_density(im, aw=200, top=0.085, bot=0.065):
    """Fraction of side-margin pixels that are 'edgy'. Character/skin previews
    sit on a smooth gradient -> near 0; busy menu/grid screens -> high. This is
    the one signal that separates skin previews from menus reliably."""
    from PIL import ImageChops
    w, h = im.size
    stage = im.crop((0, int(h * top), w, int(h * (1 - bot))))
    ah = max(1, int(stage.height * aw / stage.width))
    a = stage.resize((aw, ah)).convert("L")
    gx = ImageChops.difference(a, ImageChops.offset(a, 1, 0))
    gy = ImageChops.difference(a, ImageChops.offset(a, 0, 1))
    px = list(ImageChops.add(gx, gy).getdata())
    m = int(aw * 0.14); cnt = 0; tot = 0
    for y in range(ah):
        b = y * aw
        for x in list(range(m)) + list(range(aw - m, aw)):
            tot += 1
            if px[b + x] > 18:
                cnt += 1
    return cnt / float(tot or 1)


def _locker_guess_category(im, box):
    """Reliable auto-category. Only commits to 'lancer' when the background is a
    smooth gradient (a character/skin preview); everything else is left 'other'
    for the user to sort (no fabricated weapon/card guesses)."""
    try:
        if _locker_side_density(im) < 0.10:
            return "lancer"
    except Exception:
        pass
    return "other"


def _locker_thumb_path(iid):
    return _locker_cache_dir() / f"{iid}_{_LOCKER_CROP_VER}.jpg"


def _locker_build_thumb(path, iid, max_dim=520):
    """Crop + downscale one image, save to cache, return (cache_path, category).
    Returns (None, None) on failure."""
    try:
        from PIL import Image as _Img
        im = _Img.open(path).convert("RGB")
        box = _locker_crop_box(im)
        cat = _locker_guess_category(im, box)
        crop = im.crop(box)
        crop.thumbnail((max_dim, max_dim))
        out = _locker_thumb_path(iid)
        crop.save(out, "JPEG", quality=82)
        return out, cat
    except Exception:
        return None, None


def _locker_in_match():
    try:
        return AUTODETECT.get("phase") == "match"
    except Exception:
        return False


def _locker_sigs_path():
    return _locker_root() / "locker_sigs.json"


def _locker_load_sigs():
    global _LOCKER_SIGS
    if _LOCKER_SIGS is not None:
        return _LOCKER_SIGS
    data = {}
    try:
        p = _locker_sigs_path()
        if p.exists():
            data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        data = {}
    _LOCKER_SIGS = data if isinstance(data, dict) else {}
    return _LOCKER_SIGS


def _locker_save_sigs():
    try:
        _locker_sigs_path().write_text(json.dumps(_LOCKER_SIGS or {}), encoding="utf-8")
    except Exception:
        pass


def _locker_sig(iid):
    """Perceptual signature of a cropped thumb that captures the CHARACTER, not
    the shared purple background: high-pass (image - blur) kills the smooth
    gradient, leaving the skin's detail. Returns a flat list or None. Cached."""
    sigs = _locker_load_sigs()
    if iid in sigs:
        return sigs[iid]
    tp = _locker_thumb_path(iid)
    if not tp.exists():
        return None
    try:
        from PIL import Image as _Img, ImageFilter, ImageChops
        im = _Img.open(tp).convert("RGB").resize((32, 40))
        hp = ImageChops.difference(im, im.filter(ImageFilter.GaussianBlur(6))).resize((12, 16))
        sig = list(hp.getdata())
        sig = [c for px in sig for c in px]  # flatten RGB
        sigs[iid] = sig
        return sig
    except Exception:
        return None


def _locker_sig_dist(a, b):
    if not a or not b or len(a) != len(b):
        return 1e9
    return sum(abs(x - y) for x, y in zip(a, b)) / float(len(a))


def _locker_group(items):
    """Union-find cluster of items (each a manifest row) by perceptual signature.
    Returns list of groups; each group is a list of rows (representative first).
    Rows with no cached thumb/sig become singletons."""
    sig = {}
    changed = False
    for it in items:
        s = _locker_sig(it["id"])
        if s is not None:
            sig[it["id"]] = s
            changed = True
    if changed:
        _locker_save_sigs()
    ids = [it["id"] for it in items if it["id"] in sig]
    by_row = {it["id"]: it for it in items}
    # GROUND-TRUTH LABELS override the visual guess: each skin's canonical lancer
    # (from the name/lancer the user typed). Two skins the user tagged as
    # DIFFERENT lancers must NEVER be grouped, however similar they look.
    catalog_upper = {n.upper() for n in (load_catalog("lancers").get("lancers") or {})}
    lan_of = {}
    for it in items:
        raw = (it.get("lancer") or it.get("label") or "").strip()
        if raw:
            key, _known = _canonical_lancer(raw, catalog_upper)
            lan_of[it["id"]] = key
        else:
            lan_of[it["id"]] = None

    def _compatible(i, cl):
        li = lan_of.get(i)
        if li is None:
            return True
        for m in cl:
            lm = lan_of.get(m)
            if lm is not None and lm != li:
                return False  # conflicting lancer labels -> never the same skin
        return True

    # COMPLETE-LINKAGE clustering: a skin joins a group only if it's within the
    # threshold of EVERY existing member -- not just one. Single-linkage chained
    # different skins together (e.g. Pathojen <-> Sonar via an in-between angle);
    # requiring all-pairs-close blocks that, and the label check above hard-stops
    # any cross-lancer merge the visual signal would otherwise allow.
    clusters = []  # list of [ids]
    for i in ids:
        si = sig[i]
        placed = False
        for cl in clusters:
            if _compatible(i, cl) and all(_locker_sig_dist(si, sig[m]) < _LOCKER_SIG_THR for m in cl):
                cl.append(i)
                placed = True
                break
        if not placed:
            clusters.append([i])
    groups = []
    for member_ids in clusters:
        rows = [by_row[i] for i in member_ids]
        # representative: prefer a member the user already named, else newest
        rows.sort(key=lambda r: (0 if r.get("label") else 1))
        groups.append(rows)
    # items with no signature (uncached) -> their own singleton groups
    for it in items:
        if it["id"] not in sig:
            groups.append([it])
    return groups


def locker_manifest(view="lancer"):
    """List cosmetics with their saved label/category + whether a thumb is
    cached. Does NOT generate crops -- cheap and safe to call anytime.

    view="lancer" (default) returns ONLY items whose effective category is
    'lancer' (the auto-detected character skins) so the Locker stays a focused
    skin gallery instead of every screenshot. view="all" returns everything
    (used by the 'show all' safety toggle to rescue mis-categorized items)."""
    labels = _locker_load_labels()
    catalog_upper = {n.upper() for n in (load_catalog("lancers").get("lancers") or {})}
    all_items = []
    cats = {}
    for it in _locker_scan():
        iid = it["id"]
        lab = labels.get(iid, {})
        cached = _locker_thumb_path(iid).exists()
        eff = lab.get("category") or lab.get("autoCategory") or "other"
        raw_lan = (lab.get("lancer") or lab.get("label") or "").strip()
        lancer_key = _canonical_lancer(raw_lan, catalog_upper)[0] if raw_lan else ""
        row = {
            "id": iid,
            "name": it["name"],
            "label": lab.get("label", ""),
            "category": lab.get("category", ""),
            "autoCategory": lab.get("autoCategory", ""),
            "lancer": lab.get("lancer", ""),
            "lancerKey": lancer_key,
            "portrait": bool(lab.get("portrait")),
            "effective": eff,
            "cached": cached,
        }
        all_items.append(row)
        cats[eff] = cats.get(eff, 0) + 1
    lancer_n = cats.get("lancer", 0)
    if view == "all":
        # everything, ungrouped (so the user can reclassify individuals)
        items = all_items
    else:
        # lancer skins, with near-identical poses/angles GROUPED into one card
        lancers = [r for r in all_items if r["effective"] == "lancer"]
        items = []
        for grp in _locker_group(lancers):
            rep = dict(grp[0])
            rep["members"] = [r["id"] for r in grp]
            rep["groupCount"] = len(grp)
            # inherit a label from any named member if the rep itself is unnamed
            if not rep.get("label"):
                for r in grp:
                    if r.get("label"):
                        rep["label"] = r["label"]
                        break
            items.append(rep)
        # newest-first by keeping rep order roughly stable: sort groups by whether
        # named, then leave as-is (scan was already mtime-desc)
    return {"items": items, "total": len(items), "totalAll": len(all_items),
            "lancerCount": lancer_n, "groupedCount": len(items) if view != "all" else None,
            "hiddenCount": len(all_items) - lancer_n,
            "categories": cats, "view": view,
            "dirs": [str(d) for d in _locker_source_dirs()]}


def locker_get_thumb(iid):
    """Return (bytes, content_type) for a thumbnail. Generates+caches on demand
    unless we're mid-match (then only serves an existing cache; returns None to
    signal 'try again later')."""
    out = _locker_thumb_path(iid)
    if out.exists():
        try:
            return out.read_bytes(), "image/jpeg"
        except Exception:
            return None
    if _locker_in_match():
        return None  # defer crop work until the match ends
    # find the path for this id
    for it in _locker_scan():
        if it["id"] == iid:
            cpath, cat = _locker_build_thumb(it["path"], iid)
            if cpath and cat:
                # remember the auto-category (don't clobber a user override)
                with _LOCKER_LABELS_LOCK:
                    labs = _locker_load_labels()
                    rec = labs.setdefault(iid, {})
                    rec.setdefault("autoCategory", cat)
                    _locker_save_labels()
                try:
                    return Path(cpath).read_bytes(), "image/jpeg"
                except Exception:
                    return None
            return None
    return None


def locker_get_full(iid):
    """Return (bytes, content_type) of a larger view (cropped, higher-res)."""
    for it in _locker_scan():
        if it["id"] == iid:
            try:
                from PIL import Image as _Img
                im = _Img.open(it["path"]).convert("RGB")
                box = _locker_crop_box(im)
                crop = im.crop(box); crop.thumbnail((1100, 1100))
                import io as _io
                buf = _io.BytesIO(); crop.save(buf, "JPEG", quality=88)
                return buf.getvalue(), "image/jpeg"
            except Exception:
                return None
    return None


def locker_set_label(iid, label=None, category=None, lancer=None, portrait=None):
    with _LOCKER_LABELS_LOCK:
        labs = _locker_load_labels()
        rec = labs.setdefault(iid, {})
        if label is not None:
            rec["label"] = str(label)[:60]
        if category is not None:
            rec["category"] = str(category)[:24]
        if lancer is not None:
            rec["lancer"] = str(lancer)[:32].strip()
        if portrait is not None:
            want = bool(portrait)
            rec["portrait"] = want
            if want:
                # only ONE portrait per lancer -- clear it on every other skin
                # assigned to the same lancer
                key = (rec.get("lancer") or "").strip().upper()
                if key:
                    for other_id, other in labs.items():
                        if other_id != iid and (other.get("lancer") or "").strip().upper() == key:
                            other["portrait"] = False
        _locker_save_labels()
    return {"ok": True, "id": iid}


def _edit_distance(a, b):
    if a == b:
        return 0
    la, lb = len(a), len(b)
    if not la:
        return lb
    if not lb:
        return la
    prev = list(range(lb + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[lb]


def _canonical_lancer(name, catalog_upper):
    """Map a user-typed lancer name to the nearest catalog name, tolerating
    typos (zypher->ZEPHYR, pathogen->PATHOJEN, hollopoint->HOLLOWPOINT). Returns
    (canonical_or_original_UPPER, is_known)."""
    n = (name or "").strip().upper()
    if not n:
        return None, False
    if n in catalog_upper:
        return n, True
    best = None
    best_d = 99
    for c in catalog_upper:
        d = _edit_distance(n, c)
        if d < best_d:
            best_d, best = d, c
    # accept a fuzzy hit when the edit distance is small relative to length
    # (~1/3 of the name's length, min 2) -- catches pahtojen->PATHOJEN (d=3)
    # without letting genuinely different names collide
    if best is not None and best_d <= max(2, round(len(best) * 0.34)):
        return best, True
    return n, False


def lancer_portraits():
    """Map lancer name (canonical UPPER) -> {id, label} of the skin to show as
    that lancer's portrait. The lancer is taken from the dedicated 'lancer'
    field, falling back to the skin's typed name ('label') -- the user already
    typed lancer names there. Typos are fuzzy-matched to the catalog. Chosen
    skin = pinned portrait=true, else newest assigned. 'custom' = assigned
    lancers that don't match the bundled catalog."""
    labs = _locker_load_labels()
    catalog_upper = {n.upper() for n in (load_catalog("lancers").get("lancers") or {})}
    order = {it["id"]: idx for idx, it in enumerate(_locker_scan())}
    chosen = {}      # KEY -> (rank, id, label, pinned)
    known = {}       # KEY -> bool (matched catalog)
    counts = {}      # KEY -> number of skins assigned to that lancer
    for iid, rec in labs.items():
        # respect an EXPLICIT user category: if they said it's a weapon/card/etc,
        # it's not a lancer portrait. (auto-category alone is just a guess and is
        # overridden below when the typed name matches a known lancer.)
        user_cat = rec.get("category")
        if user_cat and user_cat != "lancer":
            continue
        raw = (rec.get("lancer") or rec.get("label") or "").strip()
        key, is_known = _canonical_lancer(raw, catalog_upper)
        if not key:
            continue
        # if the name isn't a recognizable lancer AND nothing says it's a lancer,
        # skip it (keeps random skin names / weapons out of the portrait map)
        if not is_known and user_cat != "lancer" and rec.get("autoCategory") != "lancer":
            continue
        if not _locker_thumb_path(iid).exists():
            continue
        counts[key] = counts.get(key, 0) + 1
        pinned = bool(rec.get("portrait"))
        rank = order.get(iid, 1_000_000)
        cur = chosen.get(key)
        better = (cur is None
                  or (pinned and not cur[3])
                  or (pinned == cur[3] and rank < cur[0]))
        if better:
            chosen[key] = (rank, iid, raw or key, pinned)
            known[key] = is_known
    portraits = {k: {"id": v[1], "label": v[2]} for k, v in chosen.items()}
    custom = sorted(k for k in portraits if not known.get(k))
    # counts = DISTINCT skins (grouped), so the number on a lancer card matches
    # the grouped cards shown when you filter the Locker to that lancer.
    try:
        gman = locker_manifest("lancer")
        gcounts = {}
        for it in gman["items"]:
            k = it.get("lancerKey")
            if k:
                gcounts[k] = gcounts.get(k, 0) + 1
        counts = gcounts
    except Exception:
        pass
    return {"portraits": portraits, "custom": custom, "counts": counts}


# Lancer-name aliases for matching patch-note prose to catalog keys (the game
# spells a few differently than our catalog keys).
_LANCER_ALIASES = {"PATHOJEN": ["pathojen", "pathogen"], "ZEPHYR": ["zephyr", "zypher"]}
_BUFF_WORDS = ("increase", "increased", "buff", "buffed", "improv", "enhanc", "boost",
               "raised", "extended", "higher", "faster", "stronger", "added")
_NERF_WORDS = ("decrease", "decreased", "nerf", "nerfed", "reduc", "lower", "shorten",
               "weaker", "slower", "removed", "no longer")


# a mention only counts as a balance/ability change if it sits near one of these
# (filters out cosmetic/event noise: skin rewards, stickers, name cards, etc.)
_BALANCE_SIGNALS = ("damage", " hp", "health", "duration", "cooldown", "range",
                    "skill", "abilit", "fixed", "adjust", "increase", "decrease",
                    "reduc", "buff", "nerf", "gear point", "radius", "heal",
                    "slow", "stun", "reload", "ammo", "movement speed", "recover",
                    "balance", "tuned", "scaling")


def _is_balance_change(text):
    return any(w in text.lower() for w in _BALANCE_SIGNALS)


def _classify_change(text):
    low = text.lower()
    b = sum(1 for w in _BUFF_WORDS if w in low)
    n = sum(1 for w in _NERF_WORDS if w in low)
    if b > n:
        return "buff"
    if n > b:
        return "nerf"
    return "change"


def lancer_changes(max_posts=14):
    """Scan recent OFFICIAL patch notes for mentions of each lancer (by name or
    ability) and attach the relevant excerpt, so the catalog reflects live
    balance changes. Returns {changes:{LANCER:[{date,title,url,excerpt,kind}]}}."""
    try:
        news = (get_news() or {}).get("items", [])
    except Exception:
        news = []
    posts = [n for n in news if n.get("source") == "FragPunk"][:max_posts]
    catalog = load_catalog("lancers").get("lancers") or {}
    out = {}
    for name, info in catalog.items():
        aliases = _LANCER_ALIASES.get(name, [name.lower()])
        abil = [a.lower() for a in (info.get("abilities") or {})]
        for p in posts:
            body = p.get("body") or p.get("snippet") or ""
            low = body.lower()
            hit = -1
            for al in aliases:
                hit = low.find(al)
                if hit >= 0:
                    break
            if hit < 0:
                for a in abil:
                    if len(a) >= 5:           # avoid matching tiny ability words
                        hit = low.find(a)
                        if hit >= 0:
                            break
            if hit >= 0:
                start = max(0, hit - 90)
                end = min(len(body), hit + 220)
                excerpt = body[start:end].strip()
                # skip cosmetic/event mentions (skin/sticker rewards) -- only keep
                # excerpts that actually describe a balance or ability change
                if not _is_balance_change(excerpt):
                    continue
                if start > 0:
                    excerpt = "â€¦" + excerpt
                if end < len(body):
                    excerpt = excerpt + "â€¦"
                out.setdefault(name, []).append({
                    "date": p.get("date"), "title": p.get("title"),
                    "url": p.get("url"), "excerpt": excerpt,
                    "kind": _classify_change(excerpt)})
    for name in out:
        out[name].sort(key=lambda c: c.get("date") or 0, reverse=True)
    return {"changes": out, "scanned": len(posts)}


def locker_warm_cache(limit=None, progress=None):
    """Pre-generate any missing thumbnails. Intended to be run once (e.g. from a
    dev seed or an explicit user 'build all' button), NEVER on the game's hot
    path. Skips while in a match. Returns count built."""
    built = 0
    scan = _locker_scan()
    for i, it in enumerate(scan):
        if limit and built >= limit:
            break
        if _locker_thumb_path(it["id"]).exists():
            continue
        if _locker_in_match():
            break
        cpath, cat = _locker_build_thumb(it["path"], it["id"])
        if cpath and cat:
            with _LOCKER_LABELS_LOCK:
                labs = _locker_load_labels()
                labs.setdefault(it["id"], {}).setdefault("autoCategory", cat)
                _locker_save_labels()
            built += 1
            if progress:
                progress(built, len(scan))
    return built


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # quiet
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _bytes(self, data, ctype, code=200):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "max-age=86400")
        self.end_headers()
        self.wfile.write(data)

    def _serve_file_range(self, path, ctype):
        """Serve a file with HTTP Range support so <video> can SEEK/scrub. Returns
        206 Partial Content for a Range request, else 200 full."""
        try:
            size = os.path.getsize(path)
        except Exception:
            return self._json({"error": "not found"}, 404)
        rng = self.headers.get("Range") or ""
        start, end = 0, size - 1
        partial = False
        if rng.startswith("bytes="):
            partial = True
            try:
                s, _, e = rng[6:].partition("-")
                start = int(s) if s else 0
                end = int(e) if e else size - 1
            except Exception:
                start, end = 0, size - 1
            start = max(0, start)
            end = min(end, size - 1)
            if start > end:
                start, end, partial = 0, size - 1, False
        length = end - start + 1
        try:
            with open(path, "rb") as f:
                f.seek(start)
                data = f.read(length)
        except Exception as e:
            return self._json({"error": str(e)}, 500)
        self.send_response(206 if partial else 200)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        if partial:
            self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _read_body(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode())
        except Exception:
            return {}

    # ---- GET ----
    def do_GET(self):
        # One catch-all so a failing endpoint is RECORDED (Health tab + diag log)
        # instead of silently resetting the connection -> "app isn't working".
        try:
            self._do_GET()
            diag("web", True)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            diag("web", False, msg=f"GET {self.path}", exc=e)
            try:
                self._json({"error": "internal", "detail": str(e)}, 500)
            except Exception:
                pass

    def _do_GET(self):
        path = self.path.split("?", 1)[0]

        if path == "/api/health":
            return self._json(app_health_snapshot())

        if path == "/" or path == "/index.html":
            return self._serve_ui()

        if path == "/api/regions":
            payload = []
            for r in REGIONS:
                entries = STATE["configs"].get(r["id"]) or []
                best = region_best_config(r["id"]) if entries else None
                servers = [{
                    "name": e["name"],
                    "endpoint": e["endpoint"],
                    "latencyMs": STATE["latency"].get(e["name"]),
                } for e in entries]
                real_ips = region_server_ips(r["id"])
                payload.append({
                    **r,
                    "configMapped": bool(entries),
                    "serverCount": len(entries),
                    "servers": servers,
                    "realServerCount": len(real_ips),  # harvested game-server IPs
                    "bestConfig": best["name"] if best else None,
                    "configName": best["name"] if best else None,  # back-compat
                    "endpoint": best["endpoint"] if best else None,
                    "latencyMs": region_best_latency(r["id"]),
                })
            return self._json({
                "regions": payload,
                "unmapped": STATE["unmapped"],
                "configsDir": str(STATE["configs_dir"]),
            })

        if path == "/api/latency":
            refresh_latency()
            return self._json({
                "latency": STATE["latency"],  # per-config-name -> ms
                "regionBest": {r["id"]: region_best_latency(r["id"]) for r in REGIONS},
            })

        if path == "/api/status":
            return self._json(status_snapshot(include_ip=False))

        if path == "/api/game":
            # route-independent live game/server detection (reads Fragpunk's
            # own network socket, not the VPN tunnel, not game memory)
            return self._json(game_status())

        if path == "/api/status/full":
            return self._json(status_snapshot(include_ip=True))

        if path == "/api/log":
            return self._json({"log": load_log()})

        if path == "/api/rescan":
            discover_configs(STATE["configs_dir"])
            return self._json({"ok": True,
                               "mapped": {rid: len(v) for rid, v in STATE["configs"].items()},
                               "unmapped": [u["name"] for u in STATE["unmapped"]]})

        if path == "/api/news":
            qs = self.path.split("?", 1)[1] if "?" in self.path else ""
            return self._json(get_news(force=("refresh=1" in qs)))

        if path == "/api/news/diag":
            # direct reachability test so the user can see EXACTLY why news
            # isn't loading (DNS, TLS, timeout, blocked by VPN, etc.)
            return self._json(news_diagnostics())

        if path == "/api/settings":
            return self._json({"settings": _SETTINGS, "defaults": DEFAULT_SETTINGS})

        if path == "/api/autodetect":
            # live auto-capture state (phase, queue/match clocks, session, feed)
            return self._json(autodetect_status())

        if path == "/api/scout":
            # the probe agent's warm ranking + any active slow-queue nudge
            return self._json(scout_status())

        if path == "/api/population":
            # live FragPunk concurrent players (Steam) + derived queue anchor
            return self._json(population_snapshot())

        if path == "/api/gameinfo":
            # installed game version + update-since-last-launch flag
            return self._json(game_info())

        if path == "/api/insights":
            # analytics from your log + population history (queue by region/hour)
            return self._json(play_insights())

        if path == "/api/route/profile":
            # current route-optimizer run status / progress / ranked results
            return self._json(route_profile_status())

        if path == "/api/servers":
            # harvested real game-server intel (per-region IPs + last ping)
            return self._json(load_servers())

        if path == "/api/rank":
            # competitive rank + RP history. ?refresh=1 OCRs the lobby card now
            qs = self.path.split("?", 1)[1] if "?" in self.path else ""
            return self._json(rank_status(refresh=("refresh=1" in qs)))

        if path == "/api/serverpings":
            # OCR'd in-game per-region ping table. ?refresh=1 reads it now
            qs = self.path.split("?", 1)[1] if "?" in self.path else ""
            return self._json(server_pings(refresh=("refresh=1" in qs)))

        if path == "/api/replays":
            # indexed FragPunk replays enriched with RP/region by timestamp
            return self._json(replay_library())

        if path == "/api/capture/status":
            if fragroute_capture is None:
                return self._json({"available": False, "message": "capture module unavailable"})
            return self._json(fragroute_capture.status(_captures_dir()))

        if path == "/api/ai/convo":
            # the unified chat+voice transcript, so the chat UI shows spoken turns too.
            # ?since=<ms> returns only newer turns (for lightweight polling).
            _q = urllib.parse.parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "")
            since = 0
            try:
                since = int((_q.get("since", ["0"])[0]) or 0)
            except Exception:
                since = 0
            with _CONVO_LOCK:
                turns = [t for t in _CONVO["turns"] if t.get("ts", 0) > since]
            return self._json({"turns": turns})

        if path == "/api/voice/mics":
            # list mic input devices so the user can pick which one the coach hears
            if fragroute_voice is None:
                return self._json({"ok": False, "mics": [], "message": "voice module unavailable"})
            try:
                mics = fragroute_voice.list_mics()
            except Exception as e:
                return self._json({"ok": False, "mics": [], "message": str(e)[:100]})
            return self._json({"ok": True, "mics": mics,
                               "selected": get_setting("voiceMic", "") or ""})

        if path == "/api/voice/mictest":
            # record a short burst from the chosen mic and report if it heard you
            if fragroute_voice is None:
                return self._json({"ok": False, "message": "voice module unavailable"})
            _q = urllib.parse.parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "")
            name = (_q.get("mic", [""])[0] or get_setting("voiceMic", "") or "").strip() or None
            try:
                return self._json(fragroute_voice.mic_probe(name))
            except Exception as e:
                return self._json({"ok": False, "message": str(e)[:100], "level": 0.0})

        if path == "/api/regionlock/status":
            # Direct Region Lock: current state + which regions we have enough data to
            # block + a live latency-aware suggestion (from Arizona, auto biases East).
            if fragroute_regionlock is None:
                return self._json({"available": False, "message": "region lock unavailable"})
            st = fragroute_regionlock.status()
            try:
                regions = (load_servers().get("regions", {}) or {})
                st["knownRegions"] = sorted(set(regions.keys()) | set(_REGION_SEED_CIDRS.keys()))
                st["dataRegions"] = sorted(regions.keys())      # regions we've actually seen
                cur = None
                try:
                    cur = (game_status().get("server") or {}).get("regionId")
                except Exception:
                    cur = None
                st["currentRegion"] = cur
            except Exception:
                pass
            return self._json(st)

        if path == "/api/regionlock/preview":
            # DRY-RUN: show exactly what would be blocked to force `region` -- applies
            # nothing. `region` = the one region to KEEP open.
            if fragroute_regionlock is None:
                return self._json({"ok": False, "message": "region lock unavailable"})
            _q = urllib.parse.parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "")
            target = (_q.get("region", [""])[0] or "").strip()
            if not target:
                return self._json({"ok": False, "message": "pick a region to lock to."})
            bmap = region_block_map(target)
            plan = fragroute_regionlock.plan(bmap)
            return self._json({"ok": True, "target": target,
                               "blockRegions": sorted(bmap.keys()),
                               "cidrCount": sum(len(v) for v in bmap.values()),
                               "rules": [{"region": p["region"], "proto": p["proto"],
                                          "cidrs": p["cidrs"]} for p in plan],
                               "whitelistTcpPorts": fragroute_regionlock.WHITELIST_TCP_PORTS})

        if path == "/api/capture/audiotest":
            # Record a short burst from the system-audio loopback and report the
            # level, so the user can confirm "my game sound IS being captured"
            # (recordings were silent when Stereo Mix mirrored the wrong device).
            try:
                import fragroute_audio
            except Exception:
                fragroute_audio = None
            if fragroute_audio is None or not fragroute_audio.available():
                return self._json({"ok": False, "have": False,
                                   "message": "System-audio capture unavailable on this build."})
            res = fragroute_audio.probe(seconds=1.2)
            res["have"] = True
            res["output"] = fragroute_audio.default_output_name()
            return self._json(res)

        if path == "/api/capture/clips":
            if fragroute_capture is None:
                return self._json({"items": [], "total": 0})
            out = fragroute_capture.list_clips(_captures_dir())
            try:
                out["usage"] = fragroute_capture.recordings_usage(_captures_dir())
                out["capGB"] = float(get_setting("recordingsMaxGB", 40))
            except Exception:
                pass
            return self._json(out)

        if path == "/api/learning":
            if fragroute_learning is None:
                return self._json({"modes": {}, "totalMatches": 0})
            return self._json(fragroute_learning.summary())

        if path == "/api/ai/llm/status":
            if fragroute_llm is None:
                return self._json({"available": False, "ready": False})
            return self._json(fragroute_llm.status())

        if path == "/api/ai/vision/status":
            if fragroute_llm is None:
                return self._json({"available": False, "ready": False})
            return self._json(fragroute_llm.vision_status())

        if path == "/api/ai/vision/warm":
            # background pre-load the vision model so the first Recognize isn't a cold start
            if fragroute_llm is not None:
                try:
                    fragroute_llm.warm_vision()
                except Exception:
                    pass
            return self._json({"ok": True})

        if path == "/api/ai/voice/status":
            # coach voice (neural Piper TTS) availability + installed voices
            if fragroute_tts is None:
                return self._json({"available": False, "voices": [], "engine": "sapi"})
            st = fragroute_tts.status()
            st["engine"] = "piper" if st.get("available") else "sapi"
            st["selected"] = get_setting("ttsVoice", None)
            st["rate"] = get_setting("ttsRate", 1.0)
            st["enabled"] = get_setting("coachSpeak", True)
            st["conversing"] = bool(_CONVERSE.get("on"))
            return self._json(st)

        if path == "/api/ai/persona":
            # this player's adaptive coaching-style profile
            if fragroute_persona is None:
                return self._json({"disabled": True})
            user = ((fragroute_auth.current() if fragroute_auth else {}) or {}).get("username") or "default"
            return self._json(fragroute_persona.status(user))

        if path == "/api/ai/image/status":
            if fragroute_imagegen is None:
                return self._json({"available": False})
            return self._json(fragroute_imagegen.status())

        if path == "/api/ai/images":
            if fragroute_imagegen is None:
                return self._json({"items": [], "total": 0})
            return self._json(fragroute_imagegen.list_images())

        if path == "/api/ai/maps":
            return self._json(_maps_store())

        if path == "/api/ai/map/file":
            q = urllib.parse.parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "")
            name = (q.get("name") or [""])[0]
            f = (_maps_dir() / name) if (name and "/" not in name and "\\" not in name) else None
            if not f or not f.exists():
                return self._json({"error": "not found"}, 404)
            return self._bytes(f.read_bytes(), "image/png")

        if path == "/api/ai/image/file":
            # serve a generated PNG by name. Resolve via list_images() so we serve
            # from the SAME folder the gallery lists from (raw OUT_DIR could differ).
            if fragroute_imagegen is None:
                return self._json({"error": "unavailable"}, 404)
            q = urllib.parse.parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "")
            name = (q.get("name") or [""])[0]
            items = (fragroute_imagegen.list_images() or {}).get("items", [])
            match = next((it for it in items if it.get("name") == name), None)
            if not match or not Path(match["path"]).exists():
                return self._json({"error": "not found"}, 404)
            return self._bytes(Path(match["path"]).read_bytes(), "image/png")

        if path == "/api/train/status":
            if fragroute_dataset is None:
                return self._json({"available": False})
            return self._json(fragroute_dataset.status())

        if path == "/api/train/frames":
            if fragroute_dataset is None:
                return self._json({"frames": []})
            return self._json({"frames": fragroute_dataset.list_frames()})

        if path == "/api/train/classes":
            if fragroute_dataset is None:
                return self._json({"classes": []})
            return self._json({"classes": fragroute_dataset.classes_grouped()})

        if path == "/api/train/annotation":
            if fragroute_dataset is None:
                return self._json({"error": "unavailable"}, 404)
            q = urllib.parse.parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "")
            name = (q.get("name") or [""])[0]
            a = fragroute_dataset.get_annotation(name) if name else None
            return self._json(a or {"error": "not found"}, 200 if a else 404)

        if path == "/api/train/frame":
            if fragroute_dataset is None:
                return self._json({"error": "unavailable"}, 404)
            q = urllib.parse.parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "")
            name = (q.get("name") or [""])[0]
            fp = fragroute_dataset.frame_path(name) if (name and "/" not in name and "\\" not in name) else None
            if not fp:
                return self._json({"error": "not found"}, 404)
            return self._bytes(Path(fp).read_bytes(), "image/jpeg")

        if path == "/api/video/status":
            if fragroute_video is None:
                return self._json({"available": False})
            return self._json(fragroute_video.status())

        if path == "/api/video/list":
            if fragroute_video is None:
                return self._json({"items": [], "total": 0})
            return self._json(fragroute_video.list_edits())

        if path == "/api/video/clips":
            if fragroute_capture is None:
                return self._json({"items": []})
            return self._json(fragroute_capture.list_clips(_captures_dir()))

        if path == "/api/video/file":
            # serve an edited mp4 (or source clip) for in-app playback
            q = urllib.parse.parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "")
            name = (q.get("name") or [""])[0]
            if not name or "/" in name or "\\" in name:
                return self._json({"error": "bad name"}, 400)
            for base in (_captures_dir() / "edited", _captures_dir() / "clips"):
                fp = base / name
                if fp.exists():
                    return self._serve_file_range(str(fp), "video/mp4")
            return self._json({"error": "not found"}, 404)

        if path == "/api/live":
            # what the AI is seeing in the match right now (real-time game watch)
            d = dict(LIVE_STATE)
            if d.get("inMatch") and d.get("since"):
                d["elapsed"] = int(time.time() - d["since"])
            return self._json(d)

        if path == "/api/browser":
            return self._json(browser_status())

        if path == "/api/lancers":
            # bundled Lancer profile catalog (roles, abilities, tags)
            return self._json(load_catalog("lancers"))

        if path == "/api/lancers/portraits":
            # map lancer -> the user's Locker skin to show on its profile card
            return self._json(lancer_portraits())

        if path == "/api/lancers/changes":
            # recent official patch-note balance changes per lancer
            return self._json(lancer_changes())

        if path == "/api/weapons":
            # bundled weapon catalog (categories + per-weapon entries)
            return self._json(load_catalog("weapons"))

        if path == "/api/cards":
            # bundled Shard Card catalog (system + categories + notable cards)
            return self._json(load_catalog("cards"))

        if path == "/api/setup":
            # first-run readiness checklist (components + GPU/audio + guidance)
            return self._json(setup_status())

        if path == "/api/setup/models":
            # downloadable model manifest + per-item present/progress
            if fragroute_setup is None:
                return self._json({"items": [], "total": 0})
            return self._json(fragroute_setup.status())

        if path == "/api/auth":
            # login state + whether any account exists yet (login vs register UI)
            if fragroute_auth is None:
                return self._json({"build": "n/a", "hasAccount": False, "disabled": True,
                                   "session": {"loggedIn": True}})  # auth off => open app
            return self._json(fragroute_auth.status())

        if path == "/api/entitlement":
            # effective tier + which paid features are unlocked (drives UI locks)
            if fragroute_license is None:
                return self._json({"tier": "admin", "features": {}, "disabled": True})
            return self._json(fragroute_license.status())

        if path == "/api/hardware":
            # this PC's GPU/CPU/RAM + per-feature "will it work here" verdicts
            if fragroute_hardware is None:
                return self._json({"profile": None, "capabilities": [], "disabled": True})
            return self._json(fragroute_hardware.status())

        if path == "/api/weaponskins":
            # user's per-weapon skin gallery (metadata only; images via /img)
            return self._json(weapon_skins_manifest())

        if path == "/api/weaponskins/img":
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            sid = (q.get("id") or [""])[0]
            res = weapon_skin_get(sid) if sid else None
            if not res:
                return self._json({"error": "not found"}, 404)
            return self._bytes(res[0], res[1])

        if path == "/api/weaponskins/stats":
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            wp = (q.get("weapon") or [""])[0]
            res = weapon_stats_get(wp) if wp else None
            if not res:
                return self._json({"error": "not found"}, 404)
            return self._bytes(res[0], res[1])

        if path == "/api/icons":
            return self._json(icons_manifest())

        if path == "/api/icon":
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            slot = (q.get("slot") or [""])[0]
            res = icon_get(slot) if slot else None
            if not res:
                return self._json({"error": "not found"}, 404)
            return self._bytes(res[0], res[1])

        if path == "/api/locker":
            # manifest of the user's cosmetics (cheap; no crop work).
            # default view = lancer skins only; ?view=all shows everything
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            view = (q.get("view") or ["lancer"])[0]
            return self._json(locker_manifest("all" if view == "all" else "lancer"))

        if path == "/api/locker/thumb":
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            iid = (q.get("id") or [""])[0]
            res = locker_get_thumb(iid) if iid else None
            if not res:
                # 202 = not ready yet (deferred during match) -> UI retries
                return self._json({"pending": True}, 202)
            return self._bytes(res[0], res[1])

        if path == "/api/locker/full":
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            iid = (q.get("id") or [""])[0]
            res = locker_get_full(iid) if iid else None
            if not res:
                return self._json({"error": "not found"}, 404)
            return self._bytes(res[0], res[1])

        if path == "/api/locker/warm":
            # explicit, user-initiated 'build all thumbnails' (off the hot path)
            n = locker_warm_cache()
            return self._json({"ok": True, "built": n})

        if path == "/api/img":
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            u = (q.get("u") or [""])[0]
            if not (u.startswith("https://") and _img_host_ok(u)):
                return self._json({"error": "blocked"}, 400)
            try:
                ctype, data = fetch_image(u)
                return self._bytes(data, ctype)
            except Exception as e:
                return self._json({"error": str(e)}, 502)

        return self._json({"error": "not found"}, 404)

    # ---- POST ----
    def do_POST(self):
        try:
            self._do_POST()
            diag("web", True)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            diag("web", False, msg=f"POST {self.path}", exc=e)
            try:
                self._json({"error": "internal", "detail": str(e)}, 500)
            except Exception:
                pass

    def _do_POST(self):
        path = self.path.split("?", 1)[0]
        body = self._read_body()

        # ---- paid-feature gate (enforced SERVER-SIDE; UI locks are bypassable) --
        # Maps each paid action endpoint to the entitlement it needs. Free-core
        # endpoints (connect, queue, locker, settings...) fall through untouched.
        if fragroute_license is not None:
            def _required_feature(p):
                if p == "/api/ai/chat" or p.startswith("/api/ai/recognize") \
                        or p.startswith("/api/ai/vision") or p == "/api/ai/map/capture":
                    return "coach"
                if p == "/api/ai/image/generate":
                    return "imagegen"
                if p.startswith("/api/video/"):
                    return "video"
                if p.startswith("/api/train/"):
                    return "train"
                return None
            _feat = _required_feature(path)
            if _feat and not fragroute_license.is_enabled(_feat):
                _ent = fragroute_license.entitlement()
                return self._json({"ok": False, "error": "locked", "feature": _feat,
                                   "tier": _ent["tier"],
                                   "message": "%s is a Pro feature. Start your free trial "
                                              "or add a license in Account." % _feat.title()}, 402)

        if path == "/api/connect":
            name = body.get("config")
            rid = body.get("region")
            if name:
                return self._json(connect_config(name))
            if rid:
                if rid not in REGION_BY_ID:
                    return self._json({"ok": False, "message": "unknown region"}, 400)
                return self._json(connect_region(rid))
            return self._json({"ok": False, "message": "need a region or config"}, 400)

        if path == "/api/disconnect":
            return self._json(disconnect())

        # ---- accounts (login gate) ----------------------------------------
        if path == "/api/auth/register":
            if fragroute_auth is None:
                return self._json({"ok": False, "error": "auth unavailable"}, 500)
            return self._json(fragroute_auth.register(
                body.get("username", ""), body.get("password", ""),
                body.get("email", ""), body.get("license", "")))

        if path == "/api/auth/login":
            if fragroute_auth is None:
                return self._json({"ok": False, "error": "auth unavailable"}, 500)
            return self._json(fragroute_auth.login(body.get("username", ""), body.get("password", "")))

        if path == "/api/auth/logout":
            return self._json(fragroute_auth.logout() if fragroute_auth else {"ok": True})

        if path == "/api/auth/password":
            if fragroute_auth is None:
                return self._json({"ok": False, "error": "auth unavailable"}, 500)
            return self._json(fragroute_auth.change_password(body.get("old", ""), body.get("new", "")))

        if path == "/api/auth/reset":
            if fragroute_auth is None:
                return self._json({"ok": False, "error": "auth unavailable"}, 500)
            return self._json(fragroute_auth.reset_password(
                body.get("username", ""), body.get("code", ""), body.get("new", "")))

        # ---- licensing / entitlements -------------------------------------
        if path == "/api/license/verify":
            if fragroute_license is None:
                return self._json({"valid": False, "error": "licensing unavailable"})
            return self._json(fragroute_license.verify_key(body.get("key", "")))

        if path == "/api/license/set":
            # apply a machine-wide license key (independent of the account)
            if fragroute_license is None:
                return self._json({"valid": False, "error": "licensing unavailable"})
            return self._json(fragroute_license.set_license(body.get("key", "")))

        if path == "/api/license/clear":
            return self._json(fragroute_license.clear_license() if fragroute_license else {"ok": True})

        if path == "/api/account/license":
            # bind a key to the logged-in account (re-applied on each login)
            if fragroute_auth is None:
                return self._json({"ok": False, "error": "auth unavailable"}, 500)
            return self._json(fragroute_auth.attach_license(body.get("key", "")))

        if path == "/api/quit":
            # fully terminate the app (so a rebuild can replace the locked exe).
            # cleans up recorder + tunnels first, then hard-exits the process.
            def _bye():
                try:
                    if fragroute_capture is not None:
                        fragroute_capture.stop()
                except Exception:
                    pass
                try:
                    cleanup_stray_tunnels()
                except Exception:
                    pass
                time.sleep(0.4)
                os._exit(0)
            threading.Thread(target=_bye, daemon=True).start()
            return self._json({"ok": True, "message": "quitting"})

        if path == "/api/train/annotation":
            if fragroute_dataset is None:
                return self._json({"ok": False, "message": "dataset module unavailable"}, 400)
            name = body.get("name")
            if not name:
                return self._json({"ok": False, "message": "need a frame name"}, 400)
            return self._json(fragroute_dataset.save_annotation(name, {
                "boxes": body.get("boxes") or [],
                "w": body.get("w"), "h": body.get("h"),
                "reviewed": bool(body.get("reviewed", True))}))

        if path == "/api/train/harvest":
            if fragroute_dataset is None:
                return self._json({"ok": False, "message": "dataset module unavailable"}, 400)
            return self._json(fragroute_dataset.harvest(
                video_paths=body.get("videos"), youtube_urls=body.get("youtube"),
                fps=float(body.get("fps", 0.5))))

        if path == "/api/train/bootstrap":
            if fragroute_dataset is None:
                return self._json({"ok": False, "message": "dataset module unavailable"}, 400)
            return self._json(fragroute_dataset.bootstrap())

        if path == "/api/train/autoharvest":
            if fragroute_dataset is None:
                return self._json({"ok": False, "message": "dataset module unavailable"}, 400)
            return self._json(fragroute_dataset.auto_harvest(
                folders=_harvest_folders(), fps=float(body.get("fps", 0.5))))

        if path == "/api/train/addclass":
            if fragroute_dataset is None:
                return self._json({"ok": False, "message": "dataset module unavailable"}, 400)
            return self._json(fragroute_dataset.add_class(
                body.get("name", ""), body.get("group", "custom")))

        if path == "/api/train/delete":
            if fragroute_dataset is None:
                return self._json({"ok": False, "message": "dataset module unavailable"}, 400)
            name = body.get("name")
            if not name:
                return self._json({"ok": False, "message": "need a frame name"}, 400)
            return self._json(fragroute_dataset.delete_frame(name))

        if path == "/api/train/export":
            if fragroute_dataset is None:
                return self._json({"ok": False, "message": "dataset module unavailable"}, 400)
            return self._json(fragroute_dataset.export_yolo())

        if path == "/api/train/autofill":
            if fragroute_dataset is None:
                return self._json({"ok": False, "message": "dataset module unavailable"}, 400)
            return self._json(fragroute_dataset.autofill(
                conf_thr=float(body.get("conf", 0.5)),
                add_missed=bool(body.get("addMissed", False))))

        if path == "/api/video/montage":
            if fragroute_video is None or fragroute_capture is None:
                return self._json({"ok": False, "message": "video editor unavailable"}, 400)
            items = (fragroute_capture.list_clips(_captures_dir()) or {}).get("items", [])
            by_name = {c.get("name"): c.get("path") for c in items}
            names = body.get("clips")
            if names:
                paths = [by_name[n] for n in names if n in by_name]
            else:
                paths = list(reversed([c.get("path") for c in items[:8] if c.get("path")]))
            if len(paths) < 2:
                return self._json({"ok": False, "message": "need at least 2 clips"}, 400)
            return self._json(fragroute_video.montage(
                paths, music=(body.get("music") or None), title=(body.get("title") or "FRAGROUTE Highlights")))

        if path == "/api/setup/install":
            if fragroute_setup is None:
                return self._json({"ok": False, "message": "installer unavailable"}, 400)
            keys = body.get("keys")          # list of keys, or omit for all-missing
            return self._json(fragroute_setup.download(keys))

        if path == "/api/video/highlights":
            ctx = _build_ai_ctx()
            fn = (ctx.get("actions") or {}).get("make_highlights")
            return self._json(fn() if fn else {"ok": False, "message": "unavailable"})

        if path == "/api/video/delete":
            name = body.get("name")
            if not name or "/" in name or "\\" in name:
                return self._json({"ok": False, "message": "bad name"}, 400)
            fp = _captures_dir() / "edited" / name
            try:
                if fp.exists():
                    fp.unlink()
                    return self._json({"ok": True})
            except Exception as e:
                return self._json({"ok": False, "message": str(e)}, 500)
            return self._json({"ok": False, "message": "not found"}, 404)

        if path == "/api/video/trim":
            if fragroute_video is None:
                return self._json({"ok": False, "message": "video editor unavailable"}, 400)
            items = (fragroute_capture.list_clips(_captures_dir()) or {}).get("items", []) if fragroute_capture else []
            src = next((c.get("path") for c in items if c.get("name") == body.get("clip")), None)
            if not src:
                return self._json({"ok": False, "message": "clip not found"}, 400)
            return self._json(fragroute_video.trim(src, float(body.get("start", 0)), float(body.get("dur", 10))))

        if path == "/api/train/suggest":
            if fragroute_embed is None or fragroute_dataset is None:
                return self._json({"suggestions": []})
            name, box = body.get("name"), body.get("box")
            if not name or not box or len(box) != 4:
                return self._json({"suggestions": []})
            fp = fragroute_dataset.frame_path(name)
            if not fp:
                return self._json({"suggestions": []})
            return self._json({"suggestions": fragroute_embed.suggest(fp, box, k=5)})

        if path == "/api/log":
            entry = {
                "regionId": body.get("regionId"),
                "duration": int(body.get("duration", 0)),
                "outcome": body.get("outcome", "matched"),
                "ts": body.get("ts") or int(datetime.datetime.now().timestamp() * 1000),
            }
            entries = append_log(entry)
            return self._json({"ok": True, "log": entries})

        if path == "/api/log/clear":
            save_log([])
            return self._json({"ok": True, "log": []})

        if path == "/api/log/prune":
            # remove only the junk entries: matches with an UNKNOWN/empty region
            # (off-VPN before GeoIP resolved them). Keeps all good history.
            try:
                cur = load_log()
            except Exception:
                cur = []
            kept = [e for e in cur if str(e.get("regionId") or "").lower() not in ("", "unknown", "none")]
            removed = len(cur) - len(kept)
            save_log(kept)
            return self._json({"ok": True, "removed": removed, "log": kept})

        if path == "/api/log/import":
            # replace the whole log with a provided array (for restore/import).
            items = body.get("log")
            if not isinstance(items, list):
                return self._json({"ok": False, "message": "expected a 'log' array"}, 400)
            clean = []
            for it in items[:200]:
                if not isinstance(it, dict):
                    continue
                try:
                    clean.append({
                        "regionId": it.get("regionId"),
                        "duration": int(it.get("duration", 0)),
                        "outcome": it.get("outcome", "matched"),
                        "ts": int(it.get("ts") or 0) or int(datetime.datetime.now().timestamp() * 1000),
                        **({"auto": True} if it.get("auto") else {}),
                    })
                except Exception:
                    continue
            with _LOG_LOCK:
                save_log(clean)
            return self._json({"ok": True, "log": clean})

        if path == "/api/clienterror":
            # the UI reports JS errors here so a broken front-end is visible in the
            # diag log instead of just showing an empty shell.
            try:
                b = body or {}
                diag("clienterror", False,
                     msg=("%s :: %s" % (b.get("where", "?"), b.get("error", "")))[:400])
            except Exception:
                pass
            return self._json({"ok": True})

        if path == "/api/settings":
            updated = save_settings(body or {})
            return self._json({"ok": True, "settings": updated})

        if path == "/api/settings/reset":
            updated = save_settings(dict(DEFAULT_SETTINGS))
            return self._json({"ok": True, "settings": updated})

        if path == "/api/ai/chat":
            if fragroute_ai is None:
                return self._json({"ok": False, "reply": "AI module unavailable."}, 503)
            msg = body.get("message", "")
            ctx = _build_ai_ctx()
            # ADAPTIVE PERSONALITY: learn how THIS player likes to be coached from
            # their message, and inject the resulting tone into the coach's prompt.
            _user = "default"
            if fragroute_persona is not None:
                try:
                    _user = ((fragroute_auth.current() if fragroute_auth else {}) or {}).get("username") or "default"
                    fragroute_persona.observe(_user, msg)
                    ctx["persona"] = fragroute_persona.persona_prompt(_user)
                except Exception:
                    pass
            try:
                # UNIFIED CONVERSATION: share history with voice. Use the shared
                # transcript (so a typed question remembers what you SAID), and append
                # both sides so voice sees typed turns too.
                hist = _convo_history() or body.get("history")
                out = fragroute_ai.ai_chat(msg, hist, ctx)
                diag("ai", True, msg="chat:%s" % out.get("tool"))
                _convo_add("user", msg, "text")
                _convo_add("assistant", out.get("reply"), "text")
            except Exception as e:
                diag("ai", False, msg="chat", exc=e)
                raise
            # speak the reply aloud in the coach's neural voice (gated; UI won't double-speak)
            try:
                if get_setting("coachSpeak", True) and body.get("speak") and out.get("reply"):
                    _speak(out["reply"])
            except Exception:
                pass
            return self._json(out)

        if path == "/api/ai/convo/clear":
            with _CONVO_LOCK:
                _CONVO["turns"] = []
            return self._json({"ok": True})

        if path == "/api/regionlock/apply":
            # Force a region WITHOUT a VPN by firewall-blocking the others. Requires an
            # explicit confirm flag from the UI -- this changes the Windows firewall.
            if fragroute_regionlock is None:
                return self._json({"ok": False, "message": "region lock unavailable"}, 503)
            target = ((body or {}).get("region") or "").strip()
            if not target:
                return self._json({"ok": False, "message": "pick a region to lock to."}, 400)
            if not (body or {}).get("confirm"):
                return self._json({"ok": False, "message": "confirmation required."}, 400)
            bmap = region_block_map(target)
            r = fragroute_regionlock.apply(bmap, target_region=target)
            diag("regionlock", bool(r.get("ok")), msg=r.get("message", "apply"))
            return self._json(r)

        if path == "/api/regionlock/clear":
            if fragroute_regionlock is None:
                return self._json({"ok": False, "message": "region lock unavailable"}, 503)
            r = fragroute_regionlock.clear()
            diag("regionlock", True, msg="cleared (%d rules)" % r.get("removed", 0))
            return self._json({"ok": True, "message": "Region lock off (%d rules removed)."
                               % r.get("removed", 0), **r})

        if path == "/api/capture/start":
            if fragroute_capture is None:
                return self._json({"ok": False, "message": "capture module unavailable"}, 503)
            r = fragroute_capture.start(_captures_dir(), body or {})
            diag("capture", bool(r.get("ok")), msg=r.get("message", "start"))
            return self._json(r)

        if path == "/api/capture/stop":
            if fragroute_capture is None:
                return self._json({"ok": False, "message": "capture module unavailable"}, 503)
            return self._json(fragroute_capture.stop())

        if path == "/api/capture/clip":
            if fragroute_capture is None:
                return self._json({"ok": False, "message": "capture module unavailable"}, 503)
            secs = max(60, int(body.get("seconds", 60))) if body else 60
            return self._json(fragroute_capture.save_clip(_captures_dir(), secs, body.get("label") if body else None))

        if path == "/api/docs/open":
            # open a bundled legal/readme doc (EULA/Privacy/Refund/Notices) with the
            # OS default viewer, for the disclaimer gate's "EULA" / "Privacy Policy"
            # links and any future in-app help links. Name is whitelisted -- never
            # opens an arbitrary path.
            _ALLOWED_DOCS = {"EULA.md", "PRIVACY.md", "REFUND.md", "DISCLAIMER.md",
                             "THIRD_PARTY_NOTICES.txt", "README.md"}
            name = ((body or {}).get("name") or "").strip()
            if name not in _ALLOWED_DOCS:
                return self._json({"ok": False, "message": "unknown document"}, 400)
            try:
                base = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).parent
                p = base / name
                if not p.exists():
                    return self._json({"ok": False, "message": "document not found"}, 404)
                if OS == "Windows":
                    os.startfile(str(p))       # noqa: default text/markdown viewer
                elif sys.platform == "darwin":
                    subprocess.Popen(["open", str(p)])
                else:
                    subprocess.Popen(["xdg-open", str(p)])
                return self._json({"ok": True})
            except Exception as e:
                return self._json({"ok": False, "message": str(e)[:120]})

        if path == "/api/capture/openfolder":
            # open the recordings folder in the OS file manager
            try:
                d = _captures_dir() / "clips"
                d.mkdir(parents=True, exist_ok=True)
                if OS == "Windows":
                    os.startfile(str(d))   # noqa: explorer
                elif sys.platform == "darwin":
                    subprocess.Popen(["open", str(d)])
                else:
                    subprocess.Popen(["xdg-open", str(d)])
                return self._json({"ok": True, "dir": str(d)})
            except Exception as e:
                return self._json({"ok": False, "message": str(e)})

        if path == "/api/learning/refresh":
            # pull FragPunk-ONLY facts (official + wiki) into the learning store.
            # User-initiated (force) bypasses the opt-in gate; auto calls respect it.
            if fragroute_knowledge is None:
                return self._json({"ok": False, "message": "knowledge module unavailable"}, 503)
            force = bool(body.get("force")) if body else False
            if not force and not get_setting("onlineLearning", False):
                return self._json({"ok": False, "message": "online learning is off (enable it first)"})
            try:
                r = fragroute_knowledge.refresh(force=True)
                diag("knowledge", True, msg="refresh +%d facts" % r.get("added", 0))
            except Exception as e:
                diag("knowledge", False, msg="refresh", exc=e)
                raise
            return self._json(r)

        if path == "/api/ai/image/generate":
            # the AI CREATES an image from a text prompt (local, on the 4070)
            if fragroute_imagegen is None:
                return self._json({"ok": False, "message": "image generator unavailable"}, 503)
            prompt = (body.get("prompt") or "").strip()
            if not prompt:
                return self._json({"ok": False, "message": "describe the image you want"})
            try:
                r = fragroute_imagegen.generate(
                    prompt, negative=body.get("negative"),
                    steps=int(body.get("steps", 20)),
                    width=int(body.get("width", 768)), height=int(body.get("height", 768)),
                    seed=int(body.get("seed", -1)))
                diag("ai", bool(r.get("ok")), msg="image-gen")
            except Exception as e:
                diag("ai", False, msg="image-gen", exc=e)
                raise
            return self._json(r)

        if path == "/api/ai/recognize":
            # capture the screen and identify weapons/lancers/abilities/map (grounded)
            return self._json(recognize_screen(body.get("imagePath") if body else None))

        if path == "/api/ai/voice/test":
            # speak a sample line so the user can hear the coach voice
            line = (body.get("text") if body else None) or \
                "Hey, welcome back. Nice work last game -- let's tighten up your crosshair and keep it rolling."
            _speak(line)
            return self._json({"ok": True, "engine": ("piper" if (fragroute_tts and fragroute_tts.available()) else "sapi")})

        if path == "/api/ai/voice/converse":
            # hands-free VOICE-TO-VOICE conversation: start/stop the listen<->speak loop
            act = (body.get("action") if body else None) or "toggle"
            if act == "stop":
                return self._json(converse_stop())
            if act == "toggle" and _CONVERSE.get("on"):
                return self._json(converse_stop())
            return self._json(converse_start())

        if path == "/api/ai/persona/tune":
            # shape the coach's personality: preset base, explicit nudge, thumbs, or reset
            if fragroute_persona is None:
                return self._json({"disabled": True})
            user = ((fragroute_auth.current() if fragroute_auth else {}) or {}).get("username") or "default"
            if body.get("preset"):
                fragroute_persona.set_base(user, body["preset"])
            elif body.get("reset"):
                fragroute_persona.set_base(user, "soothing")   # neutral-warm default
            elif body.get("trait"):
                fragroute_persona.nudge(user, body["trait"], float(body.get("delta", 0)))
            elif body.get("reaction"):
                fragroute_persona.observe(user, "", body["reaction"])
            return self._json(fragroute_persona.status(user))

        if path == "/api/ai/voice/preview":
            # render a wav with a SPECIFIC voice/rate (for the picker) and return its URL
            if fragroute_tts is None or not fragroute_tts.available():
                return self._json({"ok": False, "message": "neural voice not installed"})
            import tempfile as _tf
            wav = os.path.join(_tf.gettempdir(), "fragnetic_preview.wav")
            line = (body.get("text") if body else None) or "This is your Fragnetic coach. Let's get to work."
            ok = fragroute_tts.synth(line, wav, body.get("voice") if body else None,
                                     float(body.get("rate", 1.0)) if body else 1.0)
            if not ok:
                return self._json({"ok": False, "message": "synthesis failed"})
            try:
                import winsound
                threading.Thread(target=lambda: winsound.PlaySound(wav, winsound.SND_FILENAME), daemon=True).start()
            except Exception:
                pass
            return self._json({"ok": True})

        if path == "/api/ai/map/capture":
            # snap + describe the current map area into the Maps gallery
            return self._json(capture_map())

        if path == "/api/ai/vision":
            # the AI looks at an image file (screenshot/clip frame) and answers
            if fragroute_llm is None:
                return self._json({"ok": False, "message": "AI unavailable"}, 503)
            img = body.get("imagePath")
            prompt = body.get("prompt") or "Describe what you see in this FragPunk screenshot."
            if not img or not Path(img).exists():
                return self._json({"ok": False, "message": "image not found"})
            try:
                r = fragroute_llm.chat_vision(prompt, img)
                diag("ai", bool(r), msg="vision")
            except Exception as e:
                diag("ai", False, msg="vision", exc=e)
                raise
            return self._json({"ok": bool(r), "reply": r or "Vision model couldn't read that image."})

        if path == "/api/ai/vision/clip":
            # extract a frame from a saved clip and have the vision model analyze it
            if fragroute_llm is None or fragroute_capture is None:
                return self._json({"ok": False, "message": "AI/recorder unavailable"}, 503)
            clips = (fragroute_capture.list_clips(_captures_dir()) or {}).get("items", [])
            if not clips:
                return self._json({"ok": False, "message": "no clips recorded yet"})
            name = body.get("clip")
            clip = next((c for c in clips if c.get("name") == name), clips[0])
            tmp = tempfile.gettempdir()
            frames = fragroute_capture.extract_frames(clip.get("path"), tmp)
            if not frames:
                return self._json({"ok": False, "message": "couldn't read frames from the clip"})
            prompt = body.get("prompt") or ("These are frames sampled across a FragPunk "
                "match clip, in order. Briefly review what's happening and the player's "
                "crosshair placement / positioning across the sequence, with one tip to improve.")
            try:
                if len(frames) > 1:
                    r = fragroute_llm.chat_vision_multi(prompt, frames)
                else:
                    r = fragroute_llm.chat_vision(prompt, frames[0])
                diag("ai", bool(r), msg="vision:clip(%d frames)" % len(frames))
            except Exception as e:
                diag("ai", False, msg="vision:clip", exc=e)
                raise
            return self._json({"ok": bool(r), "clip": clip.get("name"), "frames": len(frames),
                               "reply": r or "Vision model couldn't read that clip."})

        if path == "/api/autocapture/reset":
            return self._json({"ok": True, "autodetect": reset_autocapture_session()})

        if path == "/api/queue/mark":
            # user tapped "Mark Queue" (button or hotkey): precise queue start
            return self._json(mark_queue_start())

        if path == "/api/gameinfo/seen":
            # acknowledge the current version (clears the "updated" flag)
            return self._json({"ok": True, "gameinfo": note_game_version_seen()})

        if path == "/api/locker/label":
            # save a user-typed name / category / lancer / portrait pin
            return self._json(locker_set_label(
                body.get("id", ""), body.get("label"), body.get("category"),
                body.get("lancer"), body.get("portrait")))

        if path == "/api/replays/note":
            return self._json(replay_set_note(
                body.get("id", ""), body.get("note"),
                body.get("review"), body.get("reviewed")))

        if path == "/api/replays/open":
            return self._json(replay_open_folder(body.get("id", "")))

        if path == "/api/diag":
            # client-side report (UI JS error, inline-browser failure, etc.) so
            # front-end problems land in the same Health view + log file.
            comp = str(body.get("component") or "ui")
            diag(comp, bool(body.get("ok", False)), msg=str(body.get("message") or ""))
            return self._json({"ok": True})

        if path == "/api/weaponskins/add":
            return self._json(weapon_skin_add(
                body.get("weapon", ""), body.get("name", ""),
                body.get("label", ""), body.get("image", "")))

        if path == "/api/weaponskins/delete":
            return self._json(weapon_skin_delete(body.get("id", "")))

        if path == "/api/icon/set":
            return self._json(icon_set(body.get("slot", ""), body.get("image", "")))

        if path == "/api/icon/del":
            return self._json(icon_del(body.get("slot", "")))

        if path == "/api/browser/open":
            return self._json(browser_open(body.get("url", "")))

        if path == "/api/browser/wipe":
            return self._json(browser_wipe())

        if path == "/api/route/profile":
            return self._json(start_route_profile(body.get("region", "")))

        if path == "/api/route/profile/cancel":
            return self._json(cancel_route_profile())

        if path == "/api/scout/refresh":
            # force an immediate real-server ping sweep + re-rank
            phase = AUTODETECT.get("phase")
            if phase != "match":
                try:
                    scout_ping_servers()
                    with _SCOUT_LOCK:
                        SCOUT["pingedAt"] = int(time.time() * 1000)
                except Exception:
                    pass
            scout_recompute()
            return self._json({"ok": True, "scout": scout_status()})

        if path == "/api/open":
            url = (body.get("url") or "").strip()
            if url.startswith("http://") or url.startswith("https://"):
                try:
                    webbrowser.open(url)
                    return self._json({"ok": True})
                except Exception as e:
                    return self._json({"ok": False, "message": str(e)}, 500)
            return self._json({"ok": False, "message": "bad url"}, 400)

        return self._json({"error": "not found"}, 404)

    def _serve_ui(self):
        html_path = SCRIPT_DIR / "fragroute_ui.html"
        if html_path.exists():
            body = html_path.read_bytes()
        else:
            body = (b"<h1>fragroute_ui.html not found</h1>"
                    b"<p>Keep fragroute_ui.html in the same folder as fragroute.py.</p>")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


# ===========================================================================
# MAIN
# ===========================================================================
def main():
    global LOG_PATH, SETTINGS_PATH, SERVERS_PATH, PLAYERS_PATH, RANK_PATH, REPLAYS_PATH, SERVERPINGS_PATH, WEAPONSKINS_PATH, ICONS_PATH
    ap = argparse.ArgumentParser(description="Fragpunk VPN route optimizer")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--configs", default=str(SCRIPT_DIR / "configs"))
    ap.add_argument("--dry-run", action="store_true",
                    help="never execute tunnel commands, just log them")
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--no-elevate", action="store_true",
                    help="don't auto-request admin/root; run exactly as launched")
    ap.add_argument("--elevated", action="store_true", help=argparse.SUPPRESS)
    args = ap.parse_args()

    # Auto-elevation: request admin/root up front so the user doesn't have to open
    # an elevated terminal. Skipped for --dry-run (never touches the network),
    # when the user opts out with --no-elevate, or if we're already elevated.
    if not args.dry_run and not args.no_elevate and not is_admin():
        if relaunch_elevated(already_relaunched=args.elevated):
            return  # an elevated instance is taking over (or the attempt failed)

    STATE["configs_dir"] = Path(args.configs)
    STATE["dry_run"] = args.dry_run
    LOG_PATH = STATE["configs_dir"].parent / "fragroute_queue_log.json"
    SETTINGS_PATH = STATE["configs_dir"].parent / "fragroute_settings.json"
    SERVERS_PATH = STATE["configs_dir"].parent / "fragroute_servers.json"
    PLAYERS_PATH = STATE["configs_dir"].parent / "fragroute_players.json"
    RANK_PATH = STATE["configs_dir"].parent / "fragroute_rank.json"
    REPLAYS_PATH = STATE["configs_dir"].parent / "fragroute_replays.json"
    SERVERPINGS_PATH = STATE["configs_dir"].parent / "fragroute_serverpings.json"
    WEAPONSKINS_PATH = STATE["configs_dir"].parent / "fragroute_weapon_skins.json"
    ICONS_PATH = STATE["configs_dir"].parent / "fragroute_icons.json"
    if fragroute_learning is not None:
        fragroute_learning.LEARNING_PATH = STATE["configs_dir"].parent / "fragroute_mode_learning.json"
    if fragroute_llm is not None:
        # the local model + llama.cpp live in an 'llm' folder next to app data
        # (dist/llm for the exe, files/llm from source)
        fragroute_llm.LLM_DIR = STATE["configs_dir"].parent / "llm"
    if fragroute_imagegen is not None:
        fragroute_imagegen.IMG_DIR = STATE["configs_dir"].parent / "sd"
        fragroute_imagegen.OUT_DIR = STATE["configs_dir"].parent / "fragroute_generated"
    if fragroute_voice is not None:
        fragroute_voice.STT_DIR = STATE["configs_dir"].parent / "stt"
        try:
            fragroute_voice.FFMPEG = fragroute_capture.find_ffmpeg() if fragroute_capture else None
        except Exception:
            fragroute_voice.FFMPEG = None
        try:
            fragroute_voice.PREFERRED_MIC = get_setting("voiceMic", None) or None
        except Exception:
            pass
        try:
            # warm the whisper server at startup so the FIRST voice turn is already
            # fast (decode-only), and stop it cleanly on exit.
            fragroute_voice.prewarm_whisper()
            import atexit
            atexit.register(lambda: fragroute_voice.stop_whisper())
        except Exception:
            pass
    if fragroute_yolo is not None:
        # offline YOLOX model + onnxruntime live in a 'yolo' folder next to app data
        fragroute_yolo.YOLO_DIR = STATE["configs_dir"].parent / "yolo"
    if fragroute_dataset is not None:
        fragroute_dataset.DATASET_DIR = STATE["configs_dir"].parent / "dataset"
        try:
            fragroute_dataset.FFMPEG = fragroute_capture.find_ffmpeg() if fragroute_capture else None
        except Exception:
            fragroute_dataset.FFMPEG = None
    if fragroute_embed is not None:
        fragroute_embed.CLIP_DIR = STATE["configs_dir"].parent / "clip"
    if fragroute_video is not None:
        try:
            fragroute_video.FFMPEG = fragroute_capture.find_ffmpeg() if fragroute_capture else None
        except Exception:
            fragroute_video.FFMPEG = None
        fragroute_video.CLIPS_DIR = str(_captures_dir() / "clips")
        fragroute_video.OUT_DIR = str(_captures_dir() / "edited")
    if fragroute_setup is not None:
        fragroute_setup.BASE_DIR = str(STATE["configs_dir"].parent)   # holds llm/ sd/ yolo/ stt/ + exe
    _base_dir = str(STATE["configs_dir"].parent)
    if fragroute_license is not None:
        fragroute_license.BASE_DIR = _base_dir            # license.json + trial marker live here
        # optional online revocation/seat endpoint (off unless the owner sets it)
        fragroute_license.ONLINE_ENDPOINT = os.environ.get("FRAGROUTE_ACTIVATE_URL") or None
    if fragroute_auth is not None:
        fragroute_auth.BASE_DIR = _base_dir               # fragroute_accounts.json lives here
        fragroute_auth.CLOUD_ENDPOINT = os.environ.get("FRAGROUTE_CLOUD_URL") or None
    if fragroute_hardware is not None:
        try:
            fragroute_hardware.FFMPEG = fragroute_capture.find_ffmpeg() if fragroute_capture else None
        except Exception:
            fragroute_hardware.FFMPEG = None
    if fragroute_tts is not None:
        fragroute_tts.TTS_DIR = str(STATE["configs_dir"].parent / "tts")   # piper + voice models
    if fragroute_persona is not None:
        fragroute_persona.BASE_DIR = str(STATE["configs_dir"].parent)      # per-user personality store
    if fragroute_regionlock is not None:
        fragroute_regionlock.STATE_DIR = str(STATE["configs_dir"].parent)  # region-lock rule state
        try:
            # never boot into a silent lock: sweep any rules a prior run/crash left
            fragroute_regionlock.cleanup_on_start()
            # and ALWAYS drop the firewall block when the app exits, so a closed
            # Fragnetic never leaves you region-locked without the app running.
            import atexit
            atexit.register(lambda: fragroute_regionlock.clear())
        except Exception:
            pass
    load_settings()

    # diagnostics on as early as possible so startup failures are captured too
    install_excepthooks()
    diag("web", True, msg=f"FRAGROUTE {APP_BUILD} starting (pid {os.getpid()})")

    mapped, unmapped = discover_configs(STATE["configs_dir"])
    kind, wgpath = find_wireguard()

    print("=" * 64)
    print(f" {APP_NAME}  -- FragPunk companion (routing - coach - capture)")
    print("=" * 64)
    print(f" OS            : {OS}")
    print(f" Admin/root    : {'YES' if is_admin() else 'NO  (tunnel switching disabled)'}")
    print(f" WireGuard     : {kind or 'NOT FOUND -- install it'} {wgpath or ''}")
    print(f" Configs dir   : {STATE['configs_dir']}")
    print(f" Mapped configs: {', '.join(mapped.keys()) if mapped else '(none -- drop .conf files in configs/)'}")
    if unmapped:
        print(f" Unmapped      : {', '.join(u['name'] for u in unmapped)}")
    print(f" Dry-run       : {'ON (safe, nothing executes)' if STATE['dry_run'] else 'OFF'}")
    print("-" * 64)

    # run below normal priority so our background work never preempts the game
    lower_process_priority()

    # remove any leftover auto-start tunnel services so we never silently route
    # you through an old exit on launch (the "auto-connect on launch" surprise)
    try:
        stray = cleanup_stray_tunnels()
        if stray:
            print(f" Cleaned stray tunnels: {', '.join(stray)}")
    except Exception:
        pass

    # warm the latency cache in the background so first paint has data
    threading.Thread(target=refresh_latency, daemon=True).start()

    # start the auto-capture monitor: watches Fragpunk's state transitions on
    # its own thread (keeps working even while the UI window is hidden in-game)
    # so queue/match times get logged automatically.
    threading.Thread(target=_autodetect_loop, daemon=True).start()

    # start the connection-health monitor: pings the active tunnel for live
    # latency/jitter/loss and can auto-reconnect if it drops.
    threading.Thread(target=_health_loop, daemon=True).start()

    # start the Scout: pre-queue recon (pings real game servers, keeps a warm
    # fastest-fill ranking) + slow-queue bailout nudges. Runs on its own thread.
    threading.Thread(target=_scout_loop, daemon=True).start()

    # global "Mark Queue" hotkey listener (off unless enabled in settings)
    threading.Thread(target=_hotkey_loop, daemon=True).start()

    # auto-harvest watcher: imports new recordings (OBS/clips/etc.) into the YOLO
    # dataset when idle. Off unless 'autoHarvest' setting is on.
    if fragroute_dataset is not None:
        threading.Thread(target=_auto_harvest_loop, daemon=True).start()

    # opt-in: refresh FragPunk-only online knowledge shortly after launch
    def _knowledge_warm():
        try:
            time.sleep(20)
            if fragroute_knowledge is not None and get_setting("onlineLearning", False):
                r = fragroute_knowledge.refresh()
                diag("knowledge", True, msg="startup refresh +%d" % r.get("added", 0))
        except Exception as e:
            diag("knowledge", False, msg="startup refresh", exc=e)
    threading.Thread(target=_knowledge_warm, daemon=True).start()

    # auto-revert watcher: undoes a mid-match VPN switch that freezes/worsens ping
    threading.Thread(target=_auto_revert_loop, daemon=True).start()
    try:
        refresh_population()  # real player count up front
        scout_recompute()   # warm the ranking now so the first paint isn't empty
    except Exception:
        pass

    url = f"http://127.0.0.1:{args.port}/"
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f" UI            : {url}")
    print(" Ctrl+C to stop.")
    print("=" * 64)

    if not args.no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping.")
        server.shutdown()


if __name__ == "__main__":
    main()
