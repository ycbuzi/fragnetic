"""FRAGROUTE local LLM -- the FragPunk-repurposed brain (free, private, on-device).

This does NOT train a model (impossible on consumer HW). Instead it runs a small
open model (Qwen2.5-3B-Instruct) via llama.cpp's `llama-server` as a localhost
sidecar, and the AI coach feeds it FragPunk-only context (RAG over the knowledge
store) + a FragPunk persona. Result: a general model wearing a FragPunk brain that
answers free-form questions, tolerates typos, and stays on-topic.

Design:
  * Prefer the Vulkan build (uses the GTX 1650 SUPER) -> CPU build as fallback.
  * LAZY: the server only starts on the first free-form question (no idle cost),
    and runs below-normal priority so it never fights the game.
  * Everything degrades: if the binary/model is missing, available() is False and
    the coach falls back to the deterministic router.

Pure stdlib (subprocess + urllib). The engine sets LLM_DIR and calls stop() on quit.
"""
import json
import os
import socket
import subprocess
import threading
import time
import urllib.request
from pathlib import Path

APP_LLM_BUILD = "llm-1"

LLM_DIR = None                  # set by fragroute.main(); else <module|exe>/llm
_LOCK = threading.Lock()
_STATE = {"proc": None, "port": None, "kind": None, "ready": False, "model": None,
          "starting": False, "error": None}

CTX_TOKENS = 4096
GEN_TOKENS = 480


def _base_dir():
    if LLM_DIR:
        return Path(LLM_DIR)
    import sys
    base = (Path(sys.executable).parent if getattr(sys, "frozen", False)
            else Path(__file__).parent)
    return base / "llm"


def _text_ggufs():
    d = _base_dir()
    out = []
    if d.exists():
        for p in sorted(d.glob("*.gguf")):
            n = p.name.lower()
            if "mmproj" in n or "-vl-" in n:
                continue
            out.append(p)
    return out


def find_models():
    """{'smart': big-model path, 'fast': small in-game path} -- by size hint in the
    filename. SMART = a big model (Apache-2.0 Qwen2.5-14B). FAST = a small model
    for in-game use on the 1650 SUPER (Apache-2.0 Qwen2.5-1.5B, or a 3B/2B)."""
    smart = None
    fasts = []
    for p in _text_ggufs():
        n = p.name.lower()
        if any(s in n for s in ("14b", "13b", "12b", "9b", "8b", "7b")):
            if smart is None:
                smart = str(p)
            continue                        # a big model is never the in-game model
        # In-game (1650S) model. PREFER a MID model (much better answers, still fits
        # 4GB) over the tiny 1.5B: Phi-3.5-mini (3.8B) / any 3B > 2B > 1.5B/1B.
        if any(s in n for s in ("phi", "mini", "3.8b", "3b")):
            fasts.append((0, str(p)))       # mid -- best in-game quality
        elif any(s in n for s in ("2b",)):
            fasts.append((1, str(p)))
        elif any(s in n for s in ("1.5b", "1b", "0.5b")):
            fasts.append((2, str(p)))
    fasts.sort(key=lambda x: x[0])
    return {"smart": smart, "fast": (fasts[0][1] if fasts else None)}


def find_model():
    """Default text model: prefer the smart (big) one, else the fast 3B, else any."""
    m = find_models()
    if m["smart"]:
        return m["smart"]
    if m["fast"]:
        return m["fast"]
    g = _text_ggufs()
    return str(g[0]) if g else None


# The engine flips this True while you're in-game so the AI uses the small/fast
# 3B on the 1650 SUPER (Vulkan1) and never touches the 4070 rendering the game.
# When idle it uses the SMART big model on the 4070 SUPER (Vulkan0).
_PREFER = {"fast": False}


def set_prefer_fast(v):
    _PREFER["fast"] = bool(v)


def rag_budget():
    """How much grounding to inject, scaled to the ACTIVE model's context window.
    The in-game 'fast' model runs with a 2048 ctx (small-GPU KV limit); stuffing 24
    facts + the system prompt into it overflows -> truncated/empty replies. A big
    out-of-game model has room for far more. This keeps the learned data RELEVANT
    across any model swap: the memory is model-agnostic, only how much of it fits
    changes. Returns {'facts': N, 'bits': M}."""
    label = _STATE.get("label")
    ctx = 2048 if label == "fast" else CTX_TOKENS      # mirrors the -c arg in ensure()
    if ctx <= 2048:   return {"facts": 5,  "bits": 9}
    if ctx <= 4096:   return {"facts": 9,  "bits": 16}
    if ctx <= 8192:   return {"facts": 14, "bits": 22}
    return {"facts": 18, "bits": 30}


def _choose():
    """Return (model_path, device_arg|None, label) per the smart/fast preference."""
    m = find_models()
    _bin, kind = find_binary()
    vk = (kind == "vulkan")
    if _PREFER["fast"] and m["fast"]:
        return m["fast"], ("Vulkan1" if vk else None), "fast"
    if m["smart"]:
        return m["smart"], ("Vulkan0" if vk else None), "smart"
    if m["fast"]:
        return m["fast"], ("Vulkan0" if vk else None), "smart"
    g = _text_ggufs()
    return (str(g[0]) if g else None), None, "?"


def find_vision():
    """The VISION model + its mmproj projector, or (None, None)."""
    d = _base_dir()
    if not d.exists():
        return None, None
    vl = [p for p in sorted(d.glob("*.gguf"))
          if "-vl-" in p.name.lower() and "mmproj" not in p.name.lower()]
    mm = sorted(d.glob("mmproj*.gguf"))
    if vl and mm:
        return str(vl[0]), str(mm[0])
    return None, None


def vision_available():
    m, mm = find_vision()
    return bool(m and mm and find_binary()[0])


def find_binary():
    """Prefer the Vulkan (GPU) server, fall back to CPU. Returns (path, kind)."""
    d = _base_dir()
    for sub, kind in (("vk", "vulkan"), ("cpu", "cpu")):
        sd = d / sub
        if sd.exists():
            for p in sd.rglob("llama-server.exe"):
                return str(p), kind
    if d.exists():
        for p in d.rglob("llama-server.exe"):
            return str(p), "unknown"
    return None, None


def available():
    return bool(find_model()) and bool(find_binary()[0])


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def _health(port, timeout=2):
    try:
        with urllib.request.urlopen("http://127.0.0.1:%d/health" % port, timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def _running(kind=None):
    p = _STATE["proc"]
    return _STATE["ready"] and p is not None and p.poll() is None


def ensure_running(timeout=240):
    """Lazily start llama-server, picking smart(4070)/fast(1650S) per game state.
    If the desired model/device changed, restarts the server. Returns True once
    /health passes."""
    model, device, label = _choose()
    binary, kind = find_binary()
    if not model or not binary:
        _STATE["error"] = "llama-server or model missing"
        return False
    with _LOCK:
        # already serving the right model on the right GPU?
        if _running() and _STATE.get("model") == Path(model).name and _STATE.get("device") == device:
            return True
        # already STARTING the right model (e.g. a prewarm is loading it)? don't
        # restart it -- fall through and just wait on that same server's /health.
        # Without this, a chat() landing mid-prewarm would kill the loading server
        # and respawn it, paying TWO cold loads (and looking like 'not loaded').
        _old = _STATE.get("proc")
        starting_right = (_STATE.get("starting") and _old is not None
                          and _old.poll() is None
                          and _STATE.get("model") == Path(model).name
                          and _STATE.get("device") == device)
        if not starting_right:
            # wrong model/GPU (or game state changed) -> stop the old server
            old = _STATE.get("proc")
            if old is not None and old.poll() is None:
                try:
                    old.terminate()
                    old.wait(timeout=4)
                except Exception:
                    try:
                        old.kill()
                    except Exception:
                        pass
            # 4GB in-game GPU: the fast text model and the vision model can't BOTH be
            # resident on the 1650S (they OOM/thrash -> the 'model isn't loaded' stall).
            # Free vision so voice's text model loads cleanly; vision reloads on its
            # next scout. (No-op for the smart model on the 12GB 4070.)
            if label == "fast" and device and _VSTATE.get("proc") is not None \
                    and _VSTATE["proc"].poll() is None:
                try:
                    _stop_state(_VSTATE)
                except Exception:
                    pass
            port = _free_port()
            # The 1650 SUPER has only ~3.5GB free, so the fast 3B's KV cache must
            # stay small or it OOMs at startup. Use a smaller context on that card.
            ctx_tok = 2048 if label == "fast" else CTX_TOKENS
            # -ngl 99 offloads all layers to GPU (Vulkan); --device pins which GPU.
            args = [binary, "-m", model, "--host", "127.0.0.1", "--port", str(port),
                    "-c", str(ctx_tok), "-ngl", "99", "--no-warmup"]
            if device:
                args += ["--device", device]
            flags = 0x08000000 if os.name == "nt" else 0          # CREATE_NO_WINDOW
            if os.name == "nt":
                flags |= 0x00004000                               # BELOW_NORMAL_PRIORITY
            try:
                proc = subprocess.Popen(args, stdout=subprocess.DEVNULL,
                                        stderr=subprocess.DEVNULL, creationflags=flags)
            except Exception as e:
                _STATE["error"] = str(e)
                return False
            _STATE.update(proc=proc, port=port, kind=kind, model=Path(model).name,
                          device=device, label=label, ready=False, starting=True, error=None)
    # poll health outside the lock so other calls can see 'starting'
    port = _STATE["port"]
    proc = _STATE["proc"]
    t0 = time.time()
    while time.time() - t0 < timeout:
        if proc.poll() is not None:
            _STATE["starting"] = False
            _STATE["error"] = "server exited during startup"
            return False
        if _health(port):
            _STATE["ready"] = True
            _STATE["starting"] = False
            return True
        time.sleep(1)
    _STATE["error"] = "startup timeout"
    _STATE["starting"] = False
    return False


# Idle auto-unload: a loaded model server keeps occupying its GPU even when idle.
# That's fine on the 4070 between matches, but a lingering server is what tanks
# in-game FPS. So we stop a server after it goes unused, freeing the GPU.
_LAST = {"text": 0.0, "vision": 0.0}
_IDLE = {"text": 150, "vision": 240}       # seconds of no use before unloading
# vision sits on the 1650S (not the game GPU), so keeping it warm longer between
# scouts costs the game nothing and keeps in-match callouts sub-second.
_WATCH = {"on": False}


def _touch(which):
    _LAST[which] = time.time()


def _stop_state(st):
    p = st.get("proc")
    if p is not None and p.poll() is None:
        try:
            p.terminate()
            p.wait(timeout=4)
        except Exception:
            try:
                p.kill()
            except Exception:
                pass
    st.update(proc=None, ready=False, starting=False)


def _idle_watch():
    if _WATCH["on"]:
        return
    _WATCH["on"] = True

    def _go():
        while True:
            time.sleep(15)
            try:
                now = time.time()
                if (_STATE.get("proc") and _STATE["proc"].poll() is None
                        and _LAST["text"] and now - _LAST["text"] > _IDLE["text"]):
                    _stop_state(_STATE)
                if (_VSTATE.get("proc") and _VSTATE["proc"].poll() is None
                        and _LAST["vision"] and now - _LAST["vision"] > _IDLE["vision"]):
                    _stop_state(_VSTATE)
            except Exception:
                pass
    threading.Thread(target=_go, daemon=True).start()


def prewarm_vision():
    """Start the vision server in the background so the first scout/recognize call
    isn't cold (cold-start is ~8s; a warm call is well under 1s). Non-blocking."""
    def _go():
        try:
            ensure_vision_running()
        except Exception:
            pass
    threading.Thread(target=_go, daemon=True).start()


def prewarm_text():
    """Start the currently-preferred TEXT model (fast 1650S in-game, smart 4070 in
    menu) in the BACKGROUND so the first chat -- e.g. a voice question -- is instant
    instead of a ~15s cold load that feels like 'the model isn't loaded'. No-op if a
    text server is already up. Non-blocking; safe to call often (e.g. on every voice
    key press, so the load overlaps your ~9s of recording + transcription)."""
    if _running() or _STATE.get("starting"):
        return
    def _go():
        try:
            ensure_running()
        except Exception:
            pass
    threading.Thread(target=_go, daemon=True).start()


def release_for_game():
    """Called when a match starts. Free the 4070 for the game by stopping the SMART
    (14B) text server on it -- text reloads as the fast 1.5B on the 1650S on demand.
    Vision lives on the 1650S (Vulkan1) and does NOT compete with the game GPU, so
    we do NOT unload it; instead we PRE-WARM it so the first in-match scout is
    instant instead of paying an ~8s cold-start."""
    if _STATE.get("label") == "smart" or (_STATE.get("device") in (None, "Vulkan0")):
        _stop_state(_STATE)
    prewarm_vision()


def chat(messages, max_tokens=GEN_TOKENS, temperature=0.3, timeout=120):
    """OpenAI-style chat completion against the local server. Returns text or None."""
    _touch("text")
    if not ensure_running():
        return None
    _idle_watch()
    body = json.dumps({"messages": messages, "max_tokens": max_tokens,
                       "temperature": temperature, "stream": False,
                       "cache_prompt": True}).encode("utf-8")
    url = "http://127.0.0.1:%d/v1/chat/completions" % _STATE["port"]
    try:
        req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            j = json.loads(r.read().decode("utf-8", "ignore"))
        return (j["choices"][0]["message"]["content"] or "").strip()
    except Exception as e:
        _STATE["error"] = str(e)
        return None


def status():
    m = find_models()
    return {
        "available": available(),
        "ready": _running(),
        "starting": bool(_STATE.get("starting")),
        "kind": _STATE.get("kind"),
        "model": _STATE.get("model") or (Path(find_model()).name if find_model() else None),
        "label": _STATE.get("label"),                 # 'smart' (4070) or 'fast' (1650S)
        "device": _STATE.get("device"),
        "hasSmart": bool(m["smart"]), "hasFast": bool(m["fast"]),
        "preferFast": bool(_PREFER["fast"]),
        "error": _STATE.get("error"),
    }


# ===========================================================================
#  VISION -- a SEPARATE, lazy server (Qwen2.5-VL + mmproj) so the AI can SEE
#  images/clip-frames without disturbing the fast text model. Loads only when
#  the first image is sent; ~3GB extra RAM while active (fine with 32GB).
# ===========================================================================
_VSTATE = {"proc": None, "port": None, "ready": False, "starting": False, "error": None}


def _vision_devices(kind):
    """GPU pin order to try for the vision server. CRITICAL: prefer the SECONDARY
    GPU (1650 SUPER = Vulkan1) so vision NEVER runs on the 4070 rendering the game
    -- without --device, Vulkan defaults to device 0 (the 4070) and every
    scout/recognize/map call competes with the game for the render GPU -> stutter.
    Fall back to Vulkan0 only if the small card can't hold the model (4GB OOM), so
    vision still works (degraded) rather than dying. CPU build: no pin."""
    if kind != "vulkan":
        return [None]
    return ["Vulkan1", "Vulkan0"]


def ensure_vision_running(timeout=150):
    with _LOCK:
        p = _VSTATE["proc"]
        if _VSTATE["ready"] and p is not None and p.poll() is None:
            return True
        model, mmproj = find_vision()
        binary, _kind = find_binary()
        if not model or not mmproj or not binary:
            _VSTATE["error"] = "vision model/mmproj/server missing"
            return False
        devices = _vision_devices(_kind)
        # free the FAST text model off the small GPU first -- they can't coexist on
        # the 1650S's 4GB, and letting vision OOM there makes it fall back to the 4070
        # and stutter the game. Vision takes the 1650S; text reloads on demand.
        if _STATE.get("label") == "fast" and _STATE.get("proc") is not None \
                and _STATE["proc"].poll() is None:
            try:
                _stop_state(_STATE)
            except Exception:
                pass

    flags = 0x08000000 if os.name == "nt" else 0
    if os.name == "nt":
        flags |= 0x00004000
    last_err = "vision startup failed"
    for device in devices:
        with _LOCK:
            port = _free_port()
            args = [binary, "-m", model, "--mmproj", mmproj, "--host", "127.0.0.1",
                    "--port", str(port), "-c", "4096", "-ngl", "99", "--no-warmup"]
            if device:
                args += ["--device", device]
            try:
                proc = subprocess.Popen(args, stdout=subprocess.DEVNULL,
                                        stderr=subprocess.DEVNULL, creationflags=flags)
            except Exception as e:
                last_err = str(e)
                continue
            _VSTATE.update(proc=proc, port=port, ready=False, starting=True,
                           error=None, device=device)
        t0 = time.time()
        while time.time() - t0 < timeout:
            if proc.poll() is not None:
                # exited at startup (most likely 4GB OOM on Vulkan1) -> try next GPU
                last_err = "vision server exited on %s (likely VRAM)" % (device or "cpu")
                break
            if _health(_VSTATE["port"]):
                _VSTATE.update(ready=True, starting=False, device=device)
                return True
            time.sleep(1)
        else:
            last_err = "vision startup timeout on %s" % (device or "cpu")
        try:
            if proc.poll() is None:
                proc.terminate()
        except Exception:
            pass
    _VSTATE.update(starting=False, error=last_err)
    return False


def warm_vision():
    """Start loading the vision model in the BACKGROUND so the first real call isn't
    a cold model load. Non-blocking; a no-op if already ready or starting."""
    try:
        p = _VSTATE.get("proc")
        if _VSTATE.get("ready") and p is not None and p.poll() is None:
            return
        if _VSTATE.get("starting"):
            return
        threading.Thread(target=ensure_vision_running, daemon=True).start()
    except Exception:
        pass


def _img_data_url(path, maxdim=1024):
    """base64 data-URL for an image, DOWNSCALED to maxdim. High-res (1080p+) frames
    tokenize into too many vision tokens and overflow the context -> empty replies;
    downscaling fixes that and speeds inference. Falls back to raw if PIL is absent."""
    import base64
    try:
        import io
        from PIL import Image
        im = Image.open(path).convert("RGB")
        w, h = im.size
        if max(w, h) > maxdim:
            s = maxdim / float(max(w, h))
            im = im.resize((max(1, int(w * s)), max(1, int(h * s))))
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=88)
        return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode("ascii")
    except Exception:
        with open(path, "rb") as f:
            return "data:image/png;base64," + base64.b64encode(f.read()).decode("ascii")


def chat_vision(prompt, image_path, max_tokens=480, timeout=180, maxdim=1024):
    """Ask the vision model about an image (a screenshot or clip frame). Returns
    text or None. The AI's eyes -- used for maps, scoreboards, crosshair review.
    `maxdim` downscales the image: for Qwen2-VL, pixels == vision tokens == encode
    time, so a small maxdim (e.g. 512) makes a warm call ~4x faster for quick
    in-game callouts where exact text legibility matters less."""
    _touch("vision")
    if not ensure_vision_running():
        return None
    _idle_watch()
    try:
        durl = _img_data_url(image_path, maxdim=maxdim)
    except Exception as e:
        _VSTATE["error"] = "read image: %s" % e
        return None
    content = [{"type": "text", "text": prompt},
               {"type": "image_url", "image_url": {"url": durl}}]
    body = json.dumps({"messages": [{"role": "user", "content": content}],
                       "max_tokens": max_tokens, "temperature": 0.2, "stream": False}).encode("utf-8")
    url = "http://127.0.0.1:%d/v1/chat/completions" % _VSTATE["port"]
    try:
        req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            j = json.loads(r.read().decode("utf-8", "ignore"))
        return (j["choices"][0]["message"]["content"] or "").strip()
    except Exception as e:
        _VSTATE["error"] = str(e)
        return None


def chat_vision_multi(prompt, image_paths, max_tokens=560, timeout=240):
    """Analyze SEVERAL images at once (e.g. frames sampled across a clip) so the AI
    can review a sequence -- crosshair placement / positioning over time."""
    _touch("vision")
    if not ensure_vision_running():
        return None
    _idle_watch()
    # several frames -> use a smaller maxdim each so the combined image tokens fit.
    content = [{"type": "text", "text": prompt}]
    for p in image_paths:
        try:
            content.append({"type": "image_url",
                            "image_url": {"url": _img_data_url(p, maxdim=768)}})
        except Exception:
            continue
    if len(content) < 2:
        return None
    body = json.dumps({"messages": [{"role": "user", "content": content}],
                       "max_tokens": max_tokens, "temperature": 0.2, "stream": False}).encode("utf-8")
    url = "http://127.0.0.1:%d/v1/chat/completions" % _VSTATE["port"]
    try:
        req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            j = json.loads(r.read().decode("utf-8", "ignore"))
        return (j["choices"][0]["message"]["content"] or "").strip()
    except Exception as e:
        _VSTATE["error"] = str(e)
        return None


def vision_status():
    m, mm = find_vision()
    return {"available": vision_available(),
            "ready": bool(_VSTATE["ready"] and _VSTATE["proc"] and _VSTATE["proc"].poll() is None),
            "starting": bool(_VSTATE.get("starting")),
            "model": Path(m).name if m else None,
            "error": _VSTATE.get("error")}


def stop():
    with _LOCK:
        for st in (_STATE, _VSTATE):
            p = st.get("proc")
            if p is not None:
                try:
                    p.terminate()
                    try:
                        p.wait(timeout=4)
                    except Exception:
                        p.kill()
                except Exception:
                    pass
            st.update(proc=None, ready=False, starting=False)
