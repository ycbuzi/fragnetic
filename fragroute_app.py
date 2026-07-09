#!/usr/bin/env python3
"""
FRAGROUTE -- desktop application shell.

Wraps the FRAGROUTE engine (fragroute.py) in a real Windows app:
  * a native window (pywebview / WebView2), not a browser tab, with the app icon
  * a system-tray icon so it lives next to Fragpunk: show/hide, always-on-top,
    quick connect / disconnect, quit
  * auto-elevation (UAC) so it can actually switch VPN tunnels

Graceful fallbacks, so it runs even with nothing extra installed:
    pywebview present  -> native window + full tray controls (best)
    else Edge/Chrome   -> chromeless app-mode window + tray quit
    else               -> opens your default browser

SAFETY: this is a SEPARATE window. It does NOT hook, inject into, or overlay
Fragpunk, so it's fine with ACE anti-cheat. Drop it on your second monitor or
alt-tab to it.

Run:
    python fragroute_app.py            # native app
    python fragroute_app.py --dry-run  # safe preview, no admin, nothing executes
"""
import argparse
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

# --- resolve where we actually live (next to the .exe when frozen) ----------
def app_dir():
    if getattr(sys, "frozen", False):          # PyInstaller .exe
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent

APP_DIR = app_dir()
# make sure we can import the engine sitting beside us
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

import fragroute as fr  # the engine (server + VPN logic)

APP_NAME = "Fragnetic"
WIN_TITLE = "Fragnetic \u2014 FragPunk Companion"
DEFAULT_PORT = 8765

# shared window state controlled from the tray
WIN = {"window": None, "on_top": False}


# ===========================================================================
# ICON
# ===========================================================================
def icon_paths():
    a = APP_DIR / "assets"
    # when frozen, data files extract to fr.SCRIPT_DIR (_MEIPASS)
    frozen_assets = Path(getattr(fr, "SCRIPT_DIR", APP_DIR)) / "assets"
    return {
        "png": [a / "fragroute.png", frozen_assets / "fragroute.png"],
        "ico": [a / "fragroute.ico", frozen_assets / "fragroute.ico"],
    }


def first_existing(paths):
    for p in paths:
        if p and Path(p).exists():
            return Path(p)
    return None


def stable_icon_path():
    """Copy the bundled .ico to a STABLE per-user path and return it.

    The Start Menu shortcut must NOT point at the icon inside the frozen app's
    PyInstaller _MEIPASS temp dir -- that folder is deleted when the app exits,
    leaving the shortcut with a missing (blank) icon. We copy it once to
    %LOCALAPPDATA%\\FRAGROUTE\\fragroute.ico so it persists. Returns None on
    failure (caller falls back to the exe's own embedded icon)."""
    try:
        src = first_existing(icon_paths()["ico"])
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        dst = Path(base) / "FRAGROUTE" / "fragroute.ico"
        if src:
            need = (not dst.exists()
                    or dst.stat().st_size != Path(src).stat().st_size)
            if need:
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
        return str(dst) if dst.exists() else None
    except Exception:
        return None


def ensure_icon():
    """Generate the icon if it's missing and Pillow is available."""
    paths = icon_paths()
    if first_existing(paths["png"]) and first_existing(paths["ico"]):
        return
    try:
        sys.path.insert(0, str(APP_DIR))
        import make_icon
        make_icon.build()
    except Exception as e:
        print("(icon generation skipped:", e, ")")


# ===========================================================================
# ENGINE / SERVER
# ===========================================================================
def free_port(preferred):
    """Return the preferred port if open, else the next free one."""
    for port in [preferred] + list(range(preferred + 1, preferred + 20)):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(("127.0.0.1", port)) != 0:
                return port
    return preferred


def start_engine(port, configs_dir, dry_run):
    """Boot the FRAGROUTE engine on a background thread; return the server.

    Mirrors fragroute.main()'s init so the packaged app runs the FULL engine:
    persisted settings + the auto-capture, connection-health, and Scout
    monitors. (Previously only pings + the HTTP server started here, so
    auto-capture/scout never ran in .exe mode.)"""
    # run below normal priority so our background work never preempts the game
    if hasattr(fr, "lower_process_priority"):
        try:
            fr.lower_process_priority()
        except Exception:
            pass
    fr.STATE["configs_dir"] = Path(configs_dir)
    fr.STATE["dry_run"] = dry_run
    fr.LOG_PATH = fr.STATE["configs_dir"].parent / "fragroute_queue_log.json"
    fr.SETTINGS_PATH = fr.STATE["configs_dir"].parent / "fragroute_settings.json"
    fr.SERVERS_PATH = fr.STATE["configs_dir"].parent / "fragroute_servers.json"
    fr.PLAYERS_PATH = fr.STATE["configs_dir"].parent / "fragroute_players.json"
    fr.RANK_PATH = fr.STATE["configs_dir"].parent / "fragroute_rank.json"
    fr.REPLAYS_PATH = fr.STATE["configs_dir"].parent / "fragroute_replays.json"
    fr.SERVERPINGS_PATH = fr.STATE["configs_dir"].parent / "fragroute_serverpings.json"
    if hasattr(fr, "WEAPONSKINS_PATH"):
        fr.WEAPONSKINS_PATH = fr.STATE["configs_dir"].parent / "fragroute_weapon_skins.json"
        # SEED on first run: if there's no live skins file yet (e.g. a fresh copy
        # on another computer), copy the snapshot bundled inside the exe so the
        # weapon skins are there with zero dependency on the original screenshots.
        try:
            if not Path(fr.WEAPONSKINS_PATH).exists():
                seed = fr.SCRIPT_DIR / "fragroute_weapon_skins.json"
                if Path(seed).exists():
                    shutil.copy2(str(seed), str(fr.WEAPONSKINS_PATH))
        except Exception:
            pass
    if hasattr(fr, "ICONS_PATH"):
        fr.ICONS_PATH = fr.STATE["configs_dir"].parent / "fragroute_icons.json"
        try:
            if not Path(fr.ICONS_PATH).exists():
                seed = fr.SCRIPT_DIR / "fragroute_icons.json"
                if Path(seed).exists():
                    shutil.copy2(str(seed), str(fr.ICONS_PATH))
        except Exception:
            pass
    if hasattr(fr, "load_settings"):
        try:
            fr.load_settings()
        except Exception:
            pass
    fr.discover_configs(fr.STATE["configs_dir"])
    # remove leftover auto-start tunnel services (the "auto-connect on launch"
    # surprise: a tunnel left installed routes you through an old exit on boot)
    if hasattr(fr, "cleanup_stray_tunnels"):
        try:
            fr.cleanup_stray_tunnels()
        except Exception:
            pass
    fr.auto_install_wireguard_async()  # built-in WireGuard: install if missing
    # warm pings so the first paint has data
    threading.Thread(target=fr.refresh_latency, daemon=True).start()
    # background monitors (each guarded so an older engine without one still boots)
    for loop_name in ("_autodetect_loop", "_health_loop", "_scout_loop",
                      "_hotkey_loop", "_auto_revert_loop"):
        loop = getattr(fr, loop_name, None)
        if loop:
            threading.Thread(target=loop, daemon=True).start()
    if hasattr(fr, "refresh_population"):
        try:
            fr.refresh_population()   # real Steam player count up front
        except Exception:
            pass
    if hasattr(fr, "scout_recompute"):
        try:
            fr.scout_recompute()   # warm the ranking now so first paint isn't empty
        except Exception:
            pass
    httpd = fr.ThreadingHTTPServer(("127.0.0.1", port), fr.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


# ===========================================================================
# ELEVATION (exe-safe: relaunches the app itself, not the engine script)
# ===========================================================================
def is_admin():
    return fr.is_admin()


def relaunch_elevated_app():
    """Re-launch THIS app with admin. Returns True if a hand-off happened and
    the current (non-elevated) process should exit."""
    if fr.OS != "Windows" or is_admin() or "--elevated" in sys.argv:
        return False
    try:
        import ctypes
        if getattr(sys, "frozen", False):
            exe = sys.executable                       # FRAGROUTE.exe
            args = sys.argv[1:] + ["--elevated"]
        else:
            exe = sys.executable                       # python(w).exe
            args = [os.path.abspath(__file__)] + sys.argv[1:] + ["--elevated"]
        params = subprocess.list2cmdline(args)
        ret = ctypes.windll.shell32.ShellExecuteW(
            None, "runas", exe, params, str(APP_DIR), 1)
        # >32 = elevated instance launched -> exit this one.
        # <=32 = user declined -> keep running (UI works, switching disabled).
        return ret > 32
    except Exception as e:
        print("Elevation request failed:", e)
        return False


# ===========================================================================
# TRAY (pystray) -- optional
# ===========================================================================
def _api_get(url, path):
    import json
    import urllib.request
    with urllib.request.urlopen(url + path, timeout=6) as r:
        return json.loads(r.read())


def _api_post(url, path, payload=None):
    import json
    import urllib.request
    data = json.dumps(payload or {}).encode()
    req = urllib.request.Request(url + path, data=data,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


def _best_ping_region(url):
    """Lowest-ping mapped region (a quick tray action; the window UI has the
    smarter heat+history recommendation)."""
    try:
        _api_get(url, "/api/latency")  # refresh
        regs = _api_get(url, "/api/regions").get("regions", [])
        mapped = [r for r in regs if r.get("configMapped")]
        with_ping = [r for r in mapped if r.get("latencyMs") is not None]
        pool = with_ping or mapped
        if not pool:
            return None
        pool.sort(key=lambda r: r.get("latencyMs") if r.get("latencyMs")
                  is not None else 9e9)
        return pool[0]["id"]
    except Exception:
        return None


def build_tray(url, on_quit):
    try:
        import pystray
        from PIL import Image
    except Exception:
        return None

    png = first_existing(icon_paths()["png"])
    # A corrupt/locked icon file must not crash app startup -- fall back to a solid image.
    try:
        image = Image.open(png) if png else Image.new("RGBA", (64, 64), (255, 47, 146, 255))
    except Exception:
        image = Image.new("RGBA", (64, 64), (255, 47, 146, 255))

    def toggle_window(icon, item):
        w = WIN.get("window")
        if not w:
            return
        try:
            if WIN.get("hidden"):
                w.show(); WIN["hidden"] = False
            else:
                w.hide(); WIN["hidden"] = True
        except Exception:
            pass

    def toggle_top(icon, item):
        w = WIN.get("window")
        WIN["on_top"] = not WIN.get("on_top")
        try:
            if w:
                w.on_top = WIN["on_top"]
        except Exception:
            pass

    def connect_best(icon, item):
        rid = _best_ping_region(url)
        if rid:
            try:
                _api_post(url, "/api/connect", {"region": rid})
            except Exception:
                pass

    def disconnect(icon, item):
        try:
            _api_post(url, "/api/disconnect")
        except Exception:
            pass

    def open_browser(icon, item):
        webbrowser.open(url)

    def quit_app(icon, item):
        try:
            icon.stop()
        except Exception:
            pass
        on_quit()

    has_window = WIN.get("window") is not None
    items = []
    if has_window:
        items.append(pystray.MenuItem("Show / Hide", toggle_window, default=True))
        items.append(pystray.MenuItem(
            "Always on top", toggle_top,
            checked=lambda i: bool(WIN.get("on_top"))))
        items.append(pystray.Menu.SEPARATOR)
    items.append(pystray.MenuItem("Connect lowest-ping region", connect_best))
    items.append(pystray.MenuItem("Disconnect", disconnect))
    items.append(pystray.Menu.SEPARATOR)
    items.append(pystray.MenuItem("Open in browser", open_browser))
    items.append(pystray.MenuItem("Quit Fragnetic", quit_app))

    return pystray.Icon(APP_NAME, image, "Fragnetic", pystray.Menu(*items))


# ===========================================================================
# WINDOW
# ===========================================================================
# PERF NOTE -- the right way to keep this app from costing FPS:
#   * KEEP normal GPU compositing on. A static 2D page is nearly free for the
#     GPU to composite. FORCING software rendering (--disable-gpu) was a mistake:
#     it pushes a blur/glow-heavy page onto the CPU, pins a core, and steals the
#     cycles the game needs for frame prep -- which tanks FPS harder than GPU
#     compositing ever did.
#   * Instead, CAP the frame rate so the WebView can't render more than a few
#     frames/sec, and let the UI's own flash-fix + Lite Mode stop repaints.
# These args are read by the WebView2 / Edge backend at startup.
_PERF_FLAGS = (
    # cap the compositor to ~10 fps (plenty for a clock + pings) so it can never
    # spike; this is the single biggest win and keeps the GPU cost trivial
    "--max-gum-fps=10 "
    # low-end-device mode trims background work; keep GPU compositing ON
    # (do NOT --disable-gpu -- that forces CPU rendering and tanks game FPS)
    "--enable-low-end-device-mode"
)


def tune_webview_perf():
    """Ask the WebView2/Edge backend to render cheaply: low frame-rate cap and
    low-end-device mode, while KEEPING GPU compositing on. Set before start."""
    existing = os.environ.get("WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS", "")
    # strip any earlier (harmful) --disable-gpu* flags we may have set before
    cleaned = " ".join(tok for tok in existing.split()
                        if not tok.startswith("--disable-gpu")
                        and not tok.startswith("--disable-software-rasterizer")
                        and not tok.startswith("--disable-gpu-compositing"))
    if "--max-gum-fps" not in cleaned:
        os.environ["WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"] = (
            (cleaned + " " + _PERF_FLAGS).strip())


def find_browser_appmode():
    """Find a Chromium-family browser that supports --app=<url> (a chromeless
    window). Checks Edge + Chrome + other Chromium browsers in BOTH machine-wide
    and per-user install locations, then PATH, then the registry App Paths (catches
    installs in non-standard folders). Edge ships on every Win10/11, so this almost
    always succeeds; returns None only on a genuinely stripped box -- and the caller
    then surfaces the URL in a message box so the app is still reachable."""
    import shutil
    local = os.environ.get("LOCALAPPDATA", "")
    cands = [
        os.path.expandvars(r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"),
        os.path.expandvars(r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"),
        os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"),
        # per-user installs (Chrome/Edge can install under LOCALAPPDATA, no admin)
        os.path.join(local, r"Google\Chrome\Application\chrome.exe") if local else None,
        os.path.join(local, r"Microsoft\Edge\Application\msedge.exe") if local else None,
        # other Chromium browsers that also honor --app=
        os.path.expandvars(r"%ProgramFiles%\BraveSoftware\Brave-Browser\Application\brave.exe"),
        os.path.expandvars(r"%ProgramFiles(x86)%\BraveSoftware\Brave-Browser\Application\brave.exe"),
        os.path.join(local, r"BraveSoftware\Brave-Browser\Application\brave.exe") if local else None,
        os.path.expandvars(r"%ProgramFiles%\Vivaldi\Application\vivaldi.exe"),
        os.path.join(local, r"Vivaldi\Application\vivaldi.exe") if local else None,
        shutil.which("msedge"), shutil.which("chrome"),
        shutil.which("brave"), shutil.which("vivaldi"),
    ]
    for c in cands:
        if c and os.path.exists(c):
            return c
    # registry App Paths -- finds a Chromium browser installed in a non-standard dir
    if os.name == "nt":
        try:
            import winreg
            for exe in ("msedge.exe", "chrome.exe", "brave.exe", "vivaldi.exe"):
                for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
                    try:
                        with winreg.OpenKey(
                            hive,
                            r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\%s" % exe
                        ) as k:
                            p = winreg.QueryValue(k, None)
                            if p and os.path.exists(p):
                                return p
                    except Exception:
                        pass
        except Exception:
            pass
    return None


# ---------------------------------------------------------------------------
# FRAMELESS WINDOW CHROME
# ---------------------------------------------------------------------------
# The window is created with frameless=True -- no native Windows title bar.
# Instead the UI (fragroute_ui.html) draws its OWN title bar: a thin draggable
# strip with minimize / maximize / close buttons. Those buttons call back into
# Python through this js_api object (exposed as `pywebview.api.*` in the page).
#
# Dragging is handled by pywebview itself: any element tagged with the CSS class
# `pywebview-drag-region` moves the OS window, so we don't need a Python hook for
# it. easy_drag is turned OFF in create_window() so ONLY that strip drags (not
# the whole page), letting the rest of the UI stay clickable.
class _PreviewBridge:
    """js_api exposed INSIDE each browser-tab WebView2 so the page's injected JS
    can ask the MAIN app to open a link in a brand-new tab (instead of popping a
    separate window). The main UI provides _brOpenNewTabFromLink(url)."""
    def new_tab(self, url):
        try:
            import json as _j
            main = WIN.get("window")
            if main is not None and url:
                main.evaluate_js("window._brOpenNewTabFromLink && _brOpenNewTabFromLink(%s)"
                                 % _j.dumps(str(url)))
        except Exception:
            pass
        return {"ok": True}


_PREVIEW_BRIDGE = _PreviewBridge()


class WindowControls:
    """Window-control callbacks the frameless titlebar invokes from JS.

    Every method is wrapped so a missing pywebview feature (older version) or a
    closed window can never throw across the JS bridge."""

    def chrome_mode(self):
        """'native' -> the window has a real Windows frame, so the page must NOT
        draw its own titlebar/resize grip. Lets the HTML stay in sync with the
        Python-side frame choice without hard-coding it."""
        return "native"

    def minimize(self):
        w = WIN.get("window")
        try:
            w.minimize()
        except Exception:
            pass

    def toggle_maximize(self):
        """Maximize <-> restore. Tracks state ourselves so the maximize glyph in
        the titlebar can flip, and so we still work on pywebview builds that lack
        a queryable maximized property."""
        w = WIN.get("window")
        try:
            if WIN.get("maximized"):
                w.restore()
                WIN["maximized"] = False
            else:
                w.maximize()
                WIN["maximized"] = True
        except Exception:
            pass
        return WIN.get("maximized", False)

    def close(self):
        _destroy_aux_windows()           # take the browser/preview down with us
        _safe_destroy(WIN.get("window"))

    # ---- USER-DRIVEN RESIZE (frameless windows have no native resize border) ---
    # pywebview 6.x makes a frameless window FormBorderStyle.None, so Windows
    # gives it no grab-able edge. We add our own corner grip in the HTML that
    # streams the desired size here as it's dragged. Sizes are in the same units
    # create_window() was given (which the inline preview also matches), so the
    # docked browser keeps tracking the tab area as the window grows/shrinks.
    def main_size(self):
        win = WIN.get("window")
        return {"w": int(WIN.get("main_w", 1180)),
                "h": int(WIN.get("main_h", 824)),
                "x": int(getattr(win, "x", 0)),
                "y": int(getattr(win, "y", 0))}

    def resize_main(self, w, h):
        win = WIN.get("window")
        try:
            ww = max(900, int(float(w)))
            hh = max(640, int(float(h)))
            win.resize(ww, hh)
            WIN["main_w"], WIN["main_h"] = ww, hh
        except Exception:
            pass
        return {"w": int(WIN.get("main_w", 1180)), "h": int(WIN.get("main_h", 824))}

    def open_browser(self, url):
        """Open a URL in an app-OWNED WebView2 window (the same engine drawing
        this UI). Renders any site, connects fine even though we're elevated
        (no external process to launch), and is wiped on close because pywebview
        runs an ephemeral profile. Reuses one browser window across navigations."""
        try:
            import webview
            try:
                target = fr._browser_normalize(url)
            except Exception:
                target = url or "about:blank"
            bw = WIN.get("browser_window")
            if bw is not None:
                try:
                    bw.load_url(target)
                    try:
                        bw.show()
                    except Exception:
                        pass
                    return {"ok": True, "engine": "webview"}
                except Exception:
                    WIN["browser_window"] = None   # it was closed; make a new one
            bw = webview.create_window(
                "FRAGROUTE — Private Browser", target,
                width=1200, height=820, min_size=(820, 560),
                background_color="#0a0a0f")
            WIN["browser_window"] = bw
            try:
                bw.events.closed += lambda: WIN.update({"browser_window": None})
            except Exception:
                pass
            return {"ok": True, "engine": "webview"}
        except Exception as e:
            return {"ok": False, "message": str(e)}

    # ---- INLINE PREVIEW: a real WebView2 window positioned over the app --------
    # An <iframe> can't show most sites (X-Frame-Options). Instead we open a real
    # top-level WebView2 window (renders anything) -- the SAME create_window path
    # that the Open-window button uses and which we've confirmed works. We give
    # it a PREDEFINED size and pin it on top, positioned over the main window's
    # content area via pywebview's own move/resize (no fragile SetParent). It
    # hides when you leave the Browser tab and is wiped on close.
    PREVIEW_TITLE = "FRAGROUTE Preview"

    def _preview_geom(self, left, top, w, h, dpr):
        """Screen rect for the preview = app CLIENT-area origin + the tab area's
        viewport rect (CSS px * devicePixelRatio), CLAMPED to the client area so
        it can never spill past the app window. Recomputed every tick from the
        live HWND, so it follows scrolling, window moves, AND monitor changes."""
        d = float(dpr) or 1.0
        L = int(float(left) * d)
        T = int(float(top) * d)
        W = max(80, int(float(w) * d))
        H = max(80, int(float(h) * d))
        hwnd = _main_hwnd()
        if hwnd:
            try:
                cw, ch = _client_size(hwnd)
                if L < 0:
                    L = 0
                if T < 0:
                    T = 0
                if L + W > cw:
                    W = max(80, cw - L)
                if T + H > ch:
                    H = max(80, ch - T)
                ox, oy = _client_origin(hwnd)
                return (ox + L, oy + T, W, H)
            except Exception:
                pass
        main = WIN.get("window")
        mx, my = int(getattr(main, "x", 130)), int(getattr(main, "y", 80))
        return (mx + L, my + T, W, H)

    _preview_log_ts = [0.0]

    def _preview_apply(self, bw, left, top, w, h, dpr, show=True):
        try:
            hwnd = _hwnd_of(bw)
        except Exception:
            hwnd = None
        # PREFERRED: docked as a child -> client-relative coords, auto-clipped,
        # auto-follows the app across monitors. No screen/DPI/origin math.
        if hwnd and _dock_child(bw, hwnd):
            d = float(dpr) or 1.0
            x = max(0, int(float(left) * d))
            y = max(0, int(float(top) * d))
            ww = max(80, int(float(w) * d))
            hh = max(80, int(float(h) * d))
            try:
                u = _u32()
                u.MoveWindow.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_int,
                                         ctypes.c_int, ctypes.c_int, wintypes.BOOL]
                u.MoveWindow(hwnd, x, y, ww, hh, True)
                # CRITICAL: WinForms only reflows the hosted WebView2 to fill the
                # form when WinForms itself moves the form. We moved it via raw
                # Win32, so the web view keeps its old rect -> the page renders in
                # the wrong place. Resize the inner web view child to fill the form.
                _GW_CHILD = 5
                child = u.GetWindow(hwnd, _GW_CHILD)
                if child:
                    u.MoveWindow(child, 0, 0, ww, hh, True)
                if show:
                    u.ShowWindow(hwnd, _SW_SHOW)
                    u.SetWindowPos(hwnd, 0, 0, 0, 0, 0,
                                   _SWP_NOMOVE | _SWP_NOSIZE | _SWP_NOACTIVATE)  # HWND_TOP
            except Exception:
                pass
            # throttled geometry log so we can SEE what it's doing, not guess
            try:
                import time as _t
                if _t.time() - self._preview_log_ts[0] > 1.0:
                    self._preview_log_ts[0] = _t.time()
                    fr.diag("browser", True,
                            msg=f"child geom in=({left:.0f},{top:.0f},{w:.0f},{h:.0f}) dpr={dpr} -> rel=({x},{y},{ww},{hh})")
            except Exception:
                pass
            return
        # FALLBACK: screen-coordinate positioning (only if reparenting failed)
        try:
            if show:
                try:
                    bw.show()
                except Exception:
                    pass
            x, y, ww, hh = self._preview_geom(left, top, w, h, dpr)
            bw.resize(ww, hh)
            bw.move(x, y)
        except Exception:
            pass

    # ---- MULTI-TAB browser: each tab is its own docked WebView2 window --------
    # WIN["preview_tabs"] = {tabId: window}; WIN["preview_active"] is shown, the
    # rest hidden. WIN["preview_window"] mirrors the active one (for re-anchor /
    # teardown helpers). Links that try to open a new window are kept IN the tab
    # (KEEP_INAPP_JS) so nothing pops out to the system browser.
    # Intercept new-window attempts: window.open / target=_blank / ctrl|middle
    # click -> ask the app (via the per-window bridge) to open it in a NEW TAB.
    # Falls back to same-tab navigation if the bridge isn't there yet.
    KEEP_INAPP_JS = ("(function(){try{if(window.__fpKeep)return;window.__fpKeep=1;"
                     "function nt(u){try{if(!u)return;if(window.pywebview&&pywebview.api&&pywebview.api.new_tab){pywebview.api.new_tab(''+u);}else{location.href=u;}}catch(_){try{location.href=u;}catch(e){}}}"
                     "window.open=function(u){if(u)nt(u);return null;};"
                     "document.addEventListener('click',function(e){var a=e.target&&e.target.closest&&e.target.closest('a[href]');if(!a)return;"
                     "if((a.target&&a.target!=='_self')||e.ctrlKey||e.metaKey||e.button===1){e.preventDefault();nt(a.href);}},true);"
                     "document.addEventListener('auxclick',function(e){if(e.button!==1)return;var a=e.target&&e.target.closest&&e.target.closest('a[href]');if(a&&a.href){e.preventDefault();nt(a.href);}},true);"
                     "}catch(_){}})()")

    def _ptabs(self):
        return WIN.setdefault("preview_tabs", {})

    def _active_win(self):
        return self._ptabs().get(WIN.get("preview_active"))

    def _show_only(self, tab):
        u = _u32()
        for tid, w in list(self._ptabs().items()):
            hwnd = _hwnd_of(w)
            if hwnd:
                try:
                    u.ShowWindow(hwnd, _SW_SHOW if tid == tab else _SW_HIDE)
                except Exception:
                    pass

    def _keep_inapp(self, bw):
        try:
            bw.evaluate_js(self.KEEP_INAPP_JS)
        except Exception:
            pass

    def preview_open(self, url, left=20, top=455, w=1136, h=300, dpr=1.0, tab="t0"):
        WIN["preview_rect"] = (left, top, w, h, dpr)
        try:
            import webview
            try:
                target = fr._browser_normalize(url)
            except Exception:
                target = url or "about:blank"
            x, y, ww, hh = self._preview_geom(left, top, w, h, dpr)
            tabs = self._ptabs()
            bw = tabs.get(tab)
            if bw is not None:                       # existing tab -> navigate it
                try:
                    bw.load_url(target)
                    WIN["preview_active"] = tab
                    WIN["preview_window"] = bw
                    self._preview_apply(bw, left, top, w, h, dpr)
                    self._show_only(tab)
                    self._keep_inapp(bw)
                    return {"ok": True, "tab": tab}
                except Exception:
                    tabs.pop(tab, None)
            bw = webview.create_window(
                self.PREVIEW_TITLE, target, frameless=True, on_top=True,
                width=ww, height=hh, background_color="#0a0a0f",
                js_api=_PREVIEW_BRIDGE)   # lets page JS request a new tab
            tabs[tab] = bw
            WIN["preview_active"] = tab
            WIN["preview_window"] = bw
            try:
                bw.events.closed += (lambda t=tab: (self._ptabs().pop(t, None)))
            except Exception:
                pass

            def _on_shown(t=tab, b=bw):
                self._preview_apply(b, left, top, w, h, dpr)
                self._show_only(t)
                self._keep_inapp(b)
            try:
                bw.events.shown += lambda: _on_shown()
            except Exception:
                _on_shown()
            self._preview_apply(bw, left, top, w, h, dpr)
            return {"ok": True, "tab": tab}
        except Exception as e:
            return {"ok": False, "message": str(e)}

    def preview_show(self, tab, left=20, top=455, w=1136, h=300, dpr=1.0):
        """Switch the active tab: show its window, hide the others."""
        WIN["preview_rect"] = (left, top, w, h, dpr)
        bw = self._ptabs().get(tab)
        if bw is not None:
            WIN["preview_active"] = tab
            WIN["preview_window"] = bw
            self._preview_apply(bw, left, top, w, h, dpr, show=True)
            self._show_only(tab)
        return {"ok": True}

    def preview_pos(self, left=20, top=455, w=1136, h=300, dpr=1.0):
        WIN["preview_rect"] = (left, top, w, h, dpr)   # cache for re-anchoring
        bw = self._active_win()
        if bw is not None:
            self._preview_apply(bw, left, top, w, h, dpr, show=True)
        return {"ok": True}

    def preview_hide(self):
        u = _u32()
        for w in list(self._ptabs().values()):
            hwnd = _hwnd_of(w)
            if hwnd:
                try:
                    u.ShowWindow(hwnd, _SW_HIDE)
                except Exception:
                    pass
        return {"ok": True}

    # Pause JS for media in a tab: stops background audio AND video decode so a
    # left-open YouTube/Twitch tab can't steal CPU/GPU/network while you're in-game.
    _PAUSE_MEDIA_JS = ("(function(){try{document.querySelectorAll('video,audio')"
        ".forEach(function(m){try{m.pause();}catch(e){}});}catch(e){}})()")

    def preview_suspend(self):
        """In-game / window-hidden: hide every tab window (WebView2 stops
        repainting -> no GPU) and pause all media in every tab (no background
        audio/video decode). Returns immediately; resume re-shows the active tab."""
        WIN["preview_suspended"] = True
        u = _u32()
        for w in list(self._ptabs().values()):
            hwnd = _hwnd_of(w)
            if hwnd:
                try:
                    u.ShowWindow(hwnd, _SW_HIDE)
                except Exception:
                    pass
            try:
                w.evaluate_js(self._PAUSE_MEDIA_JS)
            except Exception:
                pass
        return {"ok": True}

    def preview_resume(self, left=20, top=455, w=1136, h=300, dpr=1.0):
        """Back in focus: re-show the active tab where it belongs. Media stays
        paused (the user can press play again) -- we never auto-resume audio."""
        WIN["preview_suspended"] = False
        bw = self._active_win()
        if bw is not None:
            self._preview_apply(bw, left, top, w, h, dpr, show=True)
            self._show_only(WIN.get("preview_active"))
        return {"ok": True}

    def preview_close_tab(self, tab):
        bw = self._ptabs().pop(tab, None)
        if bw is not None:
            _safe_destroy(bw)
        if WIN.get("preview_active") == tab:
            WIN["preview_active"] = None
            WIN["preview_window"] = None
        return {"ok": True}

    def preview_close(self):
        for w in list(self._ptabs().values()):
            try:
                _safe_destroy(w)
            except Exception:
                pass
        WIN["preview_tabs"] = {}
        WIN["preview_active"] = None
        WIN["preview_window"] = None
        WIN["preview_child"] = None
        return {"ok": True}

    # ---- navigation: always acts on the ACTIVE tab ----------------------------
    def _preview_js(self, code):
        bw = self._active_win()
        if bw is not None:
            try:
                bw.evaluate_js(code)
            except Exception:
                pass
        return {"ok": True}

    def preview_back(self):
        return self._preview_js("history.back()")

    def preview_forward(self):
        return self._preview_js("history.forward()")

    def preview_reload(self):
        return self._preview_js("location.reload()")

    def preview_zoom(self, factor):
        try:
            z = max(0.4, min(3.0, float(factor)))
        except Exception:
            z = 1.0
        WIN["preview_zoom"] = z
        return self._preview_js("document.documentElement.style.zoom='%s'" % z)

    def preview_url(self):
        bw = self._active_win()
        if bw is not None:
            try:
                self._keep_inapp(bw)   # re-assert keep-in-app after any navigation
                return {"url": bw.get_current_url()}
            except Exception:
                pass
        return {"url": None}


# ===========================================================================
# WIN32 NATIVE TWEAKS (ctypes) -- all best-effort; any failure is swallowed so
# the app still runs. Two jobs:
#   1) dark title bar on the native-framed main window (keeps it on-brand)
#   2) make the inline browser an OWNED tool-window: no separate taskbar/alt-tab
#      entry, always pinned above the app, and torn down with it.
# ===========================================================================
import ctypes
from ctypes import wintypes

_GWL_STYLE       = -16
_GWL_EXSTYLE     = -20
_GWLP_HWNDPARENT = -8
_WS_CHILD         = 0x40000000
_WS_POPUP         = 0x80000000
_WS_EX_TOOLWINDOW = 0x00000080
_WS_EX_APPWINDOW  = 0x00040000
_SWP_NOMOVE = 0x0002
_SWP_NOSIZE = 0x0001
_SWP_NOZORDER = 0x0004
_SWP_NOACTIVATE = 0x0010
_SWP_FRAMECHANGED = 0x0020
_SW_HIDE = 0
_SW_SHOW = 5


def _hwnd_of(window):
    """The Win32 HWND behind a pywebview window (its WinForms form)."""
    try:
        return int(window.native.Handle.ToInt64())
    except Exception:
        try:
            return int(window.native.Handle.ToInt32())
        except Exception:
            return None


def set_app_user_model_id(appid="Fragnetic.App"):
    """Give the process its OWN taskbar identity. Without this, Windows groups the
    app under the host process and shows a stale/generic taskbar icon. Must be
    called BEFORE the first window is created."""
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(appid)
    except Exception:
        pass


def apply_window_icon(hwnd, ico_path):
    """Force the title-bar + TASKBAR icon to our .ico. pywebview's start(icon=)
    doesn't reliably set the WebView2 window's taskbar icon, so we set it directly
    with WM_SETICON for both the small (caption) and big (taskbar) sizes."""
    if not hwnd or not ico_path:
        return
    try:
        u = ctypes.windll.user32
        IMAGE_ICON, LR_LOADFROMFILE = 1, 0x10
        WM_SETICON, ICON_SMALL, ICON_BIG = 0x80, 0, 1
        sm = u.LoadImageW(None, str(ico_path), IMAGE_ICON, 16, 16, LR_LOADFROMFILE)
        bg = u.LoadImageW(None, str(ico_path), IMAGE_ICON, 32, 32, LR_LOADFROMFILE)
        if sm:
            u.SendMessageW(hwnd, WM_SETICON, ICON_SMALL, sm)
        if bg:
            u.SendMessageW(hwnd, WM_SETICON, ICON_BIG, bg)
    except Exception:
        pass


def _u32():
    return ctypes.windll.user32


def _get_long(hwnd, idx):
    u = _u32()
    try:
        u.GetWindowLongPtrW.restype = ctypes.c_ssize_t
        u.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
        return u.GetWindowLongPtrW(hwnd, idx)
    except Exception:
        return u.GetWindowLongW(hwnd, idx)


def _get_exstyle(hwnd):
    return _get_long(hwnd, _GWL_EXSTYLE)


def _dock_child(bw, hwnd):
    """Reparent the inline-browser window INTO the app window as a child (once).
    As a child its position is client-relative, it's clipped to the app, and it
    moves across monitors with the app -- removing all the screen-coordinate /
    DPI / multi-monitor math that made the floating version drift."""
    docked = WIN.setdefault("preview_children", set())
    if hwnd in docked:                 # each tab window is reparented exactly once
        return True
    parent = _main_hwnd()
    if not parent:
        return False
    try:
        u = _u32()
        style = int(_get_long(hwnd, _GWL_STYLE))
        _set_longptr(hwnd, _GWL_STYLE, (style & ~_WS_POPUP) | _WS_CHILD)
        u.SetParent.argtypes = [wintypes.HWND, wintypes.HWND]
        u.SetParent.restype = wintypes.HWND
        u.SetParent(hwnd, parent)
        _frame_changed(hwnd)
        docked.add(hwnd)
        WIN["preview_child"] = hwnd
        try:
            fr.diag("browser", True, msg=f"docked as child of app (hwnd {hwnd} -> {parent})")
        except Exception:
            pass
        return True
    except Exception as e:
        try:
            fr.diag("browser", False, msg="reparent failed", exc=e)
        except Exception:
            pass
        return False


def _set_longptr(hwnd, idx, val):
    u = _u32()
    try:
        u.SetWindowLongPtrW.restype = ctypes.c_ssize_t
        u.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
        return u.SetWindowLongPtrW(hwnd, idx, val)
    except Exception:
        return u.SetWindowLongW(hwnd, idx, val)


def _frame_changed(hwnd):
    u = _u32()
    try:
        u.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int,
                                   ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT]
        u.SetWindowPos(hwnd, 0, 0, 0, 0, 0,
                       _SWP_NOMOVE | _SWP_NOSIZE | _SWP_NOZORDER | _SWP_NOACTIVATE | _SWP_FRAMECHANGED)
    except Exception:
        pass


def enable_dark_titlebar(window):
    """Dark native title bar (DWM) so the native frame matches the app, and stash
    the main HWND so the inline browser can be positioned/owned against it."""
    hwnd = _hwnd_of(window)
    if not hwnd:
        return
    WIN["main_hwnd"] = hwnd
    try:
        dwm = ctypes.windll.dwmapi
        on = ctypes.c_int(1)
        for attr in (20, 19):   # DWMWA_USE_IMMERSIVE_DARK_MODE (20 new, 19 old)
            try:
                dwm.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(on), ctypes.sizeof(on))
            except Exception:
                pass
        try:                    # DWMWA_CAPTION_COLOR (Win11) -> 0x00BBGGRR for #0a0a0f
            cap = ctypes.c_int(0x000f0a0a)
            dwm.DwmSetWindowAttribute(hwnd, 35, ctypes.byref(cap), ctypes.sizeof(cap))
        except Exception:
            pass
    except Exception:
        pass


def make_owned_toolwindow(bw):
    """Turn the inline-browser window into a tool-window owned by the main app:
    no taskbar/alt-tab entry of its own, always above the app, closes with it."""
    try:
        hwnd = _hwnd_of(bw)
        if not hwnd:
            return
        ex = _get_exstyle(hwnd)
        _set_longptr(hwnd, _GWL_EXSTYLE, (ex | _WS_EX_TOOLWINDOW) & ~_WS_EX_APPWINDOW)
        mh = _main_hwnd()
        if mh:
            _set_longptr(hwnd, _GWLP_HWNDPARENT, mh)   # owned by the app window
        _frame_changed(hwnd)
    except Exception:
        pass


def _main_hwnd():
    """The app window's HWND, resolved lazily. enable_dark_titlebar tries to set
    it on the 'loaded' event, but that can fire before the native form exists --
    so fall back to reading it straight off the live window when needed. Without
    this the inline browser loses its anchor and drifts outside the app."""
    h = WIN.get("main_hwnd")
    if h:
        return h
    try:
        h = _hwnd_of(WIN.get("window"))
        if h:
            WIN["main_hwnd"] = h
            return h
    except Exception:
        pass
    return None


def _client_origin(hwnd):
    """Screen coords of the app's CLIENT top-left (below the title bar). Frame-
    agnostic, so the inline browser lines up under a native OR frameless window
    and follows the app across monitors."""
    pt = wintypes.POINT(0, 0)
    u = _u32()
    u.ClientToScreen.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
    u.ClientToScreen(hwnd, ctypes.byref(pt))
    return int(pt.x), int(pt.y)


def _client_size(hwnd):
    r = wintypes.RECT()
    u = _u32()
    u.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    u.GetClientRect(hwnd, ctypes.byref(r))
    return int(r.right - r.left), int(r.bottom - r.top)


def run_native_window(url, httpd):
    """Preferred path: pywebview native window + tray. Blocks until closed."""
    import webview

    tune_webview_perf()   # frame-rate cap + low-end mode; GPU compositing stays ON

    set_app_user_model_id()   # own taskbar identity BEFORE the window exists (fixes stale taskbar icon)

    window = webview.create_window(
        WIN_TITLE, url,
        width=1180, height=824, min_size=(900, 640),
        background_color="#0a0a0f",
        # NATIVE frame: Windows handles resize-from-any-edge, Aero Snap, maximize
        # and multi-monitor DPI for us (a frameless window has no resize border).
        # We tint the caption dark (enable_dark_titlebar) to keep it on-brand and
        # hide the redundant in-page titlebar via chrome_mode()=='native'.
        frameless=False,
        js_api=WindowControls(),
    )
    WIN["window"] = window
    WIN["main_w"], WIN["main_h"] = 1180, 824

    # Dark caption + force our icon onto the window (title bar + taskbar) once the
    # form exists. WM_SETICON on the top-level form is what the taskbar reads.
    def _on_window_ready():
        enable_dark_titlebar(window)
        try:
            apply_window_icon(_hwnd_of(window), first_existing(icon_paths()["ico"]))
        except Exception:
            pass
    try:
        window.events.loaded += _on_window_ready
    except Exception:
        _on_window_ready()

    # When the main window closes (native X or tray Quit), terminate the whole
    # process IMMEDIATELY. Relying on webview.start() returning has repeatedly
    # left a headless FRAGROUTE process alive holding the exe lock; a hard exit
    # from the close event guarantees the app actually quits.
    try:
        window.events.closed += lambda: _hard_quit()
    except Exception:
        pass

    # Re-anchor the inline browser the instant a native move/resize ends (the JS
    # tracker is blocked during the OS modal drag loop, so it'd otherwise lag).
    try:
        window.events.moved += lambda *a: _reanchor_preview()
    except Exception:
        pass
    try:
        window.events.resized += lambda *a: _reanchor_preview()
    except Exception:
        pass

    icon = build_tray(url, on_quit=lambda: _hard_quit())
    if icon is not None:
        threading.Thread(target=icon.run, daemon=True).start()

    ico = first_existing(icon_paths()["ico"])
    try:
        try:
            if ico:
                webview.start(icon=str(ico))
            else:
                webview.start()
        except TypeError:
            webview.start()  # older pywebview without icon kwarg
    except Exception:
        # WebView2 runtime MISSING or backend failed to initialize (common on a
        # fresh Windows 10 box that never got the Edge WebView2 Evergreen runtime).
        # DO NOT os._exit here -- that would kill the process before main()'s
        # fallback can open an Edge app-mode / browser window on the SAME server.
        # Stop only the native tray + child windows and re-raise; keep httpd alive.
        if icon is not None:
            try:
                icon.stop()
            except Exception:
                pass
        try:
            _destroy_aux_windows()
        except Exception:
            pass
        raise
    # Window opened and was closed normally -> full teardown + HARD exit.
    if icon is not None:
        try:
            icon.stop()
        except Exception:
            pass
    try:
        _destroy_aux_windows()
    except Exception:
        pass
    try:
        httpd.shutdown()
    except Exception:
        pass
    # HARD exit: pywebview/WebView2/.NET can leave a non-daemon message pump
    # alive after the window closes, leaving a headless FRAGROUTE process
    # holding the exe lock. Force the whole process (and its child threads)
    # down so closing the window actually quits the app.
    os._exit(0)


def _hard_quit(*_a):
    """Tear down aux windows and FORCE the process to exit. Used by the window
    close event and tray Quit so closing FRAGROUTE always fully terminates it
    (no headless leftover holding the exe lock)."""
    try:
        _destroy_aux_windows()
    except Exception:
        pass
    try:
        import fragroute_llm as _llm    # kill the local model server so it doesn't orphan
        _llm.stop()
    except Exception:
        pass
    try:
        import sys as _s
        _s.stdout.flush()
    except Exception:
        pass
    os._exit(0)


def _reanchor_preview():
    """Snap the inline browser back over the tab area after the app window is
    moved/resized. The native move/resize runs a modal loop that blocks the JS
    150ms tracker, so without this the browser is left stranded until you stop
    dragging. Re-applies the last known tab-area rect against the live window."""
    bw = WIN.get("preview_window")
    rect = WIN.get("preview_rect")
    if bw is None or not rect:
        return
    try:
        WindowControls()._preview_apply(bw, *rect, show=True)
    except Exception:
        pass


def _destroy_aux_windows():
    """Tear down the inline browser + preview windows. pywebview's GUI loop only
    returns once EVERY window is closed -- so if these survive, the main window
    'closes' but the app (and the visible browser) keep running. Destroying them
    here is what actually makes the private browser vanish on close, as promised."""
    # every browser tab window (multi-tab) + the legacy single refs
    for w in list((WIN.get("preview_tabs") or {}).values()):
        try:
            w.destroy()
        except Exception:
            pass
    WIN["preview_tabs"] = {}
    for key in ("preview_window", "browser_window"):
        w = WIN.get(key)
        if w is not None:
            try:
                w.destroy()
            except Exception:
                pass
        WIN[key] = None


def _safe_destroy(window):
    try:
        window.destroy()
    except Exception:
        os._exit(0)


def _show_url_messagebox(url):
    """Last-resort UI when there's NO WebView2 runtime AND no browser we can launch.
    The HTTP server is up, so the app IS running -- we just can't paint a window.
    Pop a native Windows message box (ctypes only, needs no browser) telling the user
    the address to open in any browser, plus how to get the built-in window next time.
    Without this the customer sees 'nothing happened' and assumes the app is broken."""
    msg = ("Fragnetic is running.\n\n"
           "Open this address in any web browser:\n\n    %s\n\n"
           "For the built-in app window next time, install the free Microsoft Edge "
           "WebView2 runtime (one-time): https://aka.ms/webview2" % url)
    try:
        if os.name == "nt":
            import ctypes
            # MB_ICONINFORMATION | MB_SETFOREGROUND
            ctypes.windll.user32.MessageBoxW(0, msg, "Fragnetic is running", 0x40 | 0x10000)
        else:
            print(msg)
    except Exception:
        print(msg)


def run_appmode_window(url, httpd):
    """Fallback: Edge/Chrome chromeless window + tray (quit only)."""
    browser = find_browser_appmode()
    profile = APP_DIR / ".appwindow"
    if browser:
        try:
            subprocess.Popen([
                browser, f"--app={url}",
                "--window-size=1180,840",
                f"--user-data-dir={profile}",
                "--no-first-run", "--no-default-browser-check",
                # PERF: cap the frame rate and use low-end-device mode so the UI
                # renders cheaply. GPU compositing stays ON (forcing software
                # rendering with --disable-gpu was what tanked in-game FPS).
                "--max-gum-fps=10",
                "--enable-low-end-device-mode",
            ], **getattr(fr, "_NO_WINDOW_KW", {}))  # no flashing console window
        except Exception:
            browser = None
    if not browser:
        # No Chromium browser to host an app-window. Open the URL in whatever the
        # default browser is (the UI is a plain local web page, so any browser works).
        opened = False
        try:
            opened = bool(webbrowser.open(url))
        except Exception:
            opened = False
        if not opened:
            # TRULY stripped box: no WebView2, no Chromium browser, and no default
            # browser we can launch. The server IS running -- make sure the customer
            # knows that and how to reach it, instead of a silent "nothing happened".
            _show_url_messagebox(url)

    stop = threading.Event()

    def on_quit():
        stop.set()

    icon = build_tray(url, on_quit=on_quit)
    if icon is not None:
        # run tray on main thread (blocks) so the menu works
        threading.Thread(target=stop.wait, daemon=True).start()
        watch = threading.Thread(target=lambda: (stop.wait(), icon.stop()),
                                 daemon=True)
        watch.start()
        try:
            icon.run()
        except Exception:
            pass          # a tray runtime error must NOT skip httpd.shutdown() below
    else:
        print("FRAGROUTE running. Close this window / press Ctrl+C to quit.")
        try:
            stop.wait()
        except KeyboardInterrupt:
            pass
    httpd.shutdown()


# ===========================================================================
# SINGLE INSTANCE  (never open the app twice)
# ---------------------------------------------------------------------------
# Two guards working together:
#   1. A global named mutex -- the hard guarantee. The first instance creates
#      it; any later instance sees it already exists and bows out. This wins
#      even on a near-simultaneous double-click.
#   2. A best-effort window focus -- when we detect we're the second instance,
#      we bring the already-running FRAGROUTE window to the front so the click
#      "does something" instead of silently nothing.
# ===========================================================================
# Session-local namespace (no "Global\\" prefix): single-instance is per-user
# session, which is exactly what we want, and a plain name needs no special
# privilege -- so it works whether we're elevated or not, and the elevated and
# non-elevated launches of the same session still see each other.
_MUTEX_NAME = "FRAGROUTE_SingleInstance_v1"
_SINGLE_INSTANCE_HANDLE = None     # kept alive for the whole process lifetime


def _find_window_contains(substr):
    """HWND of the first visible top-level window whose title contains substr
    (case-insensitive), or 0. Catches the Edge app-mode window too."""
    import ctypes
    user32 = ctypes.windll.user32
    found = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    def _cb(hwnd, _lparam):
        try:
            if not user32.IsWindowVisible(hwnd):
                return True
            n = user32.GetWindowTextLengthW(hwnd)
            if n:
                buf = ctypes.create_unicode_buffer(n + 1)
                user32.GetWindowTextW(hwnd, buf, n + 1)
                if substr.lower() in buf.value.lower():
                    found.append(hwnd)
                    return False
        except Exception:
            pass
        return True

    try:
        user32.EnumWindows(_cb, 0)
    except Exception:
        pass
    return found[0] if found else 0


def focus_existing_window():
    """Bring an already-running FRAGROUTE window to the front. Returns True if
    one was found (i.e. an instance is already up)."""
    if os.name != "nt":
        return False
    try:
        import ctypes
        user32 = ctypes.windll.user32
        # exact native title first, then a SPECIFIC phrase shared by both the
        # native and Edge app-mode titles ("...Route Optimizer"). We deliberately
        # do NOT match a loose "fragroute" -- that would catch a code editor
        # showing fragroute.py and wrongly think the app is already running.
        hwnd = user32.FindWindowW(None, WIN_TITLE) or _find_window_contains("fragpunk companion")
        if hwnd:
            user32.ShowWindow(hwnd, 9)        # SW_RESTORE (un-minimize)
            user32.SetForegroundWindow(hwnd)
            return True
    except Exception:
        pass
    return False


def acquire_single_instance():
    """Create the global mutex. True if we're the only instance, False if
    another already holds it. Never blocks startup on failure."""
    global _SINGLE_INSTANCE_HANDLE
    if os.name != "nt":
        return True
    try:
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.windll.kernel32
        k32.CreateMutexW.restype = wintypes.HANDLE
        k32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
        ERROR_ALREADY_EXISTS = 183
        handle = k32.CreateMutexW(None, False, _MUTEX_NAME)
        already = (k32.GetLastError() == ERROR_ALREADY_EXISTS)
        _SINGLE_INSTANCE_HANDLE = handle      # keep the handle alive
        return not already
    except Exception:
        return True


# ===========================================================================
# START MENU SHORTCUT  (so Windows search finds the app)
# ---------------------------------------------------------------------------
# A standalone .exe isn't indexed by Windows search. We drop (and keep current)
# a Start Menu shortcut pointing at wherever this .exe currently lives, so it
# survives moving the folder or rebuilding. Windows + frozen .exe only; best
# effort, never blocks startup.
# ===========================================================================
def ensure_start_menu_shortcut():
    if os.name != "nt" or not getattr(sys, "frozen", False):
        return
    try:
        appdata = os.environ.get("APPDATA")
        if not appdata:
            return
        exe = sys.executable
        # use a STABLE icon (copied out of the ephemeral _MEIPASS temp dir);
        # fall back to the exe's own embedded icon (index 0) which always exists
        ico = stable_icon_path() or (exe + ",0")
        workdir = os.path.dirname(exe)
        lnk = os.path.join(appdata, "Microsoft", "Windows", "Start Menu",
                           "Programs", "FRAGROUTE.lnk")

        def q(s):                          # single-quote-safe for PowerShell
            return str(s).replace("'", "''")

        ps = (
            "$ws=New-Object -ComObject WScript.Shell;"
            f"$s=$ws.CreateShortcut('{q(lnk)}');"
            f"$s.TargetPath='{q(exe)}';"
            f"$s.WorkingDirectory='{q(workdir)}';"
            f"$s.IconLocation='{q(ico)}';"
            "$s.Description='FRAGROUTE - Fragpunk VPN Route Optimizer';"
            "$s.WindowStyle=1;$s.Save()"
        )
        subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                       timeout=8, capture_output=True, **getattr(fr, "_NO_WINDOW_KW", {}))
    except Exception:
        pass


# ===========================================================================
# MAIN
# ===========================================================================
def main():
    ap = argparse.ArgumentParser(description="FRAGROUTE desktop app")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--configs", default=str(APP_DIR / "configs"))
    ap.add_argument("--dry-run", action="store_true",
                    help="never execute tunnel commands; no admin needed")
    ap.add_argument("--no-elevate", action="store_true",
                    help="don't auto-request admin")
    ap.add_argument("--browser", action="store_true",
                    help="force the Edge/Chrome app-mode window")
    ap.add_argument("--elevated", action="store_true", help=argparse.SUPPRESS)
    args = ap.parse_args()

    # SINGLE INSTANCE (pre-elevation): if a FRAGROUTE window is already open,
    # just focus it and exit -- this also avoids a second UAC prompt entirely.
    if focus_existing_window():
        return

    # admin up front so tunnel switching works (skip for dry-run / opt-out)
    if not args.dry_run and not args.no_elevate and not is_admin():
        if relaunch_elevated_app():
            return  # elevated instance is taking over

    # SINGLE INSTANCE (hard guard): claim the global mutex now that we're the
    # process that will actually run. If another instance won a simultaneous
    # race, focus it and bow out instead of starting a second server + tray.
    if not acquire_single_instance():
        focus_existing_window()
        return

    ensure_icon()
    ensure_start_menu_shortcut()   # keep the app findable in Windows search
    port = free_port(args.port)
    httpd = start_engine(port, args.configs, args.dry_run)
    url = f"http://127.0.0.1:{port}/"

    if args.browser:
        run_appmode_window(url, httpd)
        return

    try:
        run_native_window(url, httpd)        # pywebview
    except Exception as e:
        print("Native window unavailable (", e, ") -- using app-mode window.")
        run_appmode_window(url, httpd)


if __name__ == "__main__":
    main()
