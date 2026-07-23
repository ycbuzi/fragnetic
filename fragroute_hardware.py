"""Fragnetic hardware probe + capability profile.

We ship to customers whose PCs we've never seen, so nothing can assume the dev's
rig (dual NVIDIA + NVENC). This detects the GPU(s), VRAM, CPU, RAM, and which
ffmpeg encoders actually exist, then reports -- per feature -- whether it will
work, work-but-slow, or not work, with a plain reason. The engine uses the same
profile to pick the RIGHT video encoder and AI device automatically.

Pure stdlib + subprocess (PowerShell / nvidia-smi / ffmpeg). Cached after first run.
"""
import ctypes
import os
import re
import subprocess
import sys

APP_HW_BUILD = "hw-1"

FFMPEG = None            # set by engine -> path to ffmpeg.exe (for encoder probe)
_CACHE = {}


def _run(cmd, timeout=8):
    """Run a command with no console flash; return stdout text ('' on failure)."""
    try:
        si = None
        cf = 0
        if os.name == "nt":
            si = subprocess.STARTUPINFO()
            si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            cf = 0x08000000  # CREATE_NO_WINDOW
        r = subprocess.run(cmd, capture_output=True, text=True, errors="replace",
                           timeout=timeout, startupinfo=si, creationflags=cf)
        return (r.stdout or "") + (r.stderr or "")
    except Exception:
        return ""


def _ram_gb():
    if os.name != "nt":
        try:
            return round(os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / 1e9, 1)
        except Exception:
            return None
    try:
        class MS(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                        ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
        m = MS()
        m.dwLength = ctypes.sizeof(MS)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
        return round(m.ullTotalPhys / 1073741824, 1)
    except Exception:
        return None


def _cpu():
    name = ""
    if os.name == "nt":
        out = _run(["powershell", "-NoProfile", "-Command",
                    "(Get-CimInstance Win32_Processor | Select-Object -First 1).Name"])
        name = out.strip().splitlines()[-1].strip() if out.strip() else ""
    if not name:
        name = os.environ.get("PROCESSOR_IDENTIFIER", "") or "CPU"
    return {"name": name, "cores": os.cpu_count() or 0}


def _nvidia():
    """NVIDIA GPUs with real VRAM via nvidia-smi (the only reliable VRAM source)."""
    out = _run(["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"])
    gpus = []
    for line in out.strip().splitlines():
        if "," in line:
            nm, mem = line.split(",", 1)
            try:
                gpus.append({"name": nm.strip(), "vendor": "NVIDIA",
                             "vramGB": round(int(re.sub(r"[^0-9]", "", mem)) / 1024, 1)})
            except Exception:
                gpus.append({"name": nm.strip(), "vendor": "NVIDIA", "vramGB": None})
    return gpus


def _all_gpus():
    """Every display adapter (names + vendor). Merges real NVIDIA VRAM in."""
    nv = _nvidia()
    nv_names = {g["name"].lower() for g in nv}
    gpus = list(nv)
    if os.name == "nt":
        out = _run(["powershell", "-NoProfile", "-Command",
                    "Get-CimInstance Win32_VideoController | ForEach-Object { $_.Name }"])
        for nm in out.strip().splitlines():
            nm = nm.strip()
            if not nm or nm.lower() in nv_names:
                continue
            low = nm.lower()
            vendor = ("NVIDIA" if "nvidia" in low or "geforce" in low or "rtx" in low or "gtx" in low
                      else "AMD" if "radeon" in low or "amd" in low or "rx " in low
                      else "Intel" if "intel" in low or "arc" in low or "uhd" in low or "iris" in low
                      else "Other")
            if vendor == "NVIDIA":          # an NVIDIA card nvidia-smi missed (driver issue)
                continue
            gpus.append({"name": nm, "vendor": vendor, "vramGB": None})
    return gpus


def ffmpeg_encoders():
    if not FFMPEG or not os.path.exists(str(FFMPEG)):
        return set()
    out = _run([str(FFMPEG), "-hide_banner", "-encoders"])
    found = set()
    for enc in ("h264_nvenc", "hevc_nvenc", "h264_amf", "hevc_amf", "h264_qsv",
                "hevc_qsv", "libopenh264", "libx264"):
        if enc in out:
            found.add(enc)
    return found


def best_video_encoder():
    """The encoder to record/transcode with, best-available for THIS machine.
    Hardware first (low game impact), CPU last. Returns (ffmpeg_args, label, hw)."""
    enc = ffmpeg_encoders()
    if "h264_nvenc" in enc:
        return (["-c:v", "h264_nvenc", "-preset", "p5", "-cq", "23", "-pix_fmt", "yuv420p"],
                "NVIDIA NVENC", True)
    if "h264_amf" in enc:
        return (["-c:v", "h264_amf", "-quality", "balanced", "-rc", "cqp", "-qp_i", "23",
                 "-qp_p", "23", "-pix_fmt", "yuv420p"], "AMD AMF", True)
    if "h264_qsv" in enc:
        return (["-c:v", "h264_qsv", "-global_quality", "23", "-pix_fmt", "nv12"],
                "Intel Quick Sync", True)
    if "libopenh264" in enc:
        return (["-c:v", "libopenh264", "-b:v", "6M", "-pix_fmt", "yuv420p"], "CPU (libopenh264)", False)
    if "libx264" in enc:
        return (["-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p"],
                "CPU (libx264)", False)
    return ([], "none", False)


def detect(refresh=False):
    # A cached profile whose encoder probe came back 'none' is NOT trustworthy: detect() can
    # run BEFORE the engine sets FFMPEG (fragroute_setup.recommend() calls detect() during
    # startup), and ffmpeg_encoders() returns an empty set when FFMPEG is unset. Caching that
    # miss made "Record Gameplay" report "No usable video encoder / ffmpeg not found" forever,
    # even on a box with ffmpeg + NVENC. So re-probe whenever the cached encoder is 'none' AND
    # ffmpeg is now available. Same lesson as find_ffmpeg(): only cache a hit.
    if _CACHE and not refresh:
        enc = _CACHE.get("encoder") or {}
        if enc.get("label") != "none" or not (FFMPEG and os.path.exists(str(FFMPEG))):
            return _CACHE
    gpus = _all_gpus()
    best_vram = max([g.get("vramGB") or 0 for g in gpus], default=0)
    has_nv = any(g["vendor"] == "NVIDIA" for g in gpus)
    enc_args, enc_label, enc_hw = best_video_encoder()
    prof = {
        "build": APP_HW_BUILD,
        "os": "%s %s" % (os.name, sys.platform),
        "cpu": _cpu(),
        "ramGB": _ram_gb(),
        "gpus": gpus,
        "primaryGpu": (gpus[0] if gpus else None),
        "bestVramGB": best_vram or None,
        "hasNvidia": has_nv,
        "encoder": {"label": enc_label, "hardware": enc_hw, "args": enc_args},
    }
    _CACHE.clear()
    _CACHE.update(prof)
    return prof


def capabilities(prof=None):
    """Per-feature verdict for THIS PC: level = good | warn | bad, with a reason."""
    p = prof or detect()
    ram = p.get("ramGB") or 0
    vram = p.get("bestVramGB") or 0
    enc = p.get("encoder", {})
    gpus = p.get("gpus") or []
    out = []

    def add(key, label, level, why):
        out.append({"key": key, "label": label, "level": level, "why": why})

    # --- recording ---
    if enc.get("label") == "none":
        add("record", "Record Gameplay", "bad", "No usable video encoder / ffmpeg not found.")
    elif enc.get("hardware"):
        add("record", "Record Gameplay", "good", "Hardware encoder (%s) — low impact on your game." % enc["label"])
    else:
        add("record", "Record Gameplay", "warn",
            "%s — works, but uses CPU; may cost a few FPS while recording." % enc["label"])

    # --- local AI (LLM coach) --- CPU fallback always exists
    if ram >= 16 or vram >= 6:
        add("coach", "AI Coach (local LLM)", "good", "Plenty of memory — runs smoothly%s." %
            (" on your GPU" if vram >= 6 else ""))
    elif ram >= 8:
        add("coach", "AI Coach (local LLM)", "warn", "8GB RAM — use the smaller fast model; the big model may be slow.")
    else:
        add("coach", "AI Coach (local LLM)", "bad", "Under 8GB RAM — local AI will be very slow. Use the fast model only.")

    # --- image gen (SDXL) ---
    if vram >= 8:
        add("imagegen", "Image Gen (SDXL)", "good", "%.0fGB VRAM — generates in seconds." % vram)
    elif vram >= 4:
        add("imagegen", "Image Gen (SDXL)", "warn", "%.0fGB VRAM — works with tiling, slower per image." % vram)
    elif gpus:
        add("imagegen", "Image Gen (SDXL)", "warn", "Low/unknown VRAM — may fall back to CPU (minutes per image).")
    else:
        add("imagegen", "Image Gen (SDXL)", "bad", "No GPU detected — image gen on CPU is very slow.")

    # --- detector (CPU onnxruntime ships; always usable) ---
    add("detector", "Detector / Vision", "good", "Runs on CPU; a GPU just makes it faster.")

    # --- routing (no special hardware) ---
    add("routing", "Routing / Ping", "good", "Works on any PC — no special hardware needed.")
    return out


def status():
    p = detect()
    return {"build": APP_HW_BUILD, "profile": p, "capabilities": capabilities(p)}
