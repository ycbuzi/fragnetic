"""FRAGROUTE live practice detector -- real-time YOLO, but ONLY in modes with no
real human opponents. This is the one sanctioned "live" path.

SAFETY MODEL (the line is bots/solo vs. real humans being disadvantaged):
  * AUTO  -- runs automatically: Shooting Range / Training, Practice vs AI (bots).
  * OPTIN -- runs only if the user explicitly enabled it (they confirm it's a
            bot/solo lobby): Scrimmage, Custom.  The app cannot tell bots from
            humans, so the user takes responsibility for these two.
  * BLOCKED -- everything else, and anything UNKNOWN/uncertain: Standard, Ranked,
            Shard Clash vs players, etc. Using a live detector there is ESP/cheating.

Defaults to BLOCKED. The mode is re-checked every loop; the instant it leaves the
safe set (e.g. a scrimmage warmup rolls into a real match) the loop STOPS itself.
No live game hook -- frames come from the same DXGI capture the recorder uses.

The engine injects callbacks (frame source, mode source, optional callout) to keep
this module engine-agnostic. Pure stdlib + the fragroute_yolo module.
"""
import threading
import time

APP_LIVE_BUILD = "live-1"

# Mode keyword -> safety tier. Matched case-insensitively against the engine's
# current mode string. Order matters: BLOCKED words win if present.
_AUTO_WORDS = ("firing range", "shooting range", "training", "tutorial",
               "practice vs ai", "vs ai", "practice range", "warm up", "warmup")
_OPTIN_WORDS = ("scrimmage", "custom")
# explicit PvP markers that must NEVER run live, even if another word matches
_BLOCKED_WORDS = ("ranked", "standard", "competitive", "shard clash", "outbreak",
                  "duel", "deathmatch", "team deathmatch", "free for all", "ffa")

_LIVE = {"running": False, "thread": None, "tier": None, "mode": None,
         "latest": {"ts": 0, "dets": [], "ms": 0}, "stopReason": None}
_LOCK = threading.Lock()


def mode_tier(mode_text):
    """Map a mode string to 'auto' | 'optin' | 'blocked'. Unknown -> 'blocked'
    (fail safe). BLOCKED markers always win."""
    m = (mode_text or "").strip().lower()
    if not m:
        return "blocked"
    for w in _BLOCKED_WORDS:
        if w in m:
            return "blocked"
    for w in _AUTO_WORDS:
        if w in m:
            return "auto"
    for w in _OPTIN_WORDS:
        if w in m:
            return "optin"
    return "blocked"


def allowed(mode_text, optin_enabled, admin=False):
    """Should the live detector run for this mode? Returns (bool, tier).
    admin=True (the OWNER, on the owner's own PC) OVERRIDES the mode gate and enables
    live detection in ANY mode, including real matches. This is an owner-only dev
    capability kept behind admin until a provably ban-safe live approach exists -- it
    is NEVER true for customers (admin is machine-locked to the owner's PC)."""
    if admin:
        return True, "admin"
    tier = mode_tier(mode_text)
    if tier == "auto":
        return True, tier
    if tier == "optin":
        return bool(optin_enabled), tier
    return False, tier


def latest():
    """Most recent detections (for a UI overlay). Safe to call anytime."""
    return dict(_LIVE["latest"])


def is_running():
    return _LIVE["running"]


def status():
    return {"build": APP_LIVE_BUILD, "running": _LIVE["running"],
            "tier": _LIVE["tier"], "mode": _LIVE["mode"],
            "stopReason": _LIVE["stopReason"],
            "lastDetections": len(_LIVE["latest"].get("dets") or []),
            "note": "live detector runs ONLY in bot/solo practice modes"}


def _loop(get_frame, get_mode, optin_getter, callout, interval, conf_thr, admin_getter=None):
    """Detection loop. RE-CHECKS the mode every iteration and self-stops the
    moment it is no longer allowed -- this is the safety guarantee."""
    try:
        import fragroute_yolo
    except Exception:
        _LIVE["stopReason"] = "detector module missing"
        _LIVE["running"] = False
        return
    # warm the ONNX session once (first inference compiles DirectML shaders)
    try:
        fragroute_yolo._ensure_session()
    except Exception:
        pass
    while _LIVE["running"]:
        # ---- SAFETY GATE: re-evaluate the live mode every loop ----
        try:
            mode = get_mode()
            ok, tier = allowed(mode, bool(optin_getter()), admin=bool(admin_getter and admin_getter()))
        except Exception:
            mode, ok, tier = None, False, "blocked"
        _LIVE["mode"], _LIVE["tier"] = mode, tier
        if not ok:
            _LIVE["stopReason"] = "mode not allowed (%s)" % (tier)
            break
        # ---- detect on one frame ----
        try:
            fp = get_frame()                      # engine writes a frame, returns path
            if fp:
                t = time.time()
                dets = fragroute_yolo.detect_image(fp, conf_thr=conf_thr)
                ms = int((time.time() - t) * 1000)
                _LIVE["latest"] = {"ts": int(time.time() * 1000), "dets": dets, "ms": ms}
                if callout and dets:
                    try:
                        callout(dets)
                    except Exception:
                        pass
        except Exception:
            pass
        time.sleep(max(0.05, interval))
    _LIVE["running"] = False


def start(get_frame, get_mode, optin_getter, callout=None, interval=0.25, conf_thr=0.3, admin_getter=None):
    """Start the live loop if the CURRENT mode is allowed. Returns a status dict.
    get_frame()  -> path to a freshly captured frame (or None)
    get_mode()   -> current mode string from the engine
    optin_getter()-> bool, whether the user enabled the opt-in modes
    callout(dets)-> optional, called with detections for voice/overlay."""
    with _LOCK:
        if _LIVE["running"]:
            return {"ok": True, "already": True, **status()}
        _old = _LIVE.get("thread")
        _LIVE["thread"] = None
    # A rapid stop()->start() could still have the previous loop winding down. If we spawned
    # now, TWO detector loops would run at once -- doubling the per-frame GPU cost mid-match
    # (a direct FPS hit, the one thing we never do). running is already False here, so the old
    # loop WILL exit; join it first. _loop never takes _LOCK, so this can't deadlock.
    if _old is not None and _old.is_alive():
        try:
            _old.join(timeout=1.0)
        except Exception:
            pass
    with _LOCK:
        if _LIVE["running"]:
            return {"ok": True, "already": True, **status()}
        try:
            mode = get_mode()
            ok, tier = allowed(mode, bool(optin_getter()), admin=bool(admin_getter and admin_getter()))
        except Exception:
            mode, ok, tier = None, False, "blocked"
        _LIVE["mode"], _LIVE["tier"] = mode, tier
        if not ok:
            _LIVE["stopReason"] = "mode not allowed (%s)" % tier
            return {"ok": False, "reason": _LIVE["stopReason"], **status()}
        _LIVE["running"] = True
        _LIVE["stopReason"] = None
        th = threading.Thread(target=_loop, args=(get_frame, get_mode, optin_getter,
                                                  callout, interval, conf_thr, admin_getter), daemon=True)
        _LIVE["thread"] = th
        th.start()
        return {"ok": True, "started": True, **status()}


def stop(reason="stopped"):
    with _LOCK:
        _LIVE["running"] = False
        _LIVE["stopReason"] = reason
        th = _LIVE.get("thread")
    # join OUTSIDE the lock (_loop never takes _LOCK, so no deadlock) so a caller that
    # stops-then-starts can rely on the old loop being gone.
    if th is not None and th.is_alive():
        try:
            th.join(timeout=1.0)
        except Exception:
            pass
    return {"ok": True, **status()}
