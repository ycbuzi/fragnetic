"""FRAGROUTE local image generation -- the AI can CREATE images (free, on-device).

Wraps stable-diffusion.cpp (sd-cli.exe) as a sidecar, same pattern as llama.cpp /
ffmpeg. Prefers the Vulkan build so it runs on the RTX 4070 SUPER; CPU fallback.
Used for custom crosshairs/overlays, profile & skin concept art, and strategy
diagrams. Generation is on-demand (one-shot CLI per image), gated so only one
runs at a time, and naturally idle-only (you trigger it).

The diffusion model is a SIDECAR file in the `sd` folder (not in the onefile).
If it's missing, available() is False and the UI shows "needs a model".

Pure stdlib (subprocess). The engine sets IMG_DIR + OUT_DIR.
"""
import os
import subprocess
import threading
import time
from pathlib import Path

import fragroute_proc as _proc   # orphan-proof helpers (shared Windows Job Object)

APP_IMG_BUILD = "img-2"          # img-2: job-adopt sd-cli so a GPU image-gen can't orphan

IMG_DIR = None             # set by engine; default <module|exe>/sd  (binary + model)
OUT_DIR = None             # set by engine; where generated PNGs are written
_LOCK = threading.Lock()
_STATE = {"busy": False, "last": None, "error": None, "model": None}

DEFAULT_STEPS = 20
DEFAULT_SIZE = 768
DEFAULT_CFG = 7.0


def _base_dir():
    if IMG_DIR:
        return Path(IMG_DIR)
    import sys
    base = (Path(sys.executable).parent if getattr(sys, "frozen", False)
            else Path(__file__).parent)
    return base / "sd"


def find_binary():
    """Prefer the Vulkan (GPU) sd-cli, fall back to CPU. Returns (path, kind)."""
    d = _base_dir()
    for sub, kind in (("vk", "vulkan"), ("cpu", "cpu")):
        p = d / sub / "sd-cli.exe"
        if p.exists():
            return str(p), kind
    for p in d.rglob("sd-cli.exe"):
        return str(p), "unknown"
    return None, None


def find_model():
    """The diffusion model: a .safetensors/.gguf/.ckpt in sd/ or sd/models/.
    Prefers a higher-quality SDXL/FLUX/SD3 model when present (by name), else the
    first available. (Excludes the runtime's own helper .gguf inside vk/ or cpu/.)"""
    d = _base_dir()
    if not d.exists():
        return None
    cands = []
    for sub in (d, d / "models"):
        if not sub.exists():
            continue
        for ext in ("*.safetensors", "*.gguf", "*.ckpt"):
            cands += sorted(sub.glob(ext))
    if not cands:
        return None
    pref = [p for p in cands if any(k in p.name.lower() for k in ("xl", "flux", "sd3"))]
    return str((pref or cands)[0])


def _is_xl(model):
    return bool(model and any(k in os.path.basename(model).lower() for k in ("xl", "flux", "sd3")))


def available():
    return bool(find_binary()[0] and find_model())


def _out_dir():
    d = Path(OUT_DIR) if OUT_DIR else (_base_dir() / "generated")
    d.mkdir(parents=True, exist_ok=True)
    return d


def generate(prompt, negative=None, steps=DEFAULT_STEPS, width=None,
             height=None, cfg=DEFAULT_CFG, seed=-1, timeout=900):
    """Render one image from a text prompt. Returns {ok, file, message}.
    One generation at a time (busy guard). SDXL/FLUX models render at 1024."""
    binary, kind = find_binary()
    model = find_model()
    if not binary or not model:
        return {"ok": False, "message": "image generator needs a model in the sd folder"}
    # SDXL/FLUX want 1024; SD1.5 stays at the smaller default
    size = 1024 if _is_xl(model) else DEFAULT_SIZE
    width = int(width or size)
    height = int(height or size)
    with _LOCK:
        if _STATE["busy"]:
            return {"ok": False, "message": "already generating an image"}
        _STATE["busy"] = True
    p = None
    try:
        stamp = time.strftime("%Y%m%d_%H%M%S", time.localtime())
        out = _out_dir() / ("gen_%s.png" % stamp)
        args = [binary, "-M", "img_gen", "-m", model, "-p", str(prompt),
                "-o", str(out), "-W", str(int(width)), "-H", str(int(height)),
                "--steps", str(int(steps)), "--cfg-scale", str(float(cfg)),
                "--seed", str(int(seed))]
        if _is_xl(model):
            # SDXL's VAE decode at 1024 needs ~11GB on Vulkan -> OOM. Tiling decodes
            # it in pieces so it fits comfortably on the 4070 (and even the 1650S).
            args += ["--vae-tiling"]
        if negative:
            args += ["-n", str(negative)]
        flags = 0x08000000 if os.name == "nt" else 0          # CREATE_NO_WINDOW
        if os.name == "nt":
            flags |= 0x00004000                               # BELOW_NORMAL_PRIORITY
        try:
            # _proc.run job-adopts the sd-cli child so a long GPU image-gen can't
            # orphan (and keep hammering the GPU) if we're hard-killed mid-generation.
            p = _proc.run(args, capture_output=True, text=True,
                          timeout=timeout, creationflags=flags)
        except subprocess.TimeoutExpired:
            _STATE["error"] = "generation timed out"
            return {"ok": False, "message": "generation timed out"}
        if out.exists() and out.stat().st_size > 0:
            _STATE.update(last=str(out), error=None, model=Path(model).name)
            return {"ok": True, "file": str(out), "name": out.name,
                    "message": "Generated %s" % out.name}
        tail = ((p.stderr or p.stdout or "") if p is not None else "no output from generator")[-200:]
        _STATE["error"] = tail
        return {"ok": False, "message": "generation failed: %s" % tail}
    finally:
        _STATE["busy"] = False


def list_images():
    d = _out_dir()
    items = []
    for f in sorted(d.glob("*.png"), key=lambda x: x.stat().st_mtime, reverse=True):
        try:
            st = f.stat()
            items.append({"name": f.name, "path": str(f),
                          "sizeMB": round(st.st_size / 1048576.0, 2),
                          "ts": int(st.st_mtime * 1000)})
        except Exception:
            pass
    return {"items": items, "total": len(items), "dir": str(d)}


def status():
    return {"available": available(), "busy": _STATE["busy"],
            "model": Path(find_model()).name if find_model() else None,
            "kind": find_binary()[1], "error": _STATE.get("error")}
